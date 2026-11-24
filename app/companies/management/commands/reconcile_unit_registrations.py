"""Check submitted unit-registration transactions against the blockchain."""

from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from blockchain.models import BlockchainTransaction
from blockchain.services.transaction_confirmation import (
    confirm_transaction_from_chain,
)
from companies.services.unit_registration import (
    UNIT_REGISTRATION_OPERATION,
    UNIT_REGISTRY_CONTRACT_KIND,
    handle_unit_registered_event,
)


class Command(BaseCommand):
    """Reconcile pending registrations without requiring Celery."""

    help = "Verifica transações enviadas de registro de unidades."

    def handle(self, *args: Any, **options: Any) -> None:
        transaction_ids = list(
            BlockchainTransaction.objects.filter(
                status=BlockchainTransaction.Status.SUBMITTED,
                operation_type=UNIT_REGISTRATION_OPERATION,
                contract__kind=UNIT_REGISTRY_CONTRACT_KIND,
            )
            .order_by("created_at", "pk")
            .values_list("pk", flat=True)
        )

        if not transaction_ids:
            self.stdout.write(
                "Não há registros de unidade enviados aguardando confirmação."
            )
            return

        confirmed_count = 0
        failed_count = 0
        pending_count = 0
        error_count = 0

        for transaction_id in transaction_ids:
            try:
                transaction = confirm_transaction_from_chain(
                    transaction_id,
                    event_handler=handle_unit_registered_event,
                    required_event_names=("UnitRegistered",),
                )
            except ValidationError as error:
                error_count += 1
                details = "; ".join(error.messages)
                self.stderr.write(
                    self.style.ERROR(f"Transação {transaction_id}: {details}")
                )
                continue

            if transaction is None:
                pending_count += 1
            elif transaction.status == BlockchainTransaction.Status.CONFIRMED:
                confirmed_count += 1
            elif transaction.status == BlockchainTransaction.Status.FAILED:
                failed_count += 1

        summary = (
            f"Confirmadas: {confirmed_count}; falharam na blockchain: {failed_count}; "
            f"ainda pendentes: {pending_count}; erros de validação: {error_count}."
        )
        output_style = self.style.WARNING if error_count else self.style.SUCCESS
        self.stdout.write(output_style(summary))

        if error_count:
            raise CommandError(
                "Algumas transações não puderam ser reconciliadas. "
                "Consulte os erros listados acima."
            )
