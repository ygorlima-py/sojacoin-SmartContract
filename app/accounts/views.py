from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views import View

from .forms import (
    AccountAuthenticationForm,
    AccountPasswordChangeForm,
    AccountPasswordResetForm,
    AccountSetPasswordForm,
    UserProfileForm,
)
from .rules.account_access import ActiveAccountRequiredMixin


class AccountLoginView(auth_views.LoginView):
    """Authenticate active accounts and redirect only to a safe destination."""

    template_name = "accounts/login.html"
    authentication_form = AccountAuthenticationForm


class AccountLogoutView(auth_views.LogoutView):
    """End sessions through Django's CSRF-protected POST logout."""

    http_method_names = ("post", "options")
    next_page = reverse_lazy("accounts:login")


class ProfileView(ActiveAccountRequiredMixin, View):
    """Display and update the current user's own profile."""

    def get_context_data(self, request, form):
        """Build read-only company and role context for the profile layout."""
        membership = (
            request.user.company_memberships.filter(active=True)
            .select_related("company")
            .prefetch_related("roles")
            .first()
        )
        company_name = "Conta pessoal"
        role_label = "Sem função atribuída"
        if membership:
            company_name = membership.company.trade_name or membership.company.legal_name
            active_roles = [role for role in membership.roles.all() if role.active]
            if active_roles:
                role_label = active_roles[0].get_role_display()
        return {
            "form": form,
            "active_membership": membership,
            "company_name": company_name,
            "primary_role_label": role_label,
        }

    def get(self, request):
        """Display the current profile."""
        form = UserProfileForm(instance=request.user.profile)
        return render(request, "accounts/profile.html", self.get_context_data(request, form))

    def post(self, request):
        """Validate and save permitted profile fields."""
        form = UserProfileForm(request.POST, instance=request.user.profile)
        if form.is_valid():
            form.save()
            messages.success(request, "Perfil atualizado com sucesso.")
            return redirect("accounts:profile")
        return render(request, "accounts/profile.html", self.get_context_data(request, form))


class AccountPasswordChangeView(ActiveAccountRequiredMixin, auth_views.PasswordChangeView):
    """Change the current user's password after confirming the old one."""

    form_class = AccountPasswordChangeForm
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("accounts:password_change_done")


class AccountPasswordChangeDoneView(ActiveAccountRequiredMixin, auth_views.PasswordChangeDoneView):
    """Show confirmation after a successful password change."""
    template_name = "accounts/password_change_done.html"


class AccountPasswordResetView(auth_views.PasswordResetView):
    """Send a reset link for eligible users without revealing account existence."""

    form_class = AccountPasswordResetForm
    template_name = "accounts/password_reset.html"
    email_template_name = "accounts/password_reset_email.txt"
    subject_template_name = "accounts/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")


class AccountPasswordResetDoneView(auth_views.PasswordResetDoneView):
    """Show the same response whether or not the address exists."""
    template_name = "accounts/password_reset_done.html"


class AccountPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """Validate the expiring reset token and set a new password."""

    form_class = AccountSetPasswordForm
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")


class AccountPasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    """Show confirmation and a link back to login."""
    template_name = "accounts/password_reset_complete.html"
