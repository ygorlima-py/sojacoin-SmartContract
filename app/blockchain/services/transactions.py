"""Explicit state transitions for blockchain transaction records."""

from __future__ import annotations

from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.utils import timezone

from ..models import (
    CONTRACT_CONFIGURATION_OPERATION_TYPE,
    DEPLOYMENT_OPERATION_TYPE,
    BlockchainTransaction,
    DeploymentAttempt,
    DeploymentConfigurationAttempt,
    DeploymentProcess,
)
from utils.identifiers import normalize_transaction_hash


def _lock_transaction(record: BlockchainTransaction) -> BlockchainTransaction:
    """Reload a transaction under a row lock before changing its state."""

    if record.pk is None:
        raise ValidationError("The transaction must be saved before a state transition.")
    return BlockchainTransaction.objects.select_for_update().get(pk=record.pk)


def _lock_deployment_process(record: BlockchainTransaction) -> DeploymentProcess | None:
    """Lock the related process before its attempt or transaction row."""

    attempt = (
        DeploymentAttempt.objects.select_related("step")
        .filter(transaction_id=record.pk)
        .first()
    )
    if attempt is None:
        configuration_attempt = (
            DeploymentConfigurationAttempt.objects.select_related("process")
            .filter(transaction_id=record.pk)
            .first()
        )
        if configuration_attempt is None:
            return None
        process_id = configuration_attempt.process_id
    else:
        process_id = attempt.step.process_id
    return DeploymentProcess.objects.select_for_update().get(pk=process_id)


def _lock_deployment_attempt(
    record: BlockchainTransaction,
) -> DeploymentAttempt | None:
    """Lock the attempt after its process row has been locked."""

    return (
        DeploymentAttempt.objects.select_for_update()
        .select_related("step")
        .filter(transaction_id=record.pk)
        .first()
    )


def _lock_configuration_attempt(
    record: BlockchainTransaction,
) -> DeploymentConfigurationAttempt | None:
    """Lock a setup attempt after its deployment process has been locked."""

    return (
        DeploymentConfigurationAttempt.objects.select_for_update()
        .filter(transaction_id=record.pk)
        .first()
    )


def _mark_deployment_process_for_review(record: BlockchainTransaction) -> None:
    """Keep a deployment process from appearing complete after a reorganization."""

    attempt = (
        DeploymentAttempt.objects.select_related("step")
        .filter(transaction_id=record.pk)
        .first()
    )
    if attempt is not None:
        process_id = attempt.step.process_id
    else:
        configuration_attempt = (
            DeploymentConfigurationAttempt.objects.select_related("process")
            .filter(transaction_id=record.pk)
            .first()
        )
        if configuration_attempt is None:
            return
        process_id = configuration_attempt.process_id

    process = DeploymentProcess.objects.select_for_update().get(pk=process_id)
    if process.status in {
        DeploymentProcess.Status.IN_PROGRESS,
        DeploymentProcess.Status.DEPLOYED,
    }:
        process.status = DeploymentProcess.Status.NEEDS_REVIEW
        process.save()


def submit_transaction(
    record: BlockchainTransaction,
    transaction_hash: str,
    *,
    submitted_at: datetime | None = None,
) -> BlockchainTransaction:
    """Move a prepared transaction to submitted after receiving its hash."""

    canonical_hash = normalize_transaction_hash(transaction_hash)
    with db_transaction.atomic():
        # Both deployment and setup use process -> attempt -> transaction order.
        process = _lock_deployment_process(record)
        _deployment_attempt = (
            _lock_deployment_attempt(record) if process is not None else None
        )
        configuration_attempt = (
            _lock_configuration_attempt(record) if process is not None else None
        )
        locked = _lock_transaction(record)
        if locked.status == BlockchainTransaction.Status.SUBMITTED:
            if locked.transaction_hash == canonical_hash:
                return locked
            raise ValidationError("A submitted transaction cannot change its hash.")
        if locked.status != BlockchainTransaction.Status.PREPARED:
            raise ValidationError("Only a prepared transaction can be submitted.")

        if (
            locked.operation_type == DEPLOYMENT_OPERATION_TYPE
            and (process is None or _deployment_attempt is None)
        ):
            raise ValidationError(
                "A implantação precisa estar vinculada a uma etapa antes do envio."
            )
        if (
            locked.operation_type == CONTRACT_CONFIGURATION_OPERATION_TYPE
            and (process is None or configuration_attempt is None)
        ):
            raise ValidationError(
                "A configuração precisa estar vinculada a uma tentativa do processo."
            )
        if (
            locked.operation_type == DEPLOYMENT_OPERATION_TYPE
            and process is not None
            and process.status != DeploymentProcess.Status.IN_PROGRESS
        ):
            raise ValidationError(
                "A implantação só pode enviar transações enquanto estiver em andamento."
            )
        if (
            locked.operation_type == CONTRACT_CONFIGURATION_OPERATION_TYPE
            and process is not None
            and process.status != DeploymentProcess.Status.DEPLOYED
        ):
            raise ValidationError(
                "As configurações só podem ser enviadas após os nove contratos."
            )

        locked.transaction_hash = canonical_hash
        locked.submitted_at = submitted_at or timezone.now()
        locked.status = BlockchainTransaction.Status.SUBMITTED
        locked.save()
        return locked


def confirm_transaction(
    record: BlockchainTransaction,
    block_number: int,
    block_hash: str,
    *,
    confirmed_at: datetime | None = None,
) -> BlockchainTransaction:
    """Mark a submitted transaction confirmed with observed block data."""

    if block_number < 0:
        raise ValidationError("The block number cannot be negative.")
    canonical_block_hash = normalize_transaction_hash(block_hash)
    with db_transaction.atomic():
        # Keep deployment transition locks in the same order as the send path.
        process = _lock_deployment_process(record)
        _deployment_attempt = (
            _lock_deployment_attempt(record) if process is not None else None
        )
        _configuration_attempt = (
            _lock_configuration_attempt(record) if process is not None else None
        )
        locked = _lock_transaction(record)
        if locked.status == BlockchainTransaction.Status.CONFIRMED:
            if locked.block_number == block_number and locked.block_hash == canonical_block_hash:
                return locked
            raise ValidationError("A confirmed transaction cannot change its block data.")
        if locked.status != BlockchainTransaction.Status.SUBMITTED:
            raise ValidationError("Only a submitted transaction can be confirmed.")

        locked.block_number = block_number
        locked.block_hash = canonical_block_hash
        locked.confirmed_at = confirmed_at or timezone.now()
        locked.status = BlockchainTransaction.Status.CONFIRMED
        locked.save()
        return locked


def fail_transaction(
    record: BlockchainTransaction,
    error_message: str,
) -> BlockchainTransaction:
    """Mark a prepared or submitted transaction as failed."""

    with db_transaction.atomic():
        # Keep deployment state changes in the same process -> attempt ->
        # transaction lock order as preparation and submission.
        process = _lock_deployment_process(record)
        deployment_attempt = (
            _lock_deployment_attempt(record) if process is not None else None
        )
        configuration_attempt = (
            _lock_configuration_attempt(record) if process is not None else None
        )
        locked = _lock_transaction(record)
        if locked.status == BlockchainTransaction.Status.FAILED:
            return locked
        if locked.status in {
            BlockchainTransaction.Status.CONFIRMED,
            BlockchainTransaction.Status.REORGED,
        }:
            raise ValidationError("A completed transaction cannot be marked failed.")

        locked.error_message = error_message.strip()
        locked.status = BlockchainTransaction.Status.FAILED
        locked.save()

        # A definitive failure releases the step's unique open-attempt slot.
        # The attempt and transaction stay in history for later review.
        if deployment_attempt is not None and deployment_attempt.is_open:
            deployment_attempt.is_open = False
            deployment_attempt.save(update_fields=("is_open", "updated_at"))
        if configuration_attempt is not None and configuration_attempt.is_open:
            configuration_attempt.is_open = False
            configuration_attempt.save(update_fields=("is_open", "updated_at"))
        return locked


def mark_transaction_reorged(
    record: BlockchainTransaction,
    reason: str,
) -> BlockchainTransaction:
    """Record that a previously confirmed transaction left the canonical chain."""

    with db_transaction.atomic():
        process = _lock_deployment_process(record)
        _deployment_attempt = (
            _lock_deployment_attempt(record) if process is not None else None
        )
        _configuration_attempt = (
            _lock_configuration_attempt(record) if process is not None else None
        )
        locked = _lock_transaction(record)
        if locked.status == BlockchainTransaction.Status.REORGED:
            _mark_deployment_process_for_review(locked)
            return locked
        if locked.status != BlockchainTransaction.Status.CONFIRMED:
            raise ValidationError("Only a confirmed transaction can be reorged.")

        locked.error_message = reason.strip()
        locked.status = BlockchainTransaction.Status.REORGED
        locked.save()
        _mark_deployment_process_for_review(locked)
        return locked
