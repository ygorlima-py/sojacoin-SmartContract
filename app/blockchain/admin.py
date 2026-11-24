from django.contrib import admin, messages
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db.models import QuerySet
from django.forms.models import BaseInlineFormSet
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    JsonResponse,
)
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.utils.html import format_html
from django.views.decorators.http import require_POST
from web3.exceptions import Web3Exception

from .forms import DeploymentProcessStartForm
from .models import (
    BlockchainTransaction,
    ContractInstance,
    DeploymentAttempt,
    DeploymentConfigurationAttempt,
    DeploymentProcess,
    DeploymentStep,
    Network,
    ProcessedEvent,
)
from .services.deployment import (
    DEPLOYMENT_KIND_LABELS,
    cancel_deployment_process,
    fail_deployment_attempt,
    mark_deployment_submission_started,
    register_deployment_transaction_hash,
    start_deployment_process,
    start_deployment_attempt,
)
from .services.deployment_confirmation import confirm_deployment_attempt
from .services.deployment_configuration import (
    confirm_deployment_configuration,
    mark_configuration_submission_started,
    prepare_deployment_configuration,
    register_configuration_transaction_hash,
    reject_configuration_attempt,
)


@admin.register(Network)
class NetworkAdmin(admin.ModelAdmin):
    list_display = ("name", "chain_id", "active", "created_at")
    list_filter = ("active",)
    search_fields = ("name",)
    readonly_fields = ("created_at", "updated_at")

    def has_view_permission(
        self,
        request: HttpRequest,
        obj: Network | None = None,
    ) -> bool:
        """Keep RPC URLs, which may contain provider credentials, superuser-only."""

        return request.user.is_superuser

    def has_module_permission(self, request: HttpRequest) -> bool:
        """Hide network configuration from staff who cannot manage its RPC URL."""

        return request.user.is_superuser

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Restrict network and RPC endpoint setup to superusers."""

        return request.user.is_superuser

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: Network | None = None,
    ) -> bool:
        """Restrict network and RPC endpoint changes to superusers."""

        return request.user.is_superuser

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: Network | None = None,
    ) -> bool:
        """Require deactivation instead of deleting a network."""

        return False


@admin.register(ContractInstance)
class ContractInstanceAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "network",
        "kind",
        "address_normalized",
        "version",
        "active",
    )
    list_filter = ("kind", "active", "network", "company")
    search_fields = (
        "address",
        "address_normalized",
        "deployment_transaction_hash",
        "company__legal_name",
    )
    readonly_fields = ("address_normalized", "created_at", "updated_at")

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: ContractInstance | None = None,
    ) -> bool:
        """Require deactivation instead of deleting a deployment record."""

        return False


@admin.register(BlockchainTransaction)
class BlockchainTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "operation_type",
        "status",
        "transaction_hash",
        "submitted_at",
        "confirmed_at",
    )
    list_filter = ("status", "operation_type", "network", "company")
    search_fields = (
        "transaction_hash",
        "block_hash",
        "sender_address",
        "operation_type",
    )
    readonly_fields = ("created_at", "updated_at")

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: BlockchainTransaction | None = None,
    ) -> bool:
        """Keep transaction lifecycle history available for reconciliation."""

        return False


@admin.register(DeploymentConfigurationAttempt)
class DeploymentConfigurationAttemptAdmin(admin.ModelAdmin):
    """Expose configuration attempts as read-only audit records."""

    list_display = (
        "process",
        "kind",
        "transaction_status",
        "transaction_hash",
        "sender_address",
        "is_open",
        "created_at",
    )
    list_filter = ("kind", "transaction__status", "is_open", "process__network")
    search_fields = (
        "transaction__transaction_hash",
        "transaction__sender_address",
        "process__company__legal_name",
        "expected_address",
    )
    fields = (
        "process",
        "kind",
        "transaction",
        "transaction_status",
        "transaction_hash",
        "sender_address",
        "expected_address",
        "prepared_data",
        "submission_started_at",
        "is_open",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields

    def get_queryset(self, request: HttpRequest) -> QuerySet[DeploymentConfigurationAttempt]:
        """Load transaction and company details without editing lifecycle data."""

        return super().get_queryset(request).select_related(
            "process__company",
            "process__network",
            "transaction__contract",
        )

    @admin.display(description="Situação da transação")
    def transaction_status(self, attempt: DeploymentConfigurationAttempt) -> str:
        """Show state from the transaction lifecycle record."""

        return attempt.transaction.get_status_display()

    @admin.display(description="Hash da transação")
    def transaction_hash(self, attempt: DeploymentConfigurationAttempt) -> str:
        """Show the wallet hash when one has been recorded."""

        return attempt.transaction.transaction_hash or "—"

    @admin.display(description="Carteira que enviou")
    def sender_address(self, attempt: DeploymentConfigurationAttempt) -> str:
        """Show the public account recorded as the transaction sender."""

        return attempt.transaction.sender_address

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Require services to create configuration attempts."""

        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: DeploymentConfigurationAttempt | None = None,
    ) -> bool:
        """Keep configuration history append-only."""

        return False


@admin.register(ProcessedEvent)
class ProcessedEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_name",
        "network",
        "transaction_hash",
        "log_index",
        "block_number",
        "processed_at",
    )
    list_filter = ("event_name", "network", "processed_at")
    search_fields = ("event_name", "transaction_hash", "block_hash")
    readonly_fields = (
        "network",
        "contract",
        "transaction_hash",
        "log_index",
        "block_number",
        "block_hash",
        "event_name",
        "processed_at",
        "created_at",
        "updated_at",
    )

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: ProcessedEvent | None = None,
    ) -> bool:
        """Keep processed event records read-only in the admin."""

        return obj is None

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Create event markers only through the idempotent event service."""

        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: ProcessedEvent | None = None,
    ) -> bool:
        """Prevent deleting an idempotency marker from the admin."""

        return False


class DeploymentStepInline(admin.TabularInline):
    """Show all nine deployment steps without allowing manual edits."""

    model = DeploymentStep
    fields = (
        "position",
        "contract_name",
        "step_status",
        "contract_address",
        "latest_transaction_hash",
        "deployment_action",
    )
    readonly_fields = fields
    ordering = ("position",)
    extra = 0
    can_delete = False
    show_change_link = False

    @admin.display(description="Contrato")
    def contract_name(self, step: DeploymentStep) -> str:
        """Show a concise Portuguese name for the contract being deployed."""

        return DEPLOYMENT_KIND_LABELS.get(step.kind, step.get_kind_display())

    @admin.display(description="Situação")
    def step_status(self, step: DeploymentStep) -> str:
        """Show the latest known state without implying an unconfirmed success."""

        attempt = step.linked_deployment_attempt
        if step.contract_id and attempt is None:
            return "Precisa de revisão"

        if attempt is None:
            attempt = self._latest_attempt(step)
        if attempt is None:
            if step.process.status == DeploymentProcess.Status.CANCELED:
                return "Processo cancelado"
            return "Aguardando implantação"

        if (
            attempt.transaction.status == BlockchainTransaction.Status.PREPARED
            and attempt.submission_started_at is not None
        ):
            return "Envio iniciado; hash pendente de conferência"

        if attempt.transaction.status == BlockchainTransaction.Status.CONFIRMED:
            if step.contract_id is None:
                return "Transação confirmada; endereço pendente"
            return "Confirmada" if step.is_confirmed else "Precisa de revisão"

        status_labels = {
            BlockchainTransaction.Status.PREPARED: "Preparada",
            BlockchainTransaction.Status.SUBMITTED: "Aguardando confirmação",
            BlockchainTransaction.Status.CONFIRMED: "Transação confirmada",
            BlockchainTransaction.Status.FAILED: "Falhou",
            BlockchainTransaction.Status.REORGED: "Confirmação revertida",
        }
        return status_labels.get(attempt.transaction.status, "Situação desconhecida")

    @admin.display(description="Endereço do contrato")
    def contract_address(self, step: DeploymentStep) -> str:
        """Show an address only after a contract instance has been recorded."""

        if step.contract_id is None:
            return "—"
        return step.contract.address_normalized

    @admin.display(description="Hash da transação")
    def latest_transaction_hash(self, step: DeploymentStep) -> str:
        """Show the latest deployment transaction hash, if one was submitted."""

        attempt = self._latest_attempt(step)
        if attempt is None:
            return "Ainda não enviada"
        if not attempt.transaction.transaction_hash:
            if attempt.submission_started_at is not None:
                return "Informe a hash retornada pela carteira"
            return "Ainda não enviada"
        return attempt.transaction.transaction_hash

    @admin.display(description="Ação")
    def deployment_action(self, step: DeploymentStep) -> str:
        """Render wallet actions only for an authorized operator on an open process."""

        request = getattr(self, "request", None)
        if (
            request is None
            or step.process.status != DeploymentProcess.Status.IN_PROGRESS
            or not self._user_can_operate(request)
            or step.contract_id is not None
        ):
            return "—"
        if not self._previous_steps_confirmed(step):
            return "Aguardando etapa anterior"

        attempt = self._latest_attempt(step)
        if attempt is not None:
            status = attempt.transaction.status
            if status == BlockchainTransaction.Status.SUBMITTED:
                return self._confirmation_action(step, attempt)
            if status == BlockchainTransaction.Status.CONFIRMED:
                return self._confirmation_action(step, attempt)
            if (
                attempt.is_open
                and status == BlockchainTransaction.Status.PREPARED
                and attempt.submission_started_at is not None
            ):
                register_url = reverse(
                    "admin:blockchain_deploymentprocess_register_hash",
                    args=(step.process_id, step.pk, attempt.pk),
                )
                return format_html(
                    '<div class="deployment-recovery">'
                    '<input type="text" maxlength="66" placeholder="Hash da carteira" '
                    'aria-label="Hash da transação" data-attempt-id="{}" '
                    'data-register-url="{}">'
                    '<button type="button" class="button" data-register-hash>'
                    "Conferir hash"
                    "</button><span class=\"deployment-row-message\" role=\"status\"></span></div>",
                    attempt.pk,
                    register_url,
                )
            button_label = (
                "Continuar tentativa"
                if attempt.is_open and status == BlockchainTransaction.Status.PREPARED
                else "Nova tentativa"
            )
        else:
            button_label = "Conectar carteira e implantar"

        prepare_url = reverse(
            "admin:blockchain_deploymentprocess_prepare_step",
            args=(step.process_id, step.pk),
        )
        return format_html(
            '<button type="button" class="button" data-prepare-url="{}">{}</button>'
            '<span class="deployment-row-message" role="status"></span>',
            prepare_url,
            button_label,
        )

    @staticmethod
    def _confirmation_action(
        step: DeploymentStep,
        attempt: DeploymentAttempt,
    ) -> str:
        """Render a POST action that only reads the saved transaction receipt."""

        confirmation_url = reverse(
            "admin:blockchain_deploymentprocess_confirm_attempt",
            args=(step.process_id, step.pk, attempt.pk),
        )
        return format_html(
            '<button type="button" class="button" data-confirm-url="{}">'
            "Consultar confirmação"
            "</button><span class=\"deployment-row-message\" role=\"status\"></span>",
            confirmation_url,
        )

    def get_queryset(self, request: HttpRequest) -> QuerySet[DeploymentStep]:
        """Load linked contracts and transaction attempts for the nine rows."""

        return super().get_queryset(request).select_related(
            "process", "contract"
        ).prefetch_related("attempts__transaction")

    def get_formset(
        self,
        request: HttpRequest,
        obj: DeploymentProcess | None = None,
        **kwargs: object,
    ) -> type[BaseInlineFormSet]:
        """Keep the current request for permission-aware inline actions."""

        self.request = request
        return super().get_formset(request, obj, **kwargs)

    @staticmethod
    def _previous_steps_confirmed(step: DeploymentStep) -> bool:
        """Allow the wallet action only after every earlier contract is confirmed."""

        previous_steps = (
            DeploymentStep.objects.filter(
                process_id=step.process_id,
                position__lt=step.position,
            )
            .select_related("process", "contract")
            .prefetch_related("attempts__transaction")
        )
        return all(previous.is_confirmed for previous in previous_steps)

    def _user_can_operate(self, request: HttpRequest) -> bool:
        """Use the parent process add permission for wallet actions."""

        app_label = self.parent_model._meta.app_label
        model_name = self.parent_model._meta.model_name
        return request.user.has_perm(f"{app_label}.add_{model_name}")

    @staticmethod
    def _latest_attempt(step: DeploymentStep) -> DeploymentAttempt | None:
        """Return the newest attempt from the prefetched deployment history."""

        attempts = step.attempts.all()
        return max(
            attempts,
            key=lambda attempt: (attempt.created_at, attempt.pk or 0),
            default=None,
        )

    def has_add_permission(
        self,
        request: HttpRequest,
        obj: DeploymentProcess | None = None,
    ) -> bool:
        """Create steps only through the process service and start form."""

        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: DeploymentProcess | None = None,
    ) -> bool:
        """Keep deployment steps read-only in the Django Admin."""

        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: DeploymentProcess | None = None,
    ) -> bool:
        """Preserve the deployment order and its audit trail."""

        return False

    def has_view_permission(
        self,
        request: HttpRequest,
        obj: DeploymentProcess | None = None,
    ) -> bool:
        """Let process viewers see the associated read-only deployment steps."""

        app_label = self.parent_model._meta.app_label
        model_name = self.parent_model._meta.model_name
        return any(
            request.user.has_perm(f"{app_label}.{action}_{model_name}")
            for action in ("view", "change", "add")
        )


@admin.register(DeploymentProcess)
class DeploymentProcessAdmin(admin.ModelAdmin):
    """Start and review the nine-contract process from the platform admin."""

    change_list_template = "admin/blockchain/deploymentprocess/change_list.html"
    change_form_template = "admin/blockchain/deploymentprocess/change_form.html"
    inlines = (DeploymentStepInline,)
    list_display = (
        "company",
        "network",
        "status",
        "confirmed_steps",
        "owner_address_short",
        "created_at",
    )
    list_filter = ("status", "network")
    search_fields = (
        "company__legal_name",
        "company__trade_name",
        "owner_address",
    )
    ordering = ("-created_at",)
    actions = None
    fields = (
        "company_display",
        "network_display",
        "owner_address_display",
        "status_display",
        "progress_display",
        "created_at_display",
    )
    readonly_fields = fields

    @admin.display(description="Contratos confirmados")
    def confirmed_steps(self, process: DeploymentProcess) -> str:
        """Show confirmed contract count in the process list."""

        return f"{process.confirmed_step_count} de 9"

    @admin.display(description="Carteira do dono")
    def owner_address_short(self, process: DeploymentProcess) -> str:
        """Keep the list readable while retaining the full value on detail."""

        return f"{process.owner_address[:8]}…{process.owner_address[-6:]}"

    @admin.display(description="Empresa")
    def company_display(self, process: DeploymentProcess) -> str:
        """Show the company whose contracts are being deployed."""

        return str(process.company)

    @admin.display(description="Rede blockchain")
    def network_display(self, process: DeploymentProcess) -> str:
        """Show the selected network and its chain ID."""

        return f"{process.network.name} (ID {process.network.chain_id})"

    @admin.display(description="Carteira pública do administrador")
    def owner_address_display(self, process: DeploymentProcess) -> str:
        """Show the public wallet assigned as contract owner."""

        return process.owner_address

    @admin.display(description="Situação")
    def status_display(self, process: DeploymentProcess) -> str:
        """Show the process status using the model's Portuguese label."""

        return process.get_status_display()

    @admin.display(description="Progresso")
    def progress_display(self, process: DeploymentProcess) -> str:
        """Show how many of the nine required contracts are confirmed."""

        return f"{process.confirmed_step_count} de 9 contratos confirmados"

    @admin.display(description="Iniciado em")
    def created_at_display(self, process: DeploymentProcess) -> str:
        """Show the timestamp when the local process was started."""

        return process.created_at.strftime("%d/%m/%Y %H:%M")

    def get_queryset(self, request: HttpRequest) -> QuerySet[DeploymentProcess]:
        """Load company and network data and count confirmed contracts."""

        return (
            super()
            .get_queryset(request)
            .select_related("company", "network")
            .prefetch_related(
                "steps__contract",
                "steps__attempts__transaction",
                "configuration_attempts__transaction",
            )
        )

    def get_urls(self) -> list[URLPattern]:
        """Add deployment actions before Django Admin's default routes."""

        custom_urls = [
            path(
                "start/",
                self.admin_site.admin_view(self.start_deployment_view),
                name="blockchain_deploymentprocess_start",
            ),
            path(
                "<path:object_id>/cancel/",
                self.admin_site.admin_view(self.cancel_deployment_view),
                name="blockchain_deploymentprocess_cancel",
            ),
            path(
                "<int:object_id>/steps/<int:step_id>/prepare/",
                self.admin_site.admin_view(require_POST(self.prepare_step_view)),
                name="blockchain_deploymentprocess_prepare_step",
            ),
            path(
                "<int:object_id>/steps/<int:step_id>/attempts/<int:attempt_id>/started/",
                self.admin_site.admin_view(
                    require_POST(self.start_attempt_submission_view)
                ),
                name="blockchain_deploymentprocess_start_attempt_submission",
            ),
            path(
                "<int:object_id>/steps/<int:step_id>/attempts/<int:attempt_id>/hash/",
                self.admin_site.admin_view(require_POST(self.register_attempt_hash_view)),
                name="blockchain_deploymentprocess_register_hash",
            ),
            path(
                "<int:object_id>/steps/<int:step_id>/attempts/<int:attempt_id>/confirm/",
                self.admin_site.admin_view(
                    require_POST(self.confirm_attempt_view)
                ),
                name="blockchain_deploymentprocess_confirm_attempt",
            ),
            path(
                "<int:object_id>/steps/<int:step_id>/attempts/<int:attempt_id>/rejected/",
                self.admin_site.admin_view(require_POST(self.reject_attempt_view)),
                name="blockchain_deploymentprocess_reject_attempt",
            ),
            path(
                "<int:object_id>/configuration/<str:kind>/prepare/",
                self.admin_site.admin_view(
                    require_POST(self.prepare_configuration_view)
                ),
                name="blockchain_deploymentprocess_prepare_configuration",
            ),
            path(
                "<int:object_id>/configuration/attempts/<int:attempt_id>/started/",
                self.admin_site.admin_view(
                    require_POST(self.start_configuration_submission_view)
                ),
                name="blockchain_deploymentprocess_start_configuration_submission",
            ),
            path(
                "<int:object_id>/configuration/attempts/<int:attempt_id>/hash/",
                self.admin_site.admin_view(
                    require_POST(self.register_configuration_hash_view)
                ),
                name="blockchain_deploymentprocess_register_configuration_hash",
            ),
            path(
                "<int:object_id>/configuration/attempts/<int:attempt_id>/confirm/",
                self.admin_site.admin_view(
                    require_POST(self.confirm_configuration_view)
                ),
                name="blockchain_deploymentprocess_confirm_configuration",
            ),
            path(
                "<int:object_id>/configuration/attempts/<int:attempt_id>/rejected/",
                self.admin_site.admin_view(
                    require_POST(self.reject_configuration_view)
                ),
                name="blockchain_deploymentprocess_reject_configuration",
            ),
        ]
        return custom_urls + super().get_urls()

    def add_view(
        self,
        request: HttpRequest,
        form_url: str = "",
        extra_context: dict[str, object] | None = None,
    ) -> HttpResponse:
        """Route the standard Admin add link to the validated start form."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        return redirect("admin:blockchain_deploymentprocess_start")

    def start_deployment_view(self, request: HttpRequest) -> HttpResponse:
        """Create a local deployment process and its nine ordered steps."""

        if not self.has_add_permission(request):
            raise PermissionDenied

        form_data = request.POST if request.method == "POST" else None
        form = DeploymentProcessStartForm(form_data)
        if request.method == "POST" and form.is_valid():
            try:
                process = start_deployment_process(
                    company=form.cleaned_data["company"],
                    network=form.cleaned_data["network"],
                    owner_address=form.cleaned_data["owner_address"],
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(
                    request,
                    "Processo iniciado. Nenhuma transação foi enviada à blockchain.",
                )
                return redirect(
                    reverse(
                        "admin:blockchain_deploymentprocess_change",
                        args=(process.pk,),
                    )
                )

        context = {
            **self.admin_site.each_context(request),
            "title": "Iniciar implantação de contratos",
            "opts": self.model._meta,
            "form": form,
        }
        return TemplateResponse(
            request,
            "admin/blockchain/deploymentprocess/start.html",
            context,
        )

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, object] | None = None,
    ) -> HttpResponse:
        """Add wallet and cancellation context to the read-only process page."""

        context = extra_context or {}
        process = self.get_object(request, object_id)
        can_cancel = (
            process is not None
            and process.status == DeploymentProcess.Status.IN_PROGRESS
            and self.has_add_permission(request)
        )
        context["show_cancel_deployment"] = can_cancel
        context["deployment_wallet_enabled"] = (
            process is not None
            and process.status
            in {
                DeploymentProcess.Status.IN_PROGRESS,
                DeploymentProcess.Status.DEPLOYED,
            }
            and self.has_add_permission(request)
        )
        if can_cancel and process is not None:
            context["cancel_deployment_url"] = reverse(
                "admin:blockchain_deploymentprocess_cancel",
                args=(process.pk,),
            )
        if process is not None:
            context["deployment_chain_id"] = process.network.chain_id
            confirmed_step_count = process.confirmed_step_count
            context["deployment_confirmed_step_count"] = confirmed_step_count
            rows = self._configuration_rows(process)
            context["deployment_configuration_rows"] = rows
            context["show_deployment_configurations"] = (
                confirmed_step_count == 9
                or (
                    process.status == DeploymentProcess.Status.NEEDS_REVIEW
                    and any(row["attempt_id"] is not None for row in rows)
                )
            )
            context["deployment_configuration_enabled"] = (
                process.status == DeploymentProcess.Status.DEPLOYED
                and self.has_add_permission(request)
            )
            context["deployment_configuration_complete"] = (
                process.status == DeploymentProcess.Status.DEPLOYED
                and all(row["status"] == "Concluída" for row in rows)
            )
        return super().change_view(request, object_id, form_url, context)

    def _configuration_rows(
        self,
        process: DeploymentProcess,
    ) -> list[dict[str, object]]:
        """Build read-only status and action data for the two contract links."""

        specifications = (
            (
                DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY,
                "Registro de unidades",
                ContractInstance.Kind.UNIT_REGISTRY,
            ),
            (
                DeploymentConfigurationAttempt.Kind.ISSUANCE_BATCHES,
                "Lotes de emissão",
                ContractInstance.Kind.ISSUANCE_BATCHES,
            ),
        )
        rows: list[dict[str, object]] = []
        for kind, label, target_kind in specifications:
            target_step = process.steps.filter(kind=target_kind).select_related(
                "contract"
            ).first()
            target_address = (
                target_step.contract.address_normalized
                if target_step is not None and target_step.contract_id
                else "—"
            )
            attempt = (
                process.configuration_attempts.filter(kind=kind)
                .select_related("transaction")
                .order_by("-created_at", "-pk")
                .first()
            )
            status = "Aguardando configuração"
            action: str | None = None
            action_url = ""
            attempt_id: int | None = None
            register_url = ""
            confirm_url = ""
            transaction_hash = "—"
            sender_address = "—"

            if attempt is not None:
                record = attempt.transaction
                transaction_hash = record.transaction_hash or "—"
                sender_address = record.sender_address
                attempt_id = attempt.pk

            if process.status == DeploymentProcess.Status.NEEDS_REVIEW:
                if (
                    attempt is not None
                    and attempt.transaction.status == BlockchainTransaction.Status.REORGED
                ):
                    status = "Confirmação revertida; processo precisa de revisão"
                else:
                    status = "Processo precisa de revisão"
            elif process.status != DeploymentProcess.Status.DEPLOYED:
                status = "Disponível após a implantação dos nove contratos"
            elif attempt is None:
                action = "prepare"
            else:
                record = attempt.transaction
                if record.status == BlockchainTransaction.Status.CONFIRMED:
                    status = "Concluída"
                elif record.status == BlockchainTransaction.Status.REORGED:
                    status = "Confirmação revertida; processo precisa de revisão"
                elif record.status == BlockchainTransaction.Status.SUBMITTED:
                    status = "Aguardando confirmação"
                    action = "confirm"
                elif record.status == BlockchainTransaction.Status.FAILED:
                    status = "Falhou; pode tentar novamente"
                    action = "prepare"
                elif attempt.submission_started_at is not None:
                    status = "Envio iniciado; hash pendente de conferência"
                    action = "recover"
                    register_url = reverse(
                        "admin:blockchain_deploymentprocess_register_configuration_hash",
                        args=(process.pk, attempt.pk),
                    )
                else:
                    status = "Preparada"
                    action = "prepare"

            if action == "prepare":
                action_url = reverse(
                    "admin:blockchain_deploymentprocess_prepare_configuration",
                    args=(process.pk, kind),
                )
            elif action == "confirm" and attempt_id is not None:
                confirm_url = reverse(
                    "admin:blockchain_deploymentprocess_confirm_configuration",
                    args=(process.pk, attempt_id),
                )

            rows.append(
                {
                    "kind": kind,
                    "label": label,
                    "expected_address": target_address,
                    "status": status,
                    "transaction_hash": transaction_hash,
                    "sender_address": sender_address,
                    "action": action,
                    "action_url": action_url,
                    "attempt_id": attempt_id,
                    "register_url": register_url,
                    "confirm_url": confirm_url,
                }
            )
        return rows

    def cancel_deployment_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Cancel a process only through an authenticated POST request."""

        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_add_permission(request):
            raise PermissionDenied

        process = self.get_object(request, object_id)
        if process is None:
            raise Http404
        try:
            canceled = cancel_deployment_process(process.pk)
        except ValidationError as error:
            messages.error(request, "; ".join(error.messages))
            canceled = process
        else:
            messages.success(request, "Processo cancelado. O histórico foi preservado.")

        return redirect(
            reverse(
                "admin:blockchain_deploymentprocess_change",
                args=(canceled.pk,),
            )
        )

    def _get_deployment_scope(
        self,
        request: HttpRequest,
        object_id: int,
        step_id: int,
        attempt_id: int | None = None,
    ) -> tuple[DeploymentProcess, DeploymentStep, DeploymentAttempt | None]:
        """Load a step only inside the process and optional attempt from the URL."""

        process = self.get_object(request, str(object_id))
        if process is None:
            raise Http404
        try:
            step = DeploymentStep.objects.get(pk=step_id, process=process)
        except DeploymentStep.DoesNotExist as error:
            raise Http404 from error
        attempt: DeploymentAttempt | None = None
        if attempt_id is not None:
            try:
                attempt = DeploymentAttempt.objects.select_related("transaction").get(
                    pk=attempt_id,
                    step=step,
                )
            except DeploymentAttempt.DoesNotExist as error:
                raise Http404 from error
        return process, step, attempt

    @staticmethod
    def _validation_error_response(error: ValidationError) -> JsonResponse:
        """Return safe, readable validation messages for the browser UI."""

        return JsonResponse({"error": "; ".join(error.messages)}, status=400)

    def prepare_step_view(
        self,
        request: HttpRequest,
        object_id: int,
        step_id: int,
    ) -> JsonResponse:
        """Create or resume a prepared attempt for a scoped deployment step."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        self._get_deployment_scope(request, object_id, step_id)
        try:
            wallet_chain_id = int(request.POST.get("wallet_chain_id", ""))
            prepared = start_deployment_attempt(
                step_id=step_id,
                sender_address=request.POST.get("operator_address", ""),
                wallet_chain_id=wallet_chain_id,
            )
        except ValueError:
            return JsonResponse(
                {"error": "Não foi possível identificar a rede da carteira."},
                status=400,
            )
        except ValidationError as error:
            return self._validation_error_response(error)

        return JsonResponse(
            {
                "attempt_id": prepared.attempt_id,
                "chain_id": prepared.chain_id,
                "transaction_data": prepared.transaction_data,
                "started_url": reverse(
                    "admin:blockchain_deploymentprocess_start_attempt_submission",
                    args=(object_id, step_id, prepared.attempt_id),
                ),
                "register_url": reverse(
                    "admin:blockchain_deploymentprocess_register_hash",
                    args=(object_id, step_id, prepared.attempt_id),
                ),
                "rejected_url": reverse(
                    "admin:blockchain_deploymentprocess_reject_attempt",
                    args=(object_id, step_id, prepared.attempt_id),
                ),
            }
        )

    def start_attempt_submission_view(
        self,
        request: HttpRequest,
        object_id: int,
        step_id: int,
        attempt_id: int,
    ) -> JsonResponse:
        """Save the send-intent marker before JavaScript opens MetaMask."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        self._get_deployment_scope(request, object_id, step_id, attempt_id)
        try:
            attempt = mark_deployment_submission_started(attempt_id)
        except ValidationError as error:
            return self._validation_error_response(error)
        return JsonResponse({"started_at": attempt.submission_started_at.isoformat()})

    def reject_attempt_view(
        self,
        request: HttpRequest,
        object_id: int,
        step_id: int,
        attempt_id: int,
    ) -> JsonResponse:
        """Record only an explicit rejection returned by the connected wallet."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        self._get_deployment_scope(request, object_id, step_id, attempt_id)
        try:
            attempt = fail_deployment_attempt(attempt_id)
        except ValidationError as error:
            return self._validation_error_response(error)
        return JsonResponse({"status": attempt.transaction.status})

    def register_attempt_hash_view(
        self,
        request: HttpRequest,
        object_id: int,
        step_id: int,
        attempt_id: int,
    ) -> JsonResponse:
        """Verify a wallet hash against the saved network before recording it."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        self._get_deployment_scope(request, object_id, step_id, attempt_id)
        try:
            record = register_deployment_transaction_hash(
                attempt_id=attempt_id,
                transaction_hash=request.POST.get("transaction_hash", ""),
            )
        except ValidationError as error:
            return self._validation_error_response(error)
        except (ConnectionError, OSError, ImproperlyConfigured, Web3Exception):
            return JsonResponse(
                {
                    "error": (
                        "Não foi possível conferir a transação no RPC. A tentativa foi mantida; "
                        "envie novamente a mesma hash quando a rede estiver disponível."
                    ),
                },
                status=503,
            )
        if record is None:
            return JsonResponse(
                {
                    "status": "waiting_for_rpc",
                    "message": "A hash ainda não apareceu na rede. Confira novamente esta mesma hash.",
                },
                status=202,
            )
        return JsonResponse(
            {
                "status": "submitted",
                "message": "Transação enviada. Aguardando confirmação da rede.",
                "transaction_hash": record.transaction_hash,
            }
        )

    def confirm_attempt_view(
        self,
        request: HttpRequest,
        object_id: int,
        step_id: int,
        attempt_id: int,
    ) -> JsonResponse:
        """Read an attempt receipt and save its contract after chain success."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        self._get_deployment_scope(request, object_id, step_id, attempt_id)
        try:
            result = confirm_deployment_attempt(attempt_id)
        except ValidationError as error:
            return self._validation_error_response(error)
        except (ConnectionError, OSError, ImproperlyConfigured, Web3Exception):
            return JsonResponse(
                {
                    "error": (
                        "Não foi possível consultar a confirmação no RPC. "
                        "A tentativa foi preservada para nova consulta."
                    ),
                },
                status=503,
            )

        if result.status == "pending":
            return JsonResponse(
                {
                    "status": "pending",
                    "message": "A rede ainda não confirmou esta transação. Consulte novamente depois.",
                },
                status=202,
            )
        if result.status == "failed":
            return JsonResponse(
                {
                    "status": "failed",
                    "message": "A rede rejeitou a criação. Você pode iniciar uma nova tentativa.",
                }
            )
        if result.contract is None:
            return self._validation_error_response(
                ValidationError("A confirmação não retornou o endereço do contrato.")
            )
        return JsonResponse(
            {
                "status": "confirmed",
                "message": f"Contrato confirmado em {result.contract.address_normalized}.",
                "contract_address": result.contract.address_normalized,
            }
        )

    def _get_configuration_scope(
        self,
        request: HttpRequest,
        object_id: int,
        attempt_id: int,
    ) -> tuple[DeploymentProcess, DeploymentConfigurationAttempt]:
        """Load one setup attempt only when it belongs to the URL process."""

        process = self.get_object(request, str(object_id))
        if process is None:
            raise Http404
        try:
            attempt = DeploymentConfigurationAttempt.objects.select_related(
                "transaction__contract"
            ).get(pk=attempt_id, process=process)
        except DeploymentConfigurationAttempt.DoesNotExist as error:
            raise Http404 from error
        return process, attempt

    def prepare_configuration_view(
        self,
        request: HttpRequest,
        object_id: int,
        kind: str,
    ) -> JsonResponse:
        """Create or resume a setup request after checking the connected owner."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        process = self.get_object(request, str(object_id))
        if process is None:
            raise Http404
        try:
            wallet_chain_id = int(request.POST.get("wallet_chain_id", ""))
            prepared = prepare_deployment_configuration(
                process_id=process.pk,
                kind=kind,
                sender_address=request.POST.get("operator_address", ""),
                wallet_chain_id=wallet_chain_id,
            )
        except ValueError:
            return JsonResponse(
                {"error": "Não foi possível identificar a rede da carteira."},
                status=400,
            )
        except ValidationError as error:
            return self._validation_error_response(error)
        except (ConnectionError, OSError, ImproperlyConfigured, Web3Exception):
            return JsonResponse(
                {"error": "Não foi possível consultar os contratos no RPC. Tente novamente."},
                status=503,
            )

        if prepared.status == "already_configured":
            return JsonResponse(
                {
                    "status": "already_configured",
                    "current_address": prepared.current_address,
                    "message": f"Este vínculo já aponta para {prepared.current_address}.",
                }
            )
        if prepared.status == "needs_review":
            return JsonResponse(
                {
                    "status": "needs_review",
                    "current_address": prepared.current_address,
                    "expected_address": prepared.expected_address,
                    "message": "O endereço encontrado não corresponde ao processo. Revisão necessária.",
                },
            )
        if prepared.attempt_id is None:
            return self._validation_error_response(
                ValidationError("O servidor não salvou a tentativa de configuração.")
            )

        return JsonResponse(
            {
                "status": "ready",
                "attempt_id": prepared.attempt_id,
                "chain_id": prepared.chain_id,
                "owner_address": prepared.owner_address,
                "expected_address": prepared.expected_address,
                "transaction_to": prepared.transaction_to,
                "transaction_data": prepared.transaction_data,
                "started_url": reverse(
                    "admin:blockchain_deploymentprocess_start_configuration_submission",
                    args=(object_id, prepared.attempt_id),
                ),
                "register_url": reverse(
                    "admin:blockchain_deploymentprocess_register_configuration_hash",
                    args=(object_id, prepared.attempt_id),
                ),
                "confirm_url": reverse(
                    "admin:blockchain_deploymentprocess_confirm_configuration",
                    args=(object_id, prepared.attempt_id),
                ),
                "rejected_url": reverse(
                    "admin:blockchain_deploymentprocess_reject_configuration",
                    args=(object_id, prepared.attempt_id),
                ),
            }
        )

    def start_configuration_submission_view(
        self,
        request: HttpRequest,
        object_id: int,
        attempt_id: int,
    ) -> JsonResponse:
        """Save the wallet-send marker before opening MetaMask."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        self._get_configuration_scope(request, object_id, attempt_id)
        try:
            attempt = mark_configuration_submission_started(attempt_id)
        except ValidationError as error:
            return self._validation_error_response(error)
        return JsonResponse({"started_at": attempt.submission_started_at.isoformat()})

    def reject_configuration_view(
        self,
        request: HttpRequest,
        object_id: int,
        attempt_id: int,
    ) -> JsonResponse:
        """Close a setup attempt only after an explicit wallet rejection."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        self._get_configuration_scope(request, object_id, attempt_id)
        try:
            attempt = reject_configuration_attempt(attempt_id)
        except ValidationError as error:
            return self._validation_error_response(error)
        return JsonResponse({"status": attempt.transaction.status})

    def register_configuration_hash_view(
        self,
        request: HttpRequest,
        object_id: int,
        attempt_id: int,
    ) -> JsonResponse:
        """Verify the owner wallet transaction before saving its returned hash."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        self._get_configuration_scope(request, object_id, attempt_id)
        try:
            record = register_configuration_transaction_hash(
                attempt_id=attempt_id,
                transaction_hash=request.POST.get("transaction_hash", ""),
            )
        except ValidationError as error:
            return self._validation_error_response(error)
        except (ConnectionError, OSError, ImproperlyConfigured, Web3Exception):
            return JsonResponse(
                {
                    "error": (
                        "Não foi possível conferir a transação no RPC. "
                        "A tentativa foi preservada para receber a mesma hash novamente."
                    ),
                },
                status=503,
            )
        if record is None:
            return JsonResponse(
                {
                    "status": "waiting_for_rpc",
                    "message": "A hash ainda não apareceu na rede. Confira novamente esta mesma hash.",
                },
                status=202,
            )
        return JsonResponse(
            {
                "status": "submitted",
                "transaction_hash": record.transaction_hash,
                "message": "Chamada enviada. Aguardando confirmação da rede.",
            }
        )

    def confirm_configuration_view(
        self,
        request: HttpRequest,
        object_id: int,
        attempt_id: int,
    ) -> JsonResponse:
        """Check receipt, event and final linked address for a setup call."""

        if not self.has_add_permission(request):
            raise PermissionDenied
        self._get_configuration_scope(request, object_id, attempt_id)
        try:
            result = confirm_deployment_configuration(attempt_id)
        except ValidationError as error:
            return self._validation_error_response(error)
        except (ConnectionError, OSError, ImproperlyConfigured, Web3Exception):
            return JsonResponse(
                {
                    "error": (
                        "Não foi possível consultar a confirmação no RPC. "
                        "A tentativa foi preservada para nova consulta."
                    ),
                },
                status=503,
            )

        if result.status == "pending":
            return JsonResponse(
                {
                    "status": "pending",
                    "message": "A rede ainda não confirmou esta chamada. Consulte novamente depois.",
                },
                status=202,
            )
        if result.status == "failed":
            return JsonResponse(
                {
                    "status": "failed",
                    "message": "A rede reverteu a chamada. Uma nova tentativa pode ser iniciada.",
                }
            )
        if result.status == "needs_review":
            return JsonResponse(
                {
                    "status": "needs_review",
                    "expected_address": result.expected_address,
                    "current_address": result.current_address,
                    "message": "A confirmação diverge do endereço esperado. O processo precisa de revisão.",
                },
            )
        return JsonResponse(
            {
                "status": "confirmed",
                "expected_address": result.expected_address,
                "current_address": result.current_address,
                "message": f"Vínculo confirmado em {result.current_address}.",
            }
        )

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: DeploymentProcess | None = None,
    ) -> bool:
        """Keep process records read-only after their creation."""

        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: DeploymentProcess | None = None,
    ) -> bool:
        """Preserve deployment process records and their transaction history."""

        return False

    def has_view_permission(
        self,
        request: HttpRequest,
        obj: DeploymentProcess | None = None,
    ) -> bool:
        """Allow staff with view, change or start permission to review processes."""

        app_label = self.model._meta.app_label
        model_name = self.model._meta.model_name
        return any(
            request.user.has_perm(f"{app_label}.{action}_{model_name}")
            for action in ("view", "change", "add")
        )
