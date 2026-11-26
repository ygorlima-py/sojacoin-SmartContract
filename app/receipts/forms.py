"""Input forms for recording a local soybean load receipt."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from companies.models import CompanyMembership, Unit, UnitAccess
from participants.models import Participant, ParticipantTerms

from .documents import crop_year_to_bytes32, read_valid_receipt_pdf, receipt_pdf_max_size


INPUT_CLASSES = (
    "h-[42px] w-full rounded-[7px] border border-[#D7DDD4] bg-white px-3 "
    "text-[13px] text-[#17211A] outline-none transition focus:border-[#1E6B3C] "
    "focus:ring-2 focus:ring-[#1E6B3C]/15"
)
FILE_CLASSES = (
    "block w-full cursor-pointer rounded-[7px] border border-[#D7DDD4] bg-white "
    "text-[13px] text-[#5C685E] file:mr-3 file:h-[42px] file:border-0 "
    "file:bg-[#E9F2EC] file:px-4 file:font-semibold file:text-[#1E6B3C] "
    "hover:file:bg-[#DDEBE1]"
)


class RegisteredUnitChoiceField(forms.ModelChoiceField):
    """Show the human unit code and on-chain ID in each authorized choice."""

    def label_from_instance(self, unit: Unit) -> str:
        """Return a label that distinguishes similarly named storage units."""

        return f"{unit.name} — {unit.code} (ID na rede: {unit.chain_unit_id})"


class ReceiptCreateForm(forms.Form):
    """Collect validated local receipt details and their source PDFs."""

    participant = forms.ModelChoiceField(
        label="Participante",
        queryset=Participant.objects.none(),
        empty_label="Selecione o participante",
        help_text="Somente participantes com cadastro confirmado aparecem aqui.",
    )
    unit = RegisteredUnitChoiceField(
        label="Unidade de recebimento",
        queryset=Unit.objects.none(),
        empty_label="Selecione a unidade",
    )
    crop_year = forms.CharField(
        label="Safra",
        max_length=32,
        help_text="Exemplo: 2026/2027. O valor também será convertido para bytes32.",
        widget=forms.TextInput(attrs={"placeholder": "2026/2027", "autocomplete": "off"}),
    )
    gross_weight_kg = forms.IntegerField(
        label="Peso bruto (kg)",
        min_value=0,
        widget=forms.NumberInput(attrs={"min": "0", "step": "1"}),
    )
    tare_weight_kg = forms.IntegerField(
        label="Tara (kg)",
        min_value=0,
        widget=forms.NumberInput(attrs={"min": "0", "step": "1"}),
    )
    moisture_percent = forms.DecimalField(
        label="Umidade (%)",
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        max_digits=5,
        decimal_places=2,
        initial=Decimal("0.00"),
        help_text="Informação de classificação; não substitui o desconto em kg.",
        widget=forms.NumberInput(attrs={"min": "0", "max": "100", "step": "0.01"}),
    )
    moisture_deduction_kg = forms.IntegerField(
        label="Desconto por umidade (kg)",
        min_value=0,
        initial=0,
        widget=forms.NumberInput(attrs={"min": "0", "step": "1"}),
    )
    impurities_percent = forms.DecimalField(
        label="Impurezas (%)",
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        max_digits=5,
        decimal_places=2,
        initial=Decimal("0.00"),
        help_text="Informação de classificação; não substitui o desconto em kg.",
        widget=forms.NumberInput(attrs={"min": "0", "max": "100", "step": "0.01"}),
    )
    impurities_deduction_kg = forms.IntegerField(
        label="Desconto por impurezas (kg)",
        min_value=0,
        initial=0,
        widget=forms.NumberInput(attrs={"min": "0", "step": "1"}),
    )
    damaged_grain_percent = forms.DecimalField(
        label="Grãos avariados (%)",
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        max_digits=5,
        decimal_places=2,
        initial=Decimal("0.00"),
        help_text="Informação de classificação; não substitui o desconto em kg.",
        widget=forms.NumberInput(attrs={"min": "0", "max": "100", "step": "0.01"}),
    )
    damaged_grain_deduction_kg = forms.IntegerField(
        label="Desconto por grãos avariados (kg)",
        min_value=0,
        initial=0,
        widget=forms.NumberInput(attrs={"min": "0", "step": "1"}),
    )
    other_deductions_kg = forms.IntegerField(
        label="Outros descontos (kg)",
        min_value=0,
        required=False,
        initial=0,
        widget=forms.NumberInput(attrs={"min": "0", "step": "1"}),
    )
    weighed_at = forms.DateTimeField(
        label="Data e hora da pesagem",
        input_formats=("%Y-%m-%dT%H:%M",),
        help_text="Use o horário registrado no romaneio; a interpretação usa o fuso configurado no SaaS.",
        widget=forms.DateTimeInput(
            format="%Y-%m-%dT%H:%M",
            attrs={"type": "datetime-local"},
        ),
    )
    weighing_ticket_pdf = forms.FileField(
        label="Romaneio de pesagem (PDF)",
        help_text="Documento obrigatório. A hash será calculada sobre os bytes exatos do PDF.",
        widget=forms.FileInput(attrs={"accept": "application/pdf,.pdf"}),
    )
    classification_report_pdf = forms.FileField(
        label="Laudo de classificação (PDF)",
        required=False,
        help_text="Opcional. Se enviado, seu hash também ficará vinculado ao recebimento.",
        widget=forms.FileInput(attrs={"accept": "application/pdf,.pdf"}),
    )
    invoice_access_key = forms.CharField(
        label="Chave de acesso da NF-e",
        max_length=44,
        help_text="Informe os 44 dígitos da chave, sem espaços ou pontuação.",
        widget=forms.TextInput(
            attrs={
                "inputmode": "numeric",
                "autocomplete": "off",
                "placeholder": "44 dígitos",
                "maxlength": "44",
            }
        ),
    )
    invoice_pdf = forms.FileField(
        label="Nota fiscal (PDF)",
        help_text="Documento obrigatório que será guardado em armazenamento privado.",
        widget=forms.FileInput(attrs={"accept": "application/pdf,.pdf"}),
    )

    def __init__(
        self,
        *args: Any,
        company_id: int,
        membership: CompanyMembership,
        **kwargs: Any,
    ) -> None:
        """Restrict selectable participants and units to this authorized operator."""

        super().__init__(*args, **kwargs)
        self.company_id = company_id
        self.membership = membership
        self.fields["participant"].queryset = (
            Participant.objects.filter(
                company_id=company_id,
                terms_versions__status=ParticipantTerms.Status.CONFIRMED,
            )
            .distinct()
            .order_by("name", "pk")
        )
        self.fields["unit"].queryset = Unit.objects.filter(
            company_id=company_id,
            active=True,
            chain_unit_id__gt=0,
            access_grants__membership=membership,
            access_grants__active=True,
            access_grants__can_receive=True,
        ).order_by("name", "code")
        self.fields["weighed_at"].initial = timezone.localtime(timezone.now()).strftime(
            "%Y-%m-%dT%H:%M"
        )

        for field_name, field in self.fields.items():
            if isinstance(field.widget, forms.FileInput):
                field.widget.attrs["class"] = FILE_CLASSES
                continue
            field.widget.attrs["class"] = INPUT_CLASSES
            if field_name in {"participant", "unit"}:
                field.widget.attrs["class"] += " appearance-none"
        self.fields["invoice_access_key"].widget.attrs["pattern"] = "[0-9]{44}"

    def clean_crop_year(self) -> str:
        """Ensure the human crop-year value has one unambiguous bytes32 form."""

        crop_year = self.cleaned_data["crop_year"].strip()
        crop_year_to_bytes32(crop_year)
        return crop_year

    def clean_invoice_access_key(self) -> str:
        """Accept only the 44 decimal digits used by a Brazilian NF-e key."""

        access_key = self.cleaned_data["invoice_access_key"].strip()
        if len(access_key) != 44 or not access_key.isascii() or not access_key.isdigit():
            raise forms.ValidationError("A chave de acesso da NF-e deve ter exatamente 44 dígitos.")
        return access_key

    def clean_other_deductions_kg(self) -> int:
        """Treat an omitted optional deduction as zero kilograms."""

        return self.cleaned_data.get("other_deductions_kg") or 0

    def clean(self) -> dict[str, Any]:
        """Validate combined weights and inspect every uploaded PDF before saving."""

        cleaned_data = super().clean()
        gross = cleaned_data.get("gross_weight_kg")
        tare = cleaned_data.get("tare_weight_kg")
        deduction_fields = (
            "moisture_deduction_kg",
            "impurities_deduction_kg",
            "damaged_grain_deduction_kg",
            "other_deductions_kg",
        )
        if gross is not None and tare is not None:
            deductions = sum(cleaned_data.get(name) or 0 for name in deduction_fields)
            if gross <= tare:
                self.add_error("gross_weight_kg", "O peso bruto precisa ser maior que a tara.")
            elif gross - tare <= deductions:
                self.add_error(
                    None,
                    "A tara e os descontos precisam deixar um peso líquido maior que zero.",
                )
            else:
                cleaned_data["net_weight_kg"] = gross - tare - deductions

        for field_name in (
            "weighing_ticket_pdf",
            "classification_report_pdf",
            "invoice_pdf",
        ):
            document = cleaned_data.get(field_name)
            if document is None:
                continue
            try:
                read_valid_receipt_pdf(document)
            except ValidationError as error:
                self.add_error(field_name, error.messages[0])
        return cleaned_data

    @staticmethod
    def maximum_pdf_size_label() -> str:
        """Format the configured private PDF size limit for helper text."""

        megabytes = receipt_pdf_max_size() / (1024 * 1024)
        return f"{megabytes:g}".replace(".", ",")
