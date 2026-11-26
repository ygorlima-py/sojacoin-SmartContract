"""Forms for assisted company administration and team access."""

from typing import Any

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.utils.translation import gettext_lazy as _

from .models import (
    Company,
    CompanyMembership,
    MembershipRole,
    Unit,
    UnitAccess,
)
from utils.identifiers import is_valid_cnpj, normalize_cnpj


User = get_user_model()


COMPANY_INPUT_CLASSES = (
    "h-[38px] w-full rounded-[7px] border border-[#E2E5DE] bg-white "
    "px-[11px] text-[13px] text-[#17211A] outline-none transition "
    "focus:border-[#1E6B3C] focus:ring-2 focus:ring-[#1E6B3C]/15"
)


class CompanyForm(forms.ModelForm):
    """Create or edit company identity data in the assisted flow.

    ``active`` is deliberately absent from ``Meta.fields``. Company status is
    controlled by the platform workflow and cannot be changed through this
    form.
    """

    class Meta:
        model = Company
        fields = ("legal_name", "trade_name", "cnpj")
        labels = {
            "legal_name": "Razão social",
            "trade_name": "Nome fantasia",
            "cnpj": "CNPJ",
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Apply the compact Tailwind-compatible input style."""

        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = COMPANY_INPUT_CLASSES

        self.fields["legal_name"].widget.attrs["autocomplete"] = "organization"
        self.fields["trade_name"].widget.attrs["autocomplete"] = "organization"
        self.fields["cnpj"].widget.attrs["inputmode"] = "numeric"

    def clean_cnpj(self) -> str:
        """Normalize, validate and make the CNPJ unique before saving."""

        normalized = normalize_cnpj(self.cleaned_data.get("cnpj", ""))
        if len(normalized) != 14 or not is_valid_cnpj(normalized):
            raise forms.ValidationError("Informe um CNPJ válido.")

        existing = Company.objects.filter(cnpj=normalized)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("Já existe uma empresa com este CNPJ.")

        return normalized

    def save(self, commit: bool = True) -> Company:
        """Persist only the permitted company fields and preserve its status."""

        company = super().save(commit=False)
        company.cnpj = normalize_cnpj(company.cnpj)
        company.full_clean()
        if commit:
            company.save()
        return company


class UnitForm(forms.ModelForm):
    """Collect the user-editable fields for a company's physical unit.

    The company is supplied by the server-side company context. Blockchain
    identifiers, the registration hash and the active flag are intentionally
    excluded so a submitted form cannot change system-controlled state.
    """

    # The database stores the canonical 14-character value, while the form
    # also accepts the usual 18-character formatted CNPJ input.
    cnpj = forms.CharField(max_length=18, label="CNPJ")

    class Meta:
        model = Unit
        fields = ("name", "code", "address", "cnpj", "state_registration")
        labels = {
            "name": "Nome da unidade",
            "code": "Código",
            "address": "Endereço",
            "cnpj": "CNPJ",
            "state_registration": "Inscrição estadual",
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Apply the shared compact field style and browser hints."""

        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = COMPANY_INPUT_CLASSES

        self.fields["name"].widget.attrs["autocomplete"] = "organization"
        self.fields["address"].widget.attrs["autocomplete"] = "street-address"
        self.fields["cnpj"].widget.attrs["inputmode"] = "numeric"
        self.fields["address"].required = True
        self.fields["state_registration"].required = True

    def clean_code(self) -> str:
        """Return the company-scoped unit code in canonical uppercase form."""

        normalized = (self.cleaned_data.get("code") or "").strip().upper()
        existing = Unit.objects.filter(
            company_id=self.instance.company_id,
            code=normalized,
        )
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if normalized and existing.exists():
            raise forms.ValidationError("Já existe uma unidade com este código.")
        return normalized

    def clean_cnpj(self) -> str:
        """Normalize and validate the unit CNPJ before it is saved."""

        normalized = normalize_cnpj(self.cleaned_data.get("cnpj", ""))
        if len(normalized) != 14 or not is_valid_cnpj(normalized):
            raise forms.ValidationError("Informe um CNPJ válido.")

        existing = Unit.objects.filter(cnpj=normalized)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("Já existe uma unidade com este CNPJ.")

        return normalized


class CompanyMembershipForm(forms.Form):
    """Select an existing active Django user for a company membership.

    The company is supplied by the validated URL context. It is intentionally
    not a form field, so a submitted value cannot move the membership to a
    different company.
    """

    user = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label="Usuário",
        empty_label=_("Selecione um usuário"),
    )

    def __init__(
        self,
        *args: Any,
        company: Company,
        **kwargs: Any,
    ) -> None:
        """Limit choices to active accounts not already linked to the company."""

        self.company = company
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = (
            User.objects.filter(
                is_active=True,
                profile__active=True,
            )
            .exclude(company_memberships__company=company)
            .select_related("profile")
            .order_by("username")
        )
        self.fields["user"].widget.attrs["class"] = (
            "h-[38px] w-full rounded-[7px] border border-[#E2E5DE] bg-white "
            "px-[11px] text-[13px] text-[#17211A] outline-none transition "
            "focus:border-[#1E6B3C] focus:ring-2 focus:ring-[#1E6B3C]/15"
        )

    def clean_user(self) -> AbstractBaseUser:
        """Reject duplicate memberships even if the list became stale."""

        user = self.cleaned_data["user"]
        if CompanyMembership.objects.filter(
            company=self.company,
            user=user,
        ).exists():
            raise forms.ValidationError(
                "Este usuário já está vinculado a esta empresa."
            )
        return user


class MembershipRoleForm(forms.Form):
    """Select one SaaS function to grant to a company member."""

    ROLE_CHOICES = MembershipRole.Role.choices

    role = forms.ChoiceField(
        choices=ROLE_CHOICES,
        label="Função",
        widget=forms.Select(
            attrs={
                "class": COMPANY_INPUT_CLASSES,
            }
        ),
    )

    def __init__(
        self,
        *args: Any,
        membership: CompanyMembership,
        **kwargs: Any,
    ) -> None:
        """Keep the target membership outside submitted form data."""

        self.membership = membership
        super().__init__(*args, **kwargs)

    def clean_role(self) -> str:
        """Reject a function that is already active for this member."""

        role = self.cleaned_data["role"]
        if MembershipRole.objects.filter(
            membership=self.membership,
            role=role,
            active=True,
        ).exists():
            raise forms.ValidationError(
                "Esta função já está ativa para este membro."
            )
        return role


class UnitAccessForm(forms.Form):
    """Set the SaaS permissions for one member and one company unit."""

    can_view = forms.BooleanField(
        required=False,
        label="Visualização",
        widget=forms.CheckboxInput(
            attrs={"class": "h-4 w-4 rounded border-[#CCD3C9] text-[#1E6B3C]"}
        ),
    )
    can_receive = forms.BooleanField(
        required=False,
        label="Recebimento",
        widget=forms.CheckboxInput(
            attrs={"class": "h-4 w-4 rounded border-[#CCD3C9] text-[#1E6B3C]"}
        ),
    )
    can_issue = forms.BooleanField(
        required=False,
        label="Emissão",
        widget=forms.CheckboxInput(
            attrs={"class": "h-4 w-4 rounded border-[#CCD3C9] text-[#1E6B3C]"}
        ),
    )
    can_audit = forms.BooleanField(
        required=False,
        label="Auditoria",
        widget=forms.CheckboxInput(
            attrs={"class": "h-4 w-4 rounded border-[#CCD3C9] text-[#1E6B3C]"}
        ),
    )

    def __init__(
        self,
        *args: Any,
        access: UnitAccess | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize checkboxes from the current active SaaS grant."""

        super().__init__(*args, **kwargs)
        if access is not None and access.active:
            for field_name in (
                "can_view",
                "can_receive",
                "can_issue",
                "can_audit",
            ):
                self.initial[field_name] = getattr(access, field_name)
