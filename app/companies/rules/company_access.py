"""Company context and membership rules for class-based views."""

from typing import Any

from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404

from accounts.rules.account_access import ActiveAccountRequiredMixin

from companies.models import Company, CompanyMembership, MembershipRole


def get_membership_for_user(
    user: AbstractBaseUser | None,
    company_id: int,
) -> CompanyMembership:
    """Return the user's active membership or hide the company with 404."""

    if not user.is_authenticated:
        raise Http404

    return get_object_or_404(
        CompanyMembership.objects.select_related("company"),
        user=user,
        company_id=company_id,
        active=True,
    )


def require_active_company(membership: CompanyMembership) -> Company:
    """Raise 403 when an otherwise valid company is inactive."""

    if not membership.company.active:
        raise PermissionDenied("This company is inactive.")

    return membership.company


def has_administrator_role(membership: CompanyMembership) -> bool:
    """Return whether a membership has an active administrator role."""

    return membership.roles.filter(
        role=MembershipRole.Role.ADMINISTRATOR,
        active=True,
    ).exists()


class CompanyContextMixin:
    """Load company context only from a validated membership and URL id."""

    company_url_kwarg: str = "company_id"
    company_session_key: str = "active_company_id"
    require_active_company: bool = False
    membership: CompanyMembership
    company: Company

    def dispatch(
        self,
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        """Validate the URL company before executing the child view."""

        company_id = kwargs.get(self.company_url_kwarg)
        if company_id is None:
            raise ImproperlyConfigured(
                f"{self.__class__.__name__} requires '{self.company_url_kwarg}'."
            )

        self.membership = get_membership_for_user(request.user, company_id)
        self.company = self.membership.company
        if self.require_active_company:
            require_active_company(self.membership)
        self.check_company_permissions(request)

        # The session is a convenience for the UI; the URL membership check
        # above remains the authority for every request.
        request.session[self.company_session_key] = self.company.pk
        return super().dispatch(request, *args, **kwargs)

    def check_company_permissions(self, request: HttpRequest) -> None:
        """Provide a hook for views that need a role beyond membership."""

        return None

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Expose the validated company and membership to the template."""

        context = super().get_context_data(**kwargs)
        context.update(
            {
                "company": self.company,
                "membership": self.membership,
                "can_manage_company": has_administrator_role(self.membership),
            }
        )
        return context


class CompanyAccessMixin(ActiveAccountRequiredMixin, CompanyContextMixin):
    """Protect a company view with account and membership checks."""


class ActiveCompanyRequiredMixin(CompanyAccessMixin):
    """Also block operations when the selected company is inactive."""

    require_active_company = True


class CompanyAdministratorRequiredMixin(ActiveCompanyRequiredMixin):
    """Require an active administrator role for company-level changes."""

    def check_company_permissions(self, request: HttpRequest) -> None:
        """Reject members who are not active company administrators."""

        if not has_administrator_role(self.membership):
            raise PermissionDenied("The user is not a company administrator.")
