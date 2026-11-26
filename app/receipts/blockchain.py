"""Prepare and reconcile the three on-chain calls for one soybean receipt.

The browser wallet signs every transaction. This module stores the exact call
payload before opening MetaMask, verifies the broadcast transaction against that
payload, and accepts completion only after the expected event and contract state
are visible through the saved network's RPC endpoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal, cast

from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import TransactionNotFound, Web3Exception

from accounts.models import UserWallet
from accounts.rules.wallet_access import require_verified_wallet
from blockchain.models import (
    BlockchainTransaction,
    ContractInstance,
    Network,
    RECEIPT_INVOICE_LINK_OPERATION_TYPE,
    RECEIPT_OPEN_OPERATION_TYPE,
    RECEIPT_WEIGHING_CONFIRMATION_OPERATION_TYPE,
    UNIT_WALLET_PERMISSION_OPERATION_TYPE,
    UnitRegistrationAttempt,
)
from blockchain.services.contracts import bind_contract_to_web3
from blockchain.services.rpc import connect_to_rpc
from blockchain.services.transaction_confirmation import (
    DecodedContractEvent,
    confirm_transaction_from_chain,
)
from blockchain.services.transactions import fail_transaction, submit_transaction
from companies.models import UnitWalletPermission
from companies.rules.role_access import require_receiving_access
from participants.models import ParticipantTerms
from utils.identifiers import (
    is_valid_transaction_hash,
    is_valid_wallet_address,
    normalize_bytes32_hash,
    normalize_transaction_hash,
    normalize_wallet_address,
)

from .documents import ZERO_BYTES32, read_valid_receipt_pdf, receipt_pdf_hash
from .models import (
    Receipt,
    ReceiptBlockchainAttempt,
    ReceiptBlockchainOperation,
    ReceiptDocument,
    TaxInvoice,
    WeighingTicket,
)


CallKind = Literal["open", "confirm_weighing", "link_invoice"]
RECEIPT_CALL_ORDER: tuple[CallKind, ...] = (
    "open",
    "confirm_weighing",
    "link_invoice",
)
OPERATION_TYPE_BY_CALL: dict[str, str] = {
    ReceiptBlockchainAttempt.CallKind.OPEN: RECEIPT_OPEN_OPERATION_TYPE,
    ReceiptBlockchainAttempt.CallKind.CONFIRM_WEIGHING: RECEIPT_WEIGHING_CONFIRMATION_OPERATION_TYPE,
    ReceiptBlockchainAttempt.CallKind.LINK_INVOICE: RECEIPT_INVOICE_LINK_OPERATION_TYPE,
}
REQUIRED_EVENTS_BY_CALL: dict[str, tuple[str, ...]] = {
    ReceiptBlockchainAttempt.CallKind.OPEN: ("LoadReceiptOpened",),
    ReceiptBlockchainAttempt.CallKind.CONFIRM_WEIGHING: (
        "LoadReceiptConfirmed",
        "ReceiptClassified",
    ),
    ReceiptBlockchainAttempt.CallKind.LINK_INVOICE: ("TaxInvoiceLinked",),
}


@dataclass(frozen=True)
class PreparedReceiptCall:
    """Describe the saved wallet request and whether it can be sent or recovered."""

    status: Literal["ready", "recover", "submitted"]
    attempt: ReceiptBlockchainAttempt
    chain_id: int
    contract_address: str
    transaction_data: str


@dataclass(frozen=True)
class ReceiptCallConfirmation:
    """Return the confirmed, failed, or still-pending result of one receipt call."""

    status: Literal["pending", "failed", "confirmed", "needs_review"]
    attempt: ReceiptBlockchainAttempt
    transaction: BlockchainTransaction
    deposit_id: int | None


def _positive_integer(value: int, label: str) -> None:
    """Reject booleans, non-integers, and non-positive database identifiers."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"O identificador de {label} é inválido.")


def _normalize_call_data(value: object) -> str:
    """Convert Web3 transaction input into canonical lowercase hex bytes."""

    if isinstance(value, (bytes, bytearray)):
        result = Web3.to_hex(value).lower()
    elif isinstance(value, str):
        result = value.lower()
    else:
        raise ValidationError("O RPC retornou dados de transação inválidos.")
    if not re.fullmatch(r"0x(?:[0-9a-f]{2})+", result):
        raise ValidationError("O RPC retornou dados de transação inválidos.")
    return result


def _encode_unit_code(code: str) -> bytes:
    """Encode the registered unit code exactly as UnitRegistry stores bytes32."""

    value = (code or "").strip().upper()
    if not value or "\x00" in value:
        raise ValidationError("O código da unidade não pode ficar vazio.")
    encoded = value.encode("utf-8")
    if len(encoded) > 32:
        raise ValidationError("O código da unidade excede os 32 bytes do contrato.")
    return encoded.ljust(32, b"\x00")


def _load_receipt(*, company_id: int, receipt_id: int) -> Receipt:
    """Load one receipt and its immutable source data inside company scope."""

    _positive_integer(company_id, "empresa")
    _positive_integer(receipt_id, "recebimento")
    try:
        return (
            Receipt.objects.select_related(
                "company",
                "participant",
                "unit",
                "weighing_ticket",
                "tax_invoice__document",
            )
            .prefetch_related("documents")
            .get(pk=receipt_id, company_id=company_id)
        )
    except Receipt.DoesNotExist as error:
        raise ValidationError("O recebimento não pertence a esta empresa.") from error


def _get_active_contracts(
    *, company_id: int, network: Network
) -> tuple[ContractInstance, ContractInstance]:
    """Resolve the active SojaCoin and UnitRegistry saved for one company network."""

    try:
        soja_coin = ContractInstance.objects.select_related("network").get(
            company_id=company_id,
            network=network,
            kind=ContractInstance.Kind.SOJA_COIN,
            active=True,
        )
        registry = ContractInstance.objects.select_related("network").get(
            company_id=company_id,
            network=network,
            kind=ContractInstance.Kind.UNIT_REGISTRY,
            active=True,
        )
    except ContractInstance.DoesNotExist as error:
        raise ValidationError(
            "A empresa não tem SojaCoin e UnitRegistry ativos nessa rede."
        ) from error
    return soja_coin, registry


def _has_confirmed_unit_registration(
    *, receipt: Receipt, network: Network, registry: ContractInstance
) -> bool:
    """Require a successful registration transaction for this unit and deployment."""

    return UnitRegistrationAttempt.objects.filter(
        unit=receipt.unit,
        transaction__status=BlockchainTransaction.Status.CONFIRMED,
        transaction__network=network,
        transaction__contract=registry,
    ).exists()


def _has_confirmed_receive_configuration(
    *, receipt: Receipt, network: Network, registry: ContractInstance
) -> bool:
    """Find a confirmed unit-permission snapshot associated with this deployment."""

    for permission in UnitWalletPermission.objects.filter(
        unit=receipt.unit,
        sync_status=UnitWalletPermission.SyncStatus.CONFIRMED,
    ).exclude(transaction=""):
        if BlockchainTransaction.objects.filter(
            transaction_hash=permission.transaction,
            status=BlockchainTransaction.Status.CONFIRMED,
            operation_type=UNIT_WALLET_PERMISSION_OPERATION_TYPE,
            network=network,
            contract=registry,
        ).exists():
            return True
    return False


def get_receipt_network_candidates(receipt: Receipt) -> list[Network]:
    """Return networks where this receipt's unit, participant, and contracts align.

    A network is eligible only when the unit registration and a receiving
    permission have confirmed transactions against its active UnitRegistry,
    while the participant's accepted terms and active SojaCoin belong to the
    same company and network.
    """

    try:
        operation = receipt.blockchain_operation
    except ReceiptBlockchainOperation.DoesNotExist:
        operation = None
    if operation is not None:
        return [operation.network]

    attempts = (
        UnitRegistrationAttempt.objects.select_related(
            "transaction__network",
            "transaction__contract",
        )
        .filter(
            unit=receipt.unit,
            transaction__status=BlockchainTransaction.Status.CONFIRMED,
            transaction__network__active=True,
            transaction__contract__active=True,
            transaction__contract__kind=ContractInstance.Kind.UNIT_REGISTRY,
        )
        .order_by("transaction__network__name", "pk")
    )
    networks: dict[int, Network] = {}
    for registration in attempts:
        network = registration.transaction.network
        registry = registration.transaction.contract
        if not _has_confirmed_unit_registration(receipt=receipt, network=network, registry=registry):
            continue
        try:
            soja_coin = ContractInstance.objects.get(
                company=receipt.company,
                network=network,
                kind=ContractInstance.Kind.SOJA_COIN,
                active=True,
            )
        except ContractInstance.DoesNotExist:
            continue
        if not ParticipantTerms.objects.filter(
            participant=receipt.participant,
            status=ParticipantTerms.Status.CONFIRMED,
            network=network,
            contract=soja_coin,
        ).exists():
            continue
        if not _has_confirmed_receive_configuration(
            receipt=receipt,
            network=network,
            registry=registry,
        ):
            continue
        networks[network.pk] = network
    return list(networks.values())


def _current_terms(*, receipt: Receipt, network: Network, soja_coin: ContractInstance) -> ParticipantTerms:
    """Return the latest confirmed participant terms for the selected deployment."""

    terms = (
        ParticipantTerms.objects.filter(
            participant=receipt.participant,
            status=ParticipantTerms.Status.CONFIRMED,
            network=network,
            contract=soja_coin,
        )
        .order_by("-version", "-pk")
        .first()
    )
    if terms is None:
        raise ValidationError(
            "O participante não tem aceite confirmado nesta SojaCoin e nesta rede."
        )
    return terms


def _validate_unit_on_chain(
    *,
    registry_contract: Contract,
    receipt: Receipt,
) -> None:
    """Match the local unit code and registration hash to the saved chain ID."""

    unit = receipt.unit
    unit_id = unit.chain_unit_id
    if not unit.active or not unit_id:
        raise ValidationError("A unidade precisa estar ativa e registrada na blockchain.")
    code = _encode_unit_code(unit.code)
    on_chain_code, on_chain_hash, _report_hash, _inventory, _audit, active = (
        registry_contract.functions.units(unit_id).call()
    )
    if (
        bytes(on_chain_code) != code
        or normalize_bytes32_hash(Web3.to_hex(on_chain_hash))
        != normalize_bytes32_hash(unit.registration_hash)
        or not bool(active)
        or int(registry_contract.functions.unitIdByCode(code).call()) != int(unit_id)
    ):
        raise ValidationError(
            "O registro da unidade na rede não corresponde ao cadastro atual do SaaS."
        )


def _validate_call_context(
    *,
    user: AbstractBaseUser,
    company_id: int,
    receipt: Receipt,
    network: Network,
    sender_address: str,
    operation: ReceiptBlockchainOperation | None,
) -> tuple[
    ContractInstance,
    ContractInstance,
    Contract,
    Contract,
    ParticipantTerms,
    int,
    Web3,
]:
    """Recheck SaaS access, verified wallet, contracts, terms, and chain permissions."""

    membership, unit, _unit_access = require_receiving_access(
        user,
        company_id,
        receipt.unit_id,
    )
    if receipt.company_id != company_id or unit.company_id != company_id:
        raise PermissionDenied("O recebimento e a unidade precisam pertencer à empresa da URL.")
    if not receipt.company.active or not unit.active or not network.active:
        raise ValidationError("A empresa, unidade e rede precisam estar ativas.")

    normalized_sender = normalize_wallet_address(sender_address)
    if not is_valid_wallet_address(normalized_sender):
        raise ValidationError("A carteira conectada não é um endereço EVM válido.")
    user_wallet = (
        UserWallet.objects.select_related("user", "wallet")
        .filter(
            user=user,
            wallet__address_normalized=normalized_sender,
            verification_status=UserWallet.VerificationStatus.VERIFIED,
            wallet__active=True,
        )
        .first()
    )
    if user_wallet is None:
        raise PermissionDenied("Conecte uma carteira verificada no seu perfil.")
    require_verified_wallet(user_wallet)

    if operation is not None and operation.network_id != network.pk:
        raise ValidationError("O recebimento já está fixado em outra rede.")
    soja_coin, registry = _get_active_contracts(
        company_id=company_id,
        network=network,
    )
    if operation is not None and (
        operation.soja_coin_id != soja_coin.pk
        or operation.unit_registry_id != registry.pk
    ):
        raise ValidationError(
            "A implantação ativa mudou desde o início. Revise esta operação antes de continuar."
        )
    if not _has_confirmed_unit_registration(
        receipt=receipt,
        network=network,
        registry=registry,
    ):
        raise ValidationError("O registro da unidade não foi confirmado nesta rede.")
    if not _has_confirmed_receive_configuration(
        receipt=receipt,
        network=network,
        registry=registry,
    ):
        raise ValidationError("Não há permissões de recebimento confirmadas nesta rede.")

    web3 = connect_to_rpc(network)
    soja_address = Web3.to_checksum_address(soja_coin.address_normalized)
    registry_address = Web3.to_checksum_address(registry.address_normalized)
    if not web3.eth.get_code(soja_address) or not web3.eth.get_code(registry_address):
        raise ValidationError("Não há código de contrato nos endereços salvos para esta rede.")
    soja_contract = bind_contract_to_web3(web3, soja_coin)
    registry_contract = bind_contract_to_web3(web3, registry)

    if normalize_wallet_address(str(soja_contract.functions.unitRegistry().call())) != (
        registry.address_normalized
    ) or normalize_wallet_address(str(registry_contract.functions.sojaCoin().call())) != (
        soja_coin.address_normalized
    ):
        raise ValidationError("SojaCoin e UnitRegistry não estão ligados entre si nesta rede.")
    _validate_unit_on_chain(registry_contract=registry_contract, receipt=receipt)

    permission = UnitWalletPermission.objects.filter(
        unit=unit,
        wallet=normalized_sender,
        sync_status=UnitWalletPermission.SyncStatus.CONFIRMED,
    ).first()
    if permission is None or not permission.transaction:
        raise PermissionDenied("Esta carteira não tem recebimento ativo nesta unidade.")
    permission_transaction = BlockchainTransaction.objects.filter(
        transaction_hash=permission.transaction,
        status=BlockchainTransaction.Status.CONFIRMED,
        operation_type=UNIT_WALLET_PERMISSION_OPERATION_TYPE,
        network=network,
        contract=registry,
    ).first()
    if permission_transaction is None:
        raise PermissionDenied(
            "A permissão salva não foi confirmada nesta rede e neste UnitRegistry."
        )
    permission_bit = int(registry_contract.functions.PERMISSION_RECEIVE_SOYBEANS().call())
    if int(permission.permission_mask) & permission_bit == 0:
        raise PermissionDenied("A permissão atual desta carteira não inclui recebimento.")
    if not registry_contract.functions.hasPermissionForUnit(
        unit.chain_unit_id,
        Web3.to_checksum_address(normalized_sender),
        permission_bit,
    ).call():
        raise PermissionDenied("O contrato não reconhece recebimento para esta carteira.")

    terms = _current_terms(receipt=receipt, network=network, soja_coin=soja_coin)
    participant = Web3.to_checksum_address(receipt.participant.wallet_address_normalized)
    if not soja_contract.functions.participantRegistered(participant).call():
        raise ValidationError("O participante ainda não está registrado na SojaCoin escolhida.")
    if not soja_contract.functions.depositAuthorizationActive(participant).call():
        raise ValidationError("A autorização de recebimento do participante foi revogada.")
    on_chain_terms_hash = normalize_bytes32_hash(
        Web3.to_hex(soja_contract.functions.participationTermsHashByParticipant(participant).call())
    )
    if on_chain_terms_hash != normalize_bytes32_hash(terms.terms_hash):
        raise ValidationError(
            "A versão de termos confirmada no SaaS difere da autorização atual na blockchain."
        )
    if operation is not None and (
        operation.participant_terms_hash != on_chain_terms_hash
        or operation.unit_chain_id != unit.chain_unit_id
        or operation.crop_year_bytes32 != receipt.crop_year_bytes32
    ):
        raise ValidationError("Os dados usados quando a operação começou mudaram.")

    return (
        soja_coin,
        registry,
        soja_contract,
        registry_contract,
        terms,
        permission_bit,
        web3,
    )


def _read_source_documents(
    *, receipt: Receipt
) -> tuple[WeighingTicket, TaxInvoice, str, str]:
    """Re-hash the stored PDFs and verify that form records still match their bytes."""

    try:
        ticket = receipt.weighing_ticket
        invoice = receipt.tax_invoice
        weighing_document = receipt.documents.get(kind=ReceiptDocument.Kind.WEIGHING_TICKET)
        invoice_document = receipt.documents.get(kind=ReceiptDocument.Kind.TAX_INVOICE)
    except (WeighingTicket.DoesNotExist, TaxInvoice.DoesNotExist, ReceiptDocument.DoesNotExist) as error:
        raise ValidationError("O romaneio, a pesagem e a nota fiscal precisam estar completos.") from error

    try:
        with weighing_document.pdf_file.open("rb") as stored_file:
            weighing_bytes = read_valid_receipt_pdf(stored_file)
        with invoice_document.pdf_file.open("rb") as stored_file:
            invoice_bytes = read_valid_receipt_pdf(stored_file)
        report_document = receipt.documents.filter(
            kind=ReceiptDocument.Kind.CLASSIFICATION_REPORT
        ).first()
        report_bytes: bytes | None = None
        if report_document is not None:
            with report_document.pdf_file.open("rb") as stored_file:
                report_bytes = read_valid_receipt_pdf(stored_file)
    except OSError as error:
        raise ValidationError("Um PDF do recebimento não está disponível no armazenamento privado.") from error

    weighing_pdf_hash = receipt_pdf_hash(weighing_bytes)
    invoice_pdf_hash = receipt_pdf_hash(invoice_bytes)
    report_pdf_hash = receipt_pdf_hash(report_bytes) if report_bytes is not None else ZERO_BYTES32
    if (
        weighing_pdf_hash != normalize_bytes32_hash(weighing_document.file_hash)
        or weighing_pdf_hash != normalize_bytes32_hash(ticket.weighing_ticket_hash)
        or invoice_pdf_hash != normalize_bytes32_hash(invoice_document.file_hash)
        or invoice.document_id != invoice_document.pk
        or report_pdf_hash != normalize_bytes32_hash(ticket.report_hash)
        or (
            report_document is not None
            and report_pdf_hash != normalize_bytes32_hash(report_document.file_hash)
        )
    ):
        raise ValidationError("Um PDF não corresponde à hash registrada no recebimento.")
    calculated_key_hash = Web3.to_hex(Web3.keccak(text=invoice.access_key)).lower()
    if calculated_key_hash != normalize_bytes32_hash(invoice.access_key_hash):
        raise ValidationError("A hash da chave da NF-e não corresponde ao valor salvo.")
    if ticket.net_weight_kg != (
        ticket.gross_weight_kg
        - ticket.tare_weight_kg
        - ticket.moisture_deduction_kg
        - ticket.impurities_deduction_kg
        - ticket.damaged_grain_deduction_kg
        - ticket.other_deductions_kg
    ):
        raise ValidationError("O peso líquido salvo diverge dos dados do romaneio.")
    if ticket.net_weight_kg <= 0:
        raise ValidationError("O peso líquido precisa ser maior que zero.")
    return ticket, invoice, weighing_pdf_hash, invoice_pdf_hash


def _next_call_kind(operation: ReceiptBlockchainOperation | None) -> CallKind:
    """Choose the earliest unconfirmed contract call in the required sequence."""

    if operation is None:
        return "open"
    if operation.status == ReceiptBlockchainOperation.Status.NEEDS_REVIEW:
        raise ValidationError("Esta operação precisa de conferência antes de continuar.")
    if operation.status == ReceiptBlockchainOperation.Status.CONFIRMED:
        raise ValidationError("Este recebimento já foi confirmado na blockchain.")

    for call_kind in RECEIPT_CALL_ORDER:
        latest = operation.attempts.filter(call_kind=call_kind).order_by("-created_at", "-pk").first()
        if latest is None or latest.transaction.status != BlockchainTransaction.Status.CONFIRMED:
            return call_kind
    raise ValidationError("O histórico das três chamadas está inconsistente; revise o recebimento.")


def _encode_prepared_call(
    *,
    call_kind: CallKind,
    soja_contract: Contract,
    receipt: Receipt,
    ticket: WeighingTicket,
    invoice: TaxInvoice,
    deposit_id: int | None,
) -> tuple[str, dict[str, Any]]:
    """Build the calldata and immutable argument snapshot for the next call."""

    if call_kind == "open":
        arguments: dict[str, Any] = {
            "participant": receipt.participant.wallet_address_normalized,
            "unit_id": int(receipt.unit.chain_unit_id),
            "crop_year": normalize_bytes32_hash(receipt.crop_year_bytes32),
        }
        data = soja_contract.functions.openLoadReceipt(
            Web3.to_checksum_address(arguments["participant"]),
            arguments["unit_id"],
            bytes.fromhex(arguments["crop_year"][2:]),
        )._encode_transaction_data().lower()
        return data, arguments

    if not deposit_id:
        raise ValidationError("O ID do recebimento ainda não foi confirmado no contrato.")
    if call_kind == "confirm_weighing":
        ticket_timestamp = int(ticket.weighed_at.timestamp())
        weighing_arguments = (
            int(ticket.gross_weight_kg),
            int(ticket.tare_weight_kg),
            int(ticket.moisture_bps),
            int(ticket.impurities_bps),
            int(ticket.damaged_grain_bps),
            int(ticket.moisture_deduction_kg),
            int(ticket.impurities_deduction_kg),
            int(ticket.damaged_grain_deduction_kg),
            int(ticket.other_deductions_kg),
            bytes.fromhex(normalize_bytes32_hash(ticket.weighing_ticket_hash)[2:]),
            bytes.fromhex(normalize_bytes32_hash(ticket.report_hash)[2:]),
            ticket_timestamp,
        )
        arguments = {
            "deposit_id": int(deposit_id),
            "weighing_ticket": {
                "gross_weight_kg": int(ticket.gross_weight_kg),
                "tare_kg": int(ticket.tare_weight_kg),
                "moisture_bps": int(ticket.moisture_bps),
                "impurities_bps": int(ticket.impurities_bps),
                "damaged_grain_bps": int(ticket.damaged_grain_bps),
                "moisture_deduction_kg": int(ticket.moisture_deduction_kg),
                "impurities_deduction_kg": int(ticket.impurities_deduction_kg),
                "damaged_grain_deduction_kg": int(ticket.damaged_grain_deduction_kg),
                "other_deductions_kg": int(ticket.other_deductions_kg),
                "weighing_ticket_hash": normalize_bytes32_hash(ticket.weighing_ticket_hash),
                "report_hash": normalize_bytes32_hash(ticket.report_hash),
                "timestamp": ticket_timestamp,
            },
        }
        data = soja_contract.functions.confirmLoadReceipt(
            int(deposit_id),
            weighing_arguments,
        )._encode_transaction_data().lower()
        return data, arguments

    if call_kind == "link_invoice":
        invoice_hash = normalize_bytes32_hash(invoice.access_key_hash)
        arguments = {"deposit_id": int(deposit_id), "tax_invoice_key_hash": invoice_hash}
        data = soja_contract.functions.linkTaxInvoice(
            int(deposit_id),
            bytes.fromhex(invoice_hash[2:]),
        )._encode_transaction_data().lower()
        return data, arguments

    raise ValidationError("Ação de recebimento desconhecida.")


def _get_operation(receipt: Receipt) -> ReceiptBlockchainOperation | None:
    """Return the one operation already pinned to a receipt, if it exists."""

    try:
        return ReceiptBlockchainOperation.objects.select_related(
            "network",
            "soja_coin",
            "unit_registry",
            "receipt__unit",
            "receipt__participant",
        ).get(receipt=receipt)
    except ReceiptBlockchainOperation.DoesNotExist:
        return None


def prepare_next_receipt_call(
    *,
    user: AbstractBaseUser,
    company_id: int,
    receipt_id: int,
    network_id: int,
    sender_address: str,
) -> PreparedReceiptCall:
    """Save or resume the earliest eligible call for a load receipt.

    The server chooses the call arguments from immutable receipt rows. The
    browser provides only the selected network and its connected public sender
    address; both values are validated against the company's persisted records.
    An existing uncertain attempt is returned for recovery instead of replaced.
    """

    _positive_integer(network_id, "rede")
    receipt = _load_receipt(company_id=company_id, receipt_id=receipt_id)
    membership, _unit, _unit_access = require_receiving_access(
        user,
        company_id,
        receipt.unit_id,
    )
    operation = _get_operation(receipt)
    if operation is not None and operation.network_id != network_id:
        raise ValidationError("A rede deste recebimento já foi escolhida e não pode ser trocada.")
    try:
        network = Network.objects.get(pk=network_id)
    except Network.DoesNotExist as error:
        raise ValidationError("A rede selecionada não está cadastrada.") from error

    (
        soja_coin,
        registry,
        soja_contract,
        _registry_contract,
        terms,
        _permission_bit,
        web3,
    ) = _validate_call_context(
        user=user,
        company_id=company_id,
        receipt=receipt,
        network=network,
        sender_address=sender_address,
        operation=operation,
    )
    ticket, invoice, _ticket_pdf_hash, _invoice_pdf_hash = _read_source_documents(
        receipt=receipt
    )

    call_kind = _next_call_kind(operation)
    open_attempt = (
        operation.attempts.filter(call_kind=call_kind, is_open=True)
        .select_related("transaction")
        .order_by("-created_at", "-pk")
        .first()
        if operation is not None
        else None
    )
    if open_attempt is not None:
        if open_attempt.transaction.status == BlockchainTransaction.Status.SUBMITTED:
            status: Literal["ready", "recover", "submitted"] = "submitted"
        elif open_attempt.submission_started_at is not None:
            status = "recover"
        else:
            status = "ready"
        return PreparedReceiptCall(
            status=status,
            attempt=open_attempt,
            chain_id=network.chain_id,
            contract_address=soja_coin.address_normalized,
            transaction_data=open_attempt.prepared_data,
        )

    latest_timestamp = int(web3.eth.get_block("latest")["timestamp"])
    if call_kind == "confirm_weighing" and int(ticket.weighed_at.timestamp()) > latest_timestamp:
        raise ValidationError(
            "A data da pesagem está no futuro para o horário atual desta rede."
        )
    data, arguments = _encode_prepared_call(
        call_kind=call_kind,
        soja_contract=soja_contract,
        receipt=receipt,
        ticket=ticket,
        invoice=invoice,
        deposit_id=operation.deposit_id if operation is not None else None,
    )
    expected_networks = get_receipt_network_candidates(receipt)
    if not any(candidate.pk == network.pk for candidate in expected_networks):
        raise ValidationError(
            "A rede não tem os registros confirmados necessários para este recebimento."
        )

    try:
        with db_transaction.atomic():
            if operation is None:
                operation = ReceiptBlockchainOperation(
                    receipt=receipt,
                    network=network,
                    soja_coin=soja_coin,
                    unit_registry=registry,
                    started_by=user,
                    opening_sender_address=normalize_wallet_address(sender_address),
                    participant_address=receipt.participant.wallet_address_normalized,
                    participant_terms_hash=terms.terms_hash,
                    unit_chain_id=receipt.unit.chain_unit_id,
                    crop_year_bytes32=receipt.crop_year_bytes32,
                )
                operation.save()
                receipt.status = Receipt.Status.BLOCKCHAIN_IN_PROGRESS
                receipt.save(update_fields=("status", "updated_at"))

            sender = normalize_wallet_address(sender_address)
            transaction_record = BlockchainTransaction(
                company=receipt.company,
                network=network,
                contract=soja_coin,
                operation_type=OPERATION_TYPE_BY_CALL[call_kind],
                sender_address=sender,
                status=BlockchainTransaction.Status.PREPARED,
            )
            transaction_record.save()
            attempt = ReceiptBlockchainAttempt(
                operation=operation,
                transaction=transaction_record,
                requested_by=user,
                call_kind=call_kind,
                sender_address=sender,
                prepared_data=data,
                expected_arguments=arguments,
                is_open=True,
            )
            attempt.save()
    except IntegrityError as error:
        # A concurrent browser request may have prepared the same call first.
        operation = _get_operation(receipt)
        if operation is None:
            raise ValidationError("Não foi possível reservar esta etapa do recebimento.") from error
        existing = (
            operation.attempts.filter(call_kind=call_kind, is_open=True)
            .select_related("transaction")
            .order_by("-created_at", "-pk")
            .first()
        )
        if existing is None:
            raise ValidationError("Não foi possível reservar esta etapa do recebimento.") from error
        status = "submitted" if existing.transaction.status == BlockchainTransaction.Status.SUBMITTED else (
            "recover" if existing.submission_started_at else "ready"
        )
        return PreparedReceiptCall(
            status=cast(Literal["ready", "recover", "submitted"], status),
            attempt=existing,
            chain_id=network.chain_id,
            contract_address=soja_coin.address_normalized,
            transaction_data=existing.prepared_data,
        )

    return PreparedReceiptCall(
        status="ready",
        attempt=attempt,
        chain_id=network.chain_id,
        contract_address=soja_coin.address_normalized,
        transaction_data=data,
    )


def _get_attempt(*, company_id: int, receipt_id: int, attempt_id: int) -> ReceiptBlockchainAttempt:
    """Load an attempt only through its receipt, operation, and company scope."""

    _positive_integer(attempt_id, "tentativa")
    receipt = _load_receipt(company_id=company_id, receipt_id=receipt_id)
    try:
        return ReceiptBlockchainAttempt.objects.select_related(
            "operation__receipt__company",
            "operation__receipt__unit",
            "operation__receipt__participant",
            "operation__network",
            "operation__soja_coin",
            "operation__unit_registry",
            "transaction__network",
            "transaction__contract",
        ).get(pk=attempt_id, operation__receipt=receipt)
    except ReceiptBlockchainAttempt.DoesNotExist as error:
        raise ValidationError("A tentativa não pertence a este recebimento.") from error


def mark_receipt_call_started(
    *,
    user: AbstractBaseUser,
    company_id: int,
    receipt_id: int,
    attempt_id: int,
    sender_address: str,
) -> ReceiptBlockchainAttempt:
    """Record that the browser is about to display the saved MetaMask request."""

    attempt = _get_attempt(company_id=company_id, receipt_id=receipt_id, attempt_id=attempt_id)
    if not attempt.is_open or attempt.transaction.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Esta tentativa não pode mais abrir a carteira.")
    if normalize_wallet_address(sender_address) != attempt.sender_address:
        raise PermissionDenied("A carteira conectada mudou. Prepare a chamada novamente com a carteira correta.")
    if attempt.submission_started_at is not None:
        return attempt
    _validate_call_context(
        user=user,
        company_id=company_id,
        receipt=attempt.operation.receipt,
        network=attempt.operation.network,
        sender_address=attempt.sender_address,
        operation=attempt.operation,
    )
    attempt.submission_started_at = timezone.now()
    attempt.save(update_fields=("submission_started_at", "updated_at"))
    return attempt


def get_receipt_blockchain_context(receipt: Receipt) -> dict[str, Any]:
    """Build the display state for the three ordered contract calls."""

    operation = _get_operation(receipt)
    latest_by_kind: dict[str, ReceiptBlockchainAttempt | None] = {}
    current_call: str | None = None
    if operation is not None:
        for call_kind in RECEIPT_CALL_ORDER:
            attempt = (
                operation.attempts.filter(call_kind=call_kind)
                .select_related("transaction")
                .order_by("-created_at", "-pk")
                .first()
            )
            latest_by_kind[call_kind] = attempt
        if operation.status not in {
            ReceiptBlockchainOperation.Status.CONFIRMED,
            ReceiptBlockchainOperation.Status.NEEDS_REVIEW,
        }:
            for call_kind in RECEIPT_CALL_ORDER:
                attempt = latest_by_kind[call_kind]
                if (
                    attempt is None
                    or attempt.transaction.status != BlockchainTransaction.Status.CONFIRMED
                ):
                    current_call = call_kind
                    break
    else:
        latest_by_kind = {call_kind: None for call_kind in RECEIPT_CALL_ORDER}
        current_call = "open"

    labels = {
        "open": (
            "Abrir o recebimento",
            "Registra o participante, a unidade e a safra na SojaCoin.",
        ),
        "confirm_weighing": (
            "Registrar romaneio e pesagem",
            "Envia os campos do romaneio e a hash do PDF para confirmLoadReceipt.",
        ),
        "link_invoice": (
            "Vincular a nota fiscal",
            "O contrato recebe a hash Keccak da chave de acesso da NF-e. O PDF fica privado no SaaS.",
        ),
    }
    steps: list[dict[str, Any]] = []
    for call_kind in RECEIPT_CALL_ORDER:
        attempt = latest_by_kind[call_kind]
        if attempt is None:
            status = "ready" if current_call == call_kind else "waiting"
        elif attempt.transaction.status == BlockchainTransaction.Status.CONFIRMED:
            status = "confirmed"
        elif attempt.transaction.status == BlockchainTransaction.Status.FAILED:
            status = "failed"
        elif attempt.transaction.status == BlockchainTransaction.Status.REORGED:
            status = "needs_review"
        elif attempt.transaction.status == BlockchainTransaction.Status.SUBMITTED:
            status = "submitted"
        elif attempt.submission_started_at is not None:
            status = "recover"
        else:
            status = "ready"
        if (
            operation is not None
            and operation.status == ReceiptBlockchainOperation.Status.NEEDS_REVIEW
            and attempt is not None
            and attempt.transaction.status != BlockchainTransaction.Status.CONFIRMED
        ):
            # A submitted call that contradicted its event or state was kept
            # submitted for forensic review; never offer it as a normal poll.
            status = "needs_review"
        if status == "failed" and current_call == call_kind:
            # Keep the failure in history while offering a new, separately tracked retry.
            action_mode = "send"
        elif status == "recover":
            action_mode = "recover"
        elif status == "submitted":
            action_mode = "confirm"
        elif status == "ready" and current_call == call_kind:
            action_mode = "send"
        else:
            action_mode = "none"
        steps.append(
            {
                "kind": call_kind,
                "label": labels[call_kind][0],
                "description": labels[call_kind][1],
                "attempt": attempt,
                "status": status,
                "is_current": current_call == call_kind,
                "action_mode": action_mode,
            }
        )

    return {
        "blockchain_operation": operation,
        "blockchain_networks": get_receipt_network_candidates(receipt),
        "blockchain_steps": steps,
        "blockchain_current_call": current_call,
    }


def reject_receipt_call(
    *, user: AbstractBaseUser, company_id: int, receipt_id: int, attempt_id: int
) -> ReceiptBlockchainAttempt:
    """Close a prepared attempt only after MetaMask explicitly rejects it."""

    attempt = _get_attempt(company_id=company_id, receipt_id=receipt_id, attempt_id=attempt_id)
    require_receiving_access(user, company_id, attempt.operation.receipt.unit_id)
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("Esta tentativa não está aguardando resposta da carteira.")
    if attempt.transaction.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Uma transação já transmitida não pode ser marcada como recusada.")
    fail_transaction(attempt.transaction, "A carteira recusou explicitamente a solicitação.")
    attempt.refresh_from_db()
    return attempt


def _verify_chain_transaction(
    *, attempt: ReceiptBlockchainAttempt, transaction_hash: str
) -> bool:
    """Compare RPC sender, network, destination, value, and calldata to the saved attempt."""

    record = attempt.transaction
    web3 = connect_to_rpc(record.network)
    try:
        raw_transaction = web3.eth.get_transaction(transaction_hash)
    except TransactionNotFound:
        return False
    except Web3Exception as error:
        raise ConnectionError("Não foi possível consultar a transação no RPC.") from error

    actual_hash = normalize_transaction_hash(Web3.to_hex(raw_transaction["hash"]))
    actual_sender = normalize_wallet_address(str(raw_transaction.get("from", "")))
    actual_to = normalize_wallet_address(str(raw_transaction.get("to") or ""))
    if actual_hash != transaction_hash:
        raise ValidationError("O RPC retornou uma transação com outra hash.")
    if actual_sender != attempt.sender_address:
        raise ValidationError("A transação foi enviada por outra carteira.")
    if actual_to != attempt.operation.soja_coin.address_normalized:
        raise ValidationError("A transação chama outro contrato SojaCoin.")
    if raw_transaction.get("chainId") is None or int(raw_transaction["chainId"]) != record.network.chain_id:
        raise ValidationError("A transação pertence a outra rede.")
    if int(raw_transaction.get("value", 0)) != 0:
        raise ValidationError("A chamada do recebimento não pode transferir saldo nativo.")
    actual_data = _normalize_call_data(raw_transaction.get("input", raw_transaction.get("data")))
    if actual_data != attempt.prepared_data:
        raise ValidationError("Os dados enviados diferem dos preparados pelo servidor.")
    return True


def register_receipt_call_hash(
    *,
    user: AbstractBaseUser,
    company_id: int,
    receipt_id: int,
    attempt_id: int,
    transaction_hash: str,
) -> BlockchainTransaction | None:
    """Attach the same wallet hash idempotently after the RPC validates its call."""

    canonical_hash = normalize_transaction_hash(transaction_hash)
    if not is_valid_transaction_hash(canonical_hash):
        raise ValidationError("Informe uma hash de transação EVM válida.")
    attempt = _get_attempt(company_id=company_id, receipt_id=receipt_id, attempt_id=attempt_id)
    require_receiving_access(user, company_id, attempt.operation.receipt.unit_id)
    record = attempt.transaction
    if record.transaction_hash:
        if record.transaction_hash == canonical_hash:
            return record
        raise ValidationError("Esta tentativa já está associada a outra hash.")
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("A tentativa não está aguardando uma transação da carteira.")
    if record.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Somente uma tentativa preparada pode receber uma hash.")
    if not _verify_chain_transaction(attempt=attempt, transaction_hash=canonical_hash):
        # Preserve the attempt: the node may not know the transaction yet.
        return None
    return submit_transaction(record, canonical_hash)


def _read_deposit_at_block(
    *, contract: Contract, deposit_id: int, block_number: int
) -> tuple[Any, ...]:
    """Read the deposit state at the exact block that emitted the event."""

    value = contract.functions.deposits(deposit_id).call(block_identifier=block_number)
    return tuple(value)


def _event_integer(event: DecodedContractEvent, key: str) -> int:
    """Read an integer event field while converting malformed evidence to validation errors."""

    try:
        return int(event.args[key])
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError(f"O evento {event.event_name} não contém {key} válido.") from error


def _event_address(event: DecodedContractEvent, key: str) -> str:
    """Read and normalize one EVM address from a decoded event."""

    try:
        value = normalize_wallet_address(str(event.args[key]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError(f"O evento {event.event_name} não contém {key} válido.") from error
    if not is_valid_wallet_address(value):
        raise ValidationError(f"O evento {event.event_name} contém um endereço inválido.")
    return value


def _event_hash(event: DecodedContractEvent, key: str) -> str:
    """Read and normalize one bytes32 event field."""

    try:
        return normalize_bytes32_hash(Web3.to_hex(event.args[key]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError(f"O evento {event.event_name} não contém {key} válido.") from error


def _validate_open_event(
    *, attempt: ReceiptBlockchainAttempt, event: DecodedContractEvent, contract: Contract
) -> None:
    """Match LoadReceiptOpened to the prepared participant, unit, terms, and sender."""

    operation = attempt.operation
    expected = attempt.expected_arguments
    deposit_id = _event_integer(event, "depositId")
    if (
        deposit_id < 1
        or _event_address(event, "depositor") != operation.participant_address
        or _event_integer(event, "unitId") != operation.unit_chain_id
        or _event_address(event, "openBy") != attempt.sender_address
        or _event_hash(event, "participationTermsHash") != operation.participant_terms_hash
    ):
        raise ValidationError("LoadReceiptOpened diverge dos dados preparados para esta carga.")
    if normalize_wallet_address(expected["participant"]) != operation.participant_address:
        raise ValidationError("A carteira do participante mudou após a preparação da chamada.")
    if int(expected["unit_id"]) != operation.unit_chain_id:
        raise ValidationError("A unidade mudou após a preparação da chamada.")
    if normalize_bytes32_hash(expected["crop_year"]) != operation.crop_year_bytes32:
        raise ValidationError("A safra mudou após a preparação da chamada.")

    state = _read_deposit_at_block(
        contract=contract,
        deposit_id=deposit_id,
        block_number=event.block_number,
    )
    if (
        normalize_wallet_address(str(state[0])) != operation.participant_address
        or int(state[2]) != operation.unit_chain_id
        or normalize_bytes32_hash(Web3.to_hex(state[3])) != operation.crop_year_bytes32
        or normalize_bytes32_hash(Web3.to_hex(state[4])) != operation.participant_terms_hash
        or bool(state[6])
        or bool(state[8])
    ):
        raise ValidationError("O estado do recebimento aberto diverge do evento confirmado.")
    operation.deposit_id = deposit_id
    operation.save(update_fields=("deposit_id", "updated_at"))


def _validate_weighing_event(
    *,
    attempt: ReceiptBlockchainAttempt,
    event: DecodedContractEvent,
    contract: Contract,
    observed_events: dict[str, DecodedContractEvent],
) -> None:
    """Match weighing and classification events to every saved ticket field."""

    operation = attempt.operation
    expected = attempt.expected_arguments["weighing_ticket"]
    if event.event_name == "LoadReceiptConfirmed":
        checks = (
            _event_integer(event, "depositId") == operation.deposit_id,
            _event_integer(event, "unitId") == operation.unit_chain_id,
            _event_address(event, "responsibleParty") == attempt.sender_address,
            _event_address(event, "depositor") == operation.participant_address,
            _event_integer(event, "grossWeightKg") == int(expected["gross_weight_kg"]),
            _event_integer(event, "tareKg") == int(expected["tare_kg"]),
            _event_integer(event, "netWeightKg")
            == int(expected["gross_weight_kg"])
            - int(expected["tare_kg"])
            - int(expected["moisture_deduction_kg"])
            - int(expected["impurities_deduction_kg"])
            - int(expected["damaged_grain_deduction_kg"])
            - int(expected["other_deductions_kg"]),
            _event_hash(event, "weighingTicketHash")
            == normalize_bytes32_hash(expected["weighing_ticket_hash"]),
            _event_hash(event, "reportHash") == normalize_bytes32_hash(expected["report_hash"]),
            _event_integer(event, "documentTimestamp") == int(expected["timestamp"]),
        )
        if not all(checks):
            raise ValidationError("LoadReceiptConfirmed diverge dos dados da pesagem preparada.")
        observed_events[event.event_name] = event
    elif event.event_name == "ReceiptClassified":
        checks = (
            _event_integer(event, "depositId") == operation.deposit_id,
            _event_integer(event, "moistureBps") == int(expected["moisture_bps"]),
            _event_integer(event, "impuritiesBps") == int(expected["impurities_bps"]),
            _event_integer(event, "damagedGrainBps") == int(expected["damaged_grain_bps"]),
            _event_integer(event, "moistureDeductionKg") == int(expected["moisture_deduction_kg"]),
            _event_integer(event, "impuritiesDeductionKg") == int(expected["impurities_deduction_kg"]),
            _event_integer(event, "damagedGrainDeductionKg") == int(expected["damaged_grain_deduction_kg"]),
            _event_integer(event, "otherDeductionsKg") == int(expected["other_deductions_kg"]),
        )
        if not all(checks):
            raise ValidationError("ReceiptClassified diverge dos valores preparados.")
        observed_events[event.event_name] = event

    if len(observed_events) == 2:
        confirmed = observed_events["LoadReceiptConfirmed"]
        classified = observed_events["ReceiptClassified"]
        if confirmed.block_number != classified.block_number:
            raise ValidationError("Os eventos da pesagem foram emitidos em blocos diferentes.")
        state = _read_deposit_at_block(
            contract=contract,
            deposit_id=cast(int, operation.deposit_id),
            block_number=confirmed.block_number,
        )
        expected_net_weight = (
            int(expected["gross_weight_kg"])
            - int(expected["tare_kg"])
            - int(expected["moisture_deduction_kg"])
            - int(expected["impurities_deduction_kg"])
            - int(expected["damaged_grain_deduction_kg"])
            - int(expected["other_deductions_kg"])
        )
        if (
            normalize_wallet_address(str(state[0])) != operation.participant_address
            or int(state[2]) != operation.unit_chain_id
            or normalize_bytes32_hash(Web3.to_hex(state[3])) != operation.crop_year_bytes32
            or not bool(state[6])
            or bool(state[8])
            or int(state[11]) != expected_net_weight
            or normalize_bytes32_hash(Web3.to_hex(state[13]))
            != normalize_bytes32_hash(expected["weighing_ticket_hash"])
            or int(state[14]) != int(expected["timestamp"])
        ):
            raise ValidationError("O estado confirmado da carga não corresponde à pesagem.")


def _validate_invoice_event(
    *, attempt: ReceiptBlockchainAttempt, event: DecodedContractEvent, contract: Contract
) -> None:
    """Match the NF-e key hash event and final deposit state to the saved invoice."""

    operation = attempt.operation
    expected = attempt.expected_arguments
    if (
        _event_integer(event, "depositId") != operation.deposit_id
        or _event_integer(event, "unitId") != operation.unit_chain_id
        or _event_hash(event, "taxInvoiceKeyHash")
        != normalize_bytes32_hash(expected["tax_invoice_key_hash"])
        or _event_address(event, "linkedBy") != attempt.sender_address
    ):
        raise ValidationError("TaxInvoiceLinked diverge da nota fiscal preparada.")
    state = _read_deposit_at_block(
        contract=contract,
        deposit_id=cast(int, operation.deposit_id),
        block_number=event.block_number,
    )
    if (
        normalize_wallet_address(str(state[0])) != operation.participant_address
        or int(state[2]) != operation.unit_chain_id
        or normalize_bytes32_hash(Web3.to_hex(state[3])) != operation.crop_year_bytes32
        or normalize_bytes32_hash(Web3.to_hex(state[4])) != operation.participant_terms_hash
        or normalize_bytes32_hash(Web3.to_hex(state[1]))
        != normalize_bytes32_hash(expected["tax_invoice_key_hash"])
        or not bool(state[6])
        or bool(state[8])
    ):
        raise ValidationError("O estado final da carga não corresponde à nota fiscal vinculada.")

    operation.status = ReceiptBlockchainOperation.Status.CONFIRMED
    operation.confirmed_at = timezone.now()
    operation.save(update_fields=("status", "confirmed_at", "updated_at"))
    receipt = operation.receipt
    receipt.status = Receipt.Status.BLOCKCHAIN_CONFIRMED
    receipt.save(update_fields=("status", "updated_at"))


def _make_event_handler(
    *, attempt: ReceiptBlockchainAttempt, web3: Web3
) -> Callable[[BlockchainTransaction, DecodedContractEvent], None]:
    """Build a transaction-scoped handler that validates events and block state."""

    contract = bind_contract_to_web3(web3, attempt.operation.soja_coin)
    observed_weighing_events: dict[str, DecodedContractEvent] = {}

    def handle_event(
        transaction_record: BlockchainTransaction,
        event: DecodedContractEvent,
    ) -> None:
        """Apply only the event associated with this immutable transaction intent."""

        if transaction_record.pk != attempt.transaction_id:
            raise ValidationError("O evento veio de outra tentativa de recebimento.")
        if event.event_name == "LoadReceiptOpened" and attempt.call_kind == ReceiptBlockchainAttempt.CallKind.OPEN:
            _validate_open_event(attempt=attempt, event=event, contract=contract)
        elif (
            event.event_name in {"LoadReceiptConfirmed", "ReceiptClassified"}
            and attempt.call_kind == ReceiptBlockchainAttempt.CallKind.CONFIRM_WEIGHING
        ):
            _validate_weighing_event(
                attempt=attempt,
                event=event,
                contract=contract,
                observed_events=observed_weighing_events,
            )
        elif event.event_name == "TaxInvoiceLinked" and attempt.call_kind == ReceiptBlockchainAttempt.CallKind.LINK_INVOICE:
            _validate_invoice_event(attempt=attempt, event=event, contract=contract)

    return handle_event


def _mark_operation_needs_review(
    *, attempt: ReceiptBlockchainAttempt, reason: str
) -> None:
    """Stop later calls when a successful transaction contradicts its prepared intent."""

    operation = ReceiptBlockchainOperation.objects.select_for_update().get(
        pk=attempt.operation_id
    )
    if operation.status in {
        ReceiptBlockchainOperation.Status.IN_PROGRESS,
        ReceiptBlockchainOperation.Status.CONFIRMED,
    }:
        operation.status = ReceiptBlockchainOperation.Status.NEEDS_REVIEW
        operation.save(update_fields=("status", "updated_at"))
    receipt = Receipt.objects.select_for_update().get(pk=operation.receipt_id)
    if receipt.status in {
        Receipt.Status.BLOCKCHAIN_IN_PROGRESS,
        Receipt.Status.BLOCKCHAIN_CONFIRMED,
    }:
        receipt.status = Receipt.Status.NEEDS_REVIEW
        receipt.save(update_fields=("status", "updated_at"))
    if attempt.is_open:
        attempt.is_open = False
        attempt.save(update_fields=("is_open", "updated_at"))
    record = attempt.transaction
    record.error_message = reason[:4000]
    record.save(update_fields=("error_message", "updated_at"))


def confirm_receipt_call(
    *, user: AbstractBaseUser, company_id: int, receipt_id: int, attempt_id: int
) -> ReceiptCallConfirmation:
    """Poll one submitted call and confirm it only from matching event/state evidence."""

    attempt = _get_attempt(company_id=company_id, receipt_id=receipt_id, attempt_id=attempt_id)
    require_receiving_access(user, company_id, attempt.operation.receipt.unit_id)
    record = attempt.transaction
    operation = attempt.operation
    if operation.status == ReceiptBlockchainOperation.Status.NEEDS_REVIEW:
        return ReceiptCallConfirmation("needs_review", attempt, record, operation.deposit_id)
    if record.status == BlockchainTransaction.Status.CONFIRMED:
        return ReceiptCallConfirmation("confirmed", attempt, record, operation.deposit_id)
    if record.status == BlockchainTransaction.Status.FAILED:
        return ReceiptCallConfirmation("failed", attempt, record, operation.deposit_id)
    if record.status != BlockchainTransaction.Status.SUBMITTED or not record.transaction_hash:
        raise ValidationError("A tentativa ainda não tem uma transação enviada para consultar.")

    web3 = connect_to_rpc(record.network)
    try:
        if not _verify_chain_transaction(
            attempt=attempt,
            transaction_hash=record.transaction_hash,
        ):
            return ReceiptCallConfirmation(
                "pending",
                attempt,
                record,
                operation.deposit_id,
            )
    except ValidationError as error:
        with db_transaction.atomic():
            _mark_operation_needs_review(attempt=attempt, reason=" ".join(error.messages))
            attempt.refresh_from_db()
            operation.refresh_from_db()
        return ReceiptCallConfirmation("needs_review", attempt, record, operation.deposit_id)
    try:
        with db_transaction.atomic():
            try:
                confirmed_record = confirm_transaction_from_chain(
                    record.pk,
                    event_handler=_make_event_handler(attempt=attempt, web3=web3),
                    required_event_names=REQUIRED_EVENTS_BY_CALL[attempt.call_kind],
                )
            except ValidationError as error:
                try:
                    chain_receipt = web3.eth.get_transaction_receipt(record.transaction_hash)
                except TransactionNotFound:
                    return ReceiptCallConfirmation("pending", attempt, record, operation.deposit_id)
                if int(chain_receipt.get("status", 0)) == 1:
                    _mark_operation_needs_review(
                        attempt=attempt,
                        reason=" ".join(error.messages),
                    )
                    attempt.refresh_from_db()
                    operation.refresh_from_db()
                    record.refresh_from_db()
                    return ReceiptCallConfirmation(
                        "needs_review",
                        attempt,
                        record,
                        operation.deposit_id,
                    )
                raise
            if confirmed_record is None:
                return ReceiptCallConfirmation("pending", attempt, record, operation.deposit_id)
            attempt.refresh_from_db()
            confirmed_record.refresh_from_db()
            if confirmed_record.status == BlockchainTransaction.Status.FAILED:
                return ReceiptCallConfirmation("failed", attempt, confirmed_record, operation.deposit_id)
            if attempt.is_open:
                attempt.is_open = False
                attempt.save(update_fields=("is_open", "updated_at"))
            operation.refresh_from_db()
            return ReceiptCallConfirmation(
                "confirmed",
                attempt,
                confirmed_record,
                operation.deposit_id,
            )
    except TransactionNotFound:
        return ReceiptCallConfirmation("pending", attempt, record, operation.deposit_id)

