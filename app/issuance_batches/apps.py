"""Django application configuration for on-chain issuance batches."""

from django.apps import AppConfig


class IssuanceBatchesConfig(AppConfig):
    """Register batch records and their blockchain approval lifecycle."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "issuance_batches"
    verbose_name = "Lotes de emissão"
