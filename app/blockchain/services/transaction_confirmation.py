"""Read transaction receipts and apply their confirmed result locally."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import TransactionNotFound
from web3.logs import DISCARD
from web3.types import TxReceipt

from ..models import BlockchainTransaction, ContractInstance, ProcessedEvent
from .contracts import bind_contract_to_web3
from .events import process_event_once
from .rpc import connect_to_rpc
from .transactions import confirm_transaction, fail_transaction


@dataclass(frozen=True)
class DecodedContractEvent:
    """Contract event data needed by an operation-specific event handler."""

    event_name: str
    transaction_hash: str
    log_index: int
    block_number: int
    block_hash: str
    args: dict[str, Any]


TransactionEventHandler = Callable[
    [BlockchainTransaction, DecodedContractEvent], None
]


def _decode_receipt_events(
    contract: Contract,
    receipt: TxReceipt,
    transaction_hash: str,
) -> list[DecodedContractEvent]:
    """Decode events emitted by this contract from one transaction receipt."""

    decoded_events: list[DecodedContractEvent] = []
    seen_names: set[str] = set()

    for abi_entry in contract.abi:
        if abi_entry.get("type") != "event":
            continue

        event_name = abi_entry.get("name")
        if not isinstance(event_name, str) or event_name in seen_names:
            continue
        seen_names.add(event_name)

        # DISCARD ignores receipt logs from other event signatures while decoding.
        event_decoder = getattr(contract.events, event_name)()
        for event_log in event_decoder.process_receipt(receipt, errors=DISCARD):
            # A nested call can emit the same event signature from another address.
            if event_log["address"].lower() != contract.address.lower():
                continue

            decoded_events.append(
                DecodedContractEvent(
                    event_name=event_name,
                    transaction_hash=transaction_hash,
                    log_index=int(event_log["logIndex"]),
                    block_number=int(event_log["blockNumber"]),
                    block_hash=Web3.to_hex(event_log["blockHash"]),
                    args=dict(event_log["args"]),
                )
            )

    # Preserve the order in which the contract emitted the events.
    return sorted(decoded_events, key=lambda event: event.log_index)


def _record_event(
    transaction: BlockchainTransaction,
    contract: ContractInstance,
    event: DecodedContractEvent,
    event_handler: TransactionEventHandler,
) -> ProcessedEvent:
    """Record an event once and run its optional business handler atomically."""

    def apply_event(_: ProcessedEvent) -> None:
        event_handler(transaction, event)

    processed_event, _ = process_event_once(
        network=transaction.network,
        contract=contract,
        transaction_hash=event.transaction_hash,
        log_index=event.log_index,
        block_number=event.block_number,
        block_hash=event.block_hash,
        event_name=event.event_name,
        handler=apply_event,
    )
    return processed_event


def confirm_transaction_from_chain(
    transaction_id: int,
    *,
    event_handler: TransactionEventHandler | None = None,
    required_event_names: tuple[str, ...] = (),
) -> BlockchainTransaction | None:
    """Check one submitted transaction against its network receipt.

    Return ``None`` while the RPC has no receipt yet. Return the transaction
    record after marking it failed or confirmed. An optional handler applies
    operation-specific changes from decoded events, such as saving a unit's
    on-chain ID. Required event names prevent an operation from being marked
    confirmed when its expected contract event is absent. This function accepts
    a database ID so a future Celery task can call it without passing model
    objects through its queue.
    """

    if transaction_id < 1:
        raise ValidationError("The transaction ID must be a positive integer.")

    transaction = (
        BlockchainTransaction.objects.select_related("network", "contract")
        .get(pk=transaction_id)
    )

    if required_event_names and event_handler is None:
        raise ValidationError("Required events need an event handler.")

    # Completed transactions need no receipt polling unless a caller is retrying
    # an operation-specific event handler that previously failed.
    if (
        transaction.status == BlockchainTransaction.Status.CONFIRMED
        and event_handler is None
    ):
        return transaction
    if transaction.status not in {
        BlockchainTransaction.Status.SUBMITTED,
        BlockchainTransaction.Status.CONFIRMED,
    }:
        raise ValidationError("Only a submitted or confirmed transaction can be checked.")
    if not transaction.transaction_hash:
        raise ValidationError("The submitted transaction has no transaction hash.")

    # Use the endpoint saved with this transaction's network and verify its chain ID.
    web3 = connect_to_rpc(transaction.network)
    try:
        receipt: TxReceipt = web3.eth.get_transaction_receipt(
            transaction.transaction_hash
        )
    except TransactionNotFound:
        return None

    receipt_hash = Web3.to_hex(receipt["transactionHash"]).lower()
    if receipt_hash != transaction.transaction_hash:
        raise ValidationError("The RPC returned a receipt for a different transaction.")

    receipt_status = int(receipt["status"])
    if receipt_status == 0:
        if transaction.status == BlockchainTransaction.Status.CONFIRMED:
            raise ValidationError(
                "A confirmed transaction now has a reverted receipt; "
                "handle it through reorganization recovery."
            )
        return fail_transaction(
            transaction,
            "The blockchain transaction reverted (receipt status 0).",
        )
    if receipt_status != 1:
        raise ValidationError("The RPC returned an unsupported receipt status.")

    if receipt.get("blockNumber") is None or receipt.get("blockHash") is None:
        raise ValidationError("The successful receipt is missing its block data.")

    events: list[DecodedContractEvent] = []
    event_contract = transaction.contract
    if event_handler is not None or required_event_names:
        if event_contract is None:
            raise ValidationError("Event processing requires a saved contract instance.")
        if (
            event_contract.company_id != transaction.company_id
            or event_contract.network_id != transaction.network_id
        ):
            raise ValidationError(
                "The transaction contract must belong to its company and network."
            )
        # Bind with the existing RPC connection; inactive historical deployments
        # still need to be readable while their submitted transaction is checked.
        contract = bind_contract_to_web3(web3, event_contract)
        events = _decode_receipt_events(
            contract,
            receipt,
            transaction.transaction_hash,
        )

        # A successful receipt alone does not prove the requested operation
        # happened; require its event before marking the local operation done.
        missing_event_names = set(required_event_names) - {
            event.event_name for event in events
        }
        if missing_event_names:
            missing_names = ", ".join(sorted(missing_event_names))
            raise ValidationError(
                f"The successful receipt is missing required event(s): {missing_names}."
            )

    # Keep event effects and the transaction status in one database commit.
    with db_transaction.atomic():
        if event_handler is not None:
            if event_contract is None:
                raise ValidationError("Event processing requires a saved contract instance.")
            for event in events:
                _record_event(transaction, event_contract, event, event_handler)

        if transaction.status == BlockchainTransaction.Status.SUBMITTED:
            return confirm_transaction(
                transaction,
                block_number=int(receipt["blockNumber"]),
                block_hash=Web3.to_hex(receipt["blockHash"]),
            )
        return transaction
