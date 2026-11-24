"""Manage the local preparation and history of the nine-contract deployment.

The six SojaCoin modules have no constructor dependencies. SojaCoin is created
after those modules, and Unit Registry and Issuance Batches are created after
SojaCoin. This module checks that sequence and builds unsigned constructor data.
It does not send deployment transactions to a network.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from web3 import Web3
from web3.exceptions import TransactionNotFound, Web3Exception

from companies.models import Company
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
    Network,
)
from .contracts import load_contract_abi
from .rpc import connect_to_rpc
from .transactions import fail_transaction, submit_transaction


BYTECODE_DIRECTORY = Path(__file__).resolve().parent.parent / "bytecode"
# These implementations are constructor dependencies of the SojaCoin contract.
MODULES = (
    ContractInstance.Kind.SOJA_COIN_ADMINISTRATION,
    ContractInstance.Kind.SOJA_COIN_DEPOSITS,
    ContractInstance.Kind.SOJA_COIN_CPR,
    ContractInstance.Kind.SOJA_COIN_SERVICES,
    ContractInstance.Kind.SOJA_COIN_LOCKS,
    ContractInstance.Kind.SOJA_COIN_REDEMPTIONS,
)
# Keep this order aligned with the order displayed and stored in DeploymentStep.
DEPLOYMENT_ORDER = MODULES + (
    ContractInstance.Kind.SOJA_COIN,
    ContractInstance.Kind.UNIT_REGISTRY,
    ContractInstance.Kind.ISSUANCE_BATCHES,
)
# Use readable names in validation messages shown to platform operators.
DEPLOYMENT_KIND_LABELS = {
    ContractInstance.Kind.SOJA_COIN_ADMINISTRATION: "Módulo de administração",
    ContractInstance.Kind.SOJA_COIN_DEPOSITS: "Módulo de recebimentos",
    ContractInstance.Kind.SOJA_COIN_CPR: "Módulo de CPR",
    ContractInstance.Kind.SOJA_COIN_SERVICES: "Módulo de serviços",
    ContractInstance.Kind.SOJA_COIN_LOCKS: "Módulo de bloqueios",
    ContractInstance.Kind.SOJA_COIN_REDEMPTIONS: "Módulo de resgates",
    ContractInstance.Kind.SOJA_COIN: "SojaCoin",
    ContractInstance.Kind.UNIT_REGISTRY: "Registro de unidades",
    ContractInstance.Kind.ISSUANCE_BATCHES: "Lotes de emissão",
}


@dataclass(frozen=True)
class PreparedDeploymentAttempt:
    """Return the persisted attempt details needed by the browser wallet."""

    attempt_id: int
    chain_id: int
    transaction_data: str


def start_deployment_process(
    *, company: Company, network: Network, owner_address: str
) -> DeploymentProcess:
    """Create one local deployment process and its nine ordered steps.

    The caller must authorize the platform operator. Database row locks keep
    two requests from starting the same company's process at once. Existing
    active contracts and processes that are running or need review block a new
    process. The returned process contains no submitted blockchain transaction.

    Args:
        company: Company that will own the deployed contracts.
        network: EVM network where the contracts will be deployed.
        owner_address: Public wallet that the constructors will assign as owner.

    Returns:
        The saved process with its nine ordered steps.

    Raises:
        ValidationError: If the company or network is missing or inactive, if
            another process is still open, or if one of the contracts is active.
    """

    if company.pk is None or network.pk is None:
        raise ValidationError("A empresa e a rede devem estar cadastradas.")
    try:
        with transaction.atomic():
            # Lock both scope records so concurrent starts serialize per company/network.
            company = Company.objects.select_for_update().get(pk=company.pk)
            network = Network.objects.select_for_update().get(pk=network.pk)
            if not company.active or not network.active:
                raise ValidationError("A empresa e a rede devem estar ativas.")

            # A review process still occupies this scope until an operator resolves it.
            existing_process = DeploymentProcess.objects.filter(
                company=company,
                network=network,
                status__in=(
                    DeploymentProcess.Status.IN_PROGRESS,
                    DeploymentProcess.Status.NEEDS_REVIEW,
                ),
            ).first()
            if existing_process is not None:
                if existing_process.status == DeploymentProcess.Status.NEEDS_REVIEW:
                    raise ValidationError(
                        "Já existe um processo que precisa de revisão para esta empresa e rede."
                    )
                raise ValidationError(
                    "Já existe uma implantação em andamento para esta empresa e rede."
                )

            # Starting a new nine-contract rollout over an existing active contract
            # would later fail when that contract's deployment step is prepared.
            existing_contracts = list(
                ContractInstance.objects.filter(
                    company=company,
                    network=network,
                    active=True,
                    kind__in=DEPLOYMENT_ORDER,
                ).values_list("kind", flat=True)
            )
            if existing_contracts:
                labels = ", ".join(
                    DEPLOYMENT_KIND_LABELS[kind]
                    for kind in DEPLOYMENT_ORDER
                    if kind in existing_contracts
                )
                raise ValidationError(
                    "Já existem contratos ativos cadastrados para esta empresa e rede: "
                    f"{labels}. Confira esses registros antes de iniciar outra implantação."
                )

            process = DeploymentProcess.objects.create(
                company=company, network=network, owner_address=owner_address
            )
            for position, kind in enumerate(DEPLOYMENT_ORDER, start=1):
                DeploymentStep.objects.create(process=process, kind=kind, position=position)
            return process
    except (Company.DoesNotExist, Network.DoesNotExist) as error:
        raise ValidationError("Empresa ou rede não encontrada.") from error
    except IntegrityError as error:
        raise ValidationError("Não foi possível iniciar a implantação; verifique se já existe um processo em andamento.") from error


def cancel_deployment_process(process_id: int) -> DeploymentProcess:
    """Cancel a process only when every deployment attempt is still local.

    Prepared transaction records without a hash are safe to keep as history.
    A hash, submission timestamp, or submitted/confirmed/reorged status is
    evidence that a transaction may have reached the network, so cancellation
    must stop and leave the process available for review.

    Args:
        process_id: Primary key of the process to cancel.

    Returns:
        The canceled process. Repeating the request for an already canceled
        process returns the same record.

    Raises:
        ValidationError: If the ID is invalid, the process does not exist, the
            process is not running, or any attempt may have been broadcast.
    """

    if process_id < 1:
        raise ValidationError("O ID do processo deve ser um número positivo.")

    try:
        with transaction.atomic():
            process = DeploymentProcess.objects.select_for_update().get(pk=process_id)
            if process.status == DeploymentProcess.Status.CANCELED:
                return process
            if process.status != DeploymentProcess.Status.IN_PROGRESS:
                raise ValidationError("Somente uma implantação em andamento pode ser cancelada.")

            # Check both status and submission evidence. A mined transaction may
            # already be marked failed while its hash remains important history.
            sent_transaction_exists = DeploymentAttempt.objects.filter(
                step__process=process,
            ).filter(
                Q(transaction__status__in=(
                    BlockchainTransaction.Status.SUBMITTED,
                    BlockchainTransaction.Status.CONFIRMED,
                    BlockchainTransaction.Status.REORGED,
                ))
                | Q(transaction__transaction_hash__isnull=False)
                | Q(transaction__submitted_at__isnull=False)
                | Q(
                    submission_started_at__isnull=False,
                    transaction__status=BlockchainTransaction.Status.PREPARED,
                )
            ).exists()
            if sent_transaction_exists:
                raise ValidationError(
                    "Há uma transação enviada ou um envio à carteira sem resposta definitiva. "
                    "Confira a situação da tentativa antes de continuar."
                )

            process.status = DeploymentProcess.Status.CANCELED
            process.save()
            return process
    except DeploymentProcess.DoesNotExist as error:
        raise ValidationError("Processo de implantação não encontrado.") from error


def _required_dependency_kinds(kind: str) -> tuple[str, ...]:
    """List the contract kinds whose addresses belong in this constructor.

    The modules are separate implementation contracts and have no constructor
    arguments. SojaCoin receives all six module addresses. The two final
    registries each receive the SojaCoin address.
    """

    if kind in MODULES:
        return ()
    if kind == ContractInstance.Kind.SOJA_COIN:
        return MODULES
    if kind in {
        ContractInstance.Kind.UNIT_REGISTRY,
        ContractInstance.Kind.ISSUANCE_BATCHES,
    }:
        return (ContractInstance.Kind.SOJA_COIN,)
    raise ValidationError("Tipo de contrato inválido.")


def _get_confirmed_step_contract(step: DeploymentStep) -> ContractInstance:
    """Return the active contract recorded for a genuinely confirmed step.

    The contract's deployment hash must match a confirmed deployment attempt
    attached to this step, company, and network. This prevents a merely saved
    address or an unrelated transaction from being treated as deployment proof.
    """

    contract = step.contract
    if contract is None:
        raise ValidationError(
            f"A etapa {step.get_kind_display()} ainda não tem contrato confirmado."
        )
    if (
        contract.company_id != step.process.company_id
        or contract.network_id != step.process.network_id
        or contract.kind != step.kind
    ):
        raise ValidationError(
            f"O contrato da etapa {step.get_kind_display()} pertence a outra empresa, rede ou tipo."
        )
    if not contract.active:
        raise ValidationError(
            f"O contrato da etapa {step.get_kind_display()} está inativo."
        )
    if not contract.deployment_transaction_hash:
        raise ValidationError(
            f"O contrato da etapa {step.get_kind_display()} não tem hash de implantação."
        )

    # Match the contract address record to the exact confirmed deployment hash.
    has_confirmed_attempt = step.attempts.filter(
        transaction__company_id=step.process.company_id,
        transaction__network_id=step.process.network_id,
        transaction__operation_type=DEPLOYMENT_OPERATION_TYPE,
        transaction__status=BlockchainTransaction.Status.CONFIRMED,
        transaction__transaction_hash=contract.deployment_transaction_hash,
    ).exists()
    if not has_confirmed_attempt:
        raise ValidationError(
            f"A transação de implantação da etapa {step.get_kind_display()} não está confirmada."
        )
    return contract


def _get_process_dependency_addresses(
    process: DeploymentProcess,
    dependency_kinds: tuple[str, ...],
) -> dict[str, str]:
    """Return checksum addresses for confirmed dependencies in this process.

    Filtering by both process and contract kind is important. Another company
    or another deployment process may have a contract of the same kind, but its
    address must never be used to build this process's constructor data.
    """

    addresses: dict[str, str] = {}
    for kind in dependency_kinds:
        try:
            dependency_step = DeploymentStep.objects.select_related(
                "process", "contract"
            ).get(process=process, kind=kind)
        except DeploymentStep.DoesNotExist as error:
            raise ValidationError(
                f"A etapa necessária {DEPLOYMENT_KIND_LABELS[kind]} não existe neste processo."
            ) from error

        contract = _get_confirmed_step_contract(dependency_step)
        addresses[kind] = Web3.to_checksum_address(contract.address_normalized)
    return addresses


def prepare_deployment_step(step_id: int) -> str:
    """Validate one deployment step and return its unsigned constructor data.

    All scope values come from saved database records. The service checks the
    process status, company and network, the step's place in the deployment
    order, and the confirmation evidence for earlier steps. It then supplies
    the constructor builder with only the verified dependencies for this process.

    The returned string is EVM transaction data for contract creation. This
    function does not save a transaction attempt or change process progress.

    Args:
        step_id: Primary key of the DeploymentStep to prepare.

    Returns:
        Hexadecimal contract-creation data ready for a later signing step.

    Raises:
        ValidationError: If the step or process is unavailable, a prerequisite
            is incomplete, the process is inactive, or its artifacts are invalid.
    """

    if step_id < 1:
        raise ValidationError("O ID da etapa deve ser um número positivo.")

    try:
        # Load the saved process context so callers cannot choose another scope.
        step = DeploymentStep.objects.select_related(
            "process__company", "process__network", "contract"
        ).get(pk=step_id)
    except DeploymentStep.DoesNotExist as error:
        raise ValidationError("Etapa de implantação não encontrada.") from error

    process = step.process
    if process.status != DeploymentProcess.Status.IN_PROGRESS:
        raise ValidationError("O processo precisa estar em andamento para preparar esta etapa.")
    if not process.company.active:
        raise ValidationError("A empresa do processo está inativa.")
    if not process.network.active:
        raise ValidationError("A rede do processo está inativa.")
    if step.kind not in DEPLOYMENT_ORDER:
        raise ValidationError("O tipo da etapa não pertence à implantação dos nove contratos.")

    # An already linked confirmed contract must never be prepared a second time.
    if step.contract_id:
        _get_confirmed_step_contract(step)
        raise ValidationError("Esta etapa já tem um contrato confirmado.")

    # The process order is also the release order, even for modules without
    # constructor dependencies, so an operator cannot skip an earlier step.
    step_index = DEPLOYMENT_ORDER.index(step.kind)
    # A module has no constructor dependency on the prior modules, but the Admin
    # workflow intentionally releases deployment steps in one fixed sequence.
    previous_steps = list(
        DeploymentStep.objects.filter(
            process=process,
            position__lt=step.position,
        )
        .select_related("process", "contract")
        .prefetch_related("attempts__transaction")
        .order_by("position")
    )
    # Also reject a damaged process with a missing or misordered step.
    expected_previous_kinds = DEPLOYMENT_ORDER[:step_index]
    if tuple(previous.kind for previous in previous_steps) != expected_previous_kinds:
        raise ValidationError("A ordem das etapas do processo está incompleta ou inválida.")
    for previous_step in previous_steps:
        _get_confirmed_step_contract(previous_step)

    dependency_kinds = _required_dependency_kinds(step.kind)
    dependency_addresses = _get_process_dependency_addresses(process, dependency_kinds)
    return prepare_deployment_data(
        company=process.company,
        network=process.network,
        kind=step.kind,
        owner_address=process.owner_address,
        dependency_addresses=dependency_addresses,
    )


def start_deployment_attempt(
    *,
    step_id: int,
    sender_address: str,
    wallet_chain_id: int,
) -> PreparedDeploymentAttempt:
    """Prepare one step and save its transaction and attempt for MetaMask.

    The step row is locked while checking for an existing open attempt. A
    conditional unique constraint provides a second guard against concurrent
    requests. An untouched attempt from the same wallet is returned again, so
    a lost HTTP response does not create a duplicate record.

    Args:
        step_id: Primary key of the deployment step to prepare.
        sender_address: Public wallet address that will submit the deployment.
        wallet_chain_id: Chain ID currently selected in the browser wallet.

    Returns:
        The attempt ID, expected network ID and exact creation data to sign.

    Raises:
        ValidationError: If the step is unavailable, the wallet is invalid or
            on the wrong network, or another attempt is already in progress.
    """

    sender = normalize_wallet_address(sender_address)
    if not is_valid_wallet_address(sender) or sender == "0x" + "0" * 40:
        raise ValidationError("Conecte uma carteira válida para continuar.")
    if not isinstance(wallet_chain_id, int) or wallet_chain_id < 1:
        raise ValidationError("Não foi possível identificar a rede da carteira.")
    if step_id < 1:
        raise ValidationError("O ID da etapa deve ser um número positivo.")

    try:
        # Read the process key first, then lock process -> step consistently with
        # cancellation and the wallet-send marker service.
        step_scope = DeploymentStep.objects.only("process_id").get(pk=step_id)
    except DeploymentStep.DoesNotExist as error:
        raise ValidationError("Etapa de implantação não encontrada.") from error

    try:
        with transaction.atomic():
            process = (
                DeploymentProcess.objects.select_for_update()
                .select_related("company", "network")
                .get(pk=step_scope.process_id)
            )
            # Serialize attempts for this exact step before checking its history.
            step = DeploymentStep.objects.select_for_update().get(
                pk=step_id,
                process=process,
            )
            if wallet_chain_id != process.network.chain_id:
                raise ValidationError(
                    f"A carteira está na rede {wallet_chain_id}, mas o processo usa "
                    f"{process.network.name} (ID {process.network.chain_id})."
                )

            creation_data = prepare_deployment_step(step.pk)
            active_attempt = (
                DeploymentAttempt.objects.select_for_update()
                .select_related("transaction")
                .filter(step=step)
                .filter(
                    Q(is_open=True)
                    | Q(
                        transaction__status__in=(
                            BlockchainTransaction.Status.PREPARED,
                            BlockchainTransaction.Status.SUBMITTED,
                            BlockchainTransaction.Status.CONFIRMED,
                        )
                    )
                )
                .order_by("-created_at", "-pk")
                .first()
            )
            if active_attempt is not None:
                can_resume_before_wallet_prompt = (
                    active_attempt.transaction.status
                    == BlockchainTransaction.Status.PREPARED
                    and active_attempt.submission_started_at is None
                    and active_attempt.transaction.sender_address == sender
                    and active_attempt.prepared_data == creation_data.lower()
                )
                if can_resume_before_wallet_prompt:
                    return PreparedDeploymentAttempt(
                        attempt_id=active_attempt.pk,
                        chain_id=process.network.chain_id,
                        transaction_data=active_attempt.prepared_data,
                    )
                raise ValidationError(
                    "Esta etapa já tem uma tentativa que precisa ser conferida. "
                    "Use a mesma tentativa antes de iniciar outro envio."
                )

            record = BlockchainTransaction.objects.create(
                company=process.company,
                network=process.network,
                operation_type=DEPLOYMENT_OPERATION_TYPE,
                sender_address=sender,
            )
            attempt = DeploymentAttempt.objects.create(
                step=step,
                transaction=record,
                prepared_data=creation_data,
                is_open=True,
            )
            return PreparedDeploymentAttempt(
                attempt_id=attempt.pk,
                chain_id=process.network.chain_id,
                transaction_data=creation_data,
            )
    except DeploymentStep.DoesNotExist as error:
        raise ValidationError("Etapa de implantação não encontrada.") from error
    except DeploymentProcess.DoesNotExist as error:
        raise ValidationError("Processo de implantação não encontrado.") from error
    except IntegrityError as error:
        raise ValidationError(
            "Já existe uma tentativa em andamento para esta etapa."
        ) from error


def mark_deployment_submission_started(attempt_id: int) -> DeploymentAttempt:
    """Persist the wallet-send intent before asking MetaMask to broadcast.

    This marker is deliberately written before the browser opens the send
    confirmation. If the browser loses its response, the same attempt remains
    open and neither the process nor the step can be treated as unsent.

    Args:
        attempt_id: Primary key of the saved deployment attempt.

    Returns:
        The updated attempt with its first submission-intent timestamp.

    Raises:
        ValidationError: If the attempt is not open, the process is no longer
            active, or a wallet-send prompt may already have been opened.
    """

    if attempt_id < 1:
        raise ValidationError("O ID da tentativa deve ser um número positivo.")
    try:
        attempt_scope = DeploymentAttempt.objects.select_related("step").get(pk=attempt_id)
    except DeploymentAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de implantação não encontrada.") from error

    try:
        with transaction.atomic():
            process = (
                DeploymentProcess.objects.select_for_update()
                .select_related("company", "network")
                .get(pk=attempt_scope.step.process_id)
            )
            DeploymentStep.objects.select_for_update().get(
                pk=attempt_scope.step_id,
                process=process,
            )
            attempt = (
                DeploymentAttempt.objects.select_for_update()
                .select_related("transaction")
                .get(pk=attempt_id)
            )
            if not attempt.is_open:
                raise ValidationError("Esta tentativa já foi encerrada.")
            if attempt.transaction.status != BlockchainTransaction.Status.PREPARED:
                raise ValidationError("Esta tentativa não está mais preparada para envio.")
            if attempt.submission_started_at is not None:
                raise ValidationError(
                    "O pedido de envio já foi iniciado. Confira a carteira e informe a hash "
                    "da mesma tentativa; não envie outra transação."
                )
            if process.status != DeploymentProcess.Status.IN_PROGRESS:
                raise ValidationError("O processo não está mais em andamento.")
            if not process.company.active or not process.network.active:
                raise ValidationError("A empresa e a rede precisam estar ativas.")

            attempt.submission_started_at = timezone.now()
            attempt.save(update_fields=("submission_started_at", "updated_at"))
            return attempt
    except DeploymentAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de implantação não encontrada.") from error
    except DeploymentProcess.DoesNotExist as error:
        raise ValidationError("Processo de implantação não encontrado.") from error
    except DeploymentStep.DoesNotExist as error:
        raise ValidationError("Etapa de implantação não encontrada.") from error


def fail_deployment_attempt(attempt_id: int) -> DeploymentAttempt:
    """Record an explicit MetaMask rejection and release the step for retry.

    Call this only when the wallet returned its explicit user-rejected error.
    Other wallet or network errors are ambiguous and must leave the attempt open.

    Args:
        attempt_id: Primary key of the attempt rejected by the operator.

    Returns:
        The preserved attempt, now linked to a failed transaction.

    Raises:
        ValidationError: If a send was not started or the attempt is no longer
            eligible for an explicit rejection result.
    """

    if attempt_id < 1:
        raise ValidationError("O ID da tentativa deve ser um número positivo.")
    try:
        attempt_scope = DeploymentAttempt.objects.select_related("step").get(pk=attempt_id)
    except DeploymentAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de implantação não encontrada.") from error

    try:
        with transaction.atomic():
            process = DeploymentProcess.objects.select_for_update().get(
                pk=attempt_scope.step.process_id
            )
            DeploymentStep.objects.select_for_update().get(
                pk=attempt_scope.step_id,
                process=process,
            )
            attempt = (
                DeploymentAttempt.objects.select_for_update()
                .select_related("transaction")
                .get(pk=attempt_id)
            )
            if process.status != DeploymentProcess.Status.IN_PROGRESS:
                raise ValidationError("O processo não está mais em andamento.")
            if not attempt.is_open:
                return attempt
            if attempt.submission_started_at is None:
                raise ValidationError("O pedido da carteira ainda não foi iniciado.")
            if attempt.transaction.status != BlockchainTransaction.Status.PREPARED:
                raise ValidationError("A tentativa já tem um resultado registrado.")

            fail_transaction(
                attempt.transaction,
                "O operador rejeitou o envio na carteira.",
            )
            attempt.refresh_from_db()
            return attempt
    except DeploymentAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de implantação não encontrada.") from error
    except DeploymentProcess.DoesNotExist as error:
        raise ValidationError("Processo de implantação não encontrado.") from error
    except DeploymentStep.DoesNotExist as error:
        raise ValidationError("Etapa de implantação não encontrada.") from error


def _transaction_creation_data(value: object) -> str:
    """Return an RPC transaction's input field as normalized hexadecimal."""

    if isinstance(value, (bytes, bytearray)):
        return Web3.to_hex(value).lower()
    return str(value or "").lower()


def register_deployment_transaction_hash(
    *,
    attempt_id: int,
    transaction_hash: str,
) -> BlockchainTransaction | None:
    """Verify a wallet hash on the saved network and record its submission.

    The browser may repeat this call with the same hash. The attempt remains
    open when the RPC has not indexed the transaction yet or when a database
    write fails, so the operator can retry the same hash without creating a new
    wallet transaction.

    Args:
        attempt_id: Primary key of the saved deployment attempt.
        transaction_hash: Public transaction hash returned by MetaMask.

    Returns:
        The submitted transaction, or ``None`` while the selected RPC cannot
        find the hash yet.

    Raises:
        ValidationError: If the hash is invalid, belongs to another sender or
            network, targets a contract instead of creating one, or contains
            data different from the payload prepared for this attempt.
    """

    canonical_hash = normalize_transaction_hash(transaction_hash)
    if not is_valid_transaction_hash(canonical_hash):
        raise ValidationError("A hash informada pela carteira é inválida.")
    if attempt_id < 1:
        raise ValidationError("O ID da tentativa deve ser um número positivo.")

    try:
        attempt = DeploymentAttempt.objects.select_related(
            "transaction", "step__process__company", "step__process__network"
        ).get(pk=attempt_id)
    except DeploymentAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de implantação não encontrada.") from error

    record = attempt.transaction
    process = attempt.step.process
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("A tentativa não está aguardando uma resposta da carteira.")
    if process.status != DeploymentProcess.Status.IN_PROGRESS:
        raise ValidationError("O processo não está mais em andamento.")
    if not process.company.active or not process.network.active:
        raise ValidationError("A empresa e a rede precisam estar ativas.")
    if not attempt.prepared_data:
        raise ValidationError("A tentativa não contém os dados preparados para conferência.")
    if record.transaction_hash and record.transaction_hash != canonical_hash:
        raise ValidationError("Esta tentativa já está vinculada a outra hash.")

    # The endpoint comes from the process's saved network, never browser input.
    web3 = connect_to_rpc(process.network)
    try:
        chain_transaction = web3.eth.get_transaction(canonical_hash)
    except TransactionNotFound:
        return None

    found_hash = Web3.to_hex(chain_transaction["hash"]).lower()
    if found_hash != canonical_hash:
        raise ValidationError("O RPC retornou uma transação com outra hash.")
    if normalize_wallet_address(str(chain_transaction["from"])) != record.sender_address:
        raise ValidationError("A transação foi enviada por outra carteira.")
    if chain_transaction.get("to") is not None:
        raise ValidationError("A transação informada não cria um contrato.")
    chain_id = chain_transaction.get("chainId")
    if chain_id is None or int(chain_id) != process.network.chain_id:
        raise ValidationError("A transação pertence a uma rede diferente do processo.")
    creation_data = _transaction_creation_data(
        chain_transaction.get("input", chain_transaction.get("data"))
    )
    if creation_data != attempt.prepared_data:
        raise ValidationError(
            "Os dados enviados à carteira não correspondem aos dados preparados para esta etapa."
        )

    # Keep hash persistence and the lifecycle transition in one commit. If the
    # database fails, the attempt remains open for the operator to retry this hash.
    with transaction.atomic():
        locked_process = (
            DeploymentProcess.objects.select_for_update()
            .select_related("company", "network")
            .get(pk=process.pk)
        )
        DeploymentStep.objects.select_for_update().get(
            pk=attempt.step_id,
            process=locked_process,
        )
        locked_attempt = (
            DeploymentAttempt.objects.select_for_update()
            .select_related("transaction")
            .get(pk=attempt.pk)
        )
        locked_record = BlockchainTransaction.objects.select_for_update().get(
            pk=locked_attempt.transaction_id
        )
        if locked_record.status == BlockchainTransaction.Status.SUBMITTED:
            if locked_record.transaction_hash == canonical_hash:
                return locked_record
            raise ValidationError("Esta tentativa já está vinculada a outra hash.")
        if locked_record.status != BlockchainTransaction.Status.PREPARED:
            raise ValidationError("Esta tentativa não está mais preparada para envio.")
        if not locked_attempt.is_open or locked_attempt.submission_started_at is None:
            raise ValidationError("A tentativa não está mais aguardando uma resposta da carteira.")
        if locked_process.status != DeploymentProcess.Status.IN_PROGRESS:
            raise ValidationError("O processo não está mais em andamento.")
        if not locked_process.company.active or not locked_process.network.active:
            raise ValidationError("A empresa e a rede precisam estar ativas.")
        return submit_transaction(locked_record, canonical_hash)


def prepare_deployment_data(
    *,
    company: Company,
    network: Network,
    kind: str,
    owner_address: str,
    dependency_addresses: Mapping[str, str],
) -> str:
    """Encode constructor arguments using addresses verified by the caller.

    ``dependency_addresses`` must contain exactly the kinds returned by
    ``_required_dependency_kinds``. This function deliberately accepts those
    addresses as input instead of searching all ContractInstance records. The
    caller can therefore tie every constructor address to the selected process.

    Args:
        company: Active company from the saved process.
        network: Active network from the saved process.
        kind: Contract kind whose creation data is being built.
        owner_address: Public owner wallet saved on the process.
        dependency_addresses: Verified addresses keyed by contract kind.

    Returns:
        Hexadecimal contract-creation data encoded with the packaged ABI.

    Raises:
        ValidationError: If the scope, owner, dependencies, bytecode, or ABI is
            invalid, or if this contract kind is already active for the scope.
    """

    # Recheck mutable scope and duplicate-contract rules at the data-building boundary.
    if not Company.objects.filter(pk=company.pk, active=True).exists():
        raise ValidationError("Empresa inexistente ou inativa.")
    if not Network.objects.filter(pk=network.pk, active=True).exists():
        raise ValidationError("Rede inexistente ou inativa.")
    if kind not in ContractInstance.Kind.values:
        raise ValidationError("Tipo de contrato inválido.")
    owner = normalize_wallet_address(owner_address)
    if not is_valid_wallet_address(owner) or owner == "0x" + "0" * 40:
        raise ValidationError("Informe um endereço válido para o dono.")
    if ContractInstance.objects.filter(
        company=company, network=network, kind=kind, active=True
    ).exists():
        raise ValidationError("Este contrato já está cadastrado para a empresa.")

    # Require an exact dependency set so a missing or unexpected constructor
    # address cannot silently change the arguments being encoded.
    required_kinds = _required_dependency_kinds(kind)
    if set(dependency_addresses) != set(required_kinds):
        raise ValidationError("As dependências informadas para o contrato estão incompletas.")

    def dependency_address(dependency_kind: str) -> str:
        """Validate and normalize one address from the selected process."""

        address = normalize_wallet_address(dependency_addresses[dependency_kind])
        if not is_valid_wallet_address(address):
            raise ValidationError(
                f"O endereço da dependência {DEPLOYMENT_KIND_LABELS[dependency_kind]} é inválido."
            )
        return Web3.to_checksum_address(address)

    # Keep these argument layouts aligned with each Solidity constructor.
    if kind in MODULES:
        arguments: list[str] = []
    elif kind == ContractInstance.Kind.SOJA_COIN:
        arguments = [Web3.to_checksum_address(owner)] + [
            dependency_address(module) for module in MODULES
        ]
    elif kind == ContractInstance.Kind.UNIT_REGISTRY:
        arguments = [
            Web3.to_checksum_address(owner),
            dependency_address(ContractInstance.Kind.SOJA_COIN),
        ]
    else:
        arguments = [dependency_address(ContractInstance.Kind.SOJA_COIN)]

    # The filename uses only a previously validated enum value from ContractInstance.Kind.
    bytecode_path = BYTECODE_DIRECTORY / f"{kind}.json"
    # ABI and bytecode turn constructor arguments into the data a wallet can sign later.
    try:
        bytecode = json.loads(bytecode_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError("Código de implantação indisponível.") from error
    if not isinstance(bytecode, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", bytecode):
        raise ValidationError("Código de implantação inválido.")

    try:
        abi = load_contract_abi(kind)
        contract = Web3().eth.contract(abi=abi, bytecode=bytecode)
        return contract.constructor(*arguments).data_in_transaction
    except ImproperlyConfigured as error:
        raise ValidationError("ABI do contrato indisponível ou inválida.") from error
    except (TypeError, ValueError, Web3Exception) as error:
        raise ValidationError("Não foi possível montar os dados de implantação do contrato.") from error
