"""Confirm contract deployments and save their addresses from RPC receipts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction as db_transaction
from web3 import Web3
from web3.exceptions import TransactionNotFound

from utils.identifiers import (
    is_valid_transaction_hash,
    is_valid_wallet_address,
    normalize_transaction_hash,
    normalize_wallet_address,
)

from ..models import (
    DEPLOYMENT_OPERATION_TYPE,
    BlockchainTransaction,
    ContractInstance,
    DeploymentAttempt,
    DeploymentProcess,
    DeploymentStep,
)
from .deployment import BYTECODE_DIRECTORY, DEPLOYMENT_ORDER
from .rpc import connect_to_rpc
from .transactions import confirm_transaction, fail_transaction
from .contract_registration import register_contract


ConfirmationStatus = Literal["pending", "failed", "confirmed"]


@dataclass(frozen=True)
class DeploymentConfirmationResult:
    """Describe the current result of checking one deployment attempt."""

    status: ConfirmationStatus
    transaction: BlockchainTransaction
    contract: ContractInstance | None = None


def _creation_data(value: object) -> str:
    """Convert an RPC transaction input into normalized hexadecimal data."""

    if isinstance(value, str):
        normalized = value.lower()
    elif isinstance(value, (bytes, bytearray)):
        normalized = Web3.to_hex(value).lower()
    else:
        raise ValidationError("O RPC retornou dados de criação inválidos.")

    if not re.fullmatch(r"0x(?:[0-9a-f]{2})+", normalized):
        raise ValidationError("O RPC retornou dados de criação inválidos.")
    return normalized


def _rpc_hex(value: object) -> str:
    """Normalize an RPC byte string or hexadecimal string."""

    if isinstance(value, str):
        return value.lower()
    if isinstance(value, (bytes, bytearray)):
        return Web3.to_hex(value).lower()
    raise ValidationError("O RPC retornou um valor hexadecimal inválido.")


def _artifact_version(kind: str) -> str:
    """Identify the exact packaged creation bytecode used for this contract."""

    bytecode_path = BYTECODE_DIRECTORY / f"{kind}.json"
    try:
        artifact_bytes = bytecode_path.read_bytes()
    except OSError as error:
        raise ImproperlyConfigured(
            f"Could not read deployment bytecode: {bytecode_path.name}."
        ) from error

    # A content hash ties the saved version to the artifact actually shipped.
    return f"sha256:{hashlib.sha256(artifact_bytes).hexdigest()}"


def _get_attempt(attempt_id: int) -> DeploymentAttempt:
    """Load an attempt with the process scope needed for safe RPC checks."""

    if attempt_id < 1:
        raise ValidationError("O ID da tentativa deve ser positivo.")
    try:
        return DeploymentAttempt.objects.select_related(
            "transaction",
            "step__process__company",
            "step__process__network",
            "step__contract",
        ).get(pk=attempt_id)
    except DeploymentAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de implantação não encontrada.") from error


def _validate_attempt_scope(attempt: DeploymentAttempt) -> None:
    """Ensure every saved record belongs to the same deployment scope."""

    process = attempt.step.process
    record = attempt.transaction
    if (
        record.company_id != process.company_id
        or record.network_id != process.network_id
        or record.operation_type != DEPLOYMENT_OPERATION_TYPE
    ):
        raise ValidationError(
            "A tentativa e a transação não pertencem à empresa e rede do processo."
        )
    if attempt.step.kind not in DEPLOYMENT_ORDER:
        raise ValidationError("A etapa não possui um tipo de contrato válido.")


def _complete_process_after_final_deploy(process: DeploymentProcess) -> None:
    """Mark the process deployed only after its exact nine steps are confirmed.

    The caller must have saved the last confirmed transaction and linked its
    contract before invoking this helper. The model's ``clean`` method checks
    the confirmed count again when saving the new process status.
    """

    if process.status != DeploymentProcess.Status.IN_PROGRESS:
        return

    saved_kinds = tuple(
        process.steps.order_by("position").values_list("kind", flat=True)
    )
    if saved_kinds != DEPLOYMENT_ORDER:
        raise ValidationError(
            "A implantação não pode ser concluída: a ordem das nove etapas está incompleta."
        )

    # Count from the database after the final link, so a hash alone cannot
    # complete the process and a reorged transaction does not count as ready.
    if process.confirmed_step_count == len(DEPLOYMENT_ORDER):
        process.status = DeploymentProcess.Status.DEPLOYED
        process.save()


def confirm_deployment_attempt(
    attempt_id: int,
) -> DeploymentConfirmationResult:
    """Confirm a submitted contract creation and link its address to the step.

    RPC reads happen before database locks are acquired. A short database
    transaction then records the confirmation, contract and step together.
    Repeating this service call returns the contract already linked to the step.

    Args:
        attempt_id: Primary key of the saved deployment attempt.

    Returns:
        A pending or failed transaction result, or a confirmed result containing
        the saved contract instance.

    Raises:
        ValidationError: If the saved scope, RPC transaction, receipt, created
            code or existing local records do not match the prepared deployment.
    """

    attempt = _get_attempt(attempt_id)
    _validate_attempt_scope(attempt)
    process = attempt.step.process
    record = attempt.transaction
    step = attempt.step

    if record.status == BlockchainTransaction.Status.CONFIRMED:
        contract = step.contract
        if (
            contract is None
            or contract.company_id != process.company_id
            or contract.network_id != process.network_id
            or contract.kind != step.kind
            or contract.deployment_transaction_hash != record.transaction_hash
        ):
            raise ValidationError(
                "A transação está confirmada, mas o contrato ligado à etapa diverge. Revise o processo."
            )
        return DeploymentConfirmationResult("confirmed", record, contract)

    if record.status != BlockchainTransaction.Status.SUBMITTED:
        raise ValidationError("Somente uma transação enviada pode ser consultada.")
    if process.status != DeploymentProcess.Status.IN_PROGRESS:
        raise ValidationError("O processo não está em andamento.")
    if not process.company.active or not process.network.active:
        raise ValidationError("A empresa e a rede precisam estar ativas.")
    if not record.transaction_hash or not attempt.prepared_data:
        raise ValidationError("A tentativa não possui hash ou dados preparados.")
    if not is_valid_transaction_hash(record.transaction_hash):
        raise ValidationError("A hash registrada para a tentativa é inválida.")

    web3 = connect_to_rpc(process.network)
    try:
        chain_transaction = web3.eth.get_transaction(record.transaction_hash)
    except TransactionNotFound:
        return DeploymentConfirmationResult("pending", record)

    chain_hash = normalize_transaction_hash(_rpc_hex(chain_transaction["hash"]))
    if chain_hash != record.transaction_hash:
        raise ValidationError("A transação retornada pelo RPC tem outra hash.")
    if normalize_wallet_address(str(chain_transaction["from"])) != record.sender_address:
        raise ValidationError("A transação foi enviada por outra carteira.")
    if chain_transaction.get("to") is not None:
        raise ValidationError("A transação registrada não é uma criação de contrato.")
    chain_id = chain_transaction.get("chainId")
    if chain_id is None or int(chain_id) != process.network.chain_id:
        raise ValidationError("A transação pertence a outra rede.")
    transaction_input = chain_transaction.get("input")
    if transaction_input is None:
        transaction_input = chain_transaction.get("data")
    if _creation_data(transaction_input) != attempt.prepared_data:
        raise ValidationError("Os dados da transação diferem dos dados preparados.")

    try:
        receipt = web3.eth.get_transaction_receipt(record.transaction_hash)
    except TransactionNotFound:
        return DeploymentConfirmationResult("pending", record)

    receipt_hash = normalize_transaction_hash(_rpc_hex(receipt["transactionHash"]))
    if receipt_hash != record.transaction_hash:
        raise ValidationError("O recibo retornado pelo RPC pertence a outra transação.")
    if int(receipt["status"]) == 0:
        failed_record = fail_transaction(
            record,
            "A transação de criação do contrato foi revertida na rede.",
        )
        return DeploymentConfirmationResult("failed", failed_record)
    if int(receipt["status"]) != 1:
        raise ValidationError("O recibo retornado pelo RPC tem uma situação inválida.")

    block_number = receipt.get("blockNumber")
    block_hash = receipt.get("blockHash")
    contract_address = receipt.get("contractAddress")
    if block_number is None or block_hash is None:
        raise ValidationError("O recibo de sucesso não contém o bloco completo.")
    address = normalize_wallet_address(str(contract_address or ""))
    if not is_valid_wallet_address(address) or address == "0x" + "0" * 40:
        raise ValidationError("O recibo de sucesso não contém um endereço de contrato válido.")

    deployed_code = web3.eth.get_code(
        Web3.to_checksum_address(address),
        block_identifier=int(block_number),
    )
    if not deployed_code:
        raise ValidationError("O endereço criado não contém código no bloco confirmado.")
    artifact_version = _artifact_version(step.kind)

    with db_transaction.atomic():
        # Lock the process first. All deployment mutations for this process use
        # this lock as their serialization point before touching child records.
        locked_process = DeploymentProcess.objects.select_for_update().get(pk=process.pk)
        locked_step = DeploymentStep.objects.select_for_update().get(
            pk=step.pk,
            process=locked_process,
        )
        locked_attempt = DeploymentAttempt.objects.select_for_update().get(
            pk=attempt.pk,
            step=locked_step,
        )
        locked_record = BlockchainTransaction.objects.select_for_update().get(
            pk=record.pk,
        )

        if locked_record.status == BlockchainTransaction.Status.CONFIRMED:
            if (
                locked_step.contract_id
                and locked_step.contract.deployment_transaction_hash
                == locked_record.transaction_hash
            ):
                return DeploymentConfirmationResult(
                    "confirmed", locked_record, locked_step.contract
                )
            raise ValidationError(
                "A confirmação concorrente deixou registros divergentes. Revise o processo."
            )
        if (
            locked_process.status != DeploymentProcess.Status.IN_PROGRESS
            or locked_record.status != BlockchainTransaction.Status.SUBMITTED
        ):
            raise ValidationError("O estado do processo mudou durante a consulta.")
        if locked_step.contract_id:
            raise ValidationError("A etapa já possui um contrato ligado. Revise o processo.")

        confirmed_record = confirm_transaction(
            locked_record,
            int(block_number),
            _rpc_hex(block_hash),
        )
        contract = register_contract(
            company=locked_process.company,
            network=locked_process.network,
            kind=locked_step.kind,
            address=address,
            version=artifact_version,
            deployment_transaction_hash=confirmed_record.transaction_hash or "",
        )
        locked_step.contract = contract
        locked_step.save(update_fields=("contract", "updated_at"))
        if locked_attempt.is_open:
            locked_attempt.is_open = False
            locked_attempt.save(update_fields=("is_open", "updated_at"))

        # Saving DEPLOYED runs the model validation that independently requires
        # all nine linked contracts to have confirmed deployment transactions.
        _complete_process_after_final_deploy(locked_process)

        return DeploymentConfirmationResult("confirmed", confirmed_record, contract)
