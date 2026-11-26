import uuid
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from utils.identifiers import (
    is_valid_bytes32_hash,
    is_valid_wallet_address,
    normalize_bytes32_hash,
    normalize_wallet_address,
)


class UserProfile(models.Model):
    """Additional account data that belongs to one Django user."""

    # Django account that owns this profile.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    # Optional name displayed in the SaaS interface.
    full_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="The name displayed in the application.",
    )
    # Optional contact telephone number.
    phone = models.CharField(
        max_length=32,
        blank=True,
        help_text="An optional contact telephone number.",
    )
    # Profile status; access also requires the Django user to be active.
    active = models.BooleanField(
        default=True,
        help_text="Whether this profile may be used in the application.",
    )
    # Timestamp set when the profile is first created.
    created_at = models.DateTimeField(auto_now_add=True)
    # Timestamp refreshed whenever Django saves the profile.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user__username",)
        verbose_name = "user profile"
        verbose_name_plural = "user profiles"

    def __str__(self) -> str:
        """Return the display name or the linked username."""

        return self.full_name or self.user.get_username()


class Wallet(models.Model):
    """A public EVM address that can be linked to one or more users."""

    class WalletType(models.TextChoices):
        INDIVIDUAL = "individual", _("Individual wallet")
        CONTRACT = "contract", _("Contract wallet")

    # The value shown to users; no private key material is ever stored here.
    address = models.CharField("Address", max_length=42)
    # Lowercase value used for uniqueness and all comparisons.
    address_normalized = models.CharField(
        "Normalized address",
        max_length=42,
        unique=True,
        editable=False,
    )
    # Optional label for a shared or individual wallet.
    label = models.CharField("Label", max_length=120, blank=True)
    # A declared classification; contract does not prove that an address is a Safe.
    wallet_type = models.CharField(
        "Wallet type",
        max_length=16,
        choices=WalletType.choices,
        default=WalletType.INDIVIDUAL,
    )
    # Deactivation preserves history while preventing new use in the SaaS.
    active = models.BooleanField("Active", default=True)
    # Timestamp set when the wallet record is first created.
    created_at = models.DateTimeField(auto_now_add=True)
    # Timestamp refreshed whenever Django saves the wallet record.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("label", "address_normalized")
        verbose_name = "wallet"
        verbose_name_plural = "wallets"

    def clean(self) -> None:
        """Normalize and validate the public address before persistence."""

        super().clean()
        self.address = (self.address or "").strip()
        self.address_normalized = normalize_wallet_address(self.address)
        if not is_valid_wallet_address(self.address_normalized):
            raise ValidationError({"address": "Enter a valid EVM address."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep the normalized address synchronized on every write path."""

        self.address = (self.address or "").strip()
        self.address_normalized = normalize_wallet_address(self.address)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Return the optional label or the public address."""

        return self.label or self.address


class UserWallet(models.Model):
    """A user-specific link to a public wallet and its verification state."""

    class VerificationStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        VERIFIED = "verified", _("Verified")
        REVOKED = "revoked", _("Revoked")

    # User account to which this wallet link belongs.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="wallet_links",
    )
    # Public wallet record; one wallet may be shared by multiple users.
    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name="user_links",
    )
    # Application-level state of this specific user-wallet relationship.
    verification_status = models.CharField(
        max_length=16,
        choices=VerificationStatus.choices,
        default=VerificationStatus.PENDING,
    )
    # Time at which this user-wallet link was verified, if ever.
    verified_at = models.DateTimeField(null=True, blank=True)
    # Time at which access to this link was revoked.
    revoked_at = models.DateTimeField(null=True, blank=True)
    # Human-readable reason retained for the audit trail.
    revocation_reason = models.CharField(max_length=500, blank=True)
    # Timestamp set when this relationship is first created.
    created_at = models.DateTimeField(auto_now_add=True)
    # Timestamp refreshed whenever Django saves this relationship.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user", "wallet")
        verbose_name = "user wallet link"
        verbose_name_plural = "user wallet links"
        constraints = [
            models.UniqueConstraint(
                fields=("user", "wallet"),
                name="uniq_user_wallet_link",
            ),
        ]

    def clean(self) -> None:
        """Keep status, verification time and revocation history consistent."""

        super().clean()
        self.revocation_reason = (self.revocation_reason or "").strip()

        if self.verification_status == self.VerificationStatus.PENDING:
            if self.verified_at is not None:
                raise ValidationError(
                    {"verified_at": "A pending link cannot have a verification time."}
                )
            if self.revoked_at is not None or self.revocation_reason:
                raise ValidationError(
                    {"verification_status": "A pending link cannot have revocation data."}
                )

        if self.verification_status == self.VerificationStatus.VERIFIED:
            if self.verified_at is None:
                self.verified_at = timezone.now()
            if self.revoked_at is not None or self.revocation_reason:
                raise ValidationError(
                    {"verification_status": "A verified link cannot have revocation data."}
                )

        if self.verification_status == self.VerificationStatus.REVOKED:
            if self.revoked_at is None:
                self.revoked_at = timezone.now()
            if not self.revocation_reason:
                raise ValidationError(
                    {"revocation_reason": "A revocation reason is required."}
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate relationship state before saving it to the database."""

        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Return the user, wallet and current verification state."""

        return f"{self.user} — {self.wallet} ({self.get_verification_status_display()})"


class WalletVerificationChallenge(models.Model):
    """A versioned, network-bound challenge used to verify a wallet link."""

    class Purpose(models.TextChoices):
        WALLET_OWNERSHIP = "wallet_ownership", _("Wallet ownership")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        CONSUMED = "consumed", _("Consumed")
        CANCELLED = "cancelled", _("Cancelled")

    # The exact user-wallet relationship that requested this challenge.
    user_wallet = models.ForeignKey(
        UserWallet,
        on_delete=models.PROTECT,
        related_name="verification_challenges",
    )
    # Exact versioned text shown to the user before signing.
    message = models.TextField()
    # Hash of the exact message used for later audit and comparison.
    digest = models.CharField(max_length=66)
    # Random one-time identifier; it prevents replay of an old challenge.
    nonce = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # Why this signature is being requested.
    purpose = models.CharField(
        max_length=32,
        choices=Purpose.choices,
        default=Purpose.WALLET_OWNERSHIP,
    )
    # Application domain included in the signed payload.
    domain = models.CharField(max_length=120)
    # Version of the message format used to create the challenge.
    payload_version = models.PositiveSmallIntegerField(default=1)
    # EVM network on which the signature is valid; this is mandatory.
    chain_id = models.PositiveBigIntegerField()
    # Time after which a pending challenge cannot be consumed.
    expires_at = models.DateTimeField()
    # Lifecycle state; expiration is derived from expires_at while pending.
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    # Signature submitted by the wallet, when a challenge is consumed.
    signature = models.TextField(blank=True)
    # Time at which a signature was accepted by the verification service.
    consumed_at = models.DateTimeField(null=True, blank=True)
    # Time at which this challenge was cancelled.
    cancelled_at = models.DateTimeField(null=True, blank=True)
    # Human-readable cancellation reason retained for the audit trail.
    cancellation_reason = models.CharField(max_length=500, blank=True)
    # Timestamp set when this challenge is first created.
    created_at = models.DateTimeField(auto_now_add=True)
    # Timestamp refreshed whenever Django saves this challenge.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "wallet verification challenge"
        verbose_name_plural = "wallet verification challenges"
        indexes = [
            models.Index(
                fields=("user_wallet", "status"),
                name="challenge_link_status_idx",
            ),
            models.Index(fields=("expires_at",), name="challenge_expires_idx"),
        ]

    def clean(self) -> None:
        """Validate the signed payload references and lifecycle timestamps."""

        super().clean()
        self.digest = normalize_bytes32_hash(self.digest)
        self.domain = (self.domain or "").strip()
        self.cancellation_reason = (self.cancellation_reason or "").strip()

        if not is_valid_bytes32_hash(self.digest):
            raise ValidationError({"digest": "Enter a valid 32-byte hexadecimal hash."})
        if not self.domain:
            raise ValidationError({"domain": "The signing domain is required."})
        if self.chain_id <= 0:
            raise ValidationError({"chain_id": "The chain ID must be positive."})
        if not timezone.is_aware(self.expires_at):
            raise ValidationError({"expires_at": "The expiration must include a timezone."})

        if self.status == self.Status.PENDING:
            if self.consumed_at is not None or self.cancelled_at is not None:
                raise ValidationError(
                    {"status": "A pending challenge cannot have a completion timestamp."}
                )
            if self.cancellation_reason or self.signature.strip():
                raise ValidationError(
                    {"status": "A pending challenge cannot have completion data."}
                )

        if self.status == self.Status.CONSUMED:
            if self.consumed_at is None:
                self.consumed_at = timezone.now()
            if self.cancelled_at is not None or self.cancellation_reason:
                raise ValidationError(
                    {"status": "A consumed challenge cannot have cancellation data."}
                )
            if not self.signature.strip():
                raise ValidationError(
                    {"signature": "A consumed challenge requires a signature."}
                )

        if self.status == self.Status.CANCELLED:
            if self.cancelled_at is None:
                self.cancelled_at = timezone.now()
            if not self.cancellation_reason:
                raise ValidationError(
                    {"cancellation_reason": "A cancellation reason is required."}
                )
            if self.consumed_at is not None or self.signature.strip():
                raise ValidationError(
                    {"status": "A cancelled challenge cannot have consumption data."}
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate the challenge before saving its auditable state."""

        if self.pk:
            previous_status = type(self).objects.filter(pk=self.pk).values_list(
                "status",
                flat=True,
            ).first()
            if previous_status in {self.Status.CONSUMED, self.Status.CANCELLED}:
                if self.status != previous_status:
                    raise ValidationError(
                        {"status": "A completed challenge cannot be reopened."}
                    )

        self.full_clean()
        return super().save(*args, **kwargs)

    def is_expired(self, at: datetime | None = None) -> bool:
        """Return whether the challenge has passed its expiration time."""

        return (at or timezone.now()) >= self.expires_at

    def can_be_consumed(self, at: datetime | None = None) -> bool:
        """Return whether the challenge is pending and not expired."""

        return self.status == self.Status.PENDING and not self.is_expired(at)

    def __str__(self) -> str:
        """Return the linked wallet, lifecycle state and nonce."""

        return f"{self.user_wallet} — {self.get_status_display()} — {self.nonce}"
