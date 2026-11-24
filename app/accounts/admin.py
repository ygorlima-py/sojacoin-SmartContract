from django.contrib import admin

from .models import UserProfile, UserWallet, Wallet, WalletVerificationChallenge


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    """Provide controlled administrative access to profile data."""

    list_display = ("user", "full_name", "phone", "active", "created_at", "updated_at")
    list_filter = ("active",)
    search_fields = ("user__username", "user__email", "full_name", "phone")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    """Provide controlled administrative access to public wallet records."""

    list_display = (
        "address",
        "label",
        "wallet_type",
        "active",
        "created_at",
        "updated_at",
    )
    list_filter = ("wallet_type", "active")
    search_fields = ("address", "address_normalized", "label")
    readonly_fields = ("address_normalized", "created_at", "updated_at")


@admin.register(UserWallet)
class UserWalletAdmin(admin.ModelAdmin):
    """Provide controlled administrative access to user-wallet links."""

    list_display = (
        "user",
        "wallet",
        "verification_status",
        "verified_at",
        "revoked_at",
        "updated_at",
    )
    list_filter = ("verification_status",)
    search_fields = (
        "user__username",
        "user__email",
        "wallet__address",
        "wallet__label",
    )
    readonly_fields = ("verified_at", "revoked_at", "created_at", "updated_at")


@admin.register(WalletVerificationChallenge)
class WalletVerificationChallengeAdmin(admin.ModelAdmin):
    """Expose challenge history without allowing secret-key operations."""

    list_display = (
        "user_wallet",
        "purpose",
        "chain_id",
        "status",
        "expires_at",
        "consumed_at",
        "cancelled_at",
    )
    list_filter = ("purpose", "status", "chain_id")
    search_fields = (
        "user_wallet__user__username",
        "user_wallet__wallet__address",
        "digest",
        "nonce",
    )
    readonly_fields = (
        "nonce",
        "consumed_at",
        "cancelled_at",
        "created_at",
        "updated_at",
    )
