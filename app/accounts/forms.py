from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
)

from .models import UserProfile
from .rules.account_access import is_active_account


COMPACT_INPUT_CLASSES = (
    "h-[38px] w-full rounded-[7px] border border-[#E2E5DE] bg-white "
    "px-[11px] text-[13px] text-[#17211A] outline-none transition "
    "focus:border-[#1E6B3C] focus:ring-2 focus:ring-[#1E6B3C]/15"
)

PASSWORD_INPUT_CLASSES = COMPACT_INPUT_CLASSES


class CompactPasswordFieldsMixin:
    """Apply the compact field style shared by account password screens."""

    def style_visible_fields(self):
        """Add consistent sizing, labels, and browser autocomplete hints."""
        for field in self.fields.values():
            field.widget.attrs["class"] = PASSWORD_INPUT_CLASSES


class AccountPasswordChangeForm(CompactPasswordFieldsMixin, PasswordChangeForm):
    """Change a password with labels and fields styled for the SaaS UI."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["old_password"].label = "Senha atual"
        self.fields["new_password1"].label = "Nova senha"
        self.fields["new_password2"].label = "Confirme a nova senha"
        self.style_visible_fields()


class AccountPasswordResetForm(CompactPasswordFieldsMixin, PasswordResetForm):
    """Request a password reset without exposing whether an account exists."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].label = "E-mail"
        self.fields["email"].widget.attrs["autocomplete"] = "email"
        self.style_visible_fields()


class AccountSetPasswordForm(CompactPasswordFieldsMixin, SetPasswordForm):
    """Set a new password after a valid reset token is opened."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].label = "Nova senha"
        self.fields["new_password2"].label = "Confirme a nova senha"
        self.style_visible_fields()


class AccountAuthenticationForm(AuthenticationForm):
    """Authenticate Django users only when their application profile is active."""

    error_messages = {
        "invalid_login": "Usuário ou senha inválidos.",
        "inactive": "Esta conta está inativa.",
    }

    def confirm_login_allowed(self, user):
        """Reject inactive users and users without an active profile."""

        super().confirm_login_allowed(user)
        if not is_active_account(user):
            raise forms.ValidationError(self.error_messages["invalid_login"], code="invalid_login")


class UserProfileForm(forms.ModelForm):
    """Expose only the fields a user may edit on their own profile."""

    class Meta:
        model = UserProfile
        fields = ("full_name", "phone")
        labels = {"full_name": "Nome completo", "phone": "Telefone"}

    def __init__(self, *args, **kwargs):
        """Add the shared compact input style and browser hints."""
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = COMPACT_INPUT_CLASSES
        self.fields["full_name"].widget.attrs["autocomplete"] = "name"
        self.fields["phone"].widget.attrs["autocomplete"] = "tel"
