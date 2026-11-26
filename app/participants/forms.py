"""Input forms for company-scoped participant creation."""

from __future__ import annotations

from typing import Any

from django import forms
from django.core.files.uploadedfile import UploadedFile
from django.utils.translation import gettext_lazy as _

from utils.identifiers import is_valid_wallet_address, normalize_wallet_address

from .documents import read_valid_participant_terms_pdf
from .models import Participant


INPUT_CLASSES = (
    "h-[42px] w-full rounded-[7px] border border-[#D7DDD4] bg-white px-3 "
    "text-[13px] text-[#17211A] outline-none transition focus:border-[#1E6B3C] "
    "focus:ring-2 focus:ring-[#1E6B3C]/15"
)


class ParticipantForm(forms.ModelForm):
    """Collect only the participant identity fields supplied by a company user."""

    terms_document_id = forms.IntegerField(
        required=False,
        widget=forms.HiddenInput,
    )

    class Meta:
        model = Participant
        fields = ("name", "wallet_address")
        labels = {
            "name": "Nome do participante",
            "wallet_address": "Carteira pública do participante",
        }
        help_texts = {
            "wallet_address": "Informe o endereço público. Nunca informe uma chave privada.",
        }

    def __init__(self, *args: Any, company_id: int, **kwargs: Any) -> None:
        """Keep duplicate checks within the company selected by the URL."""

        super().__init__(*args, **kwargs)
        self.company_id = company_id
        for field in self.fields.values():
            field.widget.attrs["class"] = INPUT_CLASSES
        self.fields["name"].widget.attrs.update(
            {"autocomplete": "name", "placeholder": _("Nome completo")}
        )
        self.fields["wallet_address"].widget.attrs.update(
            {
                "autocomplete": "off",
                "inputmode": "text",
                "placeholder": "0x...",
                "spellcheck": "false",
            }
        )

    def clean_name(self) -> str:
        """Trim the participant display name before it becomes immutable history."""

        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Informe o nome do participante.")
        return name

    def clean_wallet_address(self) -> str:
        """Normalize EVM addresses and reject duplicates for this company."""

        address = normalize_wallet_address(self.cleaned_data.get("wallet_address", ""))
        if not is_valid_wallet_address(address):
            raise forms.ValidationError("Informe uma carteira EVM válida, iniciada por 0x.")
        duplicates = Participant.objects.filter(
            company_id=self.company_id,
            wallet_address_normalized=address,
        )
        if self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise forms.ValidationError(
                "Esta carteira já está cadastrada nesta empresa."
            )
        return address


class ParticipantTermsDocumentUploadForm(forms.Form):
    """Accept a company-approved PDF and an explicit publication confirmation."""

    pdf_file = forms.FileField(
        label="PDF dos termos",
        widget=forms.FileInput(
            attrs={
                "accept": "application/pdf,.pdf",
                "class": "absolute inset-0 h-full w-full cursor-pointer opacity-0",
                "aria-describedby": "terms-file-description",
            }
        ),
    )
    company_approval_confirmed = forms.BooleanField(
        label="Confirmo que a empresa aprovou este PDF para uso com seus participantes.",
        required=True,
    )

    def clean_pdf_file(self) -> UploadedFile:
        """Reject non-PDF, oversized or structurally invalid uploads."""

        uploaded_file: UploadedFile = self.cleaned_data["pdf_file"]
        read_valid_participant_terms_pdf(uploaded_file)
        return uploaded_file
