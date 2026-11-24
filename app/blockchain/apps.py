from django.apps import AppConfig


class BlockchainConfig(AppConfig):
    """Django configuration for network and transaction records."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "blockchain"
