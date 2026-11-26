"""Company home page views."""

from typing import Any

from django.views.generic import TemplateView

from companies.rules.company_access import CompanyAccessMixin

from .services import build_home_snapshot


class CompanyHomeView(CompanyAccessMixin, TemplateView):
    """Show an authorized member a read-only summary of company operations."""

    template_name = "dashboard/home.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add only the records and actions allowed by this membership."""

        context = super().get_context_data(**kwargs)
        context["home"] = build_home_snapshot(self.company, self.membership)
        return context
