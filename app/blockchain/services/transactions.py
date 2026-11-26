"""Explicit state transitions for blockchain transaction records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.apps import apps
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
    UNIT_REGISTRATION_OPERATION_TYPE,
    UnitRegistrationAttempt,
    UNIT_WALLET_PERMISSION_OPERATION_TYPE,
    UnitWalletPermissionAttempt,
    PARTICIPANT_REGISTRATION_OPERATION_TYPE,
    RECEIPT_INVOICE_LINK_OPERATION_TYPE,
    RECEIPT_OPEN_OPERATION_TYPE,
    RECEIPT_WEIGHING_CONFIRMATION_OPERATION_TYPE,
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


def _lock_unit_registration_attempt(
    record: BlockchainTransaction,
) -> UnitRegistrationAttempt | None:
    """Lock a unit registration attempt before its transaction row."""

    return (
        UnitRegistrationAttempt.objects.select_for_update()
        .filter(transaction_id=record.pk)
        .first()
    )


def _lock_unit_wallet_permission_attempt(
    record: BlockchainTransaction,
) -> UnitWalletPermissionAttempt | None:
    """Lock the wallet permission attempt before its transaction row."""

    return (
        UnitWalletPermissionAttempt.objects.select_for_update()
        .select_related("unit", "membership")
        .filter(transaction_id=record.pk)
        .first()
    )


def _lock_participant_registration_attempt(
    record: BlockchainTransaction,
) -> Any | None:
    """Lock the participant attempt without importing its app during startup."""

    attempt_model = apps.get_model("participants", "ParticipantRegistrationAttempt")
    return (
        attempt_model.objects.select_for_update()
        .select_related("terms")
        .filter(transaction_id=record.pk)
        .first()
    )


def _lock_receipt_blockchain_attempt(
    record: BlockchainTransaction,
) -> Any | None:
    """Lock the receipt action attached to a transaction, when it is one of ours."""

    if record.operation_type not in {
        RECEIPT_OPEN_OPERATION_TYPE,
        RECEIPT_INVOICE_LINK_OPERATION_TYPE,
        RECEIPT_WEIGHING_CONFIRMATION_OPERATION_TYPE,
    }:
        return None
    attempt_model = apps.get_model("receipts", "ReceiptBlockchainAttempt")
    return (
        attempt_model.objects.select_for_update()
        .select_related("operation__receipt")
        .filter(transaction_id=record.pk)
        .first()
    )


def _mark_receipt_operation_for_review(attempt: Any | None) -> None:
    """Keep the local load from appearing complete after one of its calls reorgs."""

    if attempt is None:
        return
    operation_model = apps.get_model("receipts", "ReceiptBlockchainOperation")
    receipt_model = apps.get_model("receipts", "Receipt")
    operation = operation_model.objects.select_for_update().get(pk=attempt.operation_id)
    if operation.status in {"in_progress", "confirmed"}:
        operation.status = "needs_review"
        operation.save(update_fields=("status", "updated_at"))
    if attempt.is_open:
        attempt.is_open = False
        attempt.save(update_fields=("is_open", "updated_at"))
    receipt = receipt_model.objects.select_for_update().get(pk=operation.receipt_id)
    if receipt.status in {"blockchain_in_progress", "blockchain_confirmed"}:
        receipt.status = "needs_review"
        receipt.save(update_fields=("status", "updated_at"))


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
        unit_attempt = (
            _lock_unit_registration_attempt(record)
            if record.operation_type == UNIT_REGISTRATION_OPERATION_TYPE
            else None
        )
        wallet_permission_attempt = (
            _lock_unit_wallet_permission_attempt(record)
            if record.operation_type == UNIT_WALLET_PERMISSION_OPERATION_TYPE
            else None
        )
        participant_attempt = (
            _lock_participant_registration_attempt(record)
            if record.operation_type == PARTICIPANT_REGISTRATION_OPERATION_TYPE
            else None
        )
        receipt_attempt = _lock_receipt_blockchain_attempt(record)
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
            locked.operation_type == UNIT_REGISTRATION_OPERATION_TYPE
            and unit_attempt is None
        ):
            raise ValidationError(
                "O registro da unidade precisa estar vinculado a uma tentativa salva."
            )
        if unit_attempt is not None and (
            not unit_attempt.is_open or unit_attempt.submission_started_at is None
        ):
            raise ValidationError(
                "A tentativa de registro da unidade não está aguardando envio."
            )
        if locked.operation_type == UNIT_WALLET_PERMISSION_OPERATION_TYPE:
            if wallet_permission_attempt is None:
                raise ValidationError(
                    "A permissão da carteira não está ligada a uma tentativa salva."
                )
            if (
                not wallet_permission_attempt.is_open
                or wallet_permission_attempt.submission_started_at is None
            ):
                raise ValidationError(
                    "A tentativa de permissão não está aguardando o envio da carteira."
                )
        if locked.operation_type == PARTICIPANT_REGISTRATION_OPERATION_TYPE:
            if participant_attempt is None:
                raise ValidationError(
                    "A transação do participante não está ligada a uma tentativa salva."
                )
            if (
                not participant_attempt.is_open
                or participant_attempt.submission_started_at is None
            ):
                raise ValidationError(
                    "A tentativa do participante não está aguardando o envio da carteira."
                )
        if locked.operation_type in {
            RECEIPT_OPEN_OPERATION_TYPE,
            RECEIPT_INVOICE_LINK_OPERATION_TYPE,
            RECEIPT_WEIGHING_CONFIRMATION_OPERATION_TYPE,
        } and (
            receipt_attempt is None
            or not receipt_attempt.is_open
            or receipt_attempt.submission_started_at is None
        ):
            raise ValidationError(
                "A chamada do recebimento não está aguardando o envio da carteira."
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
        unit_attempt = (
            _lock_unit_registration_attempt(record)
            if record.operation_type == UNIT_REGISTRATION_OPERATION_TYPE
            else None
        )
        wallet_permission_attempt = (
            _lock_unit_wallet_permission_attempt(record)
            if record.operation_type == UNIT_WALLET_PERMISSION_OPERATION_TYPE
            else None
        )
        participant_attempt = (
            _lock_participant_registration_attempt(record)
            if record.operation_type == PARTICIPANT_REGISTRATION_OPERATION_TYPE
            else None
        )
        receipt_attempt = _lock_receipt_blockchain_attempt(record)
        locked = _lock_transaction(record)
        if locked.status == BlockchainTransaction.Status.CONFIRMED:
            if locked.block_number == block_number and locked.block_hash == canonical_block_hash:
                return locked
            raise ValidationError("A confirmed transaction cannot change its block data.")
        if locked.status != BlockchainTransaction.Status.SUBMITTED:
            raise ValidationError("Only a submitted transaction can be confirmed.")
        if locked.operation_type == UNIT_REGISTRATION_OPERATION_TYPE and unit_attempt is None:
            raise ValidationError(
                "A tentativa de registro da unidade não foi encontrada."
            )
        if (
            locked.operation_type == UNIT_WALLET_PERMISSION_OPERATION_TYPE
            and wallet_permission_attempt is None
        ):
            raise ValidationError("A tentativa de permissão da carteira não foi encontrada.")
        if (
            locked.operation_type == PARTICIPANT_REGISTRATION_OPERATION_TYPE
            and participant_attempt is None
        ):
            raise ValidationError("A tentativa do participante não foi encontrada.")
        if locked.operation_type in {
            RECEIPT_OPEN_OPERATION_TYPE,
            RECEIPT_INVOICE_LINK_OPERATION_TYPE,
            RECEIPT_WEIGHING_CONFIRMATION_OPERATION_TYPE,
        } and receipt_attempt is None:
            raise ValidationError("A tentativa do recebimento não foi encontrada.")

        locked.block_number = block_number
        locked.block_hash = canonical_block_hash
        locked.confirmed_at = confirmed_at or timezone.now()
        locked.status = BlockchainTransaction.Status.CONFIRMED
        locked.save()
        if receipt_attempt is not None and receipt_attempt.is_open:
            receipt_attempt.is_open = False
            receipt_attempt.save(update_fields=("is_open", "updated_at"))
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
        unit_attempt = (
            _lock_unit_registration_attempt(record)
            if record.operation_type == UNIT_REGISTRATION_OPERATION_TYPE
            else None
        )
        wallet_permission_attempt = (
            _lock_unit_wallet_permission_attempt(record)
            if record.operation_type == UNIT_WALLET_PERMISSION_OPERATION_TYPE
            else None
        )
        participant_attempt = (
            _lock_participant_registration_attempt(record)
            if record.operation_type == PARTICIPANT_REGISTRATION_OPERATION_TYPE
            else None
        )
        receipt_attempt = _lock_receipt_blockchain_attempt(record)
        locked = _lock_transaction(record)
        if locked.status == BlockchainTransaction.Status.FAILED:
            return locked
        if locked.status in {
            BlockchainTransaction.Status.CONFIRMED,
            BlockchainTransaction.Status.REORGED,
        }:
            raise ValidationError("A completed transaction cannot be marked failed.")

        if locked.operation_type == UNIT_REGISTRATION_OPERATION_TYPE and unit_attempt is None:
            raise ValidationError(
                "A tentativa de registro da unidade não foi encontrada."
            )
        if (
            locked.operation_type == UNIT_WALLET_PERMISSION_OPERATION_TYPE
            and wallet_permission_attempt is None
        ):
            raise ValidationError("A tentativa de permissão da carteira não foi encontrada.")
        if (
            locked.operation_type == PARTICIPANT_REGISTRATION_OPERATION_TYPE
            and participant_attempt is None
        ):
            raise ValidationError("A tentativa do participante não foi encontrada.")
        if locked.operation_type in {
            RECEIPT_OPEN_OPERATION_TYPE,
            RECEIPT_INVOICE_LINK_OPERATION_TYPE,
            RECEIPT_WEIGHING_CONFIRMATION_OPERATION_TYPE,
        } and receipt_attempt is None:
            raise ValidationError("A tentativa do recebimento não foi encontrada.")

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
        if unit_attempt is not None and unit_attempt.is_open:
            unit_attempt.is_open = False
            unit_attempt.save(update_fields=("is_open", "updated_at"))
        if wallet_permission_attempt is not None and wallet_permission_attempt.is_open:
            wallet_permission_attempt.is_open = False
            wallet_permission_attempt.save(update_fields=("is_open", "updated_at"))
        if participant_attempt is not None and participant_attempt.is_open:
            participant_attempt.is_open = False
            participant_attempt.save(update_fields=("is_open", "updated_at"))
        if receipt_attempt is not None and receipt_attempt.is_open:
            receipt_attempt.is_open = False
            receipt_attempt.save(update_fields=("is_open", "updated_at"))
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
        participant_attempt = (
            _lock_participant_registration_attempt(record)
            if record.operation_type == PARTICIPANT_REGISTRATION_OPERATION_TYPE
            else None
        )
        receipt_attempt = _lock_receipt_blockchain_attempt(record)
        locked = _lock_transaction(record)
        if locked.status == BlockchainTransaction.Status.REORGED:
            _mark_deployment_process_for_review(locked)
            _mark_participant_terms_for_review(participant_attempt)
            _mark_receipt_operation_for_review(receipt_attempt)
            return locked
        if locked.status != BlockchainTransaction.Status.CONFIRMED:
            raise ValidationError("Only a confirmed transaction can be reorged.")

        locked.error_message = reason.strip()
        locked.status = BlockchainTransaction.Status.REORGED
        locked.save()
        _mark_deployment_process_for_review(locked)
        _mark_participant_terms_for_review(participant_attempt)
        _mark_receipt_operation_for_review(receipt_attempt)
        return locked


def _mark_participant_terms_for_review(attempt: Any | None) -> None:
    """Hide a participant registration after its transaction is marked reorganized."""

    if attempt is None:
        return
    if attempt.is_open:
        attempt.is_open = False
        attempt.save(update_fields=("is_open", "updated_at"))
    terms = attempt.terms
    terms.status = "needs_review"
    terms.save(update_fields=("status", "updated_at"))
