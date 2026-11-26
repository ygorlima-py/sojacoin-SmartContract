from django.apps import AppConfig


class ReceiptsConfig(AppConfig):
    """Register company-scoped local load receipt records."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "receipts"
    verbose_name = "Recebimentos"
