import json

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.http import HttpRequest, HttpResponse, JsonResponse
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
from .models import UserWallet
from .rules.account_access import ActiveAccountRequiredMixin
from .services.wallet_verification import (
    consume_wallet_verification_challenge,
    create_wallet_verification_challenge,
)
from blockchain.models import Network


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
            "wallet_links": UserWallet.objects.filter(user=request.user)
            .select_related("wallet")
            .order_by("wallet__label", "wallet__address_normalized"),
            "wallet_networks": Network.objects.filter(active=True).order_by("name"),
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


class WalletChallengeCreateView(ActiveAccountRequiredMixin, View):
    """Create an expiring ownership challenge for the signed-in user's wallet."""

    http_method_names = ["post", "options"]

    def post(self, request: HttpRequest) -> JsonResponse:
        """Return the exact challenge message that MetaMask must sign."""

        try:
            payload = json.loads(request.body or b"{}")
            address = payload.get("address", "")
            raw_chain_id = payload.get("chain_id")
            if isinstance(raw_chain_id, bool):
                raise ValueError
            chain_id = int(raw_chain_id)
            challenge = create_wallet_verification_challenge(
                request.user,
                address,
                chain_id,
            )
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            return JsonResponse(
                {"error": "Informe um endereço e uma rede válidos."},
                status=400,
            )
        except ValidationError as error:
            return JsonResponse({"error": _wallet_error_message(error)}, status=400)
        except PermissionDenied as error:
            return JsonResponse({"error": _wallet_error_message(error)}, status=400)

        return JsonResponse(
            {
                "challenge_id": challenge.pk,
                "message": challenge.message,
                "address": challenge.user_wallet.wallet.address_normalized,
                "chain_id": challenge.chain_id,
                "expires_at": challenge.expires_at.isoformat(),
            },
            status=201,
        )


class WalletChallengeVerifyView(ActiveAccountRequiredMixin, View):
    """Verify the wallet signature and mark the user's link as verified."""

    http_method_names = ["post", "options"]

    def post(self, request: HttpRequest) -> JsonResponse:
        """Consume a challenge only when it belongs to this account."""

        try:
            payload = json.loads(request.body or b"{}")
            challenge_id = int(payload.get("challenge_id"))
            signature = payload.get("signature", "")
            if not isinstance(signature, str):
                raise ValueError
            challenge = consume_wallet_verification_challenge(
                request.user,
                challenge_id,
                signature,
            )
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            return JsonResponse(
                {"error": "Os dados da assinatura estão incompletos ou inválidos."},
                status=400,
            )
        except ValidationError as error:
            return JsonResponse({"error": _wallet_error_message(error)}, status=400)
        except PermissionDenied as error:
            return JsonResponse({"error": _wallet_error_message(error)}, status=400)
        except Http404:
            return JsonResponse(
                {"error": "Solicitação de verificação não encontrada."},
                status=404,
            )

        return JsonResponse(
            {
                "status": "verified",
                "address": challenge.user_wallet.wallet.address_normalized,
                "message": "Carteira verificada e vinculada à sua conta.",
            }
        )


def _wallet_error_message(error: ValidationError | PermissionDenied) -> str:
    """Translate wallet rule errors into concise messages for the profile UI."""

    raw_messages = error.messages if isinstance(error, ValidationError) else [str(error)]
    translations = {
        "The wallet signature is invalid.": "A assinatura não corresponde a esta carteira.",
        "The wallet verification challenge is expired or already used.": (
            "Esta solicitação expirou ou já foi usada. Tente conectar a carteira novamente."
        ),
        "The wallet link is not pending verification.": (
            "Esta carteira não está aguardando verificação. Atualize a página e tente novamente."
        ),
        "The wallet is inactive.": "Esta carteira está desativada.",
        "The account is inactive.": "Sua conta está desativada.",
    }
    return "; ".join(translations.get(message, message) for message in raw_messages)


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
