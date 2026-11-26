"""Prepare and reconcile issuance-batch actions against the saved chain state.

The employee and participant wallets sign in their own browser. This service
persists each intended call before the wallet prompt, validates the returned
transaction against that snapshot, and changes business state only after RPC,
event, and contract-storage checks agree.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from collections.abc import Callable
from typing import Any, Literal

from eth_account import Account
from eth_account.messages import SignableMessage, encode_typed_data
from eth_abi import encode as abi_encode
from eth_keys.exceptions import BadSignature
from eth_utils.exceptions import ValidationError as EthSignatureValidationError
from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import TransactionNotFound, Web3Exception

from accounts.models import UserWallet
from accounts.rules.wallet_access import require_verified_wallet
from blockchain.models import BlockchainTransaction, ContractInstance, Network
from blockchain.services.contracts import bind_contract_to_web3
from blockchain.services.rpc import connect_to_rpc
from blockchain.services.transaction_confirmation import (
    DecodedContractEvent,
    confirm_transaction_from_chain,
)
from blockchain.services.transactions import fail_transaction, submit_transaction
from companies.rules.role_access import require_issuance_access
from receipts.models import (
    Receipt,
    ReceiptBlockchainAttempt,
    ReceiptBlockchainOperation,
)
from utils.identifiers import (
    is_valid_transaction_hash,
    is_valid_wallet_address,
    normalize_bytes32_hash,
    normalize_transaction_hash,
    normalize_wallet_address,
)

from .models import (
    ISSUANCE_BATCH_OPERATION_TYPE,
    IssuanceBatch,
    IssuanceBatchApprovalLink,
    IssuanceBatchAttempt,
    IssuanceBatchAuthorizationChallenge,
    IssuanceBatchReceipt,
)


logger = logging.getLogger(__name__)


# The bearer link remains available for review/cancellation for a bounded week.
# The contract's own batch deadline independently controls whether approval is valid.
LINK_LIFETIME = timedelta(days=7)
# A 65-byte ECDSA signature encoded as 0x plus 130 hexadecimal characters.
MAX_SIGNATURE_LENGTH = 132
# Canonical lowercase byte strings persisted as the exact wallet calldata.
HEX_BYTES_RE = re.compile(r"0x(?:[0-9a-f]{2})+\Z")
# Every source receipt operation must have its three ordered calls confirmed.
REQUIRED_RECEIPT_CALLS = {
    ReceiptBlockchainAttempt.CallKind.OPEN,
    ReceiptBlockchainAttempt.CallKind.CONFIRM_WEIGHING,
    ReceiptBlockchainAttempt.CallKind.LINK_INVOICE,
}
# These states keep a receipt reserved. Only a confirmed cancellation releases it.
ACTIVE_BATCH_STATUSES = {
    IssuanceBatch.Status.CREATION_PREPARED,
    IssuanceBatch.Status.CREATION_SUBMITTED,
    IssuanceBatch.Status.AWAITING_APPROVAL,
    IssuanceBatch.Status.APPROVAL_SUBMITTED,
    # Legacy signature approvals remain locked until explicitly reviewed.
    IssuanceBatch.Status.APPROVED,
    IssuanceBatch.Status.COMPLETED,
    IssuanceBatch.Status.CANCEL_SUBMITTED,
    IssuanceBatch.Status.NEEDS_REVIEW,
}
# Must match EIP712("SojaCoin Token Issuance Batches", "1") in Solidity.
EIP712_DOMAIN = {
    "name": "SojaCoin Token Issuance Batches",
    "version": "1",
}


@dataclass(frozen=True)
class PreparedBatchAction:
    """Return the saved MetaMask request and its recovery state."""

    batch: IssuanceBatch
    attempt: IssuanceBatchAttempt
    status: Literal["ready", "recover", "submitted"]
    chain_id: int
    contract_address: str
    transaction_data: str


@dataclass(frozen=True)
class BatchChallengeResult:
    """Return the persisted EIP-712 challenge and exact typed data."""

    challenge: IssuanceBatchAuthorizationChallenge
    typed_data: dict[str, Any]


@dataclass(frozen=True)
class BatchTransactionResult:
    """Describe whether one prepared batch transaction is pending or reconciled."""

    status: Literal["pending", "failed", "confirmed", "needs_review"]
    attempt: IssuanceBatchAttempt
    transaction: BlockchainTransaction


def calculate_summary_hash(
    *,
    deposit_id: int,
    unit_id: int,
    crop_year: str,
    invoice_key_hash: str,
    weighing_ticket_hash: str,
    net_weight_kg: int,
) -> str:
    """Reproduce Solidity's iterative ``abi.encode`` summary for a single receipt.

    The contract hashes the array length first, then folds each receipt into the
    previous digest. Preserving that order and each ABI type is essential: the
    result is signed by the participant and fixes exactly which load is approved.
    """

    if min(deposit_id, unit_id, net_weight_kg) < 1:
        raise ValidationError("O ID da carga, o ID da unidade e o peso precisam ser positivos.")
    
    crop_year_hash = normalize_bytes32_hash(crop_year)
    invoice_hash = normalize_bytes32_hash(invoice_key_hash)
    ticket_hash = normalize_bytes32_hash(weighing_ticket_hash)
    
    for value in (crop_year_hash, invoice_hash, ticket_hash):
        if len(bytes.fromhex(value[2:])) != 32:
            raise ValidationError("O resumo do lote contém uma hash bytes32 inválida.")

    # The first digest commits the receipt count. Each following digest folds
    # one receipt into this accumulator using Solidity's exact ABI field order.
    summary = Web3.keccak(abi_encode(["uint256"], [1]))
    summary = Web3.keccak(
        abi_encode(
            ["bytes32", "uint256", "uint256", "bytes32", "bytes32", "bytes32", "uint256"],
            [summary, deposit_id, unit_id, bytes.fromhex(crop_year_hash[2:]),
             bytes.fromhex(invoice_hash[2:]), bytes.fromhex(ticket_hash[2:]), net_weight_kg],
        )
    )
    return Web3.to_hex(summary).lower()


def _load_receipt(*, company_id: int, receipt_id: int) -> Receipt:
    """Load a receipt and its immutable source data inside company scope."""

    try:
        return (
            Receipt.objects.select_related(
                "company",
                "participant",
                "unit",
                "weighing_ticket",
                "tax_invoice",
                "blockchain_operation__network",
                "blockchain_operation__soja_coin",
                "blockchain_operation__unit_registry",
            )
            .prefetch_related("blockchain_operation__attempts__transaction")
            .get(pk=receipt_id, company_id=company_id)
        )
    except Receipt.DoesNotExist as error:
        raise ValidationError("O recebimento não pertence à empresa selecionada.") from error


def _load_batch(*, company_id: int, batch_id: int) -> IssuanceBatch:
    """Load a batch with its fixed deployment and participant references."""

    try:
        return IssuanceBatch.objects.select_related(
            "company",
            "participant",
            "unit",
            "network",
            "soja_coin",
            "unit_registry",
            "batches_contract",
            "created_by",
        ).get(pk=batch_id, company_id=company_id)
    except IssuanceBatch.DoesNotExist as error:
        raise ValidationError("O lote não pertence à empresa selecionada.") from error


def _get_attempt(*, company_id: int, batch_id: int, attempt_id: int) -> IssuanceBatchAttempt:
    """Scope an attempt through its batch and company before any state change."""

    try:
        return IssuanceBatchAttempt.objects.select_related(
            "batch__company",
            "batch__participant",
            "batch__network",
            "batch__soja_coin",
            "batch__unit_registry",
            "batch__batches_contract",
            "transaction__network",
            "transaction__contract",
            "link",
            "challenge",
        ).get(pk=attempt_id, batch_id=batch_id, batch__company_id=company_id)
    except IssuanceBatchAttempt.DoesNotExist as error:
        raise ValidationError("A tentativa não pertence a este lote.") from error


def _active_batch_row(receipt: Receipt) -> IssuanceBatchReceipt | None:
    """Return the current batch reservation for the receipt, if one exists."""

    return (
        IssuanceBatchReceipt.objects.select_related("batch")
        .filter(receipt=receipt, is_current=True)
        .order_by("-pk")
        .first()
    )


def _unresolved_batch_row(receipt: Receipt) -> IssuanceBatchReceipt | None:
    """Find any batch that still owns or may still own this chain deposit."""

    return (
        IssuanceBatchReceipt.objects.select_related("batch")
        .filter(receipt=receipt, batch__status__in=ACTIVE_BATCH_STATUSES)
        .order_by("-pk")
        .first()
    )


def _get_live_receipt_operation(receipt: Receipt) -> ReceiptBlockchainOperation:
    """Require a fully reconciled receipt operation and all three confirmed calls."""

    try:
        operation = receipt.blockchain_operation
    except ReceiptBlockchainOperation.DoesNotExist as error:
        raise ValidationError("O recebimento ainda não foi confirmado na blockchain.") from error
    if (
        receipt.status != Receipt.Status.BLOCKCHAIN_CONFIRMED
        or operation.status != ReceiptBlockchainOperation.Status.CONFIRMED
        or operation.deposit_id is None
    ):
        raise ValidationError("O recebimento não está confirmado e livre de revisão.")
    attempts = list(operation.attempts.select_related("transaction").all())
    latest_by_kind: dict[str, ReceiptBlockchainAttempt] = {}
    for attempt in attempts:
        if attempt.call_kind not in latest_by_kind or attempt.pk > latest_by_kind[attempt.call_kind].pk:
            latest_by_kind[attempt.call_kind] = attempt
    if set(latest_by_kind) != REQUIRED_RECEIPT_CALLS or any(
        latest_by_kind[kind].transaction.status != BlockchainTransaction.Status.CONFIRMED
        for kind in REQUIRED_RECEIPT_CALLS
    ):
        raise ValidationError("As três transações do recebimento precisam estar confirmadas.")
    if any(
        attempt.transaction.status == BlockchainTransaction.Status.REORGED
        for attempt in attempts
    ):
        raise ValidationError("O histórico do recebimento contém uma transação reorganizada.")
    return operation


def _active_contracts(
    *, receipt: Receipt, operation: ReceiptBlockchainOperation
) -> ContractInstance:
    """Require the saved batch module to match the receipt's company and network."""

    if not receipt.company.active or not receipt.unit.active or not operation.network.active:
        raise ValidationError("A empresa, unidade ou rede deste recebimento está inativa.")
    if (
        operation.soja_coin.company_id != receipt.company_id
        or operation.unit_registry.company_id != receipt.company_id
        or operation.soja_coin.network_id != operation.network_id
        or operation.unit_registry.network_id != operation.network_id
        or operation.soja_coin.kind != ContractInstance.Kind.SOJA_COIN
        or operation.unit_registry.kind != ContractInstance.Kind.UNIT_REGISTRY
        or not operation.soja_coin.active
        or not operation.unit_registry.active
    ):
        raise ValidationError("A operação do recebimento aponta para implantações inválidas.")
    try:
        batches = ContractInstance.objects.get(
            company_id=receipt.company_id,
            network_id=operation.network_id,
            kind=ContractInstance.Kind.ISSUANCE_BATCHES,
            active=True,
        )
    except ContractInstance.DoesNotExist as error:
        raise ValidationError("Não há TokenIssuanceBatches ativo para esta empresa e rede.") from error
    if batches.network_id != operation.network_id:
        raise ValidationError("O módulo de lotes não pertence à rede deste recebimento.")
    return batches


def _get_contracts(
    *,
    receipt: Receipt,
    operation: ReceiptBlockchainOperation,
    batches_instance: ContractInstance,
) -> tuple[Web3, Contract, Contract, Contract]:
    """Bind the three saved deployments and verify their reciprocal links on-chain."""

    web3 = connect_to_rpc(operation.network)
    addresses = (
        operation.soja_coin.address_normalized,
        operation.unit_registry.address_normalized,
        batches_instance.address_normalized,
    )
    if any(not web3.eth.get_code(Web3.to_checksum_address(address)) for address in addresses):
        raise ValidationError("Falta código de contrato em um dos endereços configurados para esta rede.")
    soja_coin = bind_contract_to_web3(web3, operation.soja_coin)
    registry = bind_contract_to_web3(web3, operation.unit_registry)
    batches = bind_contract_to_web3(web3, batches_instance)
    if normalize_wallet_address(str(soja_coin.functions.unitRegistry().call())) != registry.address.lower():
        raise ValidationError("A SojaCoin não aponta para o UnitRegistry salvo no recebimento.")
    if normalize_wallet_address(str(registry.functions.sojaCoin().call())) != soja_coin.address.lower():
        raise ValidationError("O UnitRegistry não aponta para a SojaCoin do recebimento.")
    if normalize_wallet_address(str(soja_coin.functions.issuanceBatchModule().call())) != batches.address.lower():
        raise ValidationError("A SojaCoin não está ligada a este TokenIssuanceBatches.")
    if normalize_wallet_address(str(batches.functions.sojaCoin().call())) != soja_coin.address.lower():
        raise ValidationError("O TokenIssuanceBatches está ligado a outra SojaCoin.")
    if receipt.unit.chain_unit_id is None:
        raise ValidationError("A unidade ainda não tem ID confirmado na blockchain.")
    return web3, soja_coin, registry, batches


def _verified_operator_wallet(*, user: AbstractBaseUser, sender_address: str) -> str:
    """Require a verified wallet owned by the current SaaS user."""

    sender = normalize_wallet_address(sender_address)
    if not is_valid_wallet_address(sender):
        raise ValidationError("Conecte uma carteira EVM válida.")
    link = (
        UserWallet.objects.select_related("user", "wallet")
        .filter(
            user=user,
            wallet__address_normalized=sender,
            wallet__active=True,
            verification_status=UserWallet.VerificationStatus.VERIFIED,
        )
        .first()
    )
    if link is None:
        raise PermissionDenied("Conecte uma carteira verificada no perfil deste usuário.")
    require_verified_wallet(link)
    return sender


def _validate_unit_registration(
    *, receipt: Receipt, registry_contract: Contract
) -> None:
    """Match the local unit code and registration hash to the active chain record."""

    unit = receipt.unit
    encoded_code = unit.code.strip().upper().encode("utf-8")
    if not encoded_code or len(encoded_code) > 32:
        raise ValidationError("O código da unidade não cabe em bytes32.")
    expected_code = encoded_code.ljust(32, b"\x00")
    on_chain_code, on_chain_hash, _report_hash, _inventory, _audit, active = (
        registry_contract.functions.units(unit.chain_unit_id).call()
    )
    code_id = int(registry_contract.functions.unitIdByCode(expected_code).call())
    if (
        bytes(on_chain_code) != expected_code
        or normalize_bytes32_hash(Web3.to_hex(on_chain_hash)) != normalize_bytes32_hash(unit.registration_hash)
        or not bool(active)
        or code_id != unit.chain_unit_id
    ):
        raise ValidationError("A unidade local não corresponde ao registro ativo do UnitRegistry.")


def _read_chain_deposit(
    *, soja_contract: Contract, deposit_id: int, block_identifier: int | str = "latest"
) -> tuple[Any, ...]:
    """Read one public deposit tuple at the requested canonical block."""

    return tuple(soja_contract.functions.deposits(deposit_id).call(block_identifier=block_identifier))


def _validate_deposit_snapshot(
    *, receipt: Receipt, operation: ReceiptBlockchainOperation, soja_contract: Contract
) -> dict[str, Any]:
    """Compare every batch-relevant local field with SojaCoin's deposit mapping."""

    if operation.deposit_id is None:
        raise ValidationError("O recebimento não possui ID confirmado na SojaCoin.")
    
    if not hasattr(receipt, "weighing_ticket") or not hasattr(receipt, "tax_invoice"):
        raise ValidationError("A pesagem e a NF-e precisam estar salvas no recebimento.")
    
    deposit = _read_chain_deposit(soja_contract=soja_contract, deposit_id=operation.deposit_id)
    expected_producer = receipt.participant.wallet_address_normalized
    expected_invoice_hash = normalize_bytes32_hash(receipt.tax_invoice.access_key_hash)
    expected_ticket_hash = normalize_bytes32_hash(receipt.weighing_ticket.weighing_ticket_hash)
    expected_crop_year = normalize_bytes32_hash(receipt.crop_year_bytes32)
    expected_weight = int(receipt.weighing_ticket.net_weight_kg)
    
    # Tuple positions follow SojaCoin.Deposit's public getter in the packaged ABI.
    chain_values = {
        "producer": normalize_wallet_address(str(deposit[0])),
        "invoice_hash": normalize_bytes32_hash(Web3.to_hex(deposit[1])),
        "unit_id": int(deposit[2]),
        "crop_year": normalize_bytes32_hash(Web3.to_hex(deposit[3])),
        "receipt_confirmed": bool(deposit[6]),
        "issued": bool(deposit[7]),
        "canceled": bool(deposit[8]),
        "closed": bool(deposit[9]),
        "net_weight_kg": int(deposit[11]),
        "returned_weight_kg": int(deposit[12]),
        "ticket_hash": normalize_bytes32_hash(Web3.to_hex(deposit[13])),
    }
    if (
        chain_values["producer"] != expected_producer
        or chain_values["invoice_hash"] != expected_invoice_hash
        or chain_values["unit_id"] != receipt.unit.chain_unit_id
        or chain_values["crop_year"] != expected_crop_year
        or not chain_values["receipt_confirmed"]
        or chain_values["issued"]
        or chain_values["canceled"]
        or chain_values["closed"]
        or chain_values["net_weight_kg"] != expected_weight
        or chain_values["returned_weight_kg"] != 0
        or chain_values["ticket_hash"] != expected_ticket_hash
        or expected_weight < 1
    ):
        
        raise ValidationError("Os dados locais da carga divergem da SojaCoin ou não estão elegíveis.")
    
    return {
        "deposit_id": int(operation.deposit_id),
        "unit_id": int(receipt.unit.chain_unit_id),
        "crop_year": expected_crop_year,
        "invoice_key_hash": expected_invoice_hash,
        "weighing_ticket_hash": expected_ticket_hash,
        "net_weight_kg": expected_weight,
    }


def _validate_issuer(
    *,
    user: AbstractBaseUser,
    company_id: int,
    receipt: Receipt,
    sender_address: str,
    operation: ReceiptBlockchainOperation,
    registry_contract: Contract,
) -> str:
    """Require SaaS role/unit access, a verified wallet and its live issue bit."""

    require_issuance_access(user, company_id, receipt.unit_id)
    
    sender = _verified_operator_wallet(user=user, sender_address=sender_address)
    permission_bit = int(registry_contract.functions.PERMISSION_ISSUE_SJC().call())
    
    if not registry_contract.functions.hasPermissionForUnit(
        receipt.unit.chain_unit_id,
        Web3.to_checksum_address(sender),
        permission_bit,
    ).call():
        
        raise PermissionDenied("O UnitRegistry não reconhece permissão de emissão para esta carteira.")
    
    return sender


def _preflight_create_call(
    *, batches_contract: Contract, deposit_id: int, deadline: int, sender: str
) -> int:
    """Simulate the exact contract call so audit, deposit and permission checks run."""

    try:
        batch_id = int(
            batches_contract.functions.createIssuanceBatch([deposit_id], deadline).call(
                {"from": Web3.to_checksum_address(sender)}
            )
        )
        
    except (Web3Exception, ValueError) as error:
        # This eth_call is intentionally the full on-chain preflight. It exercises
        # audit validity and UnitRegistry permission checks without broadcasting.
        raise ValidationError(
            "A rede recusou a pré-validação do lote. Confira auditoria, permissão de emissão e estado da carga."
        ) from error
        
    if batch_id < 1:
        raise ValidationError("O contrato retornou um próximo ID de lote inválido.")
    return batch_id


def _active_attempt(batch: IssuanceBatch, kind: str) -> IssuanceBatchAttempt | None:
    """Return the one open attempt for an action, if it must be resumed."""

    return (
        batch.attempts.filter(kind=kind, is_open=True)
        .select_related("transaction")
        .order_by("-created_at", "-pk")
        .first()
    )


def _has_open_participant_attempt(batch: IssuanceBatch) -> bool:
    """Protect a bearer link while an approval or cancel wallet response is unresolved."""

    return batch.attempts.filter(
        kind__in=(
            IssuanceBatchAttempt.Kind.APPROVE,
            IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE,
            IssuanceBatchAttempt.Kind.CANCEL,
        ),
        is_open=True,
    ).exists()


def _action_status(attempt: IssuanceBatchAttempt) -> Literal["ready", "recover", "submitted"]:
    """Describe whether an open attempt can send, needs hash recovery, or was submitted."""

    if attempt.transaction.status == BlockchainTransaction.Status.SUBMITTED:
        return "submitted"
    if attempt.submission_started_at is not None:
        return "recover"
    return "ready"


def _prepared_action(
    *, batch: IssuanceBatch, attempt: IssuanceBatchAttempt
) -> PreparedBatchAction:
    """Build the stable response for an already persisted attempt."""

    return PreparedBatchAction(
        batch=batch,
        attempt=attempt,
        status=_action_status(attempt),
        chain_id=batch.network.chain_id,
        contract_address=batch.batches_contract.address_normalized,
        transaction_data=attempt.prepared_data,
    )


def prepare_issuance_batch(
    *,
    user: AbstractBaseUser,
    company_id: int,
    receipt_id: int,
    deadline: int,
    sender_address: str,
) -> PreparedBatchAction:
    """Validate and persist a one-receipt batch before opening the employee wallet."""

    if isinstance(deadline, bool) or not isinstance(deadline, int) or deadline < 1:
        raise ValidationError("Informe um prazo de aprovação válido.")
    receipt = _load_receipt(company_id=company_id, receipt_id=receipt_id)
    operation = _get_live_receipt_operation(receipt)
    membership, _unit, _unit_access = require_issuance_access(user, company_id, receipt.unit_id)
    sender = _verified_operator_wallet(user=user, sender_address=sender_address)
    existing_row = _unresolved_batch_row(receipt)
    retry_batch: IssuanceBatch | None = None
    if existing_row is not None:
        existing_batch = existing_row.batch
        if existing_batch.status == IssuanceBatch.Status.CREATION_PREPARED:
            if existing_batch.creator_address != sender:
                raise PermissionDenied("Retome a criação com a mesma carteira que preparou o lote.")
            open_attempt = _active_attempt(existing_batch, IssuanceBatchAttempt.Kind.CREATE)
            if open_attempt is not None:
                return _prepared_action(batch=existing_batch, attempt=open_attempt)
            # A definitive wallet refusal or mined revert closes the old attempt.
            # The batch snapshot remains fixed and may receive a fresh transaction.
            retry_batch = existing_batch
            if deadline != existing_batch.approval_deadline:
                raise ValidationError("A tentativa anterior fixou outro prazo. Reabra o recebimento e use o prazo salvo.")
        else:
            raise ValidationError(
                f"O recebimento já está reservado pelo lote #{existing_batch.chain_batch_id or existing_batch.pk}."
            )
    historical_review = (
        IssuanceBatchReceipt.objects.filter(
            receipt=receipt,
            batch__status=IssuanceBatch.Status.NEEDS_REVIEW,
        )
        .select_related("batch")
        .first()
    )
    if historical_review is not None:
        raise ValidationError("Um lote anterior deste recebimento precisa de conferência antes de qualquer nova ação.")

    if receipt.company_id != company_id or membership.company_id != receipt.company_id:
        raise PermissionDenied("O recebimento não pertence à empresa autorizada.")
    if operation.network_id != receipt.blockchain_operation.network_id:
        raise ValidationError("A rede do lote deve ser a mesma rede que confirmou o recebimento.")
    if operation.status == ReceiptBlockchainOperation.Status.NEEDS_REVIEW:
        raise ValidationError("O recebimento precisa de conferência antes da criação do lote.")
    if deadline <= int(timezone.now().timestamp()):
        raise ValidationError("O prazo de aprovação precisa estar no futuro.")

    batch_contract_instance = _active_contracts(receipt=receipt, operation=operation)
    web3, soja_contract, registry_contract, batches_contract = _get_contracts(
        receipt=receipt,
        operation=operation,
        batches_instance=batch_contract_instance,
    )
    sender = _validate_issuer(
        user=user,
        company_id=company_id,
        receipt=receipt,
        sender_address=sender,
        operation=operation,
        registry_contract=registry_contract,
    )
    _validate_unit_registration(receipt=receipt, registry_contract=registry_contract)
    deposit_data = _validate_deposit_snapshot(
        receipt=receipt,
        operation=operation,
        soja_contract=soja_contract,
    )
    latest_block_time = int(web3.eth.get_block("latest")["timestamp"])
    # Use the chain clock as authority so a skewed Django host cannot prepare
    # an approval deadline that is already expired on the contract.
    if deadline <= latest_block_time:
        raise ValidationError("O prazo precisa ser posterior ao horário do bloco mais recente da rede.")
    deposit_id = deposit_data["deposit_id"]
    if int(batches_contract.functions.batchByDeposit(deposit_id).call()) != 0:
        raise ValidationError("A carga já está associada a um lote na blockchain.")
    if bool(batches_contract.functions.isDepositLocked(deposit_id).call()):
        raise ValidationError("A carga está bloqueada por outra operação de lote.")
    # Persist this immutable commitment before the wallet opens; the event,
    # participant signature and final contract reads must all match it.
    summary_hash = calculate_summary_hash(**deposit_data)
    if retry_batch is not None:
        saved_row = retry_batch.receipt_rows.get()
        if (
            saved_row.deposit_id != deposit_data["deposit_id"]
            or saved_row.unit_chain_id != deposit_data["unit_id"]
            or saved_row.crop_year_bytes32 != deposit_data["crop_year"]
            or saved_row.invoice_key_hash != deposit_data["invoice_key_hash"]
            or saved_row.weighing_ticket_hash != deposit_data["weighing_ticket_hash"]
            or saved_row.net_weight_kg != deposit_data["net_weight_kg"]
            or retry_batch.summary_hash != summary_hash
        ):
            raise ValidationError("A carga mudou desde a tentativa anterior. O lote requer conferência.")
    _preflight_create_call(
        batches_contract=batches_contract,
        deposit_id=deposit_id,
        deadline=deadline,
        sender=sender,
    )
    transaction_data = batches_contract.functions.createIssuanceBatch(
        [deposit_id], deadline
    )._encode_transaction_data().lower()
    if not HEX_BYTES_RE.fullmatch(transaction_data):
        raise ValidationError("O servidor não conseguiu codificar a chamada do lote.")

    try:
        with db_transaction.atomic():
            locked_receipt = Receipt.objects.select_for_update().get(pk=receipt.pk)
            if retry_batch is not None:
                batch = IssuanceBatch.objects.select_for_update().get(pk=retry_batch.pk)
                if batch.status != IssuanceBatch.Status.CREATION_PREPARED or _active_attempt(
                    batch, IssuanceBatchAttempt.Kind.CREATE
                ) is not None:
                    raise ValidationError("Outra solicitação já retomou a criação deste lote.")
            else:
                if _unresolved_batch_row(locked_receipt) is not None:
                    raise ValidationError("Outra solicitação já reservou este recebimento para um lote.")
                batch = IssuanceBatch(
                    company=receipt.company,
                    participant=receipt.participant,
                    unit=receipt.unit,
                    network=operation.network,
                    soja_coin=operation.soja_coin,
                    unit_registry=operation.unit_registry,
                    batches_contract=batch_contract_instance,
                    created_by=user,
                    creator_address=sender,
                    total_net_weight_kg=deposit_data["net_weight_kg"],
                    approval_deadline=deadline,
                    summary_hash=summary_hash,
                    status=IssuanceBatch.Status.CREATION_PREPARED,
                )
                batch.save()
                IssuanceBatchReceipt(
                    batch=batch,
                    receipt=receipt,
                    deposit_id=deposit_id,
                    unit_chain_id=deposit_data["unit_id"],
                    crop_year_bytes32=deposit_data["crop_year"],
                    invoice_key_hash=deposit_data["invoice_key_hash"],
                    weighing_ticket_hash=deposit_data["weighing_ticket_hash"],
                    net_weight_kg=deposit_data["net_weight_kg"],
                ).save()
            transaction_record = BlockchainTransaction(
                company=receipt.company,
                network=operation.network,
                contract=batch_contract_instance,
                operation_type=ISSUANCE_BATCH_OPERATION_TYPE,
                sender_address=sender,
                status=BlockchainTransaction.Status.PREPARED,
            )
            transaction_record.save()
            attempt = IssuanceBatchAttempt(
                batch=batch,
                transaction=transaction_record,
                requested_by=user,
                kind=IssuanceBatchAttempt.Kind.CREATE,
                sender_address=sender,
                prepared_data=transaction_data,
                expected_arguments={
                    "deposit_ids": [deposit_id],
                    "deadline": deadline,
                    "participant": receipt.participant.wallet_address_normalized,
                    "total_net_weight_kg": deposit_data["net_weight_kg"],
                    "summary_hash": summary_hash,
                },
            )
            attempt.save()
    except IntegrityError as error:
        # The partial unique receipt reservation protects concurrent tabs/processes.
        existing = _active_batch_row(receipt)
        if existing is None:
            raise ValidationError("Não foi possível reservar esta carga para o lote.") from error
        existing_attempt = _active_attempt(existing.batch, IssuanceBatchAttempt.Kind.CREATE)
        if existing_attempt is None or existing_attempt.sender_address != sender:
            raise ValidationError("Outra solicitação já iniciou um lote para esta carga.") from error
        return _prepared_action(batch=existing.batch, attempt=existing_attempt)
    return PreparedBatchAction(
        batch=batch,
        attempt=attempt,
        status="ready",
        chain_id=operation.network.chain_id,
        contract_address=batch_contract_instance.address_normalized,
        transaction_data=transaction_data,
    )


def create_approval_link(
    *, user: AbstractBaseUser, company_id: int, batch_id: int
) -> tuple[IssuanceBatchApprovalLink, str]:
    """Create a seven-day participant link after the batch is confirmed."""

    batch = _load_batch(company_id=company_id, batch_id=batch_id)
    require_issuance_access(user, company_id, batch.unit_id)
    if batch.status != IssuanceBatch.Status.AWAITING_APPROVAL:
        raise ValidationError("O link só pode ser criado enquanto o lote aguarda aprovação.")
    creation_confirmed = batch.attempts.filter(
        kind=IssuanceBatchAttempt.Kind.CREATE,
        transaction__status=BlockchainTransaction.Status.CONFIRMED,
    ).exists()
    if not creation_confirmed or batch.chain_batch_id is None:
        raise ValidationError("A criação do lote ainda não foi confirmada na rede.")
    now = timezone.now()
    # Link validity is independent of the contract deadline: after that deadline,
    # the participant can still inspect the batch and prepare its cancellation.
    expires_at = now + LINK_LIFETIME

    # Return the bearer secret exactly once; only its irreversible lookup digest
    # is stored, so the database cannot recreate a lost participant URL.
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with db_transaction.atomic():
        locked_batch = IssuanceBatch.objects.select_for_update().get(pk=batch.pk)
        if locked_batch.status != IssuanceBatch.Status.AWAITING_APPROVAL:
            raise ValidationError("O lote já tem uma aprovação ou cancelamento em andamento.")
        if _has_open_participant_attempt(locked_batch):
            raise ValidationError("Resolva a tentativa aberta do participante antes de gerar outro link.")
        for previous_link in locked_batch.approval_links.filter(
            used_at__isnull=True,
            canceled_at__isnull=True,
        ):
            previous_link.canceled_at = now
            previous_link.save(update_fields=("canceled_at", "updated_at"))
        link = IssuanceBatchApprovalLink(
            batch=locked_batch,
            token_hash=token_hash,
            expires_at=expires_at,
            created_by=user,
        )
        link.save()
    return link, raw_token


def cancel_approval_link(
    *, user: AbstractBaseUser, company_id: int, batch_id: int, link_id: int
) -> IssuanceBatchApprovalLink:
    """Cancel an unused participant link while preserving its access record."""

    batch = _load_batch(company_id=company_id, batch_id=batch_id)
    require_issuance_access(user, company_id, batch.unit_id)
    with db_transaction.atomic():
        locked_batch = IssuanceBatch.objects.select_for_update().get(pk=batch.pk)
        if locked_batch.status != IssuanceBatch.Status.AWAITING_APPROVAL:
            raise ValidationError("O link só pode ser cancelado enquanto o lote aguarda aprovação.")
        if _has_open_participant_attempt(locked_batch):
            raise ValidationError("Não cancele o link enquanto uma transação do participante estiver sem resultado.")
        try:
            link = IssuanceBatchApprovalLink.objects.select_for_update().get(
                pk=link_id,
                batch=locked_batch,
            )
        except IssuanceBatchApprovalLink.DoesNotExist as error:
            raise ValidationError("O link não pertence a este lote.") from error
        if link.used_at is not None:
            raise ValidationError("Um link já usado não pode ser cancelado.")
        if link.canceled_at is None:
            link.canceled_at = timezone.now()
            link.save(update_fields=("canceled_at", "updated_at"))
    return link


def get_approval_link(
    token: str,
    *,
    allow_completed: bool = False,
    allow_in_progress: bool = False,
    allow_cancel: bool = False,
) -> IssuanceBatchApprovalLink:
    """Resolve a bearer code by its SHA-256 digest and return only its one batch."""

    if not isinstance(token, str) or len(token) < 32 or len(token) > 128:
        raise ValidationError("Este link de aprovação é inválido ou expirou.")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        link = IssuanceBatchApprovalLink.objects.select_related(
            "batch__company",
            "batch__participant",
            "batch__unit",
            "batch__network",
            "batch__soja_coin",
            "batch__unit_registry",
            "batch__batches_contract",
        ).prefetch_related("batch__receipt_rows__receipt__documents").get(token_hash=token_hash)
    except IssuanceBatchApprovalLink.DoesNotExist as error:
        raise ValidationError("Este link de aprovação é inválido ou expirou.") from error
    read_completed = (
        allow_completed
        and link.used_at is not None
        and link.batch.status in {
            IssuanceBatch.Status.APPROVED,
            IssuanceBatch.Status.COMPLETED,
            IssuanceBatch.Status.NEEDS_REVIEW,
        }
        and link.expires_at > timezone.now()
    )
    in_progress = (
        allow_in_progress
        and link.batch.status == IssuanceBatch.Status.APPROVAL_SUBMITTED
        and link.used_at is None
        and link.canceled_at is None
    )
    cancel_lifecycle = (
        allow_cancel
        and link.batch.status in {
            IssuanceBatch.Status.AWAITING_APPROVAL,
            IssuanceBatch.Status.CANCEL_SUBMITTED,
            IssuanceBatch.Status.CANCELED,
            IssuanceBatch.Status.NEEDS_REVIEW,
        }
        and link.used_at is None
        and link.expires_at > timezone.now()
        and (
            link.canceled_at is None
            or link.batch.status in {IssuanceBatch.Status.CANCELED, IssuanceBatch.Status.NEEDS_REVIEW}
        )
    )
    if not link.is_usable and not (read_completed or in_progress or cancel_lifecycle):
        raise ValidationError("Este link foi usado, cancelado ou expirou.")
    if link.batch.status == IssuanceBatch.Status.NEEDS_REVIEW and not allow_completed:
        raise ValidationError("Este lote precisa de conferência antes de qualquer aprovação.")
    return link


def _get_batch_chain(
    batch: IssuanceBatch,
) -> tuple[Web3, Contract, Contract, Contract]:
    """Connect to the saved network and bind the saved SojaCoin and batch module."""

    if not batch.network.active or not batch.batches_contract.active or not batch.soja_coin.active:
        raise ValidationError("A rede ou os contratos deste lote estão inativos.")
    web3 = connect_to_rpc(batch.network)
    for contract_address in (batch.batches_contract.address_normalized, batch.soja_coin.address_normalized):
        if not web3.eth.get_code(Web3.to_checksum_address(contract_address)):
            raise ValidationError("O RPC não encontrou código em um contrato salvo para este lote.")
    batch_contract = bind_contract_to_web3(web3, batch.batches_contract)
    soja_contract = bind_contract_to_web3(web3, batch.soja_coin)
    if normalize_wallet_address(str(batch_contract.functions.sojaCoin().call())) != batch.soja_coin.address_normalized:
        raise ValidationError("O módulo de lotes está ligado a outra SojaCoin.")
    if normalize_wallet_address(str(soja_contract.functions.issuanceBatchModule().call())) != batch.batches_contract.address_normalized:
        raise ValidationError("A SojaCoin não reconhece o módulo de lotes salvo.")
    return web3, batch_contract, soja_contract, bind_contract_to_web3(web3, batch.unit_registry)


def _check_pending_batch(batch: IssuanceBatch, contract: Contract) -> int:
    """Require local and on-chain pending status before participant signing."""

    if batch.chain_batch_id is None or batch.status not in {
        IssuanceBatch.Status.AWAITING_APPROVAL,
        IssuanceBatch.Status.APPROVAL_SUBMITTED,
    }:
        raise ValidationError("O lote não está aguardando aprovação do participante.")
    chain_batch = contract.functions.batches(batch.chain_batch_id).call()
    # Solidity enum value 1 is PendingApproval; 2 is Approved.
    if int(chain_batch[7]) != 1:
        raise ValidationError("O estado atual do lote na blockchain não permite assinatura.")
    if (
        normalize_wallet_address(str(chain_batch[0])) != batch.participant.wallet_address_normalized
        or normalize_wallet_address(str(chain_batch[1])) != batch.creator_address
        or int(chain_batch[2]) != batch.total_net_weight_kg
        or int(chain_batch[3]) != batch.receipt_rows.count()
        or int(chain_batch[5]) != batch.approval_deadline
        or normalize_bytes32_hash(Web3.to_hex(chain_batch[6])) != batch.summary_hash
    ):
        raise ValidationError("O lote salvo no SaaS diverge do estado atual na blockchain.")
    # This participant-wide on-chain nonce prevents replaying an older approval.
    return int(contract.functions.nonces(Web3.to_checksum_address(batch.participant.wallet_address_normalized)).call())


def _build_typed_data(
    *, batch: IssuanceBatch, nonce: int
) -> dict[str, Any]:
    """Build the Solidity-compatible EIP-712 participant authorization."""

    return {
        "domain": {
            **EIP712_DOMAIN,
            "chainId": batch.network.chain_id,
            "verifyingContract": Web3.to_checksum_address(batch.batches_contract.address_normalized),
        },
        "primaryType": "IssuanceBatchAuthorization",
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "IssuanceBatchAuthorization": [
                {"name": "batchId", "type": "uint256"},
                {"name": "producer", "type": "address"},
                {"name": "summaryHash", "type": "bytes32"},
                {"name": "totalNetWeightKg", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        "message": {
            "batchId": batch.chain_batch_id,
            "producer": Web3.to_checksum_address(batch.participant.wallet_address_normalized),
            "summaryHash": batch.summary_hash,
            "totalNetWeightKg": batch.total_net_weight_kg,
            "nonce": nonce,
            "deadline": batch.approval_deadline,
        },
    }


def _typed_data_digest(typed_data: dict[str, Any]) -> tuple[SignableMessage, str]:
    """Return the signable payload and final EIP-712 digest for one message."""

    try:
        signable = encode_typed_data(full_message=typed_data)
    except (TypeError, ValueError) as error:
        raise ValidationError("Não foi possível montar a autorização EIP-712 do lote.") from error
    digest = Web3.to_hex(
        Web3.keccak(b"\x19" + bytes(signable.version) + signable.header + signable.body)
    ).lower()
    return signable, digest


def _require_individual_wallet(web3: Web3, wallet_address: str) -> None:
    """Reject contract wallets until the SaaS supports ERC-1271 signature checks."""

    if web3.eth.get_code(Web3.to_checksum_address(wallet_address)):
        raise ValidationError("Esta etapa ainda não aceita carteira de contrato; use uma conta individual EOA.")


def create_approval_challenge(
    *, token: str, wallet_address: str, wallet_chain_id: int
) -> BatchChallengeResult:
    """Create a single-use EIP-712 challenge after checking the live batch and producer."""

    link = get_approval_link(token)
    batch = link.batch
    wallet = normalize_wallet_address(wallet_address)
    if wallet != batch.participant.wallet_address_normalized:
        raise PermissionDenied("Conecte a carteira pública indicada para este participante.")
    if not is_valid_wallet_address(wallet):
        raise ValidationError("Conecte uma carteira EVM válida.")
    if isinstance(wallet_chain_id, bool) or wallet_chain_id != batch.network.chain_id:
        raise ValidationError("A carteira precisa estar na rede salva para este lote.")
    web3, batch_contract, _soja_contract, _registry = _get_batch_chain(batch)
    _require_individual_wallet(web3, wallet)
    # The nonce and deadline are copied into the exact EIP-712 payload shown to MetaMask.
    nonce = _check_pending_batch(batch, batch_contract)
    if int(web3.eth.get_block("latest")["timestamp"]) >= batch.approval_deadline:
        raise ValidationError("O prazo do lote expirou. A carga continua bloqueada até o cancelamento.")
    typed_data = _build_typed_data(batch=batch, nonce=nonce)
    _signable, local_digest = _typed_data_digest(typed_data)
    chain_digest = normalize_bytes32_hash(
        Web3.to_hex(batch_contract.functions.issuanceBatchAuthorizationHash(batch.chain_batch_id).call())
    )
    if local_digest != chain_digest:
        raise ValidationError("O digest calculado não corresponde ao contrato. Nenhuma aprovação foi preparada.")

    with db_transaction.atomic():
        locked_link = IssuanceBatchApprovalLink.objects.select_for_update().get(pk=link.pk)
        if not locked_link.is_usable:
            raise ValidationError("O link de aprovação deixou de estar ativo.")
        pending_challenges = IssuanceBatchAuthorizationChallenge.objects.select_for_update().filter(
            link=locked_link,
            status=IssuanceBatchAuthorizationChallenge.Status.PENDING,
        )
        # Challenge records use a non-deletable manager that rejects bulk updates;
        # saving each row also enforces its one-way pending-to-invalidated lifecycle.
        for pending_challenge in pending_challenges:
            pending_challenge.status = IssuanceBatchAuthorizationChallenge.Status.INVALIDATED
            pending_challenge.save(update_fields=("status", "updated_at"))
        challenge = IssuanceBatchAuthorizationChallenge(
            link=locked_link,
            chain_nonce=nonce,
            deadline=batch.approval_deadline,
            digest=local_digest,
            typed_data=typed_data,
        )
        challenge.save()
    return BatchChallengeResult(challenge=challenge, typed_data=typed_data)


def _recover_typed_data_signer(typed_data: dict[str, Any], signature: str) -> str:
    """Recover an EOA signer from the exact EIP-712 message shown in MetaMask."""

    if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
        raise ValidationError("A assinatura não tem o formato EIP-712 esperado.")
    signable, _digest = _typed_data_digest(typed_data)
    try:
        return normalize_wallet_address(Account.recover_message(signable, signature=signature))
    except (BadSignature, EthSignatureValidationError, TypeError, ValueError) as error:
        raise ValidationError("A assinatura EIP-712 não pôde ser validada.") from error


def _validate_signature(
    *,
    batch: IssuanceBatch,
    challenge: IssuanceBatchAuthorizationChallenge,
    signature: str,
    web3: Web3,
    batch_contract: Contract,
) -> str:
    """Recheck nonce, deadline, EIP-712 domain, digest and participant signer."""

    if challenge.status != IssuanceBatchAuthorizationChallenge.Status.PENDING:
        raise ValidationError("Este desafio já foi usado ou invalidado.")
    # A participant's approval is enforced by the chain clock. Checking the
    # latest block here avoids presenting a signature request that will already
    # revert when the Django host clock is behind the network.
    latest_block_timestamp = int(web3.eth.get_block("latest")["timestamp"])
    if latest_block_timestamp >= challenge.deadline:
        challenge.status = IssuanceBatchAuthorizationChallenge.Status.INVALIDATED
        challenge.save(update_fields=("status", "updated_at"))
        raise ValidationError("A assinatura expirou. Gere uma nova solicitação.")
    nonce = _check_pending_batch(batch, batch_contract)
    if nonce != challenge.chain_nonce or challenge.deadline != batch.approval_deadline:
        challenge.status = IssuanceBatchAuthorizationChallenge.Status.INVALIDATED
        challenge.save(update_fields=("status", "updated_at"))
        raise ValidationError("Nonce ou prazo mudou. Gere uma nova assinatura para o estado atual.")
    expected_data = _build_typed_data(batch=batch, nonce=nonce)
    _signable, expected_digest = _typed_data_digest(expected_data)
    if challenge.typed_data != expected_data or challenge.digest != expected_digest:
        challenge.status = IssuanceBatchAuthorizationChallenge.Status.INVALIDATED
        challenge.save(update_fields=("status", "updated_at"))
        raise ValidationError("O conteúdo do desafio não corresponde ao lote atual.")
    chain_digest = normalize_bytes32_hash(
        Web3.to_hex(batch_contract.functions.issuanceBatchAuthorizationHash(batch.chain_batch_id).call())
    )
    if chain_digest != expected_digest:
        raise ValidationError("O digest do contrato mudou. Atualize a página e assine novamente.")
    wallet = batch.participant.wallet_address_normalized
    _require_individual_wallet(web3, wallet)
    recovered = _recover_typed_data_signer(challenge.typed_data, signature)
    if recovered != wallet:
        raise PermissionDenied("A assinatura foi feita por uma carteira diferente da do participante.")
    return signature.lower()


def prepare_participant_approval(
    *,
    token: str,
    challenge_id: int,
    signature: str,
    wallet_address: str,
    wallet_chain_id: int,
) -> PreparedBatchAction:
    """Verify the participant's signature and persist the approval call before send."""

    link = get_approval_link(token)
    batch = link.batch
    wallet = normalize_wallet_address(wallet_address)
    if wallet != batch.participant.wallet_address_normalized:
        raise PermissionDenied("A carteira conectada não corresponde ao participante do lote.")
    if wallet_chain_id != batch.network.chain_id:
        raise ValidationError("A carteira está em outra rede.")
    if len(signature) > MAX_SIGNATURE_LENGTH:
        raise ValidationError("A assinatura excede o tamanho permitido.")
    challenge = IssuanceBatchAuthorizationChallenge.objects.get(
        pk=challenge_id,
        link=link,
    )
    if batch.status == IssuanceBatch.Status.APPROVAL_SUBMITTED:
        existing = _active_attempt(batch, IssuanceBatchAttempt.Kind.APPROVE)
        if existing is not None:
            if existing.challenge_id != challenge.pk or existing.signature != signature.lower():
                raise ValidationError("Já existe uma tentativa de aprovação aguardando resposta da carteira.")
            return _prepared_action(batch=batch, attempt=existing)
    web3, batch_contract, _soja, _registry = _get_batch_chain(batch)
    signed_signature = _validate_signature(
        batch=batch,
        challenge=challenge,
        signature=signature,
        web3=web3,
        batch_contract=batch_contract,
    )
    transaction_data = batch_contract.functions.approveIssuanceBatch(
        batch.chain_batch_id,
        bytes.fromhex(signed_signature[2:]),
    )._encode_transaction_data().lower()
    expected = {
        "batch_id": batch.chain_batch_id,
        "producer": wallet,
        "summary_hash": batch.summary_hash,
        "total_net_weight_kg": batch.total_net_weight_kg,
        "nonce": challenge.chain_nonce,
        "deadline": challenge.deadline,
    }
    try:
        with db_transaction.atomic():
            locked_batch = IssuanceBatch.objects.select_for_update().get(pk=batch.pk)
            if locked_batch.status != IssuanceBatch.Status.AWAITING_APPROVAL:
                raise ValidationError("O lote deixou de aguardar aprovação.")
            current_challenge = IssuanceBatchAuthorizationChallenge.objects.select_for_update().get(
                pk=challenge.pk
            )
            signature = _validate_signature(
                batch=locked_batch,
                challenge=current_challenge,
                signature=signed_signature,
                web3=web3,
                batch_contract=batch_contract,
            )
            tx_record = BlockchainTransaction(
                company=locked_batch.company,
                network=locked_batch.network,
                contract=locked_batch.batches_contract,
                operation_type=ISSUANCE_BATCH_OPERATION_TYPE,
                sender_address=wallet,
                status=BlockchainTransaction.Status.PREPARED,
            )
            tx_record.save()
            attempt = IssuanceBatchAttempt(
                batch=locked_batch,
                transaction=tx_record,
                requested_by=None,
                link=link,
                challenge=current_challenge,
                kind=IssuanceBatchAttempt.Kind.APPROVE,
                sender_address=wallet,
                prepared_data=transaction_data,
                expected_arguments=expected,
                signature=signature,
            )
            attempt.save()
            current_challenge.signature = signature
            current_challenge.signed_at = timezone.now()
            current_challenge.status = IssuanceBatchAuthorizationChallenge.Status.SIGNED
            current_challenge.save(update_fields=("signature", "signed_at", "status", "updated_at"))
    except IntegrityError as error:
        existing = _active_attempt(batch, IssuanceBatchAttempt.Kind.APPROVE)
        if existing is None:
            raise ValidationError("Não foi possível reservar a aprovação deste lote.") from error
        return _prepared_action(batch=batch, attempt=existing)
    return PreparedBatchAction(
        batch=batch,
        attempt=attempt,
        status="ready",
        chain_id=batch.network.chain_id,
        contract_address=batch.batches_contract.address_normalized,
        transaction_data=transaction_data,
    )


def prepare_participant_approval_and_issue(
    *,
    token: str,
    wallet_address: str,
    wallet_chain_id: int,
) -> PreparedBatchAction:
    """Persist one participant transaction that atomically approves and issues SJC."""

    link = get_approval_link(token)
    batch = link.batch
    wallet = normalize_wallet_address(wallet_address)
    if not is_valid_wallet_address(wallet):
        raise ValidationError("Conecte uma carteira EVM válida.")
    if wallet != batch.participant.wallet_address_normalized:
        raise PermissionDenied("A carteira conectada não corresponde ao participante do lote.")
    if isinstance(wallet_chain_id, bool) or wallet_chain_id != batch.network.chain_id:
        raise ValidationError("A carteira está em outra rede.")
    if batch.receipt_rows.count() != 1:
        raise ValidationError("A aprovação com emissão automática está disponível para um recebimento por lote.")

    existing = _active_attempt(batch, IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE)
    if existing is not None:
        if existing.link_id != link.pk or existing.sender_address != wallet:
            raise ValidationError("Já existe uma tentativa aberta para este lote.")
        return _prepared_action(batch=batch, attempt=existing)

    web3, batch_contract, _soja_contract, _registry = _get_batch_chain(batch)
    _check_pending_batch(batch, batch_contract)
    chain_deposit_ids = [
        int(value)
        for value in batch_contract.functions.getBatchDepositIds(batch.chain_batch_id).call()
    ]
    local_deposit_ids = _batch_deposit_ids(batch)
    if chain_deposit_ids != local_deposit_ids or len(chain_deposit_ids) != 1:
        raise ValidationError("O lote na rede não corresponde ao recebimento salvo no SaaS.")
    if int(web3.eth.get_block("latest")["timestamp"]) >= batch.approval_deadline:
        raise ValidationError("O prazo deste lote expirou.")

    transaction_function = batch_contract.functions.approveAndIssueBatch(batch.chain_batch_id)
    try:
        # Simulate all approval, audit, inventory, permission, and CPR checks first.
        transaction_function.call({"from": Web3.to_checksum_address(wallet)})
    except (Web3Exception, ValueError) as error:
        raise ValidationError(
            "A rede recusou a simulação. Confira o prazo, a auditoria, o estoque e a permissão de emissão do lote."
        ) from error
    transaction_data = transaction_function._encode_transaction_data().lower()
    expected = {
        "batch_id": batch.chain_batch_id,
        "producer": wallet,
        "created_by": batch.creator_address,
        "deposit_id": local_deposit_ids[0],
        "amount_deposits": 1,
        "total_net_weight_kg": batch.total_net_weight_kg,
        "deadline": batch.approval_deadline,
        "summary_hash": batch.summary_hash,
    }

    try:
        with db_transaction.atomic():
            locked_batch = IssuanceBatch.objects.select_for_update().get(pk=batch.pk)
            locked_link = IssuanceBatchApprovalLink.objects.select_for_update().get(pk=link.pk)
            if locked_batch.status != IssuanceBatch.Status.AWAITING_APPROVAL:
                raise ValidationError("O lote deixou de aguardar aprovação.")
            if not locked_link.is_usable:
                raise ValidationError("O link de aprovação deixou de estar ativo.")
            active_attempt = _active_attempt(locked_batch, IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE)
            if active_attempt is not None:
                if active_attempt.link_id != locked_link.pk or active_attempt.sender_address != wallet:
                    raise ValidationError("Já existe uma tentativa aberta para este lote.")
                return _prepared_action(batch=locked_batch, attempt=active_attempt)

            tx_record = BlockchainTransaction(
                company=locked_batch.company,
                network=locked_batch.network,
                contract=locked_batch.batches_contract,
                operation_type=ISSUANCE_BATCH_OPERATION_TYPE,
                sender_address=wallet,
                status=BlockchainTransaction.Status.PREPARED,
            )
            tx_record.save()
            attempt = IssuanceBatchAttempt(
                batch=locked_batch,
                transaction=tx_record,
                link=locked_link,
                kind=IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE,
                sender_address=wallet,
                prepared_data=transaction_data,
                expected_arguments=expected,
            )
            attempt.save()
    except IntegrityError as error:
        existing = _active_attempt(batch, IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE)
        if existing is None:
            raise ValidationError("Não foi possível reservar a aprovação deste lote.") from error
        return _prepared_action(batch=batch, attempt=existing)

    return PreparedBatchAction(
        batch=batch,
        attempt=attempt,
        status="ready",
        chain_id=batch.network.chain_id,
        contract_address=batch.batches_contract.address_normalized,
        transaction_data=transaction_data,
    )


def _allowed_cancel_senders(batch: IssuanceBatch, soja_contract: Contract) -> set[str]:
    """Return the creator, participant and current SojaCoin owner accepted by Solidity."""

    owner = normalize_wallet_address(str(soja_contract.functions.owner().call()))
    return {batch.creator_address, batch.participant.wallet_address_normalized, owner}


def prepare_batch_cancellation(
    *,
    batch: IssuanceBatch,
    sender_address: str,
    user: AbstractBaseUser | None = None,
    requested_by: AbstractBaseUser | None = None,
    token: str = "",
) -> PreparedBatchAction:
    """Prepare cancellation by a permitted creator, participant or SojaCoin owner."""

    if user is not None:
        require_issuance_access(user, batch.company_id, batch.unit_id)
        sender = _verified_operator_wallet(user=user, sender_address=sender_address)
    else:
        link = get_approval_link(token, allow_cancel=True)
        if link.batch_id != batch.pk:
            raise PermissionDenied("O link não autoriza operações neste lote.")
        sender = normalize_wallet_address(sender_address)
        if sender != batch.participant.wallet_address_normalized:
            raise PermissionDenied("Somente o participante deste link pode cancelar por esta página.")
    web3, batch_contract, soja_contract, _registry = _get_batch_chain(batch)
    if batch.status != IssuanceBatch.Status.AWAITING_APPROVAL or batch.chain_batch_id is None:
        raise ValidationError("Somente um lote pendente de aprovação pode ser cancelado.")
    allowed_senders = _allowed_cancel_senders(batch, soja_contract)
    if sender not in allowed_senders:
        raise PermissionDenied("Esta carteira não está autorizada pelo contrato a cancelar o lote.")
    row_ids = list(batch.receipt_rows.values_list("deposit_id", flat=True))
    if int(batch_contract.functions.batches(batch.chain_batch_id).call()[7]) != 1:
        raise ValidationError("O estado atual do lote na blockchain não permite cancelamento.")
    try:
        batch_contract.functions.cancelIssuanceBatch(batch.chain_batch_id).call(
            {"from": Web3.to_checksum_address(sender)}
        )
    except (Web3Exception, ValueError) as error:
        raise ValidationError("A rede recusou o pré-teste do cancelamento deste lote.") from error
    tx_data = batch_contract.functions.cancelIssuanceBatch(
        batch.chain_batch_id
    )._encode_transaction_data().lower()
    try:
        with db_transaction.atomic():
            locked_batch = IssuanceBatch.objects.select_for_update().get(pk=batch.pk)
            existing = _active_attempt(locked_batch, IssuanceBatchAttempt.Kind.CANCEL)
            if existing is not None:
                if existing.sender_address != sender:
                    raise PermissionDenied("Retome o cancelamento com a mesma carteira que o preparou.")
                return _prepared_action(batch=locked_batch, attempt=existing)
            tx_record = BlockchainTransaction(
                company=locked_batch.company,
                network=locked_batch.network,
                contract=locked_batch.batches_contract,
                operation_type=ISSUANCE_BATCH_OPERATION_TYPE,
                sender_address=sender,
                status=BlockchainTransaction.Status.PREPARED,
            )
            tx_record.save()
            attempt = IssuanceBatchAttempt(
                batch=locked_batch,
                transaction=tx_record,
                requested_by=requested_by,
                link=link if user is None else None,
                kind=IssuanceBatchAttempt.Kind.CANCEL,
                sender_address=sender,
                prepared_data=tx_data,
                expected_arguments={"batch_id": locked_batch.chain_batch_id, "deposit_ids": row_ids},
            )
            attempt.save()
    except IntegrityError as error:
        existing = _active_attempt(batch, IssuanceBatchAttempt.Kind.CANCEL)
        if existing is None:
            raise ValidationError("Não foi possível reservar o cancelamento do lote.") from error
        return _prepared_action(batch=batch, attempt=existing)
    return PreparedBatchAction(
        batch=batch,
        attempt=attempt,
        status="ready",
        chain_id=batch.network.chain_id,
        contract_address=batch.batches_contract.address_normalized,
        transaction_data=tx_data,
    )


def _attempt_authorization(
    attempt: IssuanceBatchAttempt,
    *,
    user: AbstractBaseUser | None = None,
    token: str = "",
) -> None:
    """Recheck the current actor and SaaS scope before a transaction lifecycle action."""

    batch = attempt.batch
    if attempt.kind in {
        IssuanceBatchAttempt.Kind.APPROVE,
        IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE,
    }:
        link = get_approval_link(
            token,
            allow_completed=True,
            allow_in_progress=True,
        )
        if attempt.link_id != link.pk or link.batch_id != batch.pk:
            raise PermissionDenied("Esta tentativa não pertence ao link de aprovação aberto.")
        if attempt.sender_address != batch.participant.wallet_address_normalized:
            raise PermissionDenied("Esta tentativa não foi preparada para a carteira do participante.")
        return
    if user is not None:
        require_issuance_access(user, batch.company_id, batch.unit_id)
        _verified_operator_wallet(user=user, sender_address=attempt.sender_address)
        return
    link = get_approval_link(
        token,
        allow_completed=True,
        allow_in_progress=True,
        allow_cancel=attempt.kind == IssuanceBatchAttempt.Kind.CANCEL,
    )
    if (
        attempt.kind != IssuanceBatchAttempt.Kind.CANCEL
        or attempt.link_id != link.pk
        or link.batch_id != batch.pk
        or attempt.sender_address != batch.participant.wallet_address_normalized
    ):
        raise PermissionDenied("O link não autoriza esta tentativa.")


def mark_batch_action_started(
    *,
    attempt: IssuanceBatchAttempt,
    sender_address: str,
    user: AbstractBaseUser | None = None,
    token: str = "",
) -> IssuanceBatchAttempt:
    """Save the wallet-prompt timestamp before the browser asks MetaMask to send."""

    _attempt_authorization(attempt, user=user, token=token)
    if normalize_wallet_address(sender_address) != attempt.sender_address:
        raise PermissionDenied("A carteira conectada mudou desde a preparação desta chamada.")
    if not attempt.is_open or attempt.transaction.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Esta tentativa não pode mais abrir a carteira.")
    if attempt.submission_started_at is None:
        attempt.submission_started_at = timezone.now()
        attempt.save(update_fields=("submission_started_at", "updated_at"))
    return attempt


def reject_batch_action(
    *,
    attempt: IssuanceBatchAttempt,
    user: AbstractBaseUser | None = None,
    token: str = "",
) -> IssuanceBatchAttempt:
    """Close a prepared transaction only after the wallet explicitly rejects it."""

    _attempt_authorization(attempt, user=user, token=token)
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("Esta tentativa não está aguardando resposta da carteira.")
    if attempt.transaction.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Uma transação já enviada não pode ser marcada como recusada.")
    fail_transaction(attempt.transaction, "A carteira recusou explicitamente a solicitação.")
    attempt.refresh_from_db()
    attempt.is_open = False
    attempt.save(update_fields=("is_open", "updated_at"))
    _restore_batch_after_revert(attempt)
    return attempt


def _verify_transaction(
    attempt: IssuanceBatchAttempt,
    transaction_hash: str,
) -> bool:
    """Compare the RPC transaction against the saved network, sender, target and calldata."""

    transaction_record = attempt.transaction
    web3 = connect_to_rpc(transaction_record.network)
    try:
        raw_transaction = web3.eth.get_transaction(transaction_hash)
    except TransactionNotFound:
        return False
    except Web3Exception as error:
        raise ConnectionError("Não foi possível consultar esta transação no RPC.") from error
    actual_hash = normalize_transaction_hash(Web3.to_hex(raw_transaction["hash"]))
    actual_sender = normalize_wallet_address(str(raw_transaction.get("from", "")))
    actual_to = normalize_wallet_address(str(raw_transaction.get("to") or ""))
    actual_data = raw_transaction.get("input", raw_transaction.get("data"))
    if actual_hash != transaction_hash:
        raise ValidationError("O RPC retornou outra hash de transação.")
    if actual_sender != attempt.sender_address:
        raise ValidationError("A transação foi enviada por outra carteira.")
    if actual_to != attempt.batch.batches_contract.address_normalized:
        raise ValidationError("A transação chama outro contrato TokenIssuanceBatches.")
    if raw_transaction.get("chainId") is None or int(raw_transaction["chainId"]) != transaction_record.network.chain_id:
        raise ValidationError("A transação pertence a outra rede.")
    if int(raw_transaction.get("value", 0)) != 0:
        raise ValidationError("A chamada do lote não pode transferir saldo nativo.")
    if isinstance(actual_data, (bytes, bytearray)):
        normalized_data = Web3.to_hex(actual_data).lower()
    elif isinstance(actual_data, str):
        normalized_data = actual_data.lower()
    else:
        raise ValidationError("O RPC retornou calldata inválida.")
    if not HEX_BYTES_RE.fullmatch(normalized_data) or normalized_data != attempt.prepared_data:
        raise ValidationError("Os dados enviados diferem da chamada salva no SaaS.")
    return True


def register_batch_transaction_hash(
    *,
    attempt: IssuanceBatchAttempt,
    transaction_hash: str,
    user: AbstractBaseUser | None = None,
    token: str = "",
) -> BlockchainTransaction | None:
    """Attach a hash only after the RPC proves it is the exact prepared call."""

    _attempt_authorization(attempt, user=user, token=token)
    canonical_hash = normalize_transaction_hash(transaction_hash)
    if not is_valid_transaction_hash(canonical_hash):
        raise ValidationError("Informe uma hash de transação EVM válida.")
    record = attempt.transaction
    if record.transaction_hash:
        if record.transaction_hash == canonical_hash:
            return record
        raise ValidationError("Esta tentativa já está associada a outra hash.")
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("A tentativa não está aguardando uma transação da carteira.")
    if record.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Somente uma tentativa preparada pode receber uma hash.")
    if not _verify_transaction(attempt, canonical_hash):
        return None
    submitted = submit_transaction(record, canonical_hash)
    batch = attempt.batch
    expected_status = {
        IssuanceBatchAttempt.Kind.CREATE: IssuanceBatch.Status.CREATION_SUBMITTED,
        IssuanceBatchAttempt.Kind.APPROVE: IssuanceBatch.Status.APPROVAL_SUBMITTED,
        IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE: IssuanceBatch.Status.APPROVAL_SUBMITTED,
        IssuanceBatchAttempt.Kind.CANCEL: IssuanceBatch.Status.CANCEL_SUBMITTED,
    }[attempt.kind]
    if batch.status != expected_status:
        batch.status = expected_status
        batch.save(update_fields=("status", "updated_at"))
    return submitted


def _event_address(event: DecodedContractEvent, key: str) -> str:
    """Normalize one indexed or non-indexed event address."""

    try:
        return normalize_wallet_address(str(event.args[key]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError(f"O evento {event.event_name} não contém a carteira {key} esperada.") from error


def _event_hash(event: DecodedContractEvent, key: str) -> str:
    """Normalize one event bytes32 value."""

    try:
        return normalize_bytes32_hash(Web3.to_hex(event.args[key]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError(f"O evento {event.event_name} não contém a hash {key} esperada.") from error


def _event_integer(event: DecodedContractEvent, key: str) -> int:
    """Read one event integer while rejecting absent or malformed values."""

    try:
        return int(event.args[key])
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError(f"O evento {event.event_name} não contém o inteiro {key} esperado.") from error


def _batch_deposit_ids(batch: IssuanceBatch) -> list[int]:
    """Return the preserved deposit IDs in the same order sent to Solidity."""

    return list(batch.receipt_rows.order_by("pk").values_list("deposit_id", flat=True))


def _validate_created_event_state(
    *,
    attempt: IssuanceBatchAttempt,
    event: DecodedContractEvent,
    batch_contract: Contract,
    soja_contract: Contract,
) -> None:
    """Validate BatchCreated fields and storage as they existed at its block."""

    batch = attempt.batch
    expected = attempt.expected_arguments
    batch_id = _event_integer(event, "batchId")
    deposit_ids = [int(value) for value in expected["deposit_ids"]]
    if batch_id < 1:
        raise ValidationError("O evento BatchCreated retornou um ID de lote inválido.")
    if (
        _event_address(event, "producer") != batch.participant.wallet_address_normalized
        or _event_address(event, "createdBy") != attempt.sender_address
        or _event_integer(event, "amountDeposits") != len(deposit_ids)
        or _event_integer(event, "totalNetWeightKg") != int(expected["total_net_weight_kg"])
        or _event_hash(event, "summaryHash") != expected["summary_hash"]
        or _event_integer(event, "deadline") != int(expected["deadline"])
    ):
        raise ValidationError("O evento BatchCreated diverge do lote preparado pelo SaaS.")

    state = batch_contract.functions.batches(batch_id).call(block_identifier=event.block_number)
    chain_deposit_ids = [
        int(deposit_id)
        for deposit_id in batch_contract.functions.getBatchDepositIds(batch_id).call(
            block_identifier=event.block_number
        )
    ]
    row = batch.receipt_rows.get()
    deposit = _read_chain_deposit(
        soja_contract=soja_contract,
        deposit_id=row.deposit_id,
        block_identifier=event.block_number,
    )
    if (
        normalize_wallet_address(str(state[0])) != batch.participant.wallet_address_normalized
        or normalize_wallet_address(str(state[1])) != attempt.sender_address
        or int(state[2]) != batch.total_net_weight_kg
        or int(state[3]) != len(deposit_ids)
        or int(state[4]) != 0
        or int(state[5]) != batch.approval_deadline
        or normalize_bytes32_hash(Web3.to_hex(state[6])) != batch.summary_hash
        or int(state[7]) != 1
        or chain_deposit_ids != deposit_ids
        or int(batch_contract.functions.batchByDeposit(row.deposit_id).call(block_identifier=event.block_number))
        != batch_id
        or normalize_wallet_address(str(deposit[0])) != batch.participant.wallet_address_normalized
        or normalize_bytes32_hash(Web3.to_hex(deposit[1])) != row.invoice_key_hash
        or int(deposit[2]) != row.unit_chain_id
        or normalize_bytes32_hash(Web3.to_hex(deposit[3])) != row.crop_year_bytes32
        or not bool(deposit[6])
        or bool(deposit[7])
        or bool(deposit[8])
        or bool(deposit[9])
        or int(deposit[11]) != row.net_weight_kg
        or int(deposit[12]) != 0
        or normalize_bytes32_hash(Web3.to_hex(deposit[13])) != row.weighing_ticket_hash
    ):
        raise ValidationError("O evento BatchCreated não corresponde ao estado das cargas no bloco.")

    # The chain assigns this ID; it is intentionally read from the emitted event.
    batch.chain_batch_id = batch_id
    batch.status = IssuanceBatch.Status.AWAITING_APPROVAL
    batch.save(update_fields=("chain_batch_id", "status", "updated_at"))


def _validate_approved_event_state(
    *,
    attempt: IssuanceBatchAttempt,
    event: DecodedContractEvent,
    batch_contract: Contract,
) -> None:
    """Require the producer, recorded signer, summary and approved storage to match."""

    batch = attempt.batch
    expected = attempt.expected_arguments
    if (
        _event_integer(event, "batchId") != batch.chain_batch_id
        or _event_address(event, "producer") != batch.participant.wallet_address_normalized
        or _event_address(event, "recordedBy") != attempt.sender_address
        or _event_hash(event, "summaryHash") != batch.summary_hash
    ):
        raise ValidationError("O evento BatchApproved diverge da assinatura e do lote esperados.")
    state = batch_contract.functions.batches(batch.chain_batch_id).call(
        block_identifier=event.block_number
    )
    deposit_ids = _batch_deposit_ids(batch)
    if (
        normalize_wallet_address(str(state[0])) != expected["producer"]
        or normalize_wallet_address(str(state[1])) != batch.creator_address
        or int(state[2]) != expected["total_net_weight_kg"]
        or int(state[3]) != len(deposit_ids)
        or int(state[4]) != 0
        or int(state[5]) != expected["deadline"]
        or normalize_bytes32_hash(Web3.to_hex(state[6])) != expected["summary_hash"]
        or int(state[7]) != 2
        or [int(value) for value in batch_contract.functions.getBatchDepositIds(
            batch.chain_batch_id
        ).call(block_identifier=event.block_number)] != deposit_ids
        or any(
            not bool(batch_contract.functions.isDepositInApprovedBatch(deposit_id).call(
                block_identifier=event.block_number
            ))
            for deposit_id in deposit_ids
        )
    ):
        raise ValidationError("O lote não aparece aprovado no estado da blockchain deste bloco.")

    batch.status = IssuanceBatch.Status.APPROVED
    batch.save(update_fields=("status", "updated_at"))
    link = attempt.link
    if link is not None and link.used_at is None:
        link.used_at = timezone.now()
        link.save(update_fields=("used_at", "updated_at"))


def _validate_canceled_event_state(
    *,
    attempt: IssuanceBatchAttempt,
    event: DecodedContractEvent,
    batch_contract: Contract,
) -> None:
    """Require the canceling wallet, canceled status and released deposits to agree."""

    batch = attempt.batch
    if (
        _event_integer(event, "batchId") != batch.chain_batch_id
        or _event_address(event, "canceledBy") != attempt.sender_address
    ):
        raise ValidationError("O evento BatchCanceled não corresponde à carteira autorizada.")
    state = batch_contract.functions.batches(batch.chain_batch_id).call(
        block_identifier=event.block_number
    )
    deposit_ids = _batch_deposit_ids(batch)
    on_chain_ids = [
        int(value)
        for value in batch_contract.functions.getBatchDepositIds(batch.chain_batch_id).call(
            block_identifier=event.block_number
        )
    ]
    if (
        int(state[7]) != 4
        or on_chain_ids != deposit_ids
        or any(
            int(batch_contract.functions.batchByDeposit(deposit_id).call(
                block_identifier=event.block_number
            )) != 0
            for deposit_id in deposit_ids
        )
    ):
        raise ValidationError("O lote não foi cancelado ou suas cargas ainda não foram liberadas.")

    batch.status = IssuanceBatch.Status.CANCELED
    batch.save(update_fields=("status", "updated_at"))
    for row in batch.receipt_rows.filter(is_current=True):
        row.is_current = False
        row.save(update_fields=("is_current", "updated_at"))
    for link in batch.approval_links.filter(used_at__isnull=True, canceled_at__isnull=True):
        link.canceled_at = timezone.now()
        link.save(update_fields=("canceled_at", "updated_at"))


def _validate_automatic_issuance_events(
    *,
    attempt: IssuanceBatchAttempt,
    token_event: DecodedContractEvent,
    observed_events: list[DecodedContractEvent],
    batch_contract: Contract,
    soja_contract: Contract,
) -> None:
    """Match both contract event streams and final storage before marking issuance complete."""

    batch = attempt.batch
    expected = attempt.expected_arguments
    batch_address = batch.batches_contract.address_normalized
    token_address = batch.soja_coin.address_normalized
    batch_events = [
        event
        for event in observed_events
        if event.contract_instance is not None
        and event.contract_instance.address_normalized == batch_address
    ]
    token_events = [
        event
        for event in observed_events
        if event.contract_instance is not None
        and event.contract_instance.address_normalized == token_address
    ]

    def exactly_one(events: list[DecodedContractEvent], name: str) -> DecodedContractEvent:
        matches = [event for event in events if event.event_name == name]
        if len(matches) != 1:
            raise ValidationError(f"A transação precisa conter exatamente um evento {name}.")
        return matches[0]

    approved = exactly_one(batch_events, "BatchApproved")
    recorded = exactly_one(batch_events, "BatchIssuanceRecorded")
    completed = exactly_one(batch_events, "BatchCompleted")
    issued = exactly_one(token_events, "TokensIssued")
    if token_event.log_index != issued.log_index:
        raise ValidationError("O evento TokensIssued usado na confirmação não corresponde ao recebimento.")

    batch_id = int(expected["batch_id"])
    deposit_id = int(expected["deposit_id"])
    producer = batch.participant.wallet_address_normalized
    creator = batch.creator_address
    summary_hash = expected["summary_hash"]
    issued_amount = _event_integer(issued, "amount")
    if (
        _event_integer(approved, "batchId") != batch_id
        or _event_address(approved, "producer") != producer
        or _event_address(approved, "recordedBy") != producer
        or _event_hash(approved, "summaryHash") != summary_hash
        or _event_integer(recorded, "batchId") != batch_id
        or _event_integer(recorded, "depositId") != deposit_id
        or _event_integer(recorded, "amountIssued") != 1
        or _event_integer(recorded, "amountDeposits") != 1
        or _event_integer(completed, "batchId") != batch_id
        or _event_address(completed, "producer") != producer
        or _event_integer(completed, "amountDeposits") != 1
        or _event_integer(issued, "depositId") != deposit_id
        or _event_address(issued, "wallet") != producer
        or _event_address(issued, "issuedBy") != creator
        or issued_amount < 1
    ):
        raise ValidationError("Os eventos de aprovação e emissão divergem do lote salvo no SaaS.")

    minted_total = 0
    participant_amount = 0
    zero_address = "0x" + "00" * 20
    for event in token_events:
        if event.event_name != "Transfer":
            continue
        sender = _event_address(event, "from")
        if sender != zero_address:
            continue
        recipient = _event_address(event, "to")
        amount = _event_integer(event, "value")
        minted_total += amount
        if recipient == producer:
            participant_amount += amount
    if minted_total != issued_amount or participant_amount > issued_amount:
        raise ValidationError("Os eventos ERC-20 não correspondem à quantidade de TokensIssued.")

    state = batch_contract.functions.batches(batch_id).call(block_identifier=token_event.block_number)
    chain_deposit_ids = [
        int(value)
        for value in batch_contract.functions.getBatchDepositIds(batch_id).call(
            block_identifier=token_event.block_number
        )
    ]
    row = batch.receipt_rows.get()
    deposit = _read_chain_deposit(
        soja_contract=soja_contract,
        deposit_id=deposit_id,
        block_identifier=token_event.block_number,
    )
    if (
        normalize_wallet_address(str(state[0])) != producer
        or normalize_wallet_address(str(state[1])) != creator
        or int(state[2]) != int(expected["total_net_weight_kg"])
        or int(state[3]) != 1
        or int(state[4]) != 1
        or int(state[5]) != int(expected["deadline"])
        or normalize_bytes32_hash(Web3.to_hex(state[6])) != summary_hash
        or int(state[7]) != 3
        or chain_deposit_ids != [deposit_id]
        or int(batch_contract.functions.batchByDeposit(deposit_id).call(
            block_identifier=token_event.block_number
        )) != batch_id
        or not bool(batch_contract.functions.depositIssued(deposit_id).call(
            block_identifier=token_event.block_number
        ))
        or normalize_wallet_address(str(deposit[0])) != producer
        or int(deposit[5]) != issued_amount
        or not bool(deposit[6])
        or not bool(deposit[7])
        or bool(deposit[8])
        or bool(deposit[9])
        or int(deposit[11]) != row.net_weight_kg
        or int(deposit[12]) != 0
    ):
        raise ValidationError("O estado final do lote ou do recebimento diverge dos eventos confirmados.")

    decimals = int(soja_contract.functions.decimals().call(block_identifier=token_event.block_number))
    if decimals != 6:
        raise ValidationError("A precisão do token mudou; confira a emissão antes de apresentá-la.")
    batch.issued_amount_sjc = issued_amount
    batch.participant_amount_sjc = participant_amount
    batch.token_decimals = decimals
    batch.status = IssuanceBatch.Status.COMPLETED
    batch.save(
        update_fields=(
            "issued_amount_sjc",
            "participant_amount_sjc",
            "token_decimals",
            "status",
            "updated_at",
        )
    )
    if attempt.link_id is not None and attempt.link.used_at is None:
        attempt.link.used_at = timezone.now()
        attempt.link.save(update_fields=("used_at", "updated_at"))


def _make_batch_event_handler(
    *, attempt: IssuanceBatchAttempt, web3: Web3
) -> Callable[[BlockchainTransaction, DecodedContractEvent], None]:
    """Create an event handler that checks one operation and its historical chain state."""

    observed_events: list[DecodedContractEvent] = []

    def handle(_transaction: BlockchainTransaction, event: DecodedContractEvent) -> None:
        """Apply only the action's expected event; unrelated contract events are ignored."""

        observed_events.append(event)
        batch_contract = bind_contract_to_web3(web3, attempt.batch.batches_contract)
        soja_contract = bind_contract_to_web3(web3, attempt.batch.soja_coin)
        expected_event = {
            IssuanceBatchAttempt.Kind.CREATE: "BatchCreated",
            IssuanceBatchAttempt.Kind.APPROVE: "BatchApproved",
            IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE: "TokensIssued",
            IssuanceBatchAttempt.Kind.CANCEL: "BatchCanceled",
        }[attempt.kind]
        if event.event_name != expected_event:
            return
        if attempt.kind == IssuanceBatchAttempt.Kind.CREATE:
            _validate_created_event_state(
                attempt=attempt,
                event=event,
                batch_contract=batch_contract,
                soja_contract=soja_contract,
            )
        elif attempt.kind == IssuanceBatchAttempt.Kind.APPROVE:
            _validate_approved_event_state(
                attempt=attempt,
                event=event,
                batch_contract=batch_contract,
            )
        elif attempt.kind == IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE:
            _validate_automatic_issuance_events(
                attempt=attempt,
                token_event=event,
                observed_events=observed_events,
                batch_contract=batch_contract,
                soja_contract=soja_contract,
            )
        else:
            _validate_canceled_event_state(
                attempt=attempt,
                event=event,
                batch_contract=batch_contract,
            )

    return handle


def _mark_attempt_closed(attempt: IssuanceBatchAttempt) -> None:
    """Close an attempt after its transaction reaches a terminal or review state."""

    attempt.refresh_from_db()
    if attempt.is_open:
        attempt.is_open = False
        attempt.save(update_fields=("is_open", "updated_at"))


def _restore_batch_after_revert(attempt: IssuanceBatchAttempt) -> None:
    """Return to the last usable business state after an on-chain revert."""

    batch = attempt.batch
    restored_status = {
        IssuanceBatchAttempt.Kind.CREATE: IssuanceBatch.Status.CREATION_PREPARED,
        IssuanceBatchAttempt.Kind.APPROVE: IssuanceBatch.Status.AWAITING_APPROVAL,
        IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE: IssuanceBatch.Status.AWAITING_APPROVAL,
        IssuanceBatchAttempt.Kind.CANCEL: IssuanceBatch.Status.AWAITING_APPROVAL,
    }[attempt.kind]
    if batch.status != restored_status and batch.status != IssuanceBatch.Status.NEEDS_REVIEW:
        batch.status = restored_status
        batch.save(update_fields=("status", "updated_at"))


def _mark_batch_for_review(attempt: IssuanceBatchAttempt, reason: str) -> None:
    """Stop all normal progress when a mined event or resulting state is inconsistent."""

    batch = attempt.batch
    if batch.status != IssuanceBatch.Status.NEEDS_REVIEW:
        batch.status = IssuanceBatch.Status.NEEDS_REVIEW
        batch.review_reason = reason.strip()
        batch.save(update_fields=("status", "review_reason", "updated_at"))
    _mark_attempt_closed(attempt)


def confirm_batch_transaction(
    *,
    attempt: IssuanceBatchAttempt,
    user: AbstractBaseUser | None = None,
    token: str = "",
) -> BatchTransactionResult:
    """Poll one transaction and confirm only matching events and batch storage."""

    _attempt_authorization(attempt, user=user, token=token)
    record = attempt.transaction
    batch = attempt.batch
    if record.status == BlockchainTransaction.Status.REORGED:
        return BatchTransactionResult("needs_review", attempt, record)
    if batch.status == IssuanceBatch.Status.NEEDS_REVIEW:
        return BatchTransactionResult("needs_review", attempt, record)
    if record.status == BlockchainTransaction.Status.FAILED:
        return BatchTransactionResult("failed", attempt, record)
    if record.status not in {
        BlockchainTransaction.Status.SUBMITTED,
        BlockchainTransaction.Status.CONFIRMED,
    } or not record.transaction_hash:
        raise ValidationError("Esta tentativa ainda não tem uma transação enviada para consultar.")
    web3 = connect_to_rpc(record.network)
    try:
        if not _verify_transaction(attempt, record.transaction_hash):
            return BatchTransactionResult("pending", attempt, record)
    except ValidationError as error:
        _mark_batch_for_review(attempt, " ".join(error.messages))
        attempt.refresh_from_db()
        batch.refresh_from_db()
        return BatchTransactionResult("needs_review", attempt, record)

    required_event = {
        IssuanceBatchAttempt.Kind.CREATE: ("BatchCreated",),
        IssuanceBatchAttempt.Kind.APPROVE: ("BatchApproved",),
        IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE: (
            "BatchApproved",
            "BatchIssuanceRecorded",
            "BatchCompleted",
            "TokensIssued",
        ),
        IssuanceBatchAttempt.Kind.CANCEL: ("BatchCanceled",),
    }[attempt.kind]
    try:
        with db_transaction.atomic():
            confirmed_record = confirm_transaction_from_chain(
                record.pk,
                event_handler=_make_batch_event_handler(attempt=attempt, web3=web3),
                required_event_names=required_event,
                additional_event_contracts=(batch.soja_coin,)
                if attempt.kind == IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE
                else (),
            )
            if confirmed_record is None:
                return BatchTransactionResult("pending", attempt, record)
            confirmed_record.refresh_from_db()
            attempt.refresh_from_db()
            batch.refresh_from_db()
            if confirmed_record.status == BlockchainTransaction.Status.FAILED:
                _restore_batch_after_revert(attempt)
                _mark_attempt_closed(attempt)
                return BatchTransactionResult("failed", attempt, confirmed_record)
            _mark_attempt_closed(attempt)
            if batch.status == IssuanceBatch.Status.NEEDS_REVIEW:
                return BatchTransactionResult("needs_review", attempt, confirmed_record)
            return BatchTransactionResult("confirmed", attempt, confirmed_record)
    except TransactionNotFound:
        return BatchTransactionResult("pending", attempt, record)
    except ValidationError as error:
        # Required event missing or an event/state mismatch after a successful receipt
        # is a review state. A missing receipt remains pending, not a failed action.
        try:
            chain_receipt = web3.eth.get_transaction_receipt(record.transaction_hash)
        except TransactionNotFound:
            return BatchTransactionResult("pending", attempt, record)
        if int(chain_receipt.get("status", 0)) == 1:
            with db_transaction.atomic():
                _mark_batch_for_review(attempt, " ".join(error.messages))
            attempt.refresh_from_db()
            batch.refresh_from_db()
            record.refresh_from_db()
            return BatchTransactionResult("needs_review", attempt, record)
        raise


def get_receipt_issuance_context(receipt: Receipt) -> dict[str, Any]:
    """Build the receipt-page state for eligibility, active batch and prior history."""

    row = _unresolved_batch_row(receipt)
    batch = row.batch if row is not None else None
    local_eligibility = (
        receipt.status == Receipt.Status.BLOCKCHAIN_CONFIRMED
        and row is None
        and receipt.unit.active
        and receipt.weighing_ticket.net_weight_kg > 0
    )
    return {
        "issuance_batch": batch,
        "issuance_batch_eligible_locally": local_eligibility,
        "issuance_display": get_batch_issuance_display(batch) if batch is not None else None,
        "issuance_batch_history": receipt.issuance_batches.select_related(
            "network", "created_by"
        ).order_by("-created_at", "-pk"),
    }


def _format_token_units(amount: int, decimals: int) -> str:
    """Format integer ERC-20 units as a Portuguese decimal without float rounding."""

    if amount < 0 or not 0 <= decimals <= 77:
        raise ValueError("Token amount or decimal precision is outside the supported range.")
    divisor = 10**decimals
    whole, remainder = divmod(amount, divisor)
    if decimals == 0 or remainder == 0:
        return str(whole)

    fraction = str(remainder).zfill(decimals).rstrip("0")
    return f"{whole},{fraction}"


def get_batch_issuance_display(batch: IssuanceBatch) -> dict[str, Any] | None:
    """Return confirmed issuance evidence and a best-effort latest token balance."""

    if (
        batch.status != IssuanceBatch.Status.COMPLETED
        or batch.issued_amount_sjc is None
        or batch.participant_amount_sjc is None
        or batch.token_decimals is None
    ):
        return None

    confirmed_attempt = (
        batch.attempts.filter(
            kind=IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE,
            transaction__status=BlockchainTransaction.Status.CONFIRMED,
        )
        .select_related("transaction")
        .first()
    )
    if confirmed_attempt is None:
        return None

    transaction_record = confirmed_attempt.transaction
    display: dict[str, Any] = {
        "issued_amount": _format_token_units(batch.issued_amount_sjc, batch.token_decimals),
        "participant_amount": _format_token_units(
            batch.participant_amount_sjc, batch.token_decimals
        ),
        "token_decimals": batch.token_decimals,
        "token_address": batch.soja_coin.address_normalized,
        "transaction_hash": transaction_record.transaction_hash,
        "block_number": transaction_record.block_number,
        "balance": None,
        "balance_block_number": None,
    }

    try:
        web3 = connect_to_rpc(batch.network)
        token = bind_contract_to_web3(web3, batch.soja_coin)
        decimals = int(token.functions.decimals().call())
        if decimals != batch.token_decimals:
            return display
        balance_block = int(web3.eth.block_number)
        raw_balance = int(
            token.functions.balanceOf(
                Web3.to_checksum_address(batch.participant.wallet_address_normalized)
            ).call(block_identifier=balance_block)
        )
        display["balance"] = _format_token_units(raw_balance, decimals)
        display["balance_block_number"] = balance_block
    except Exception:
        # Issuance evidence stays visible when an RPC is temporarily unavailable.
        logger.warning(
            "Could not query the latest SJC balance for issuance batch %s.",
            batch.pk,
            exc_info=True,
        )
    return display


def get_batch_status_label(batch: IssuanceBatch) -> str:
    """Return a deadline-aware display label without mutating chain state."""

    if batch.approval_expired:
        return "Prazo expirado — a carga segue bloqueada até o cancelamento confirmado"
    return batch.get_status_display()
