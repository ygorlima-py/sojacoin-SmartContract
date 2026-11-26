"""Prepare and track unit registration transactions sent by an owner wallet.

The service prepares the exact UnitRegistry call in Django, but the connected
wallet signs and broadcasts it. Receipt polling uses the shared blockchain
confirmation service, which applies the existing UnitRegistered event handler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import TransactionNotFound, Web3Exception

from utils.identifiers import (
    is_valid_bytes32_hash,
    is_valid_transaction_hash,
    is_valid_wallet_address,
    normalize_transaction_hash,
    normalize_wallet_address,
)
from blockchain.models import (
    BlockchainTransaction,
    ContractInstance,
    Network,
    UNIT_REGISTRATION_OPERATION_TYPE,
    UnitRegistrationAttempt,
)
from blockchain.services.contracts import bind_contract_to_web3
from blockchain.services.rpc import connect_to_rpc
from blockchain.services.transaction_confirmation import confirm_transaction_from_chain
from blockchain.services.transactions import fail_transaction, submit_transaction

from ..models import Unit
from .unit_registration import (
    UNIT_REGISTRY_CONTRACT_KIND,
    handle_unit_registered_event,
)


@dataclass(frozen=True)
class PreparedUnitRegistration:
    """Return the server-prepared wallet request and its recovery state."""

    status: Literal["ready", "recover", "submitted"]
    attempt: UnitRegistrationAttempt
    chain_id: int
    owner_address: str
    contract_address: str
    transaction_data: str


@dataclass(frozen=True)
class UnitRegistrationConfirmation:
    """Describe the result of checking one unit registration receipt."""

    status: Literal["pending", "failed", "confirmed"]
    attempt: UnitRegistrationAttempt
    transaction: BlockchainTransaction
    chain_unit_id: int | None


def _require_positive_id(value: int, field_name: str) -> None:
    """Reject non-positive IDs before using them in database lookups."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"O campo {field_name} deve ser um número positivo.")


def _encode_unit_code(code: str) -> bytes:
    """Encode a unit code as Solidity ``bytes32`` using UTF-8 and zero padding."""

    normalized_code = (code or "").strip().upper()
    if not normalized_code or "\x00" in normalized_code:
        raise ValidationError("O código da unidade não pode ficar vazio ou conter byte nulo.")

    encoded_code = normalized_code.encode("utf-8")
    if len(encoded_code) > 32:
        raise ValidationError(
            "O código da unidade é longo demais para o contrato. Use até 32 bytes UTF-8."
        )

    # Solidity stores this bytes32 value left-aligned and pads its end with zero bytes.
    return encoded_code.ljust(32, b"\x00")


def _transaction_data(value: object) -> str:
    """Normalize transaction input returned by Web3 for exact comparison."""

    if isinstance(value, (bytes, bytearray)):
        data = Web3.to_hex(value).lower()
    elif isinstance(value, str):
        data = value.lower()
    else:
        raise ValidationError("O RPC retornou dados de transação inválidos.")

    if not re.fullmatch(r"0x(?:[0-9a-f]{2})+", data):
        raise ValidationError("O RPC retornou dados de transação inválidos.")
    return data


def _get_unit_attempt(attempt_id: int) -> UnitRegistrationAttempt:
    """Load an attempt with the unit, network and transaction it protects."""

    _require_positive_id(attempt_id, "tentativa")
    try:
        return UnitRegistrationAttempt.objects.select_related(
            "unit__company",
            "transaction__network",
            "transaction__contract",
        ).get(pk=attempt_id)
    except UnitRegistrationAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de registro da unidade não encontrada.") from error


def _get_contracts_for_registration(
    *,
    company_id: int,
    network: Network,
) -> tuple[ContractInstance, ContractInstance]:
    """Return the active UnitRegistry and SojaCoin for the same company/network."""

    common_filter = {
        "company_id": company_id,
        "network": network,
        "active": True,
    }
    try:
        registry = ContractInstance.objects.select_related("network").get(
            **common_filter,
            kind=ContractInstance.Kind.UNIT_REGISTRY,
        )
        soja_coin = ContractInstance.objects.get(
            **common_filter,
            kind=ContractInstance.Kind.SOJA_COIN,
        )
    except ContractInstance.DoesNotExist as error:
        raise ValidationError(
            "A empresa precisa ter SojaCoin e UnitRegistry ativos nesta rede."
        ) from error

    return registry, soja_coin


def _check_contract_setup(
    web3: Web3,
    registry: ContractInstance,
    soja_coin: ContractInstance,
    sender_address: str,
) -> tuple[Contract, str]:
    """Verify deployed code, owner and the two saved contract links."""

    registry_address = Web3.to_checksum_address(registry.address_normalized)
    soja_coin_address = Web3.to_checksum_address(soja_coin.address_normalized)
    if not web3.eth.get_code(registry_address):
        raise ValidationError("Não há contrato UnitRegistry implantado neste endereço.")
    if not web3.eth.get_code(soja_coin_address):
        raise ValidationError("Não há contrato SojaCoin implantado neste endereço.")

    registry_contract = bind_contract_to_web3(web3, registry)
    soja_coin_contract = bind_contract_to_web3(web3, soja_coin)
    current_owner = normalize_wallet_address(
        str(registry_contract.functions.owner().call())
    )
    if current_owner != sender_address:
        raise ValidationError(
            "Conecte a carteira dona do UnitRegistry para registrar esta unidade."
        )

    registry_soja_coin = normalize_wallet_address(
        str(registry_contract.functions.sojaCoin().call())
    )
    soja_coin_registry = normalize_wallet_address(
        str(soja_coin_contract.functions.unitRegistry().call())
    )
    if (
        registry_soja_coin != soja_coin.address_normalized
        or soja_coin_registry != registry.address_normalized
    ):
        raise ValidationError(
            "O UnitRegistry e a SojaCoin não estão conectados entre si nesta rede."
        )

    return registry_contract, current_owner


def _build_registration_call_data(
    registry_contract: Contract,
    unit_code: str,
    registration_hash: str,
) -> str:
    """Encode the exact ``registerUnit`` call for one saved unit snapshot."""

    code_bytes = _encode_unit_code(unit_code)
    hash_bytes = bytes.fromhex(registration_hash[2:])
    return (
        registry_contract.functions.registerUnit(code_bytes, hash_bytes)
        ._encode_transaction_data()
        .lower()
    )


def _verify_prepared_attempt_before_wallet(
    attempt: UnitRegistrationAttempt,
    network: Network,
    sender_address: str,
) -> tuple[str, str]:
    """Recheck active deployment, owner and calldata before reopening the wallet."""

    registry, soja_coin = _get_contracts_for_registration(
        company_id=attempt.unit.company_id,
        network=network,
    )
    if registry.pk != attempt.transaction.contract_id:
        raise ValidationError(
            "O UnitRegistry ativo mudou desde a preparação. Revise esta tentativa antes de enviar."
        )

    web3 = connect_to_rpc(network)
    registry_contract, current_owner = _check_contract_setup(
        web3,
        registry,
        soja_coin,
        sender_address,
    )
    encoded_code = _encode_unit_code(attempt.unit_code)
    if int(registry_contract.functions.unitIdByCode(encoded_code).call()) != 0:
        raise ValidationError(
            "Este código já existe no UnitRegistry. Confira o vínculo antes de enviar."
        )

    expected_data = _build_registration_call_data(
        registry_contract,
        attempt.unit_code,
        attempt.registration_hash,
    )
    if expected_data != attempt.prepared_data:
        raise ValidationError(
            "Os dados da chamada preparada não correspondem ao ABI atual do UnitRegistry."
        )
    return current_owner, registry.address_normalized


def prepare_unit_registration(
    *,
    company_id: int,
    unit_id: int,
    network_id: int,
    sender_address: str,
    wallet_chain_id: int,
) -> PreparedUnitRegistration:
    """Prepare one unit registration and persist its recovery record.

    The company, unit and network IDs are checked together so a browser cannot
    submit one company's unit to another company's registry. An existing open
    attempt is returned only to the same wallet and network; it is never silently
    replaced by a second transaction.

    Args:
        company_id: Company validated from the request URL.
        unit_id: Local unit selected from that company.
        network_id: Active network selected by the operator.
        sender_address: Public wallet selected in the browser provider.
        wallet_chain_id: Chain ID reported by the browser wallet.

    Returns:
        The saved attempt, chain ID and exact transaction request. ``status``
        is ``ready`` for a new send, ``recover`` when a wallet response may be
        uncertain, or ``submitted`` when the same hash was already recorded.

    Raises:
        ValidationError: If the unit, network, wallet, contract, hash or current
            on-chain state is not valid for this operation.
        ConnectionError: If the selected network RPC cannot be reached.
    """

    _require_positive_id(company_id, "empresa")
    _require_positive_id(unit_id, "unidade")
    _require_positive_id(network_id, "rede")
    if isinstance(wallet_chain_id, bool) or not isinstance(wallet_chain_id, int):
        raise ValidationError("A rede informada pela carteira é inválida.")

    normalized_sender = normalize_wallet_address(sender_address)
    if not is_valid_wallet_address(normalized_sender):
        raise ValidationError("Conecte uma carteira pública válida.")

    try:
        with db_transaction.atomic():
            unit = (
                Unit.objects.select_for_update()
                .select_related("company")
                .get(pk=unit_id, company_id=company_id)
            )
            if not unit.company.active:
                raise ValidationError("A empresa está inativa.")
            if not unit.active:
                raise ValidationError("Unidades inativas não podem ser registradas.")
            if unit.chain_unit_id is not None:
                raise ValidationError("Esta unidade já possui um ID confirmado na blockchain.")
            if not is_valid_bytes32_hash(unit.registration_hash):
                raise ValidationError("A unidade ainda não tem uma hash de cadastro válida.")

            encoded_code = _encode_unit_code(unit.code)
            network = Network.objects.get(pk=network_id)
            if wallet_chain_id != network.chain_id:
                raise ValidationError(
                    f"A MetaMask está na rede {wallet_chain_id}; selecione {network.name} "
                    f"(ID {network.chain_id})."
                )

            open_attempt = (
                UnitRegistrationAttempt.objects.select_for_update()
                .select_related("transaction__contract", "transaction__network")
                .filter(unit=unit, is_open=True)
                .first()
            )
            if open_attempt is not None:
                if (
                    open_attempt.transaction.network_id != network.pk
                    or open_attempt.transaction.sender_address != normalized_sender
                ):
                    raise ValidationError(
                        "Esta unidade já tem uma tentativa aberta em outra carteira ou rede. "
                        "Retome a tentativa existente."
                    )
                if (
                    open_attempt.unit_code != unit.code
                    or open_attempt.registration_hash != unit.registration_hash
                ):
                    raise ValidationError(
                        "Os dados da unidade mudaram durante uma tentativa aberta. "
                        "Revise a tentativa antes de continuar."
                    )

                transaction_record = open_attempt.transaction
                if transaction_record.status == BlockchainTransaction.Status.SUBMITTED:
                    status: Literal["ready", "recover", "submitted"] = "submitted"
                elif open_attempt.submission_started_at is not None:
                    status = "recover"
                else:
                    # A new wallet prompt is allowed only while the saved
                    # deployment and network are still active.
                    if not network.active or not transaction_record.contract.active:
                        raise ValidationError(
                            "A rede ou o UnitRegistry desta tentativa foi desativado. "
                            "Revise a tentativa antes de continuar."
                        )
                    current_owner, contract_address = _verify_prepared_attempt_before_wallet(
                        open_attempt,
                        network,
                        normalized_sender,
                    )
                    status = "ready"
                if status != "ready":
                    current_owner = normalized_sender
                    contract_address = transaction_record.contract.address_normalized
                return PreparedUnitRegistration(
                    status=status,
                    attempt=open_attempt,
                    chain_id=network.chain_id,
                    owner_address=current_owner,
                    contract_address=contract_address,
                    transaction_data=open_attempt.prepared_data,
                )

            if not network.active:
                raise ValidationError("A rede selecionada está inativa.")

            registry, soja_coin = _get_contracts_for_registration(
                company_id=company_id,
                network=network,
            )

            web3 = connect_to_rpc(network)
            registry_contract, current_owner = _check_contract_setup(
                web3,
                registry,
                soja_coin,
                normalized_sender,
            )
            existing_chain_id = int(
                registry_contract.functions.unitIdByCode(encoded_code).call()
            )
            if existing_chain_id != 0:
                raise ValidationError(
                    "Este código já existe no UnitRegistry, mas não está vinculado à unidade local. "
                    "Confira o cadastro antes de iniciar outra transação."
                )

            prepared_data = _build_registration_call_data(
                registry_contract,
                unit.code,
                unit.registration_hash,
            )

            transaction_record = BlockchainTransaction(
                company=unit.company,
                network=network,
                contract=registry,
                operation_type=UNIT_REGISTRATION_OPERATION_TYPE,
                sender_address=current_owner,
                status=BlockchainTransaction.Status.PREPARED,
            )
            transaction_record.save()
            attempt = UnitRegistrationAttempt(
                unit=unit,
                transaction=transaction_record,
                unit_code=unit.code,
                registration_hash=unit.registration_hash,
                prepared_data=prepared_data,
                is_open=True,
            )
            attempt.save()

            return PreparedUnitRegistration(
                status="ready",
                attempt=attempt,
                chain_id=network.chain_id,
                owner_address=current_owner,
                contract_address=registry.address_normalized,
                transaction_data=prepared_data,
            )
    except Unit.DoesNotExist as error:
        raise ValidationError("A unidade não pertence à empresa informada.") from error
    except Network.DoesNotExist as error:
        raise ValidationError("A rede selecionada não existe.") from error
    except IntegrityError as error:
        raise ValidationError(
            "Já existe uma tentativa aberta para esta unidade. Recarregue a página e retome-a."
        ) from error
    except Web3Exception as error:
        raise ConnectionError(
            "Não foi possível consultar o UnitRegistry na rede selecionada."
        ) from error


def mark_unit_registration_submission_started(
    attempt_id: int,
) -> UnitRegistrationAttempt:
    """Persist wallet-send intent before showing the MetaMask confirmation.

    Once this marker exists, the browser must not create another transaction
    automatically. It must either report an explicit rejection or recover the
    hash from the same wallet request.
    """

    attempt = _get_unit_attempt(attempt_id)
    with db_transaction.atomic():
        locked_attempt = (
            UnitRegistrationAttempt.objects.select_for_update()
            .select_related(
                "unit__company",
                "transaction__network",
                "transaction__contract",
            )
            .get(pk=attempt.pk)
        )
        record = BlockchainTransaction.objects.select_for_update().get(
            pk=locked_attempt.transaction_id
        )
        if not locked_attempt.is_open:
            raise ValidationError("Esta tentativa já foi encerrada.")
        if record.status == BlockchainTransaction.Status.SUBMITTED:
            return locked_attempt
        if record.status != BlockchainTransaction.Status.PREPARED:
            raise ValidationError("Esta tentativa não pode abrir uma solicitação na carteira.")
        if not locked_attempt.unit.active or not locked_attempt.unit.company.active:
            raise ValidationError("A empresa e a unidade precisam estar ativas para enviar.")
        if not locked_attempt.transaction.network.active:
            raise ValidationError("A rede desta tentativa está inativa.")
        if not locked_attempt.transaction.contract.active:
            raise ValidationError("O UnitRegistry desta tentativa está inativo.")
        if locked_attempt.submission_started_at is None:
            locked_attempt.submission_started_at = timezone.now()
            locked_attempt.save(update_fields=("submission_started_at", "updated_at"))
        return locked_attempt


def reject_unit_registration_attempt(attempt_id: int) -> UnitRegistrationAttempt:
    """Record an explicit wallet rejection and release the unit for a new try."""

    attempt = _get_unit_attempt(attempt_id)
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("A tentativa não está aguardando uma resposta da carteira.")
    if attempt.transaction.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Uma transação enviada não pode ser marcada como recusada.")

    # The shared lifecycle service closes the attempt while preserving its record.
    fail_transaction(attempt.transaction, "A carteira recusou o registro da unidade.")
    attempt.refresh_from_db()
    return attempt


def register_unit_registration_transaction_hash(
    *,
    attempt_id: int,
    transaction_hash: str,
) -> BlockchainTransaction | None:
    """Verify a wallet hash against the saved call and mark it as submitted.

    The RPC must return the exact prepared call from the expected owner, to the
    saved UnitRegistry, on the saved chain, with zero native currency. A missing
    transaction returns ``None`` so the same hash can be retried later.
    """

    canonical_hash = normalize_transaction_hash(transaction_hash)
    if not is_valid_transaction_hash(canonical_hash):
        raise ValidationError("A hash informada pela carteira é inválida.")

    attempt = _get_unit_attempt(attempt_id)
    record = attempt.transaction
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("A tentativa não está aguardando o resultado da carteira.")
    if record.transaction_hash:
        if record.transaction_hash == canonical_hash:
            return record
        raise ValidationError("Esta tentativa já está vinculada a outra hash.")
    if record.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Somente uma tentativa preparada pode receber uma hash.")

    web3 = connect_to_rpc(record.network)
    try:
        chain_transaction = web3.eth.get_transaction(canonical_hash)
    except TransactionNotFound:
        return None
    except Web3Exception as error:
        raise ConnectionError("Não foi possível consultar a transação no RPC.") from error

    found_hash = normalize_transaction_hash(Web3.to_hex(chain_transaction["hash"]))
    if found_hash != canonical_hash:
        raise ValidationError("O RPC retornou uma transação com outra hash.")
    if normalize_wallet_address(str(chain_transaction["from"])) != record.sender_address:
        raise ValidationError("A transação foi enviada por outra carteira.")
    if normalize_wallet_address(str(chain_transaction.get("to") or "")) != (
        record.contract.address_normalized
    ):
        raise ValidationError("A transação chama outro contrato, não o UnitRegistry esperado.")

    chain_id = chain_transaction.get("chainId")
    if chain_id is None or int(chain_id) != record.network.chain_id:
        raise ValidationError("A transação pertence a uma rede diferente da tentativa.")
    if int(chain_transaction.get("value", 0)) != 0:
        raise ValidationError("O registro da unidade não pode transferir saldo nativo.")

    transaction_input = chain_transaction.get("input")
    if transaction_input is None:
        transaction_input = chain_transaction.get("data")
    if _transaction_data(transaction_input) != attempt.prepared_data:
        raise ValidationError("Os dados enviados diferem da chamada preparada pelo servidor.")

    with db_transaction.atomic():
        locked_attempt = UnitRegistrationAttempt.objects.select_for_update().get(
            pk=attempt.pk
        )
        if not locked_attempt.is_open or locked_attempt.submission_started_at is None:
            raise ValidationError("A tentativa não está mais aberta para registrar uma hash.")
        locked_record = BlockchainTransaction.objects.select_for_update().get(
            pk=record.pk
        )
        if locked_record.status == BlockchainTransaction.Status.SUBMITTED:
            if locked_record.transaction_hash == canonical_hash:
                return locked_record
            raise ValidationError("Esta tentativa já está vinculada a outra hash.")
        if locked_record.status != BlockchainTransaction.Status.PREPARED:
            raise ValidationError("Somente uma tentativa preparada pode receber uma hash.")

        # Keep the attempt and transaction locks in the same order as lifecycle services.
        return submit_transaction(locked_record, canonical_hash)


def confirm_unit_registration_attempt(
    attempt_id: int,
) -> UnitRegistrationConfirmation:
    """Check the receipt and apply its matching UnitRegistered event once.

    The shared confirmation service checks the saved network and receipt. The
    event handler validates the unit code, registration hash and sender before
    saving ``Unit.chain_unit_id`` in the same database transaction.
    """

    attempt = _get_unit_attempt(attempt_id)
    record = attempt.transaction
    if record.status not in {
        BlockchainTransaction.Status.SUBMITTED,
        BlockchainTransaction.Status.CONFIRMED,
        BlockchainTransaction.Status.FAILED,
    }:
        raise ValidationError("A tentativa ainda não tem uma transação enviada para consultar.")

    if record.status == BlockchainTransaction.Status.FAILED:
        return UnitRegistrationConfirmation(
            status="failed",
            attempt=attempt,
            transaction=record,
            chain_unit_id=attempt.unit.chain_unit_id,
        )

    try:
        confirmed_record = confirm_transaction_from_chain(
            record.pk,
            event_handler=handle_unit_registered_event,
            required_event_names=("UnitRegistered",),
        )
    except Web3Exception as error:
        raise ConnectionError(
            "Não foi possível consultar o recibo da transação no RPC."
        ) from error
    attempt.refresh_from_db()
    attempt.unit.refresh_from_db()

    if confirmed_record is None:
        status: Literal["pending", "failed", "confirmed"] = "pending"
    elif confirmed_record.status == BlockchainTransaction.Status.FAILED:
        status = "failed"
    elif confirmed_record.status == BlockchainTransaction.Status.CONFIRMED:
        status = "confirmed"
    else:
        raise ValidationError("A consulta retornou uma situação de transação inesperada.")

    return UnitRegistrationConfirmation(
        status=status,
        attempt=attempt,
        transaction=confirmed_record or record,
        chain_unit_id=attempt.unit.chain_unit_id,
    )
