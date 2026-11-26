"""Local receipt, weighing, invoice and private-document records."""

from __future__ import annotations

import re
from pathlib import Path
from decimal import Decimal
from typing import Any, NoReturn
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from web3 import Web3

from blockchain.models import (
    BlockchainTransaction,
    ContractInstance,
    Network,
    NonDeletableModel,
    RECEIPT_INVOICE_LINK_OPERATION_TYPE,
    RECEIPT_OPEN_OPERATION_TYPE,
    RECEIPT_WEIGHING_CONFIRMATION_OPERATION_TYPE,
)
from companies.models import Company, Unit
from participants.models import Participant
from utils.identifiers import (
    is_valid_bytes32_hash,
    is_valid_wallet_address,
    normalize_bytes32_hash,
    normalize_wallet_address,
)

from .documents import (
    ZERO_BYTES32,
    crop_year_to_bytes32,
    read_valid_receipt_pdf,
    receipt_pdf_hash,
)


class TimestampedModel(models.Model):
    """Provide consistent creation and update timestamps to receipt records."""

    created_at = models.DateTimeField("Criado em", auto_now_add=True)
    updated_at = models.DateTimeField("Atualizado em", auto_now=True)

    class Meta:
        abstract = True


def receipt_document_upload_path(instance: ReceiptDocument, filename: str) -> str:
    """Store PDFs under random names inside the configured private media root."""

    return (
        f"receipts/company_{instance.receipt.company_id}/"
        f"receipt_{instance.receipt_id}/{uuid4().hex}.pdf"
    )


class Receipt(TimestampedModel):
    """A locally recorded soybean load awaiting the later blockchain workflow."""

    class Status(models.TextChoices):
        READY_FOR_BLOCKCHAIN = "ready_for_blockchain", "Aguardando envio à blockchain"
        BLOCKCHAIN_IN_PROGRESS = "blockchain_in_progress", "Envio à blockchain em andamento"
        BLOCKCHAIN_CONFIRMED = "blockchain_confirmed", "Confirmado na blockchain"
        NEEDS_REVIEW = "needs_review", "Precisa de conferência"

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="receipts",
        verbose_name="Empresa",
    )
    participant = models.ForeignKey(
        Participant,
        on_delete=models.PROTECT,
        related_name="receipts",
        verbose_name="Participante",
    )
    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        related_name="receipts",
        verbose_name="Unidade",
    )
    crop_year = models.CharField("Safra", max_length=32)
    crop_year_bytes32 = models.CharField(
        "Safra codificada para bytes32",
        max_length=66,
        editable=False,
    )
    status = models.CharField(
        "Situação",
        max_length=32,
        choices=Status.choices,
        default=Status.READY_FOR_BLOCKCHAIN,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_receipts",
        verbose_name="Criado por",
    )

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "recebimento"
        verbose_name_plural = "recebimentos"
        indexes = [
            models.Index(
                fields=("company", "status", "created_at"),
                name="receipt_company_state_idx",
            ),
            models.Index(fields=("unit", "created_at"), name="receipt_unit_date_idx"),
        ]

    def clean(self) -> None:
        """Validate company ownership and derive the deterministic crop-year word."""

        super().clean()
        self.crop_year = (self.crop_year or "").strip()
        if self.company_id and self.participant_id:
            if self.participant.company_id != self.company_id:
                raise ValidationError({"participant": "O participante precisa pertencer à empresa."})
        if self.company_id and self.unit_id:
            if self.unit.company_id != self.company_id:
                raise ValidationError({"unit": "A unidade precisa pertencer à empresa."})
        self.crop_year_bytes32 = crop_year_to_bytes32(self.crop_year)
        if self.status not in self.Status.values:
            raise ValidationError({"status": "A situação do recebimento é inválida."})

        if self.pk:
            previous_status = type(self).objects.only("status").get(pk=self.pk).status
            allowed_transitions = {
                (self.Status.READY_FOR_BLOCKCHAIN, self.Status.BLOCKCHAIN_IN_PROGRESS),
                (self.Status.BLOCKCHAIN_IN_PROGRESS, self.Status.BLOCKCHAIN_CONFIRMED),
                (self.Status.BLOCKCHAIN_IN_PROGRESS, self.Status.NEEDS_REVIEW),
                (self.Status.BLOCKCHAIN_CONFIRMED, self.Status.NEEDS_REVIEW),
            }
            if previous_status != self.status and (
                previous_status,
                self.status,
            ) not in allowed_transitions:
                raise ValidationError("A situação do recebimento não pode voltar a uma etapa anterior.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Recalculate the encoded crop year on every persistence path."""

        self.crop_year_bytes32 = crop_year_to_bytes32(self.crop_year)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Keep operational evidence from being removed through ordinary ORM calls."""

        raise ValidationError("Recebimentos e seus documentos não podem ser apagados.")

    def __str__(self) -> str:
        return f"Recebimento #{self.pk} — {self.participant.name} — {self.unit.name}"


class ReceiptDocument(TimestampedModel):
    """An immutable private PDF attached to one receipt."""

    class Kind(models.TextChoices):
        WEIGHING_TICKET = "weighing_ticket", "Romaneio"
        CLASSIFICATION_REPORT = "classification_report", "Laudo de classificação"
        TAX_INVOICE = "tax_invoice", "Nota fiscal"

    receipt = models.ForeignKey(
        Receipt,
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name="Recebimento",
    )
    kind = models.CharField("Tipo de documento", max_length=32, choices=Kind.choices)
    original_filename = models.CharField("Nome original", max_length=255)
    pdf_file = models.FileField("PDF", upload_to=receipt_document_upload_path, max_length=300)
    file_hash = models.CharField("Hash Keccak-256 do PDF", max_length=66, editable=False)
    size_bytes = models.PositiveBigIntegerField("Tamanho em bytes", editable=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_receipt_documents",
        verbose_name="Enviado por",
    )

    class Meta:
        ordering = ("kind", "pk")
        verbose_name = "documento do recebimento"
        verbose_name_plural = "documentos do recebimento"
        constraints = [
            models.UniqueConstraint(
                fields=("receipt", "kind"),
                name="uniq_receipt_document_kind",
            ),
        ]

    def clean(self) -> None:
        """Validate the PDF and derive its exact content hash and byte size."""

        super().clean()
        self.original_filename = Path(
            (self.original_filename or "").replace("\\", "/")
        ).name.strip()[:255]
        if not self.original_filename:
            raise ValidationError({"original_filename": "O nome do documento não pode ficar vazio."})
        if not self.pdf_file:
            raise ValidationError({"pdf_file": "Envie o documento em PDF."})

        pdf_bytes = read_valid_receipt_pdf(self.pdf_file.file)
        calculated_hash = receipt_pdf_hash(pdf_bytes)
        if self.file_hash and normalize_bytes32_hash(self.file_hash) != calculated_hash:
            raise ValidationError({"file_hash": "A hash não corresponde aos bytes armazenados."})
        self.file_hash = calculated_hash
        self.size_bytes = len(pdf_bytes)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate document identity and prohibit replacing an uploaded file."""

        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "receipt_id",
                "kind",
                "original_filename",
                "file_hash",
                "size_bytes",
                "uploaded_by_id",
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Documentos do recebimento são imutáveis após o envio.")
            if previous.pdf_file.name != self.pdf_file.name:
                raise ValidationError("O PDF do recebimento não pode ser substituído.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Preserve source PDFs as supporting evidence for the receipt."""

        raise ValidationError("Documentos do recebimento não podem ser apagados.")

    def __str__(self) -> str:
        return f"{self.get_kind_display()} — recebimento #{self.receipt_id}"


class WeighingTicket(TimestampedModel):
    """Store the weighing and classification values used by contract calldata."""

    receipt = models.OneToOneField(
        Receipt,
        on_delete=models.PROTECT,
        related_name="weighing_ticket",
        verbose_name="Recebimento",
    )
    gross_weight_kg = models.PositiveBigIntegerField("Peso bruto (kg)")
    tare_weight_kg = models.PositiveBigIntegerField("Tara (kg)")
    net_weight_kg = models.PositiveBigIntegerField("Peso líquido (kg)", editable=False)
    moisture_bps = models.PositiveSmallIntegerField("Umidade (centésimos de %)")
    impurities_bps = models.PositiveSmallIntegerField("Impurezas (centésimos de %)")
    damaged_grain_bps = models.PositiveSmallIntegerField("Grãos avariados (centésimos de %)")
    moisture_deduction_kg = models.PositiveBigIntegerField("Desconto de umidade (kg)")
    impurities_deduction_kg = models.PositiveBigIntegerField("Desconto de impurezas (kg)")
    damaged_grain_deduction_kg = models.PositiveBigIntegerField("Desconto de avariados (kg)")
    other_deductions_kg = models.PositiveBigIntegerField("Outros descontos (kg)", default=0)
    weighing_ticket_hash = models.CharField("Hash do romaneio", max_length=66, editable=False)
    report_hash = models.CharField(
        "Hash do laudo",
        max_length=66,
        default=ZERO_BYTES32,
        blank=True,
        editable=False,
    )
    weighed_at = models.DateTimeField("Pesado em")

    class Meta:
        ordering = ("-weighed_at", "-pk")
        verbose_name = "romaneio de pesagem"
        verbose_name_plural = "romaneios de pesagem"

    @property
    def total_deductions_kg(self) -> int:
        """Return all deductions in kilograms using the contract's integer units."""

        return (
            self.moisture_deduction_kg
            + self.impurities_deduction_kg
            + self.damaged_grain_deduction_kg
            + self.other_deductions_kg
        )

    @property
    def moisture_percent(self) -> Decimal:
        """Return stored hundredths-of-percent as a display-ready Decimal."""

        return Decimal(self.moisture_bps) / 100

    @property
    def impurities_percent(self) -> Decimal:
        """Return stored impurities basis points as a percentage."""

        return Decimal(self.impurities_bps) / 100

    @property
    def damaged_grain_percent(self) -> Decimal:
        """Return stored damaged-grain basis points as a percentage."""

        return Decimal(self.damaged_grain_bps) / 100

    def clean(self) -> None:
        """Enforce contract-compatible weights, basis points, hashes and dates."""

        super().clean()
        if self.gross_weight_kg <= self.tare_weight_kg:
            raise ValidationError({"gross_weight_kg": "O peso bruto precisa ser maior que a tara."})
        if self.gross_weight_kg - self.tare_weight_kg <= self.total_deductions_kg:
            raise ValidationError("Os descontos precisam ser menores que o peso bruto líquido da tara.")
        for field_name in ("moisture_bps", "impurities_bps", "damaged_grain_bps"):
            value = getattr(self, field_name)
            if value > 10_000:
                raise ValidationError({field_name: "O percentual deve ficar entre 0% e 100%."})
        if self.weighed_at and self.weighed_at > timezone.now():
            raise ValidationError({"weighed_at": "A pesagem não pode estar no futuro."})

        self.net_weight_kg = self.gross_weight_kg - self.tare_weight_kg - self.total_deductions_kg
        ticket_document = self.receipt.documents.filter(
            kind=ReceiptDocument.Kind.WEIGHING_TICKET
        ).first()
        if ticket_document is None:
            raise ValidationError("O PDF do romaneio é obrigatório.")
        self.weighing_ticket_hash = normalize_bytes32_hash(ticket_document.file_hash)

        report_document = self.receipt.documents.filter(
            kind=ReceiptDocument.Kind.CLASSIFICATION_REPORT
        ).first()
        self.report_hash = (
            normalize_bytes32_hash(report_document.file_hash) if report_document else ZERO_BYTES32
        )
        if not is_valid_bytes32_hash(self.weighing_ticket_hash) or not is_valid_bytes32_hash(
            self.report_hash
        ):
            raise ValidationError("Hashes dos documentos precisam ter 32 bytes.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Recompute derived weight and hashes before each database write."""

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Romaneio — recebimento #{self.receipt_id}"


class TaxInvoice(TimestampedModel):
    """Store the NF-e access key and document reference for a receipt.

    The contract's invoice hash is derived from the 44-digit access key, not
    from the PDF bytes. The associated ``ReceiptDocument.file_hash`` separately
    identifies the exact PDF saved in private storage.
    """

    receipt = models.OneToOneField(
        Receipt,
        on_delete=models.PROTECT,
        related_name="tax_invoice",
        verbose_name="Recebimento",
    )
    access_key = models.CharField("Chave de acesso da NF-e", max_length=44)
    access_key_hash = models.CharField(
        "Hash Keccak-256 da chave de acesso",
        max_length=66,
        editable=False,
    )
    document = models.OneToOneField(
        ReceiptDocument,
        on_delete=models.PROTECT,
        related_name="tax_invoice",
        verbose_name="PDF da nota fiscal",
    )

    class Meta:
        verbose_name = "nota fiscal do recebimento"
        verbose_name_plural = "notas fiscais dos recebimentos"

    def clean(self) -> None:
        """Validate the Brazilian 44-digit access key and matching private PDF."""

        super().clean()
        self.access_key = (self.access_key or "").strip()
        if not re.fullmatch(r"[0-9]{44}", self.access_key, flags=re.ASCII):
            raise ValidationError({"access_key": "A chave de acesso da NF-e deve ter 44 dígitos."})
        if self.document_id:
            if (
                self.document.receipt_id != self.receipt_id
                or self.document.kind != ReceiptDocument.Kind.TAX_INVOICE
            ):
                raise ValidationError("O PDF da nota precisa pertencer a este recebimento.")
        self.access_key_hash = Web3.to_hex(Web3.keccak(text=self.access_key)).lower()

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep the on-chain key hash derived from the exact normalized key."""

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"NF-e do recebimento #{self.receipt_id}"


class ReceiptBlockchainOperation(NonDeletableModel):
    """Pin a receipt to one network, deployment, sender and confirmed deposit."""

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "Em andamento"
        CONFIRMED = "confirmed", "Confirmado na blockchain"
        NEEDS_REVIEW = "needs_review", "Precisa de conferência"

    receipt = models.OneToOneField(
        Receipt,
        on_delete=models.PROTECT,
        related_name="blockchain_operation",
        verbose_name="Recebimento",
    )
    network = models.ForeignKey(
        Network,
        on_delete=models.PROTECT,
        related_name="receipt_operations",
        verbose_name="Rede",
    )
    soja_coin = models.ForeignKey(
        ContractInstance,
        on_delete=models.PROTECT,
        related_name="receipt_operations",
        verbose_name="SojaCoin",
    )
    unit_registry = models.ForeignKey(
        ContractInstance,
        on_delete=models.PROTECT,
        related_name="unit_receipt_operations",
        verbose_name="UnitRegistry",
    )
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="started_receipt_blockchain_operations",
        verbose_name="Iniciado por",
    )
    # These values are snapshots: retries must keep the original on-chain intent.
    opening_sender_address = models.CharField("Carteira que abriu o recebimento", max_length=42)
    participant_address = models.CharField("Carteira do participante", max_length=42)
    participant_terms_hash = models.CharField("Hash dos termos aceitos", max_length=66)
    unit_chain_id = models.PositiveBigIntegerField("ID da unidade na rede")
    crop_year_bytes32 = models.CharField("Safra codificada", max_length=66)
    deposit_id = models.PositiveBigIntegerField(
        "ID do recebimento na rede",
        null=True,
        blank=True,
    )
    status = models.CharField(
        "Situação",
        max_length=16,
        choices=Status.choices,
        default=Status.IN_PROGRESS,
    )
    confirmed_at = models.DateTimeField("Confirmado em", null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "operação blockchain do recebimento"
        verbose_name_plural = "operações blockchain dos recebimentos"
        constraints = [
            models.UniqueConstraint(
                fields=("network", "soja_coin", "deposit_id"),
                condition=Q(deposit_id__isnull=False),
                name="uniq_receipt_chain_deposit",
            ),
        ]

    def clean(self) -> None:
        """Validate all deployment references and canonical public identifiers."""

        super().clean()
        self.opening_sender_address = normalize_wallet_address(self.opening_sender_address)
        self.participant_address = normalize_wallet_address(self.participant_address)
        self.participant_terms_hash = normalize_bytes32_hash(self.participant_terms_hash)
        self.crop_year_bytes32 = normalize_bytes32_hash(self.crop_year_bytes32)

        if not is_valid_wallet_address(self.opening_sender_address):
            raise ValidationError({"opening_sender_address": "Informe uma carteira EVM válida."})
        if not is_valid_wallet_address(self.participant_address):
            raise ValidationError({"participant_address": "A carteira do participante é inválida."})
        for field_name in ("participant_terms_hash", "crop_year_bytes32"):
            if not is_valid_bytes32_hash(getattr(self, field_name)):
                raise ValidationError({field_name: "Informe uma hash bytes32 válida."})
        if self.unit_chain_id < 1:
            raise ValidationError({"unit_chain_id": "A unidade precisa ter ID confirmado na rede."})

        if self._state.adding and self.receipt_id:
            if self.receipt.participant.wallet_address_normalized != self.participant_address:
                raise ValidationError("A carteira precisa corresponder ao participante do recebimento.")
            if self.receipt.unit.chain_unit_id != self.unit_chain_id:
                raise ValidationError("O ID informado não corresponde à unidade registrada na blockchain.")
            if self.receipt.crop_year_bytes32 != self.crop_year_bytes32:
                raise ValidationError("A safra codificada precisa corresponder ao recebimento local.")
        if self.network_id and self.soja_coin_id and self.unit_registry_id:
            if (
                self.soja_coin.company_id != self.receipt.company_id
                or self.unit_registry.company_id != self.receipt.company_id
                or self.soja_coin.network_id != self.network_id
                or self.unit_registry.network_id != self.network_id
                or self.network_id != self.soja_coin.network_id
                or self.soja_coin.kind != ContractInstance.Kind.SOJA_COIN
                or self.unit_registry.kind != ContractInstance.Kind.UNIT_REGISTRY
            ):
                raise ValidationError("A operação deve usar os contratos da empresa e da rede escolhidas.")

        if self.status == self.Status.CONFIRMED:
            if self.deposit_id is None or self.confirmed_at is None:
                raise ValidationError("A operação confirmada precisa de ID e data de confirmação.")

        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "receipt_id",
                "network_id",
                "soja_coin_id",
                "unit_registry_id",
                "started_by_id",
                "opening_sender_address",
                "participant_address",
                "participant_terms_hash",
                "unit_chain_id",
                "crop_year_bytes32",
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("O contexto de uma operação blockchain não pode ser alterado.")
            if previous.deposit_id is not None and previous.deposit_id != self.deposit_id:
                raise ValidationError("O ID confirmado do recebimento não pode ser substituído.")
            if previous.status == self.Status.CONFIRMED and self.status != self.Status.NEEDS_REVIEW:
                raise ValidationError("Uma operação confirmada não pode voltar ao andamento.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Normalize snapshots and validate the operation before persisting."""

        self.opening_sender_address = normalize_wallet_address(self.opening_sender_address)
        self.participant_address = normalize_wallet_address(self.participant_address)
        self.participant_terms_hash = normalize_bytes32_hash(self.participant_terms_hash)
        self.crop_year_bytes32 = normalize_bytes32_hash(self.crop_year_bytes32)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Preserve the chain binding and its transaction evidence."""

        raise ValidationError("Operações blockchain de recebimentos não podem ser apagadas.")

    def __str__(self) -> str:
        return f"Recebimento #{self.receipt_id} — {self.get_status_display()}"


class ReceiptBlockchainAttempt(NonDeletableModel):
    """Keep one immutable calldata snapshot for a single contract call."""

    class CallKind(models.TextChoices):
        OPEN = "open", "Abrir recebimento"
        LINK_INVOICE = "link_invoice", "Vincular nota fiscal"
        CONFIRM_WEIGHING = "confirm_weighing", "Confirmar pesagem"

    operation = models.ForeignKey(
        ReceiptBlockchainOperation,
        on_delete=models.PROTECT,
        related_name="attempts",
        verbose_name="Operação blockchain",
    )
    transaction = models.OneToOneField(
        BlockchainTransaction,
        on_delete=models.PROTECT,
        related_name="receipt_chain_attempt",
        verbose_name="Transação blockchain",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_receipt_blockchain_attempts",
        verbose_name="Preparada por",
    )
    call_kind = models.CharField("Chamada", max_length=24, choices=CallKind.choices)
    sender_address = models.CharField("Carteira que enviou esta chamada", max_length=42)
    prepared_data = models.TextField("Calldata preparada")
    expected_arguments = models.JSONField("Argumentos preparados")
    is_open = models.BooleanField("Tentativa aberta", default=True)
    submission_started_at = models.DateTimeField("Envio iniciado em", null=True, blank=True)

    class Meta:
        ordering = ("created_at", "pk")
        verbose_name = "tentativa blockchain do recebimento"
        verbose_name_plural = "tentativas blockchain do recebimento"
        constraints = [
            models.UniqueConstraint(
                fields=("operation", "call_kind"),
                condition=Q(is_open=True),
                name="uniq_open_receipt_chain_attempt",
            ),
        ]

    def clean(self) -> None:
        """Enforce matching company, network, deployment, sender, and call type."""

        super().clean()
        self.sender_address = normalize_wallet_address(self.sender_address)
        self.prepared_data = (self.prepared_data or "").strip().lower()
        if not is_valid_wallet_address(self.sender_address):
            raise ValidationError({"sender_address": "Informe uma carteira EVM válida."})
        if not re.fullmatch(r"0x(?:[0-9a-f]{2})+", self.prepared_data):
            raise ValidationError({"prepared_data": "O calldata preparado é inválido."})
        if not isinstance(self.expected_arguments, dict):
            raise ValidationError({"expected_arguments": "Os argumentos preparados são inválidos."})

        if self.operation_id and self.transaction_id:
            operation = self.operation
            record = self.transaction
            expected_operation_type = {
                self.CallKind.OPEN: RECEIPT_OPEN_OPERATION_TYPE,
                self.CallKind.LINK_INVOICE: RECEIPT_INVOICE_LINK_OPERATION_TYPE,
                self.CallKind.CONFIRM_WEIGHING: RECEIPT_WEIGHING_CONFIRMATION_OPERATION_TYPE,
            }.get(self.call_kind)
            if (
                expected_operation_type is None
                or record.operation_type != expected_operation_type
                or record.company_id != operation.receipt.company_id
                or record.network_id != operation.network_id
                or record.contract_id != operation.soja_coin_id
                or record.sender_address != self.sender_address
            ):
                raise ValidationError("A tentativa deve usar o contrato, rede e carteira da operação.")
            if (
                self.call_kind == self.CallKind.OPEN
                and self.sender_address != operation.opening_sender_address
            ):
                raise ValidationError("A abertura deve usar a carteira salva na operação.")
            if self.submission_started_at is None and record.status != BlockchainTransaction.Status.PREPARED:
                raise ValidationError("Uma transação enviada precisa ter horário de início salvo.")
            if record.status in {
                BlockchainTransaction.Status.CONFIRMED,
                BlockchainTransaction.Status.FAILED,
                BlockchainTransaction.Status.REORGED,
            } and self.is_open:
                raise ValidationError("Uma tentativa encerrada não pode continuar aberta.")

        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "operation_id",
                "transaction_id",
                "requested_by_id",
                "call_kind",
                "sender_address",
                "prepared_data",
                "expected_arguments",
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Os dados preparados de uma tentativa não podem ser alterados.")
            if previous.submission_started_at and previous.submission_started_at != self.submission_started_at:
                raise ValidationError("O início do envio não pode ser alterado.")
            if not previous.is_open and self.is_open:
                raise ValidationError("Uma tentativa encerrada não pode ser reaberta.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Normalize calldata and validate the immutable transaction snapshot."""

        self.sender_address = normalize_wallet_address(self.sender_address)
        self.prepared_data = (self.prepared_data or "").strip().lower()
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Preserve each submitted or rejected call for audit and recovery."""

        raise ValidationError("Tentativas blockchain de recebimentos não podem ser apagadas.")

    @property
    def status(self) -> str:
        """Expose the underlying transaction state without duplicating it."""

        return self.transaction.status

    def __str__(self) -> str:
        return f"{self.get_call_kind_display()} — {self.transaction}"
