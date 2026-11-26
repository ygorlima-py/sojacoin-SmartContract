"""Read-only Django Admin views for participant history and chain attempts."""

from django.contrib import admin
from django.http import HttpRequest

from .models import (
    Participant,
    ParticipantAcceptanceChallenge,
    ParticipantAcceptanceLink,
    ParticipantRegistrationAttempt,
    ParticipantTerms,
)


class ReadOnlyHistoryAdmin(admin.ModelAdmin):
    """Prevent Admin edits from bypassing participant service validations."""

    def has_add_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        """Create all operational records through the company workflow."""

        return False

    def has_change_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        """Keep terms and transaction history immutable in the Admin."""

        return False

    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        """Preserve participant, signature and chain evidence for review."""

        return False


@admin.register(Participant)
class ParticipantAdmin(ReadOnlyHistoryAdmin):
    """Expose company, address and creation metadata for support review."""

    list_display = ("name", "company", "wallet_address_normalized", "created_at")
    list_filter = ("company",)
    search_fields = ("name", "wallet_address_normalized", "company__legal_name")
    readonly_fields = (
        "company",
        "name",
        "wallet_address",
        "wallet_address_normalized",
        "created_by",
        "created_at",
        "updated_at",
    )


@admin.register(ParticipantTerms)
class ParticipantTermsAdmin(ReadOnlyHistoryAdmin):
    """Display the exact text version, hash and its current lifecycle state."""

    list_display = ("participant", "version", "status", "network", "created_at")
    list_filter = ("status", "network")
    search_fields = ("participant__name", "terms_hash", "participant__wallet_address")
    readonly_fields = tuple(field.name for field in ParticipantTerms._meta.fields)


@admin.register(ParticipantRegistrationAttempt)
class ParticipantRegistrationAttemptAdmin(ReadOnlyHistoryAdmin):
    """Expose prepared calls and linked transaction evidence for support."""

    list_display = ("terms", "kind", "transaction", "sender_address", "is_open")
    list_filter = ("kind", "is_open", "transaction__status", "transaction__network")
    search_fields = ("terms__participant__name", "sender_address", "transaction__transaction_hash")
    readonly_fields = tuple(
        field.name for field in ParticipantRegistrationAttempt._meta.fields
    )


@admin.register(ParticipantAcceptanceLink)
class ParticipantAcceptanceLinkAdmin(ReadOnlyHistoryAdmin):
    """Show only the token digest, never a usable raw acceptance link."""

    list_display = ("terms", "expires_at", "used_at", "canceled_at", "created_by")
    list_filter = ("expires_at", "used_at", "canceled_at")
    readonly_fields = tuple(field.name for field in ParticipantAcceptanceLink._meta.fields)


@admin.register(ParticipantAcceptanceChallenge)
class ParticipantAcceptanceChallengeAdmin(ReadOnlyHistoryAdmin):
    """Expose signed typed data and status without permitting database edits."""

    list_display = ("link", "chain_nonce", "deadline", "status", "signed_at")
    list_filter = ("status",)
    readonly_fields = tuple(
        field.name for field in ParticipantAcceptanceChallenge._meta.fields
    )
