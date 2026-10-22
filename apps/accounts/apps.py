
from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = 'apps.accounts'
    verbose_name = 'User Accounts & Authentication'

    def ready(self):
        from apps.accounts.signals import register_audit_signals

        register_audit_signals()
