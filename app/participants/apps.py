from django.apps import AppConfig


class ParticipantsConfig(AppConfig):
    """Register participant records and participation-terms workflows."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "participants"
    verbose_name = "Participantes"
