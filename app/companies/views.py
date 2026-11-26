from collections.abc import Mapping
from typing import Any, TypedDict

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db.models import Prefetch, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404
from django.views.generic import (
    CreateView,
    DetailView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)
from accounts.models import UserWallet
from accounts.rules.account_access import ActiveAccountRequiredMixin
from blockchain.models import (
    BlockchainTransaction,
    ContractInstance,
    UnitRegistrationAttempt,
    UnitWalletPermissionAttempt,
    UNIT_REGISTRATION_OPERATION_TYPE,
)

from .forms import (
    CompanyForm,
    CompanyMembershipForm,
    MembershipRoleForm,
    UnitAccessForm,
    UnitForm,
)
from .models import (
    AccessChange,
    Company,
    CompanyMembership,
    MembershipRole,
    Unit,
    UnitAccess,
    UnitWalletPermission,
)
from .rules.company_access import (
    CompanyAccessMixin,
    CompanyAdministratorRequiredMixin,
    has_administrator_role,
)
from .rules.role_access import require_administrator_access, require_role_grant_access
from .services.access_history import (
    grant_membership_role,
    revoke_membership_role,
    set_unit_access,
)
from .services.membership import add_company_membership
from .services.unit_blockchain_registration import (
    confirm_unit_registration_attempt,
    mark_unit_registration_submission_started,
    prepare_unit_registration,
    register_unit_registration_transaction_hash,
    reject_unit_registration_attempt,
)
from .services.unit_registration import save_unit_registration
from .services.unit_wallet_permissions import (
    WalletPermissionCandidate,
    confirm_wallet_permission_attempt,
    get_unit_wallet_permission_candidates,
    mark_wallet_permission_submission_started,
    prepare_unit_wallet_permission,
    register_wallet_permission_transaction_hash,
    reject_wallet_permission_attempt,
)
from utils.identifiers import is_valid_bytes32_hash


class UnitWalletPermissionRow(TypedDict):
    """Describe one wallet permission row presented on the unit detail page."""

    wallet: str
    member: str
    status: str
    permission_active: bool
    attempt: UnitWalletPermissionAttempt | None
    latest_attempt: UnitWalletPermissionAttempt | None
    can_revoke: bool
    membership_id: int | None
    user_wallet_id: int | None
    target: str


class CompanyListView(ActiveAccountRequiredMixin, TemplateView):
    """List every company membership available to the signed-in user."""

    template_name = "companies/company_list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Provide memberships without selecting a company implicitly."""

        context = super().get_context_data(**kwargs)
        context["memberships"] = (
            CompanyMembership.objects.filter(user=self.request.user, active=True)
            .select_related("company")
            .order_by("company__legal_name")
        )
        return context


class CompanyDetailView(CompanyAccessMixin, TemplateView):
    """Display a company only after validating the URL against membership."""

    template_name = "companies/company_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Expose the edit-link state without using it as authorization."""

        context = super().get_context_data(**kwargs)
        context["can_edit"] = has_administrator_role(self.membership)
        return context


class CompanyUpdateView(CompanyAdministratorRequiredMixin, UpdateView):
    """Allow an active company administrator to edit company identity data."""

    model = Company
    form_class = CompanyForm
    template_name = "companies/company_form.html"
    context_object_name = "company"

    def get_object(self, queryset: QuerySet[Company] | None = None) -> Company:
        """Return the company loaded by the membership-scoped context."""

        return self.company

    def get_success_url(self) -> str:
        """Return to the same company after a successful update."""

        return reverse("companies:detail", kwargs={"company_id": self.company.pk})

    def form_valid(self, form: CompanyForm) -> HttpResponse:
        """Save the form and show a confirmation on the next page."""

        response = super().form_valid(form)
        messages.success(self.request, "Dados da empresa atualizados com sucesso.")
        return response


class MemberListView(CompanyAccessMixin, ListView):
    """List the selected company's memberships and their active roles."""

    model = CompanyMembership
    template_name = "companies/member_list.html"
    context_object_name = "members"

    def get_queryset(self) -> QuerySet[CompanyMembership]:
        """Load only this company's members, including inactive memberships."""

        return (
            CompanyMembership.objects.filter(company=self.company)
            .select_related("user", "user__profile")
            .prefetch_related(
                Prefetch(
                    "roles",
                    queryset=MembershipRole.objects.filter(active=True),
                    to_attr="active_roles",
                )
            )
            .order_by("user__username")
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Expose the administrator-only add-member action state."""

        context = super().get_context_data(**kwargs)
        context["can_manage_team"] = has_administrator_role(self.membership)
        return context


class MemberCreateView(CompanyAdministratorRequiredMixin, FormView):
    """Add an existing Django account to the validated company."""

    form_class = CompanyMembershipForm
    template_name = "companies/member_form.html"

    def get_form_kwargs(self) -> dict[str, Any]:
        """Pass only the server-validated company to the form."""

        kwargs = super().get_form_kwargs()
        kwargs["company"] = self.company
        return kwargs

    def get_success_url(self) -> str:
        """Return to the company team after creating the membership."""

        return reverse(
            "companies:member_list",
            kwargs={"company_id": self.company.pk},
        )

    def form_valid(self, form: CompanyMembershipForm) -> HttpResponse:
        """Create the membership and append its access-history record."""

        try:
            add_company_membership(
                company=self.company,
                user=form.cleaned_data["user"],
                actor=self.request.user,
            )
        except ValidationError as error:
            form.add_error("user", error)
            return self.form_invalid(form)

        messages.success(self.request, "Usuário adicionado à equipe.")
        return HttpResponseRedirect(self.get_success_url())


class MemberDetailView(CompanyAccessMixin, DetailView):
    """Display a membership belonging to the selected company."""

    model = CompanyMembership
    template_name = "companies/member_detail.html"
    context_object_name = "member"
    pk_url_kwarg = "membership_id"

    def get_queryset(self) -> QuerySet[CompanyMembership]:
        """Prevent access to memberships belonging to another company."""

        return (
            CompanyMembership.objects.filter(company=self.company)
            .select_related("user", "user__profile")
            .prefetch_related(
                Prefetch(
                    "roles",
                    queryset=MembershipRole.objects.filter(active=True),
                    to_attr="active_roles",
                )
            )
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Expose role-management actions only to company administrators."""

        context = super().get_context_data(**kwargs)
        can_manage_roles = has_administrator_role(self.membership)
        context["can_manage_roles"] = can_manage_roles
        context["can_grant_roles"] = (
            can_manage_roles
            and self.object.active
            and self.object.user_id != self.request.user.pk
        )
        grants = {
            access.unit_id: access
            for access in UnitAccess.objects.filter(
                membership=self.object,
            ).select_related("unit")
        }
        context["can_manage_unit_access"] = can_manage_roles and self.object.active
        context["unit_access_rows"] = [
            {"unit": unit, "access": grants.get(unit.pk)}
            for unit in self.company.units.all().order_by("name")
        ]
        return context


class MemberRoleCreateView(CompanyAdministratorRequiredMixin, FormView):
    """Grant one function to a member of the validated company."""

    form_class = MembershipRoleForm
    template_name = "companies/member_role_form.html"

    def get_target_membership(self) -> CompanyMembership:
        """Load the target membership only inside the selected company."""

        if not hasattr(self, "target_membership"):
            self.target_membership = get_object_or_404(
                CompanyMembership.objects.select_related("user", "company"),
                company=self.company,
                pk=self.kwargs["membership_id"],
            )
        return self.target_membership

    def get_form_kwargs(self) -> dict[str, Any]:
        """Validate the target and keep it outside the submitted form."""

        target_membership = self.get_target_membership()
        require_role_grant_access(self.request.user, target_membership)
        kwargs = super().get_form_kwargs()
        kwargs["membership"] = target_membership
        return kwargs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Show the member receiving the new function."""

        context = super().get_context_data(**kwargs)
        context["member"] = self.get_target_membership()
        return context

    def get_success_url(self) -> str:
        """Return to the target member after a successful grant."""

        return reverse(
            "companies:member_detail",
            kwargs={
                "company_id": self.company.pk,
                "membership_id": self.kwargs["membership_id"],
            },
        )

    def form_valid(self, form: MembershipRoleForm) -> HttpResponse:
        """Grant the role and record the actor and new value."""

        try:
            grant_membership_role(
                membership=self.get_target_membership(),
                role=form.cleaned_data["role"],
                actor=self.request.user,
            )
        except ValidationError as error:
            form.add_error("role", error)
            return self.form_invalid(form)

        messages.success(self.request, "Função concedida ao membro.")
        return HttpResponseRedirect(self.get_success_url())


class MemberRoleRevokeView(CompanyAdministratorRequiredMixin, View):
    """Revoke an active function without deleting its historical record."""

    def get_target_role(self) -> MembershipRole:
        """Load an active role scoped to the company and target membership."""

        return get_object_or_404(
            MembershipRole.objects.select_related("membership", "membership__company"),
            pk=self.kwargs["role_id"],
            membership__company=self.company,
            membership_id=self.kwargs["membership_id"],
            active=True,
        )

    def post(
        self,
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        """Authorize the administrator and revoke the selected role."""

        role = self.get_target_role()
        require_administrator_access(request.user, self.company.pk)
        try:
            revoke_membership_role(role, request.user)
        except ValidationError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Função revogada do membro.")

        return HttpResponseRedirect(
            reverse(
                "companies:member_detail",
                kwargs={
                    "company_id": self.company.pk,
                    "membership_id": self.kwargs["membership_id"],
                },
            )
        )


class MemberUnitAccessUpdateView(CompanyAdministratorRequiredMixin, FormView):
    """Create or update the SaaS access flags for one company unit."""

    form_class = UnitAccessForm
    template_name = "companies/unit_access_form.html"

    def get_target_membership(self) -> CompanyMembership:
        """Load the target member only inside the selected company."""

        if not hasattr(self, "target_membership"):
            self.target_membership = get_object_or_404(
                CompanyMembership.objects.select_related("user", "company"),
                company=self.company,
                pk=self.kwargs["membership_id"],
            )
        if not self.target_membership.active:
            raise PermissionDenied("O vínculo do membro está inativo.")
        return self.target_membership

    def get_target_unit(self) -> Unit:
        """Load the unit only when it belongs to the selected company."""

        if not hasattr(self, "target_unit"):
            self.target_unit = get_object_or_404(
                Unit,
                company=self.company,
                pk=self.kwargs["unit_id"],
            )
        return self.target_unit

    def get_current_access(self) -> UnitAccess | None:
        """Return the existing membership-unit grant, if one exists."""

        return UnitAccess.objects.filter(
            membership=self.get_target_membership(),
            unit=self.get_target_unit(),
        ).first()

    def get_form_kwargs(self) -> dict[str, Any]:
        """Keep member and unit scope in the server-side URL context."""

        kwargs = super().get_form_kwargs()
        kwargs["access"] = self.get_current_access()
        return kwargs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Show the member and unit receiving the SaaS access flags."""

        context = super().get_context_data(**kwargs)
        context["member"] = self.get_target_membership()
        context["unit"] = self.get_target_unit()
        context["current_access"] = self.get_current_access()
        return context

    def get_success_url(self) -> str:
        """Return to the member detail after saving the unit access."""

        return reverse(
            "companies:member_detail",
            kwargs={
                "company_id": self.company.pk,
                "membership_id": self.kwargs["membership_id"],
            },
        )

    def form_valid(self, form: UnitAccessForm) -> HttpResponse:
        """Persist SaaS flags and append an immutable access-history record."""

        try:
            set_unit_access(
                membership=self.get_target_membership(),
                unit=self.get_target_unit(),
                actor=self.request.user,
                can_view=form.cleaned_data["can_view"],
                can_receive=form.cleaned_data["can_receive"],
                can_issue=form.cleaned_data["can_issue"],
                can_audit=form.cleaned_data["can_audit"],
            )
        except ValidationError as error:
            form.add_error(None, error)
            return self.form_invalid(form)

        messages.success(self.request, "Acesso à unidade atualizado.")
        return HttpResponseRedirect(self.get_success_url())


class AccessHistoryListView(CompanyAdministratorRequiredMixin, ListView):
    """Show immutable SaaS access changes in plain language."""

    model = AccessChange
    template_name = "companies/access_history_list.html"
    context_object_name = "changes"
    paginate_by = 50

    ACTION_LABELS = {
        AccessChange.Action.MEMBERSHIP_GRANTED: "Membro adicionado",
        AccessChange.Action.MEMBERSHIP_REVOKED: "Membro desativado",
        AccessChange.Action.ROLE_GRANTED: "Função concedida",
        AccessChange.Action.ROLE_REVOKED: "Função revogada",
        AccessChange.Action.UNIT_ACCESS_UPDATED: "Acesso à unidade alterado",
        AccessChange.Action.WALLET_PERMISSION_UPDATED: "Permissão de carteira alterada",
    }
    ROLE_LABELS = {
        MembershipRole.Role.ADMINISTRATOR: "Administrador",
        MembershipRole.Role.REGISTRATION: "Cadastro",
        MembershipRole.Role.RECEIVING: "Recebimento",
        MembershipRole.Role.ISSUANCE: "Emissão",
        MembershipRole.Role.AUDITOR: "Auditoria",
    }
    PERMISSION_LABELS = {
        "can_view": "Visualização",
        "can_receive": "Recebimento",
        "can_issue": "Emissão",
        "can_audit": "Auditoria",
    }

    def get_queryset(self) -> QuerySet[AccessChange]:
        """Load only this company's history with display relationships."""

        return (
            AccessChange.objects.filter(company=self.company)
            .select_related(
                "company",
                "membership__user__profile",
                "unit",
                "actor__profile",
            )
            .order_by("-occurred_at", "-pk")
        )

    def _permission_text(self, value: object) -> str:
        """Translate a stored permission flag for display."""

        return "Concedida" if value else "Não concedida"

    def _role_text(self, value: object) -> str:
        """Translate a role code for display."""

        return self.ROLE_LABELS.get(str(value), "Não informada")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Prepare safe, readable summaries without exposing stored JSON."""

        context = super().get_context_data(**kwargs)
        context["history_rows"] = [
            self._history_row(change)
            for change in context["changes"]
        ]
        return context

    def _history_row(self, change: AccessChange) -> dict[str, object]:
        """Build the complete presentation context for one history entry."""

        previous = change.previous_value or {}
        current = change.new_value or {}
        action_label = self.ACTION_LABELS.get(change.action, change.get_action_display())
        details: list[dict[str, str]] = []

        if change.action == AccessChange.Action.UNIT_ACCESS_UPDATED:
            if current.get("active") is False:
                action_label = "Acesso à unidade revogado"
            elif not previous:
                action_label = "Acesso à unidade criado"
            elif previous.get("active") is False:
                action_label = "Acesso à unidade reativado"
            for field, label in self.PERMISSION_LABELS.items():
                if current.get("active") is False:
                    if field in previous:
                        details.append({"label": label, "after": self._permission_text(previous[field]), "before": ""})
                elif field in current:
                    old = previous.get(field)
                    new = current[field]
                    if previous.get("active") is True and field in previous and old == new:
                        continue
                    before = self._permission_text(old) if previous.get("active") is True and field in previous else ""
                    details.append({"label": label, "before": before, "after": self._permission_text(new)})
        elif change.action in {AccessChange.Action.ROLE_GRANTED, AccessChange.Action.ROLE_REVOKED}:
            role = current.get("role", previous.get("role"))
            details.append({"label": "Função", "before": "", "after": self._role_text(role)})
        elif change.action == AccessChange.Action.WALLET_PERMISSION_UPDATED:
            permission = str(current.get("permission", previous.get("permission", "")))
            action = str(current.get("action", ""))
            was_active = bool(previous.get("active", False))
            is_active = bool(current.get("active", False))
            action_label = (
                f"Permissão de {permission.lower()} concedida"
                if action == "grant" and is_active
                else f"Permissão de {permission.lower()} revogada"
            )
            wallet = str(current.get("wallet", previous.get("wallet", "—")))
            details.extend(
                (
                    {"label": "Carteira", "before": "", "after": wallet},
                    {"label": "Permissão", "before": "", "after": permission or "—"},
                    {
                        "label": "Situação",
                        "before": "Ativa" if was_active else "Sem permissão",
                        "after": "Ativa" if is_active else "Revogada",
                    },
                )
            )

        return {
            "change": change,
            "action_label": action_label,
            "details": details,
        }


class UnitListView(CompanyAccessMixin, ListView):
    """List only the units belonging to the validated company context."""

    model = Unit
    template_name = "companies/unit_list.html"
    context_object_name = "units"

    def get_queryset(self) -> QuerySet:
        """Filter units by the company loaded by ``CompanyAccessMixin``."""

        return self.company.units.all().order_by("name")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Expose the unit list and the administrator-only action state."""

        context = super().get_context_data(**kwargs)
        context["can_manage_units"] = has_administrator_role(self.membership)
        return context


class UnitDetailView(CompanyAccessMixin, DetailView):
    """Display a unit only when it belongs to the selected company."""

    model = Unit
    template_name = "companies/unit_detail.html"
    context_object_name = "unit"
    pk_url_kwarg = "unit_id"

    def get_queryset(self) -> QuerySet:
        """Limit object lookup to units owned by the validated company."""

        return self.company.units.all()

    def _wallet_permission_rows(self) -> dict[str, list[UnitWalletPermissionRow]]:
        """Combine the latest confirmed snapshot with each wallet's latest attempt."""

        snapshots = {
            row.wallet: row
            for row in UnitWalletPermission.objects.filter(unit=self.object).select_related(
                "membership__user"
            )
        }
        attempts = list(
            UnitWalletPermissionAttempt.objects.filter(unit=self.object)
            .select_related(
                "membership__user",
                "user_wallet__wallet",
                "transaction__network",
                "transaction__contract",
            )
            .order_by("-created_at", "-pk")
        )
        latest_attempts: dict[tuple[str, str], UnitWalletPermissionAttempt] = {}
        for attempt in attempts:
            key = (attempt.permission_kind, attempt.wallet_address)
            latest_attempts.setdefault(key, attempt)

        result: dict[str, list[UnitWalletPermissionRow]] = {"receive": [], "issue": []}
        # These are the UnitRegistry constants PERMISSION_RECEIVE_SOYBEANS (1) and
        # PERMISSION_ISSUE_SJC (2), used only to read the confirmed mask snapshot.
        permission_bits = {"receive": 1, "issue": 2}
        for permission_kind, bit in permission_bits.items():
            addresses = {
                wallet for wallet, snapshot in snapshots.items()
                if int(snapshot.permission_mask) & bit
            }
            addresses.update(
                wallet for kind, wallet in latest_attempts if kind == permission_kind
            )

            for wallet in sorted(addresses):
                snapshot = snapshots.get(wallet)
                attempt = latest_attempts.get((permission_kind, wallet))
                permission_active = bool(
                    snapshot
                    and snapshot.sync_status == UnitWalletPermission.SyncStatus.CONFIRMED
                    and int(snapshot.permission_mask) & bit
                )
                pending = bool(attempt and attempt.is_open)
                failed = bool(
                    attempt
                    and attempt.transaction.status == BlockchainTransaction.Status.FAILED
                )

                if pending:
                    status = (
                        "Aguardando confirmação"
                        if attempt.action == UnitWalletPermissionAttempt.Action.GRANT
                        else "Aguardando revogação"
                    )
                elif failed and permission_active:
                    status = "Ativa; revogação não concluída"
                elif failed:
                    status = "Falhou"
                elif permission_active:
                    status = "Ativa"
                elif attempt and attempt.action == UnitWalletPermissionAttempt.Action.REVOKE:
                    status = "Revogada"
                else:
                    status = "Sem permissão ativa"

                membership = (
                    snapshot.membership if snapshot and snapshot.membership_id else None
                ) or (attempt.membership if attempt else None)
                user_wallet = attempt.user_wallet if attempt else None
                if user_wallet is None and membership is not None:
                    user_wallet = (
                        UserWallet.objects.filter(
                            user_id=membership.user_id,
                            wallet__address_normalized=wallet,
                        )
                        .select_related("wallet")
                        .first()
                    )

                result[permission_kind].append(
                    {
                        "wallet": wallet,
                        "member": str(membership) if membership else "Pessoa não vinculada",
                        "status": status,
                        "permission_active": permission_active,
                        "attempt": attempt if pending else None,
                        "latest_attempt": attempt,
                        "can_revoke": bool(
                            permission_active and not pending and membership and user_wallet
                        ),
                        "membership_id": membership.pk if membership else None,
                        "user_wallet_id": user_wallet.pk if user_wallet else None,
                        "target": (
                            f"{membership.pk}:{user_wallet.pk}"
                            if membership and user_wallet
                            else ""
                        ),
                    }
                )

        return result

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Expose unit status, available registries and the latest attempt."""

        context = super().get_context_data(**kwargs)
        can_manage_units = has_administrator_role(self.membership)
        attempts = UnitRegistrationAttempt.objects.filter(unit=self.object).select_related(
            "transaction__network",
            "transaction__contract",
        )
        open_attempt = attempts.filter(is_open=True).order_by("-created_at", "-pk").first()
        latest_attempt = attempts.order_by("-created_at", "-pk").first()

        # Offer only networks with both required active deployments for this company.
        registry_rows: list[dict[str, Any]] = []
        registries = ContractInstance.objects.filter(
            company=self.company,
            kind=ContractInstance.Kind.UNIT_REGISTRY,
            active=True,
            network__active=True,
        ).select_related("network").order_by("network__name")
        for registry in registries:
            has_soja_coin = ContractInstance.objects.filter(
                company=self.company,
                network_id=registry.network_id,
                kind=ContractInstance.Kind.SOJA_COIN,
                active=True,
            ).exists()
            if has_soja_coin:
                registry_rows.append(
                    {
                        "network": registry.network,
                        "registry": registry,
                    }
                )

        context["can_manage_units"] = can_manage_units
        context["unit_registry_options"] = registry_rows
        context["unit_registration_attempt"] = open_attempt or latest_attempt
        context["blockchain_transaction"] = (
            latest_attempt.transaction if latest_attempt is not None else None
        )
        registration_hash_valid = is_valid_bytes32_hash(self.object.registration_hash)
        context["unit_registration_hash_valid"] = registration_hash_valid
        context["can_register_unit_on_chain"] = (
            can_manage_units
            and self.company.active
            and self.object.active
            and self.object.chain_unit_id is None
            and registration_hash_valid
            and bool(registry_rows)
        )

        # The permission operation must use the same confirmed deployment that
        # wrote the unit's chain ID; the browser cannot choose another network.
        permission_registration = (
            UnitRegistrationAttempt.objects.filter(
                unit=self.object,
                transaction__status=BlockchainTransaction.Status.CONFIRMED,
                transaction__operation_type=UNIT_REGISTRATION_OPERATION_TYPE,
                transaction__contract__kind=ContractInstance.Kind.UNIT_REGISTRY,
            )
            .select_related("transaction__network", "transaction__contract")
            .order_by("-transaction__confirmed_at", "-pk")
            .first()
            if self.object.chain_unit_id is not None
            else None
        )
        context["wallet_permission_registration"] = permission_registration
        context["wallet_permission_rows"] = self._wallet_permission_rows()
        receive_candidates: list[WalletPermissionCandidate] = (
            get_unit_wallet_permission_candidates(
                self.object,
                "receive",
            )
        )
        issue_candidates: list[WalletPermissionCandidate] = (
            get_unit_wallet_permission_candidates(
                self.object,
                "issue",
            )
        )
        context["receive_wallet_candidates"] = receive_candidates
        context["issue_wallet_candidates"] = issue_candidates
        permission_rows = context["wallet_permission_rows"]
        context["wallet_permission_sections"] = [
            (
                "receive",
                "Recebimento",
                receive_candidates,
                permission_rows["receive"],
            ),
            (
                "issue",
                "Emissão",
                issue_candidates,
                permission_rows["issue"],
            ),
        ]
        context["can_manage_wallet_permissions"] = bool(
            can_manage_units
            and self.company.active
            and self.object.active
            and permission_registration is not None
            and permission_registration.transaction.contract.active
            and permission_registration.transaction.network.active
        )
        return context


class UnitRegistrationActionView(CompanyAdministratorRequiredMixin, View):
    """Share company and unit scoping for unit-registration POST endpoints."""

    def get_unit(self) -> Unit:
        """Load the target unit only inside the administrator's company."""

        return get_object_or_404(
            Unit.objects.select_related("company"),
            pk=self.kwargs["unit_id"],
            company_id=self.company.pk,
        )

    def get_attempt(self) -> UnitRegistrationAttempt:
        """Load an attempt only when it belongs to this URL's unit and company."""

        return get_object_or_404(
            UnitRegistrationAttempt.objects.select_related(
                "unit",
                "transaction__network",
                "transaction__contract",
            ),
            pk=self.kwargs["attempt_id"],
            unit_id=self.get_unit().pk,
            transaction__company_id=self.company.pk,
        )

    def validation_error(self, error: ValidationError) -> JsonResponse:
        """Return Django validation details as a small JSON error response."""

        return JsonResponse({"error": "; ".join(error.messages)}, status=400)

    def service_unavailable(self, error: Exception) -> JsonResponse:
        """Tell the browser to keep the attempt and retry the same operation."""

        return JsonResponse(
            {
                "error": str(error)
                or "Não foi possível consultar a rede agora. Tente novamente depois."
            },
            status=503,
        )


class UnitRegistrationPrepareView(UnitRegistrationActionView):
    """Prepare and persist the exact call before the browser opens MetaMask."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Validate wallet/network data and return the saved attempt request."""

        unit = self.get_unit()
        try:
            prepared = prepare_unit_registration(
                company_id=self.company.pk,
                unit_id=unit.pk,
                network_id=int(request.POST.get("network_id", "")),
                sender_address=request.POST.get("sender_address", ""),
                wallet_chain_id=int(request.POST.get("wallet_chain_id", ""), 10),
            )
        except (TypeError, ValueError):
            return JsonResponse(
                {"error": "Selecione uma rede e conecte uma carteira válida."},
                status=400,
            )
        except ValidationError as error:
            return self.validation_error(error)
        except (ConnectionError, ImproperlyConfigured) as error:
            return self.service_unavailable(error)

        attempt_kwargs = {
            "company_id": self.company.pk,
            "unit_id": unit.pk,
            "attempt_id": prepared.attempt.pk,
        }
        result: dict[str, Any] = {
            "status": prepared.status,
            "attempt_id": prepared.attempt.pk,
            "chain_id": prepared.chain_id,
            "owner_address": prepared.owner_address,
            "contract_address": prepared.contract_address,
            "transaction_data": prepared.transaction_data,
            "started_url": reverse("companies:unit_registration_started", kwargs=attempt_kwargs),
            "rejected_url": reverse("companies:unit_registration_rejected", kwargs=attempt_kwargs),
            "register_url": reverse("companies:unit_registration_hash", kwargs=attempt_kwargs),
            "confirm_url": reverse("companies:unit_registration_confirm", kwargs=attempt_kwargs),
        }
        if prepared.status == "submitted":
            result["message"] = "Esta tentativa já foi enviada. Consulte a confirmação da rede."
        elif prepared.status == "recover":
            result["message"] = (
                "Esta tentativa já abriu a carteira. Informe a hash dela; não envie outra transação."
            )
        else:
            result["message"] = "Tentativa preparada. Confirme a operação na carteira."
        return JsonResponse(result)


class UnitRegistrationStartedView(UnitRegistrationActionView):
    """Save a send-intent marker before a wallet confirmation prompt is opened."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Mark the existing attempt as potentially submitted by the wallet."""

        attempt = self.get_attempt()
        try:
            mark_unit_registration_submission_started(attempt.pk)
        except ValidationError as error:
            return self.validation_error(error)
        return JsonResponse({"status": "ready"})


class UnitRegistrationRejectedView(UnitRegistrationActionView):
    """Record an explicit rejection from the connected wallet."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Close the rejected attempt while preserving its transaction record."""

        attempt = self.get_attempt()
        try:
            reject_unit_registration_attempt(attempt.pk)
        except ValidationError as error:
            return self.validation_error(error)
        return JsonResponse({"status": "failed", "message": "Envio recusado pela carteira."})


class UnitRegistrationHashView(UnitRegistrationActionView):
    """Verify the wallet-returned hash against the saved call on the RPC."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Save a verified hash or ask the browser to retry with that same hash."""

        attempt = self.get_attempt()
        try:
            transaction_record = register_unit_registration_transaction_hash(
                attempt_id=attempt.pk,
                transaction_hash=request.POST.get("transaction_hash", ""),
            )
        except ValidationError as error:
            return self.validation_error(error)
        except (ConnectionError, ImproperlyConfigured) as error:
            return self.service_unavailable(error)

        if transaction_record is None:
            return JsonResponse(
                {
                    "status": "waiting_for_rpc",
                    "message": (
                        "A rede ainda não encontrou esta transação. Tente conferir a mesma hash novamente."
                    ),
                },
                status=202,
            )
        return JsonResponse(
            {
                "status": "submitted",
                "message": "Hash conferida. Aguardando confirmação da rede.",
            }
        )


class UnitRegistrationConfirmView(UnitRegistrationActionView):
    """Query the saved transaction receipt without opening or sending a wallet call."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Return the chain result after validating its UnitRegistered event."""

        attempt = self.get_attempt()
        try:
            result = confirm_unit_registration_attempt(attempt.pk)
        except ValidationError as error:
            return self.validation_error(error)
        except (ConnectionError, ImproperlyConfigured) as error:
            return self.service_unavailable(error)

        response: dict[str, Any] = {
            "status": result.status,
            "chain_unit_id": result.chain_unit_id,
        }
        if result.status == "pending":
            response["message"] = "A rede ainda não confirmou esta transação. Consulte novamente depois."
        elif result.status == "failed":
            response["message"] = "A transação falhou na rede. Você pode iniciar uma nova tentativa."
        else:
            response["message"] = (
                f"Unidade registrada na blockchain. ID: {result.chain_unit_id}."
            )
        return JsonResponse(response)


class UnitWalletPermissionActionView(CompanyAdministratorRequiredMixin, View):
    """Scope wallet-permission endpoints to the active company and unit."""

    def get_unit(self) -> Unit:
        """Load only the unit belonging to the company validated by the mixin."""

        return get_object_or_404(
            Unit.objects.select_related("company"),
            pk=self.kwargs["unit_id"],
            company_id=self.company.pk,
        )

    def get_attempt(self) -> UnitWalletPermissionAttempt:
        """Hide an attempt unless its unit and transaction match the URL company."""

        return get_object_or_404(
            UnitWalletPermissionAttempt.objects.select_related(
                "unit",
                "membership__user",
                "user_wallet__wallet",
                "transaction__network",
                "transaction__contract",
            ),
            pk=self.kwargs["attempt_id"],
            unit_id=self.get_unit().pk,
            transaction__company_id=self.company.pk,
        )

    def validation_error(self, error: ValidationError) -> JsonResponse:
        """Return a clear form-level error without changing the saved attempt."""

        return JsonResponse({"error": "; ".join(error.messages)}, status=400)


class UnitWalletPermissionPrepareView(UnitWalletPermissionActionView):
    """Save a verified permission call before asking the owner to sign it."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Resolve the selected member wallet and return the MetaMask request."""

        target_parts = request.POST.get("target", "").split(":")
        if len(target_parts) != 2:
            return JsonResponse({"error": "Selecione uma pessoa e uma carteira."}, status=400)
        try:
            membership_id, user_wallet_id = (int(value) for value in target_parts)
            wallet_chain_id = int(request.POST.get("wallet_chain_id", ""), 10)
        except (TypeError, ValueError):
            return JsonResponse({"error": "A pessoa, carteira ou rede é inválida."}, status=400)

        try:
            prepared = prepare_unit_wallet_permission(
                company_id=self.company.pk,
                unit_id=self.get_unit().pk,
                membership_id=membership_id,
                user_wallet_id=user_wallet_id,
                permission_kind=self.kwargs["permission_kind"],
                action=self.kwargs["action"],
                owner_address=request.POST.get("owner_address", ""),
                wallet_chain_id=wallet_chain_id,
                actor=request.user,
            )
        except ValidationError as error:
            return self.validation_error(error)
        except (ConnectionError, ImproperlyConfigured) as error:
            return JsonResponse({"error": str(error)}, status=503)

        attempt_kwargs = {
            "company_id": self.company.pk,
            "unit_id": self.get_unit().pk,
            "attempt_id": prepared.attempt.pk,
        }
        return JsonResponse(
            {
                "status": prepared.status,
                "attempt_id": prepared.attempt.pk,
                "chain_id": prepared.chain_id,
                "owner_address": prepared.owner_address,
                "contract_address": prepared.contract_address,
                "transaction_data": prepared.transaction_data,
                "started_url": reverse(
                    "companies:unit_wallet_permission_started", kwargs=attempt_kwargs
                ),
                "rejected_url": reverse(
                    "companies:unit_wallet_permission_rejected", kwargs=attempt_kwargs
                ),
                "register_url": reverse(
                    "companies:unit_wallet_permission_hash", kwargs=attempt_kwargs
                ),
                "confirm_url": reverse(
                    "companies:unit_wallet_permission_confirm", kwargs=attempt_kwargs
                ),
            }
        )


class UnitWalletPermissionStartedView(UnitWalletPermissionActionView):
    """Persist the intent to open the owner's wallet prompt."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Mark the attempt as uncertain before the browser sends the call."""

        try:
            attempt = mark_wallet_permission_submission_started(self.get_attempt().pk)
        except ValidationError as error:
            return self.validation_error(error)
        return JsonResponse({"status": "ready", "attempt_id": attempt.pk})


class UnitWalletPermissionRejectedView(UnitWalletPermissionActionView):
    """Record a definitive cancellation returned by the connected wallet."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Close the attempt while keeping its transaction history."""

        try:
            reject_wallet_permission_attempt(self.get_attempt().pk)
        except ValidationError as error:
            return self.validation_error(error)
        return JsonResponse(
            {"status": "failed", "message": "A carteira recusou a alteração de permissão."}
        )


class UnitWalletPermissionHashView(UnitWalletPermissionActionView):
    """Verify and save the transaction hash returned by the owner wallet."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Keep the same attempt open if its transaction is not visible in the RPC yet."""

        try:
            transaction_record = register_wallet_permission_transaction_hash(
                attempt_id=self.get_attempt().pk,
                transaction_hash=request.POST.get("transaction_hash", ""),
            )
        except ValidationError as error:
            return self.validation_error(error)
        except (ConnectionError, ImproperlyConfigured) as error:
            return JsonResponse({"error": str(error)}, status=503)

        if transaction_record is None:
            return JsonResponse(
                {
                    "status": "waiting_for_rpc",
                    "message": "A rede ainda não encontrou a transação. Confira novamente a mesma hash.",
                },
                status=202,
            )
        return JsonResponse(
            {"status": "submitted", "message": "Transação enviada. Aguardando confirmação."}
        )


class UnitWalletPermissionConfirmView(UnitWalletPermissionActionView):
    """Query the existing permission transaction without opening the wallet."""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        """Return pending, failed or confirmed status for the saved attempt."""

        try:
            result = confirm_wallet_permission_attempt(self.get_attempt().pk)
        except ValidationError as error:
            return self.validation_error(error)
        except (ConnectionError, ImproperlyConfigured) as error:
            return JsonResponse({"error": str(error)}, status=503)

        messages = {
            "pending": "A rede ainda não confirmou esta alteração. Consulte novamente depois.",
            "failed": "A transação falhou. Você poderá tentar novamente.",
            "confirmed": "Permissão atualizada e confirmada na blockchain.",
        }
        return JsonResponse(
            {
                "status": result.status,
                "message": messages[result.status],
                "transaction_hash": result.transaction.transaction_hash,
            }
        )


class UnitCreateView(CompanyAdministratorRequiredMixin, CreateView):
    """Create and hash a unit locally for the validated active company."""

    model = Unit
    form_class = UnitForm
    template_name = "companies/unit_form.html"
    context_object_name = "unit"

    def get_form_kwargs(self) -> dict[str, Any]:
        """Bind the form to a new unit owned by the validated company."""

        kwargs = super().get_form_kwargs()
        kwargs["instance"] = Unit(company=self.company)
        return kwargs

    def get_success_url(self) -> str:
        """Return to the detail page of the newly created unit."""

        return reverse(
            "companies:unit_detail",
            kwargs={"company_id": self.company.pk, "unit_id": self.object.pk},
        )

    def form_valid(self, form: UnitForm) -> HttpResponse:
        """Validate and persist the local registration without blockchain I/O."""

        try:
            self.object = save_unit_registration(form.instance, form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
            return self.form_invalid(form)

        messages.success(self.request, "Unidade cadastrada localmente.")
        return HttpResponseRedirect(self.get_success_url())


class UnitUpdateView(CompanyAdministratorRequiredMixin, UpdateView):
    """Edit a company unit locally while preserving its on-chain ID."""

    model = Unit
    form_class = UnitForm
    template_name = "companies/unit_form.html"
    context_object_name = "unit"
    pk_url_kwarg = "unit_id"

    def get_queryset(self) -> QuerySet:
        """Limit the editable unit lookup to the validated company."""

        return self.company.units.all()

    def get_success_url(self) -> str:
        """Return to the detail page of the updated unit."""

        return reverse(
            "companies:unit_detail",
            kwargs={"company_id": self.company.pk, "unit_id": self.object.pk},
        )

    def form_valid(self, form: UnitForm) -> HttpResponse:
        """Validate and persist the local registration without blockchain I/O."""

        try:
            self.object = save_unit_registration(form.instance, form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
            return self.form_invalid(form)

        messages.success(self.request, "Unidade atualizada localmente.")
        return HttpResponseRedirect(self.get_success_url())
