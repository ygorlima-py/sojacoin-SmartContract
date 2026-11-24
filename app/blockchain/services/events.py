"""Idempotent processing of blockchain logs."""

from __future__ import annotations

from collections.abc import Callable

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction

from ..models import ContractInstance, Network, ProcessedEvent
from utils.identifiers import normalize_transaction_hash


EventHandler = Callable[[ProcessedEvent], None]


def process_event_once(
    *,
    network: Network,
    contract: ContractInstance,
    transaction_hash: str,
    log_index: int,
    block_number: int,
    block_hash: str,
    event_name: str,
    handler: EventHandler | None = None,
) -> tuple[ProcessedEvent, bool]:
    """Record and apply one event at most once.

    The handler runs in the same database transaction as the marker. If the
    handler fails, the marker is rolled back and the event can be retried.
    A duplicate event returns the existing marker and never calls the handler.
    """

    if log_index < 0:
        raise ValidationError("The event log index cannot be negative.")
    if block_number < 0:
        raise ValidationError("The event block number cannot be negative.")
    if network.pk != contract.network_id:
        raise ValidationError("The event contract must belong to the selected network.")

    canonical_transaction_hash = normalize_transaction_hash(transaction_hash)
    with db_transaction.atomic():
        event, created = ProcessedEvent.objects.get_or_create(
            network=network,
            transaction_hash=canonical_transaction_hash,
            log_index=log_index,
            defaults={
                "contract": contract,
                "block_number": block_number,
                "block_hash": block_hash,
                "event_name": event_name,
            },
        )
        if not created:
            return event, False

        if handler is not None:
            handler(event)
        return event, True
