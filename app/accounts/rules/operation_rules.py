"""Cross-app invariants for participant, receipt and issuance workflows.

The concrete models will live in ``participants``, ``cprs``, ``receipts`` and
``issuance``. These small validators keep the shared business invariants in one
place without making the accounts app own those future records.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from django.core.exceptions import ValidationError

from utils.identifiers import (
    is_valid_bytes32_hash,
    is_valid_wallet_address,
    normalize_bytes32_hash,
    normalize_wallet_address,
)


MAX_RECEIPTS_PER_BATCH: Final[int] = 64
ISSUANCE_BATCH_PENDING_APPROVAL: Final[str] = "pending_approval"
ISSUANCE_BATCH_APPROVED: Final[str] = "approved"
BLOCKCHAIN_TRANSACTION_CONFIRMED: Final[str] = "confirmed"


@dataclass(frozen=True)
class BatchReceiptCandidate:
    """Immutable receipt data needed before adding a load to an issuance batch."""

    receipt_id: int
    producer_address: str
    eligible: bool = True


def require_registered_participants(
    producer_registered: bool,
    creditor_registered: bool,
) -> None:
    """Require both parties to be registered before creating a CPR."""

    errors: dict[str, str] = {}
    if not producer_registered:
        errors["producer"] = "The producer must be registered."
    if not creditor_registered:
        errors["creditor"] = "The creditor must be registered."
    if errors:
        raise ValidationError(errors)


def require_accepted_terms(
    terms_accepted: bool,
    *,
    operation_name: str = "operation",
) -> None:
    """Require accepted terms before an operation that depends on them."""

    if not terms_accepted:
        raise ValidationError(
            f"Accepted terms are required before this {operation_name}."
        )


def require_cpr_balance_unchanged(
    original_balance_sjc_units: int,
    submitted_balance_sjc_units: int,
) -> None:
    """Reject a manual update that changes a CPR's outstanding balance."""

    if original_balance_sjc_units < 0 or submitted_balance_sjc_units < 0:
        raise ValidationError("CPR balances cannot be negative.")
    if original_balance_sjc_units != submitted_balance_sjc_units:
        raise ValidationError(
            "A CPR balance can only change through a confirmed contract operation."
        )


def calculate_net_weight_kg(
    gross_weight_kg: int,
    tare_weight_kg: int,
    moisture_deduction_kg: int = 0,
    impurities_deduction_kg: int = 0,
    damaged_grain_deduction_kg: int = 0,
    other_deduction_kg: int = 0,
) -> int:
    """Calculate net weight using the same integer-kilogram rule as the contract."""

    weights = (
        gross_weight_kg,
        tare_weight_kg,
        moisture_deduction_kg,
        impurities_deduction_kg,
        damaged_grain_deduction_kg,
        other_deduction_kg,
    )
    if any(weight < 0 for weight in weights):
        raise ValidationError("Weights and deductions cannot be negative.")

    load_weight_kg = gross_weight_kg - tare_weight_kg
    total_deductions_kg = (
        moisture_deduction_kg
        + impurities_deduction_kg
        + damaged_grain_deduction_kg
        + other_deduction_kg
    )
    if load_weight_kg <= 0:
        raise ValidationError("Gross weight must be greater than tare weight.")
    if load_weight_kg <= total_deductions_kg:
        raise ValidationError("Deductions must be lower than the load weight.")

    return load_weight_kg - total_deductions_kg


def require_unique_weighing_ticket(
    weighing_ticket_hash: str,
    *,
    is_used: Callable[[str], bool],
) -> str:
    """Return a normalized ticket hash only when it has not been used before."""

    normalized_hash = normalize_bytes32_hash(weighing_ticket_hash)
    if not is_valid_bytes32_hash(normalized_hash):
        raise ValidationError("The weighing ticket hash must be a bytes32 value.")
    if is_used(normalized_hash):
        raise ValidationError("A weighing ticket cannot be used more than once.")

    return normalized_hash


def require_compatible_batch(
    candidates: Sequence[BatchReceiptCandidate],
) -> str:
    """Require a non-empty batch of eligible loads from one producer."""

    if not candidates:
        raise ValidationError("An issuance batch must contain at least one receipt.")
    if len(candidates) > MAX_RECEIPTS_PER_BATCH:
        raise ValidationError(
            f"An issuance batch cannot contain more than {MAX_RECEIPTS_PER_BATCH} receipts."
        )

    receipt_ids = [candidate.receipt_id for candidate in candidates]
    if any(receipt_id <= 0 for receipt_id in receipt_ids):
        raise ValidationError("Receipt IDs must be positive.")
    if len(set(receipt_ids)) != len(receipt_ids):
        raise ValidationError("A receipt cannot appear twice in the same batch.")

    if any(not candidate.eligible for candidate in candidates):
        raise ValidationError("Every receipt must be eligible for issuance.")

    producers = [
        normalize_wallet_address(candidate.producer_address)
        for candidate in candidates
    ]
    if any(not is_valid_wallet_address(producer) for producer in producers):
        raise ValidationError("Every receipt must have a valid producer wallet.")
    if len(set(producers)) != 1:
        raise ValidationError("All receipts in a batch must belong to one producer.")

    return producers[0]


def require_approved_batch_for_issuance(batch_status: str) -> None:
    """Prevent token issuance until the producer approval is confirmed locally."""

    if batch_status != ISSUANCE_BATCH_APPROVED:
        raise ValidationError(
            "The producer approval must be confirmed before token issuance."
        )


def can_display_issued_tokens(transaction_status: str) -> bool:
    """Return whether the UI may display tokens as issued."""

    return transaction_status == BLOCKCHAIN_TRANSACTION_CONFIRMED


def require_confirmed_issuance(transaction_status: str) -> None:
    """Reject an issuance result that is not confirmed by the blockchain."""

    if not can_display_issued_tokens(transaction_status):
        raise ValidationError(
            "Tokens cannot be displayed as issued before blockchain confirmation."
        )
