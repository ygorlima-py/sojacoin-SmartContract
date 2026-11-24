from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = 'accounts'

    def ready(self):
        """Load account signals after Django has initialized the app registry."""

        from . import signals  # noqa: F401
