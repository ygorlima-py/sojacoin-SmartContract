from collections.abc import Mapping
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch, QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
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

from accounts.rules.account_access import ActiveAccountRequiredMixin

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
from .services.unit_registration import save_unit_registration


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

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Expose the administrator-only unit management action state."""

        context = super().get_context_data(**kwargs)
        context["can_manage_units"] = has_administrator_role(self.membership)
        return context


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
