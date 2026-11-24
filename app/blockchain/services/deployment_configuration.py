"""Prepare, submit and confirm the two SojaCoin setup transactions.

The setup calls happen only after all nine contracts are confirmed. Django
encodes and verifies the call, while the connected owner wallet signs and
broadcasts it. Private keys never enter this service or the database.
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
from web3.logs import DISCARD

from utils.identifiers import (
    is_valid_transaction_hash,
    is_valid_wallet_address,
    normalize_transaction_hash,
    normalize_wallet_address,
)

from ..models import (
    CONTRACT_CONFIGURATION_OPERATION_TYPE,
    BlockchainTransaction,
    ContractInstance,
    DeploymentConfigurationAttempt,
    DeploymentProcess,
)
from .contracts import bind_contract_to_web3
from .deployment import DEPLOYMENT_ORDER
from .rpc import connect_to_rpc
from .transactions import confirm_transaction, fail_transaction, submit_transaction


ConfigurationKind = Literal["unit_registry", "issuance_batches"]


@dataclass(frozen=True)
class ConfigurationSpec:
    """Map a setup action to its helper contract, ABI call, getter and event."""

    target_kind: str
    function_name: str
    getter_name: str
    event_name: str
    event_address_argument: str


CONFIGURATION_SPECS: dict[str, ConfigurationSpec] = {
    DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY: ConfigurationSpec(
        target_kind=ContractInstance.Kind.UNIT_REGISTRY,
        function_name="configureUnitRegistry",
        getter_name="unitRegistry",
        event_name="UnitRegistryConfigured",
        event_address_argument="unitRegistry",
    ),
    DeploymentConfigurationAttempt.Kind.ISSUANCE_BATCHES: ConfigurationSpec(
        target_kind=ContractInstance.Kind.ISSUANCE_BATCHES,
        function_name="configureIssuanceBatches",
        getter_name="issuanceBatchModule",
        event_name="IssuanceBatchesConfigured",
        event_address_argument="issuanceBatches",
    ),
}


@dataclass(frozen=True)
class PreparedConfiguration:
    """Return the saved wallet request or an already observed on-chain link."""

    status: Literal["ready", "already_configured", "needs_review"]
    attempt_id: int | None
    chain_id: int
    owner_address: str
    transaction_to: str
    transaction_data: str
    expected_address: str
    current_address: str


@dataclass(frozen=True)
class ConfigurationConfirmation:
    """Describe the result of checking one configuration transaction."""

    status: Literal["pending", "failed", "confirmed", "needs_review"]
    attempt: DeploymentConfigurationAttempt
    transaction: BlockchainTransaction
    expected_address: str
    current_address: str = ""


def _get_attempt(attempt_id: int) -> DeploymentConfigurationAttempt:
    """Load the process, transaction and SojaCoin used by one saved attempt."""

    if attempt_id < 1:
        raise ValidationError("O ID da tentativa deve ser positivo.")
    try:
        return DeploymentConfigurationAttempt.objects.select_related(
            "process__company",
            "process__network",
            "transaction__contract",
        ).get(pk=attempt_id)
    except DeploymentConfigurationAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de configuração não encontrada.") from error


def _get_confirmed_contracts(
    process: DeploymentProcess,
) -> dict[str, ContractInstance]:
    """Return this process's nine active contracts after checking every receipt."""

    steps = list(
        process.steps.select_related("contract")
        .prefetch_related("attempts__transaction")
        .order_by("position")
    )
    if tuple(step.kind for step in steps) != DEPLOYMENT_ORDER:
        raise ValidationError("As nove etapas do processo estão incompletas ou fora de ordem.")

    contracts: dict[str, ContractInstance] = {}
    for step in steps:
        contract = step.contract
        if (
            not step.is_confirmed
            or contract is None
            or not contract.active
            or contract.company_id != process.company_id
            or contract.network_id != process.network_id
            or contract.kind != step.kind
        ):
            raise ValidationError(
                f"O contrato {step.get_kind_display()} não está confirmado neste processo."
            )
        contracts[step.kind] = contract
    return contracts


def _configuration_function_data(
    contract: Contract,
    function_name: str,
    address: str,
) -> str:
    """Encode one address argument using the packaged SojaCoin ABI."""

    # The ABI supplies the selector and argument encoding; do not accept calldata
    # from the browser because it could target a different helper contract.
    function = getattr(contract.functions, function_name)(
        Web3.to_checksum_address(address)
    )
    data = function._encode_transaction_data()
    if not isinstance(data, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", data):
        raise ValidationError("O ABI produziu dados de configuração inválidos.")
    return data.lower()


def _read_contract_address(
    contract: Contract,
    function_name: str,
    *,
    block_number: int | None = None,
) -> str:
    """Read and normalize a public contract address from a view function."""

    function = getattr(contract.functions, function_name)()
    address = (
        function.call()
        if block_number is None
        else function.call(block_identifier=block_number)
    )
    normalized = normalize_wallet_address(str(address))
    if not is_valid_wallet_address(normalized):
        raise ValidationError("A blockchain retornou um endereço inválido.")
    return normalized


def _mark_process_for_review(process_id: int) -> DeploymentProcess:
    """Keep the process from appearing ready when chain state diverges."""

    with db_transaction.atomic():
        process = DeploymentProcess.objects.select_for_update().get(pk=process_id)
        if process.status == DeploymentProcess.Status.DEPLOYED:
            process.status = DeploymentProcess.Status.NEEDS_REVIEW
            process.save()
        return process


def prepare_deployment_configuration(
    *,
    process_id: int,
    kind: ConfigurationKind,
    sender_address: str,
    wallet_chain_id: int,
) -> PreparedConfiguration:
    """Save or resume a wallet request for one of the two setup calls.

    The contract addresses, destination and calldata are taken from the nine
    confirmed steps and the packaged ABI. The caller supplies only the public
    address selected in the wallet and that wallet's chain ID.

    Args:
        process_id: Saved deployment process that owns the nine contract steps.
        kind: Which one-time SojaCoin link should be configured.
        sender_address: Public account currently selected in the wallet.
        wallet_chain_id: Chain ID reported by the connected wallet.

    Returns:
        The persisted request to send, an already-correct on-chain link, or a
        review result when chain state conflicts with this process.

    Raises:
        ValidationError: If the process, owner, network, deployment steps or
            open-attempt state does not permit this configuration.
    """

    if process_id < 1:
        raise ValidationError("O ID do processo deve ser positivo.")
    spec = CONFIGURATION_SPECS.get(kind)
    if spec is None:
        raise ValidationError("Tipo de configuração inválido.")

    sender = normalize_wallet_address(sender_address)
    if not is_valid_wallet_address(sender) or sender == "0x" + "0" * 40:
        raise ValidationError("Conecte a carteira dona do contrato SojaCoin.")
    if not isinstance(wallet_chain_id, int) or wallet_chain_id < 1:
        raise ValidationError("Não foi possível identificar a rede da carteira.")

    try:
        process = DeploymentProcess.objects.select_related("company", "network").get(
            pk=process_id
        )
    except DeploymentProcess.DoesNotExist as error:
        raise ValidationError("Processo de implantação não encontrado.") from error

    if process.status != DeploymentProcess.Status.DEPLOYED:
        raise ValidationError("Conclua os nove contratos antes de configurar os vínculos.")
    if not process.company.active or not process.network.active:
        raise ValidationError("A empresa e a rede precisam estar ativas.")
    if wallet_chain_id != process.network.chain_id:
        raise ValidationError("A carteira está conectada a uma rede diferente do processo.")

    contracts = _get_confirmed_contracts(process)
    soja_coin = contracts[ContractInstance.Kind.SOJA_COIN]
    target = contracts[spec.target_kind]
    web3 = connect_to_rpc(process.network)

    # Confirm the saved addresses still have code before preparing a wallet call.
    for deployment in (soja_coin, target):
        code = web3.eth.get_code(
            Web3.to_checksum_address(deployment.address_normalized)
        )
        if not code:
            _mark_process_for_review(process.pk)
            return PreparedConfiguration(
                status="needs_review",
                attempt_id=None,
                chain_id=process.network.chain_id,
                owner_address="",
                transaction_to=soja_coin.address_normalized,
                transaction_data="",
                expected_address=target.address_normalized,
                current_address="",
            )

    # These helper contracts must have been created for this exact SojaCoin.
    target_contract = bind_contract_to_web3(web3, target)
    reverse_link = _read_contract_address(target_contract, "sojaCoin")
    if reverse_link != soja_coin.address_normalized:
        _mark_process_for_review(process.pk)
        return PreparedConfiguration(
            status="needs_review",
            attempt_id=None,
            chain_id=process.network.chain_id,
            owner_address="",
            transaction_to=soja_coin.address_normalized,
            transaction_data="",
            expected_address=target.address_normalized,
            current_address=reverse_link,
        )

    sojacoin_contract = bind_contract_to_web3(web3, soja_coin)
    current_owner = _read_contract_address(sojacoin_contract, "owner")
    if current_owner != sender:
        raise ValidationError(
            "A carteira conectada não é a dona atual do contrato SojaCoin."
        )

    current_link = _read_contract_address(sojacoin_contract, spec.getter_name)
    zero_address = "0x" + "0" * 40
    if current_link == target.address_normalized:
        return PreparedConfiguration(
            status="already_configured",
            attempt_id=None,
            chain_id=process.network.chain_id,
            owner_address=current_owner,
            transaction_to=soja_coin.address_normalized,
            transaction_data="",
            expected_address=target.address_normalized,
            current_address=current_link,
        )
    if current_link != zero_address:
        _mark_process_for_review(process.pk)
        return PreparedConfiguration(
            status="needs_review",
            attempt_id=None,
            chain_id=process.network.chain_id,
            owner_address=current_owner,
            transaction_to=soja_coin.address_normalized,
            transaction_data="",
            expected_address=target.address_normalized,
            current_address=current_link,
        )

    prepared_data = _configuration_function_data(
        sojacoin_contract,
        spec.function_name,
        target.address_normalized,
    )

    try:
        with db_transaction.atomic():
            locked_process = (
                DeploymentProcess.objects.select_for_update()
                .select_related("company", "network")
                .get(pk=process.pk)
            )
            if (
                locked_process.status != DeploymentProcess.Status.DEPLOYED
                or not locked_process.company.active
                or not locked_process.network.active
            ):
                raise ValidationError("O processo deixou de estar disponível para configuração.")

            open_attempt = (
                DeploymentConfigurationAttempt.objects.select_for_update()
                .select_related("transaction")
                .filter(process=locked_process, kind=kind, is_open=True)
                .first()
            )
            if open_attempt is not None:
                if (
                    open_attempt.transaction.status == BlockchainTransaction.Status.PREPARED
                    and open_attempt.submission_started_at is None
                    and open_attempt.transaction.sender_address == sender
                    and open_attempt.expected_address == target.address_normalized
                    and open_attempt.prepared_data == prepared_data
                ):
                    return PreparedConfiguration(
                        status="ready",
                        attempt_id=open_attempt.pk,
                        chain_id=locked_process.network.chain_id,
                        owner_address=current_owner,
                        transaction_to=soja_coin.address_normalized,
                        transaction_data=open_attempt.prepared_data,
                        expected_address=target.address_normalized,
                        current_address=current_link,
                    )
                raise ValidationError(
                    "Esta configuração já tem uma tentativa para conferir. Use a mesma tentativa."
                )

            previous_attempts = DeploymentConfigurationAttempt.objects.filter(
                process=locked_process,
                kind=kind,
            ).select_related("transaction")
            if previous_attempts.filter(
                transaction__status=BlockchainTransaction.Status.REORGED
            ).exists():
                locked_process.status = DeploymentProcess.Status.NEEDS_REVIEW
                locked_process.save()
                return PreparedConfiguration(
                    status="needs_review",
                    attempt_id=None,
                    chain_id=locked_process.network.chain_id,
                    owner_address=current_owner,
                    transaction_to=soja_coin.address_normalized,
                    transaction_data="",
                    expected_address=target.address_normalized,
                    current_address=current_link,
                )
            if previous_attempts.filter(
                transaction__status=BlockchainTransaction.Status.CONFIRMED
            ).exists():
                locked_process.status = DeploymentProcess.Status.NEEDS_REVIEW
                locked_process.save()
                return PreparedConfiguration(
                    status="needs_review",
                    attempt_id=None,
                    chain_id=locked_process.network.chain_id,
                    owner_address=current_owner,
                    transaction_to=soja_coin.address_normalized,
                    transaction_data="",
                    expected_address=target.address_normalized,
                    current_address=current_link,
                )

            transaction_record = BlockchainTransaction.objects.create(
                company=locked_process.company,
                network=locked_process.network,
                contract=soja_coin,
                operation_type=CONTRACT_CONFIGURATION_OPERATION_TYPE,
                sender_address=sender,
            )
            attempt = DeploymentConfigurationAttempt.objects.create(
                process=locked_process,
                kind=kind,
                transaction=transaction_record,
                expected_address=target.address_normalized,
                prepared_data=prepared_data,
            )
            return PreparedConfiguration(
                status="ready",
                attempt_id=attempt.pk,
                chain_id=locked_process.network.chain_id,
                owner_address=current_owner,
                transaction_to=soja_coin.address_normalized,
                transaction_data=prepared_data,
                expected_address=target.address_normalized,
                current_address=current_link,
            )
    except DeploymentProcess.DoesNotExist as error:
        raise ValidationError("Processo de implantação não encontrado.") from error
    except IntegrityError as error:
        raise ValidationError(
            "Já existe uma tentativa aberta para esta configuração. Confira-a antes de continuar."
        ) from error


def mark_configuration_submission_started(
    attempt_id: int,
) -> DeploymentConfigurationAttempt:
    """Save the wallet-prompt marker before the browser requests a signature.

    Args:
        attempt_id: Primary key of the saved configuration attempt.

    Returns:
        The attempt with its first wallet-send timestamp saved.

    Raises:
        ValidationError: If the process or attempt is no longer eligible, or a
            wallet prompt may already have been opened.
    """

    attempt = _get_attempt(attempt_id)
    try:
        with db_transaction.atomic():
            process = (
                DeploymentProcess.objects.select_for_update()
                .select_related("company", "network")
                .get(pk=attempt.process_id)
            )
            locked_attempt = DeploymentConfigurationAttempt.objects.select_for_update().get(
                pk=attempt.pk,
                process=process,
            )
            record = BlockchainTransaction.objects.select_for_update().get(
                pk=locked_attempt.transaction_id
            )
            if (
                process.status != DeploymentProcess.Status.DEPLOYED
                or not process.company.active
                or not process.network.active
            ):
                raise ValidationError("O processo não está pronto para configuração.")
            if not locked_attempt.is_open or record.status != BlockchainTransaction.Status.PREPARED:
                raise ValidationError("A tentativa não está preparada para abrir a carteira.")
            if locked_attempt.submission_started_at is not None:
                raise ValidationError(
                    "O pedido da carteira já foi iniciado. Confira a mesma tentativa."
                )

            # Persist uncertainty before opening MetaMask so a lost response
            # cannot silently create a second configuration transaction.
            locked_attempt.submission_started_at = timezone.now()
            locked_attempt.save(update_fields=("submission_started_at", "updated_at"))
            return locked_attempt
    except DeploymentProcess.DoesNotExist as error:
        raise ValidationError("Processo de implantação não encontrado.") from error


def reject_configuration_attempt(attempt_id: int) -> DeploymentConfigurationAttempt:
    """Record an explicit wallet rejection and release the attempt slot.

    Args:
        attempt_id: Primary key of the prepared configuration attempt.

    Returns:
        The preserved attempt linked to a failed transaction.

    Raises:
        ValidationError: If no wallet prompt was started or a hash has already
            been submitted. A late rejection can never discard an on-chain hash.
    """

    attempt = _get_attempt(attempt_id)
    try:
        with db_transaction.atomic():
            # Lock in the same process -> attempt -> transaction order used by
            # the submission path, then make the rejection decision on fresh rows.
            process = DeploymentProcess.objects.select_for_update().get(
                pk=attempt.process_id
            )
            locked_attempt = DeploymentConfigurationAttempt.objects.select_for_update().get(
                pk=attempt.pk,
                process=process,
            )
            record = BlockchainTransaction.objects.select_for_update().get(
                pk=locked_attempt.transaction_id
            )
            if locked_attempt.submission_started_at is None:
                raise ValidationError("A carteira ainda não recebeu este pedido.")
            if record.status == BlockchainTransaction.Status.FAILED:
                return locked_attempt
            if record.status != BlockchainTransaction.Status.PREPARED:
                # In particular, never convert a transaction with a saved hash
                # to failed merely because a rejection request arrived late.
                raise ValidationError(
                    "Somente uma rejeição explícita pode encerrar esta tentativa."
                )

            fail_transaction(
                record,
                "A carteira rejeitou explicitamente a chamada de configuração.",
            )
            locked_attempt.refresh_from_db()
            return locked_attempt
    except DeploymentProcess.DoesNotExist as error:
        raise ValidationError("Processo de implantação não encontrado.") from error


def _transaction_data(value: object) -> str:
    """Normalize the transaction input returned by a Web3 provider."""

    if isinstance(value, (bytes, bytearray)):
        normalized = Web3.to_hex(value).lower()
    elif isinstance(value, str):
        normalized = value.lower()
    else:
        raise ValidationError("O RPC retornou dados de transação inválidos.")
    if not re.fullmatch(r"0x(?:[0-9a-f]{2})+", normalized):
        raise ValidationError("O RPC retornou dados de transação inválidos.")
    return normalized


def register_configuration_transaction_hash(
    *,
    attempt_id: int,
    transaction_hash: str,
) -> BlockchainTransaction | None:
    """Verify a wallet hash and record it, preserving the same attempt on retry.

    Args:
        attempt_id: Primary key of the open attempt created before the wallet
            prompt.
        transaction_hash: Hash returned by the connected wallet.

    Returns:
        The saved transaction, or ``None`` while the RPC has not indexed the
        hash yet. An RPC error raises without closing the attempt.

    Raises:
        ValidationError: If the hash is invalid or the RPC transaction does not
            match the saved owner, network, SojaCoin destination and calldata.
    """

    canonical_hash = normalize_transaction_hash(transaction_hash)
    if not is_valid_transaction_hash(canonical_hash):
        raise ValidationError("A hash informada pela carteira é inválida.")
    attempt = _get_attempt(attempt_id)
    process = attempt.process
    record = attempt.transaction
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("A tentativa não está aguardando resposta da carteira.")
    if process.status != DeploymentProcess.Status.DEPLOYED:
        raise ValidationError("O processo não está mais disponível para configuração.")
    if record.transaction_hash and record.transaction_hash != canonical_hash:
        raise ValidationError("Esta tentativa já está vinculada a outra hash.")
    if record.status == BlockchainTransaction.Status.SUBMITTED:
        # A same-hash retry is already complete locally; it does not need the
        # RPC to rediscover a transaction that was verified before submission.
        if record.transaction_hash == canonical_hash:
            return record
        raise ValidationError("Esta tentativa já está vinculada a outra hash.")

    web3 = connect_to_rpc(process.network)
    try:
        chain_transaction = web3.eth.get_transaction(canonical_hash)
    except TransactionNotFound:
        return None

    found_hash = normalize_transaction_hash(Web3.to_hex(chain_transaction["hash"]))
    if found_hash != canonical_hash:
        raise ValidationError("O RPC retornou uma transação com outra hash.")
    if normalize_wallet_address(str(chain_transaction["from"])) != record.sender_address:
        raise ValidationError("A transação foi enviada por outra carteira.")
    if normalize_wallet_address(str(chain_transaction.get("to") or "")) != (
        attempt.transaction.contract.address_normalized
    ):
        raise ValidationError("A transação chama outro contrato, não o SojaCoin deste processo.")
    chain_id = chain_transaction.get("chainId")
    if chain_id is None or int(chain_id) != process.network.chain_id:
        raise ValidationError("A transação pertence a uma rede diferente do processo.")
    if int(chain_transaction.get("value", 0)) != 0:
        raise ValidationError("A chamada de configuração não pode transferir valor.")

    transaction_input = chain_transaction.get("input")
    if transaction_input is None:
        transaction_input = chain_transaction.get("data")
    if _transaction_data(transaction_input) != attempt.prepared_data:
        raise ValidationError("Os dados enviados diferem da chamada preparada no servidor.")

    with db_transaction.atomic():
        locked_process = (
            DeploymentProcess.objects.select_for_update()
            .select_related("company", "network")
            .get(pk=process.pk)
        )
        locked_attempt = DeploymentConfigurationAttempt.objects.select_for_update().get(
            pk=attempt.pk,
            process=locked_process,
        )
        locked_record = BlockchainTransaction.objects.select_for_update().get(
            pk=record.pk
        )
        if locked_record.status == BlockchainTransaction.Status.SUBMITTED:
            if locked_record.transaction_hash == canonical_hash:
                return locked_record
            raise ValidationError("Esta tentativa já está vinculada a outra hash.")
        if not locked_attempt.is_open or locked_attempt.submission_started_at is None:
            raise ValidationError("A tentativa não está mais aguardando resposta da carteira.")
        if locked_process.status != DeploymentProcess.Status.DEPLOYED:
            raise ValidationError("O processo não está mais disponível para configuração.")
        if not locked_process.company.active or not locked_process.network.active:
            raise ValidationError("A empresa e a rede precisam estar ativas.")
        # submit_transaction applies the shared lifecycle transition while the
        # process and configuration attempt are already locked in this order.
        return submit_transaction(locked_record, canonical_hash)


def _decode_expected_event(
    contract: Contract,
    receipt: object,
    *,
    event_name: str,
    address_argument: str,
    expected_address: str,
    expected_sender: str,
) -> bool:
    """Check that the receipt contains the exact SojaCoin setup event."""

    try:
        event = getattr(contract.events, event_name)()
        decoded = event.process_receipt(receipt, errors=DISCARD)
    except (KeyError, TypeError, ValueError, Web3Exception):
        return False

    for entry in decoded:
        args = entry.get("args", {})
        event_contract_address = normalize_wallet_address(str(entry.get("address", "")))
        event_address = normalize_wallet_address(str(args.get(address_argument, "")))
        event_sender = normalize_wallet_address(str(args.get("configuredBy", "")))
        if (
            event_contract_address == normalize_wallet_address(contract.address)
            and event_address == expected_address
            and event_sender == expected_sender
        ):
            return True
    return False


def _record_configuration_confirmation(
    attempt: DeploymentConfigurationAttempt,
    *,
    block_number: int,
    block_hash: str,
    event_valid: bool,
    current_address: str,
) -> ConfigurationConfirmation:
    """Apply transaction and configuration state changes in one database commit."""

    with db_transaction.atomic():
        process = DeploymentProcess.objects.select_for_update().get(
            pk=attempt.process_id
        )
        locked_attempt = DeploymentConfigurationAttempt.objects.select_for_update().get(
            pk=attempt.pk,
            process=process,
        )
        record = BlockchainTransaction.objects.select_for_update().get(
            pk=locked_attempt.transaction_id
        )
        confirmed_record = confirm_transaction(
            record,
            block_number,
            block_hash,
        )
        if locked_attempt.is_open:
            locked_attempt.is_open = False
            locked_attempt.save(update_fields=("is_open", "updated_at"))

        if (
            process.status != DeploymentProcess.Status.DEPLOYED
            or not event_valid
            or current_address != locked_attempt.expected_address
        ):
            if process.status != DeploymentProcess.Status.NEEDS_REVIEW:
                process.status = DeploymentProcess.Status.NEEDS_REVIEW
                process.save()
            return ConfigurationConfirmation(
                status="needs_review",
                attempt=locked_attempt,
                transaction=confirmed_record,
                expected_address=locked_attempt.expected_address,
                current_address=current_address,
            )

        return ConfigurationConfirmation(
            status="confirmed",
            attempt=locked_attempt,
            transaction=confirmed_record,
            expected_address=locked_attempt.expected_address,
            current_address=current_address,
        )


def confirm_deployment_configuration(
    attempt_id: int,
) -> ConfigurationConfirmation:
    """Check one setup receipt, event and final SojaCoin address on the RPC.

    Args:
        attempt_id: Primary key of the submitted configuration attempt.

    Returns:
        A typed pending, failed, confirmed or review result. The service never
        creates a second wallet transaction while checking a receipt.

    Raises:
        ValidationError: If the saved scope or RPC evidence is inconsistent.
        ConnectionError: If the configured RPC cannot be reached. The attempt
            remains available for the same query to be retried.
    """

    attempt = _get_attempt(attempt_id)
    process = attempt.process
    record = attempt.transaction
    spec = CONFIGURATION_SPECS.get(attempt.kind)
    if spec is None:
        raise ValidationError("Tipo de configuração inválido.")
    if (
        record.company_id != process.company_id
        or record.network_id != process.network_id
        or record.operation_type != CONTRACT_CONFIGURATION_OPERATION_TYPE
        or record.contract_id is None
        or record.contract.kind != ContractInstance.Kind.SOJA_COIN
    ):
        raise ValidationError("A tentativa não pertence ao processo ou à rede informada.")

    if record.status == BlockchainTransaction.Status.FAILED:
        return ConfigurationConfirmation(
            status="failed",
            attempt=attempt,
            transaction=record,
            expected_address=attempt.expected_address,
        )
    if record.status == BlockchainTransaction.Status.REORGED:
        return ConfigurationConfirmation(
            status="needs_review",
            attempt=attempt,
            transaction=record,
            expected_address=attempt.expected_address,
        )
    if record.status not in {
        BlockchainTransaction.Status.SUBMITTED,
        BlockchainTransaction.Status.CONFIRMED,
    }:
        raise ValidationError("Somente uma transação enviada pode ser consultada.")
    if not record.transaction_hash:
        raise ValidationError("A tentativa não tem uma hash para consultar.")

    web3 = connect_to_rpc(process.network)
    sojacoin_contract = bind_contract_to_web3(web3, record.contract)

    if record.status == BlockchainTransaction.Status.CONFIRMED:
        current_address = _read_contract_address(
            sojacoin_contract,
            spec.getter_name,
        )
        if current_address == attempt.expected_address:
            return ConfigurationConfirmation(
                status="confirmed",
                attempt=attempt,
                transaction=record,
                expected_address=attempt.expected_address,
                current_address=current_address,
            )
        _mark_process_for_review(process.pk)
        return ConfigurationConfirmation(
            status="needs_review",
            attempt=attempt,
            transaction=record,
            expected_address=attempt.expected_address,
            current_address=current_address,
        )

    try:
        chain_transaction = web3.eth.get_transaction(record.transaction_hash)
    except TransactionNotFound:
        return ConfigurationConfirmation(
            status="pending",
            attempt=attempt,
            transaction=record,
            expected_address=attempt.expected_address,
        )

    found_hash = normalize_transaction_hash(Web3.to_hex(chain_transaction["hash"]))
    if found_hash != record.transaction_hash:
        raise ValidationError("O RPC retornou uma transação com outra hash.")
    if normalize_wallet_address(str(chain_transaction["from"])) != record.sender_address:
        raise ValidationError("A transação foi enviada por outra carteira.")
    if normalize_wallet_address(str(chain_transaction.get("to") or "")) != (
        record.contract.address_normalized
    ):
        raise ValidationError("A transação chama outro contrato.")
    chain_id = chain_transaction.get("chainId")
    if chain_id is None or int(chain_id) != process.network.chain_id:
        raise ValidationError("A transação pertence a uma rede diferente do processo.")
    if int(chain_transaction.get("value", 0)) != 0:
        raise ValidationError("A chamada de configuração transferiu valor.")
    transaction_input = chain_transaction.get("input")
    if transaction_input is None:
        transaction_input = chain_transaction.get("data")
    if _transaction_data(transaction_input) != attempt.prepared_data:
        raise ValidationError("Os dados da transação diferem da chamada preparada.")

    try:
        receipt = web3.eth.get_transaction_receipt(record.transaction_hash)
    except TransactionNotFound:
        return ConfigurationConfirmation(
            status="pending",
            attempt=attempt,
            transaction=record,
            expected_address=attempt.expected_address,
        )

    receipt_hash = normalize_transaction_hash(Web3.to_hex(receipt["transactionHash"]))
    if receipt_hash != record.transaction_hash:
        raise ValidationError("O recibo retornado pelo RPC pertence a outra transação.")
    receipt_status = int(receipt["status"])
    if receipt_status == 0:
        failed_record = fail_transaction(
            record,
            "A chamada de configuração foi revertida na blockchain.",
        )
        attempt.refresh_from_db()
        return ConfigurationConfirmation(
            status="failed",
            attempt=attempt,
            transaction=failed_record,
            expected_address=attempt.expected_address,
        )
    if receipt_status != 1:
        raise ValidationError("O recibo retornado pelo RPC tem uma situação inválida.")
    if int(chain_transaction.get("value", 0)) != 0:
        raise ValidationError("A chamada de configuração transferiu valor.")

    block_number = receipt.get("blockNumber")
    block_hash_value = receipt.get("blockHash")
    if block_number is None or block_hash_value is None:
        raise ValidationError("O recibo de sucesso não contém o bloco completo.")

    event_valid = _decode_expected_event(
        sojacoin_contract,
        receipt,
        event_name=spec.event_name,
        address_argument=spec.event_address_argument,
        expected_address=attempt.expected_address,
        expected_sender=record.sender_address,
    )
    current_address = _read_contract_address(
        sojacoin_contract,
        spec.getter_name,
        block_number=int(block_number),
    )
    return _record_configuration_confirmation(
        attempt,
        block_number=int(block_number),
        block_hash=Web3.to_hex(block_hash_value),
        event_valid=event_valid,
        current_address=current_address,
    )
