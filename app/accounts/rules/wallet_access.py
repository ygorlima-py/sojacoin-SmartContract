"""Wallet ownership and operation authorization rules.

These rules protect the Django side of wallet use. They deliberately do not
pretend that a verified user-wallet link is an on-chain permission. A future
blockchain adapter must perform that second check against the configured
contract and network before sending an operational transaction.
"""

from collections.abc import Callable
from datetime import datetime
from typing import TypeAlias

from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from ..models import UserWallet, WalletVerificationChallenge
from .account_access import is_active_account


WalletSignatureValidator: TypeAlias = Callable[
    [WalletVerificationChallenge, str], bool
]


def get_user_wallet(
    user: AbstractBaseUser | None,
    user_wallet_id: int,
) -> UserWallet:
    """Return a wallet link owned by the active user or hide it with 404."""

    if not is_active_account(user):
        raise Http404

    return get_object_or_404(
        UserWallet.objects.select_related("user", "wallet"),
        pk=user_wallet_id,
        user_id=user.pk,
    )


def require_verified_wallet(user_wallet: UserWallet) -> UserWallet:
    """Allow an operation only when the linked account and wallet are active."""

    if not is_active_account(user_wallet.user):
        raise PermissionDenied("The linked account is inactive.")
    if not user_wallet.wallet.active:
        raise PermissionDenied("The wallet is inactive.")
    if user_wallet.verification_status != UserWallet.VerificationStatus.VERIFIED:
        raise PermissionDenied("The wallet must be verified before use.")

    return user_wallet


def require_verified_wallet_for_operation(
    user: AbstractBaseUser | None,
    user_wallet_id: int,
) -> UserWallet:
    """Return a verified user-wallet link ready for an operation.

    The returned link proves application ownership and verification only. The
    caller must still check the wallet's confirmed permission in the target
    blockchain contract.
    """

    return require_verified_wallet(get_user_wallet(user, user_wallet_id))


def get_user_challenge(
    user: AbstractBaseUser | None,
    challenge_id: int,
) -> WalletVerificationChallenge:
    """Return a challenge belonging to the active user or hide it with 404."""

    if not is_active_account(user):
        raise Http404

    return get_object_or_404(
        WalletVerificationChallenge.objects.select_related(
            "user_wallet__user",
            "user_wallet__wallet",
        ),
        pk=challenge_id,
        user_wallet__user_id=user.pk,
    )


def require_consumable_challenge(
    challenge: WalletVerificationChallenge,
    *,
    at: datetime | None = None,
) -> WalletVerificationChallenge:
    """Reject an expired, canceled, consumed, or inactive-wallet challenge."""

    if not challenge.user_wallet.wallet.active:
        raise PermissionDenied("The wallet is inactive.")
    if challenge.user_wallet.verification_status != UserWallet.VerificationStatus.PENDING:
        raise PermissionDenied("The wallet link is not pending verification.")
    if not challenge.can_be_consumed(at):
        raise PermissionDenied("The wallet verification challenge is expired or already used.")

    return challenge


def consume_wallet_challenge(
    user: AbstractBaseUser | None,
    challenge_id: int,
    signature: str,
    *,
    signature_validator: WalletSignatureValidator,
    at: datetime | None = None,
) -> WalletVerificationChallenge:
    """Consume a valid challenge once and mark its user-wallet link verified.

    ``signature_validator`` belongs to the future blockchain integration. It
    must verify the exact challenge digest for the linked EOA or contract
    wallet. This rule only performs ownership, expiry, one-time state and the
    atomic database transition; it never accepts a raw signature by itself.
    """

    if not is_active_account(user):
        raise PermissionDenied("The account is inactive.")

    normalized_signature = (signature or "").strip()
    if not normalized_signature:
        raise ValidationError({"signature": "A wallet signature is required."})

    consumed_at = at or timezone.now()
    with transaction.atomic():
        challenge = get_object_or_404(
            WalletVerificationChallenge.objects.select_for_update().select_related(
                "user_wallet__user",
                "user_wallet__wallet",
            ),
            pk=challenge_id,
            user_wallet__user_id=user.pk,
        )
        require_consumable_challenge(challenge, at=consumed_at)

        if not signature_validator(challenge, normalized_signature):
            raise ValidationError({"signature": "The wallet signature is invalid."})

        challenge.status = WalletVerificationChallenge.Status.CONSUMED
        challenge.signature = normalized_signature
        challenge.consumed_at = consumed_at
        challenge.save()

        user_wallet = challenge.user_wallet
        user_wallet.verification_status = UserWallet.VerificationStatus.VERIFIED
        user_wallet.verified_at = consumed_at
        user_wallet.revoked_at = None
        user_wallet.revocation_reason = ""
        user_wallet.save()

    return challenge
