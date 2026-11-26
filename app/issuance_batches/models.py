"""Durable local records for issuance batches, approval links and attempts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, NoReturn

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from blockchain.models import (
    BlockchainTransaction,
    ContractInstance,
    Network,
    NonDeletableModel,
)
from companies.models import Company, Unit
from participants.models import Participant
from receipts.models import Receipt
from utils.identifiers import (
    is_valid_bytes32_hash,
    is_valid_wallet_address,
    normalize_bytes32_hash,
    normalize_wallet_address,
)


ISSUANCE_BATCH_OPERATION_TYPE = "issuance_batch"
"""Stable discriminator for all batch-related BlockchainTransaction rows."""


class IssuanceBatch(NonDeletableModel):
    """Snapshot one or more receipts and their on-chain participant approval."""

    class Status(models.TextChoices):
        """Keep local lifecycle labels aligned with the chain batch state."""

        CREATION_PREPARED = "creation_prepared", _("Criação preparada")
        CREATION_SUBMITTED = "creation_submitted", _("Criação enviada")
        AWAITING_APPROVAL = "awaiting_approval", _("Aguardando aprovação")
        APPROVAL_SUBMITTED = "approval_submitted", _("Aprovação e emissão enviadas")
        APPROVED = "approved", _("Aprovado")
        COMPLETED = "completed", _("Tokens emitidos")
        CANCEL_SUBMITTED = "cancel_submitted", _("Cancelamento enviado")
        CANCELED = "canceled", _("Cancelado")
        NEEDS_REVIEW = "needs_review", _("Precisa de conferência")

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="issuance_batches",
        verbose_name="Empresa",
    )
    participant = models.ForeignKey(
        Participant,
        on_delete=models.PROTECT,
        related_name="issuance_batches",
        verbose_name="Participante",
    )
    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        related_name="issuance_batches",
        verbose_name="Unidade",
    )
    network = models.ForeignKey(
        Network,
        on_delete=models.PROTECT,
        related_name="issuance_batches",
        verbose_name="Rede",
    )
    soja_coin = models.ForeignKey(
        ContractInstance,
        on_delete=models.PROTECT,
        related_name="issuance_batches_as_token",
        verbose_name="SojaCoin",
    )
    unit_registry = models.ForeignKey(
        ContractInstance,
        on_delete=models.PROTECT,
        related_name="issuance_batches_as_registry",
        verbose_name="UnitRegistry",
    )
    batches_contract = models.ForeignKey(
        ContractInstance,
        on_delete=models.PROTECT,
        related_name="issuance_batches_as_module",
        verbose_name="TokenIssuanceBatches",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_issuance_batches",
        verbose_name="Criado por",
    )
    creator_address = models.CharField(
        "Carteira criadora",
        max_length=42,
        help_text="Carteira verificada do operador que criou o lote; o evento on-chain deve confirmá-la.",
    )
    chain_batch_id = models.PositiveBigIntegerField(
        "ID do lote na rede",
        null=True,
        blank=True,
    )
    total_net_weight_kg = models.PositiveBigIntegerField("Peso líquido total (kg)")
    approval_deadline = models.PositiveBigIntegerField(
        "Prazo de aprovação (Unix)",
        help_text=(
            "Prazo Unix fixado no contrato. Após expirar, o lote continua reservando a carga "
            "até a confirmação de um cancelamento."
        ),
    )
    summary_hash = models.CharField(
        "Hash do resumo do lote",
        max_length=66,
        help_text="Keccak-256 dos dados das cargas na ordem definida em TokenIssuanceBatches.sol.",
    )
    issued_amount_sjc = models.PositiveBigIntegerField(
        "Quantidade total emitida (unidades mínimas)",
        null=True,
        blank=True,
        help_text="Unidades inteiras confirmadas no evento TokensIssued; use decimals() para exibir SJC.",
    )
    participant_amount_sjc = models.PositiveBigIntegerField(
        "Quantidade recebida pelo participante (unidades mínimas)",
        null=True,
        blank=True,
        help_text="SJC cunhado para o participante nesta transação, separado dos pagamentos de CPR.",
    )
    token_decimals = models.PositiveSmallIntegerField(
        "Casas decimais do token",
        null=True,
        blank=True,
        help_text="Valor de decimals() lido no bloco da emissão confirmada.",
    )
    review_reason = models.TextField(
        "Motivo da conferência",
        blank=True,
        help_text="Primeira divergência que interrompeu o fluxo; preservada para análise operacional.",
    )
    status = models.CharField(
        "Situação",
        max_length=32,
        choices=Status.choices,
        default=Status.CREATION_PREPARED,
    )
    receipts = models.ManyToManyField(
        Receipt,
        through="IssuanceBatchReceipt",
        related_name="issuance_batches",
        verbose_name="Recebimentos",
    )

    class Meta:
        """Order batches newest first and index company/network lookups."""

        ordering = ("-created_at", "-pk")
        verbose_name = "lote de emissão"
        verbose_name_plural = "lotes de emissão"
        indexes = [
            models.Index(fields=("company", "status", "created_at"), name="issue_batch_company_state_idx"),
            models.Index(fields=("network", "chain_batch_id"), name="issue_batch_chain_id_idx"),
        ]

    def clean(self) -> None:
        """Validate the deployment snapshot and chain fields before persistence."""

        super().clean()
        self.creator_address = normalize_wallet_address(self.creator_address)
        self.summary_hash = normalize_bytes32_hash(self.summary_hash)
        if not is_valid_wallet_address(self.creator_address):
            raise ValidationError({"creator_address": "A carteira criadora é inválida."})
        if not is_valid_bytes32_hash(self.summary_hash):
            raise ValidationError({"summary_hash": "A hash do resumo é inválida."})
        if self.total_net_weight_kg < 1:
            raise ValidationError({"total_net_weight_kg": "O peso líquido precisa ser positivo."})
        if self.approval_deadline < 1:
            raise ValidationError({"approval_deadline": "Informe um prazo válido."})
        if self.participant_id and self.company_id and self.participant.company_id != self.company_id:
            raise ValidationError({"participant": "O participante precisa pertencer à empresa do lote."})
        if self.unit_id and self.company_id and self.unit.company_id != self.company_id:
            raise ValidationError({"unit": "A unidade precisa pertencer à empresa do lote."})
        expected_kinds = (
            (self.soja_coin, ContractInstance.Kind.SOJA_COIN),
            (self.unit_registry, ContractInstance.Kind.UNIT_REGISTRY),
            (self.batches_contract, ContractInstance.Kind.ISSUANCE_BATCHES),
        )
        for instance, kind in expected_kinds:
            if instance and (
                instance.company_id != self.company_id
                or instance.network_id != self.network_id
                or instance.kind != kind
            ):
                raise ValidationError("Os contratos precisam pertencer à empresa e à rede do lote.")
        if self.chain_batch_id is not None and self.chain_batch_id < 1:
            raise ValidationError({"chain_batch_id": "O ID do lote na rede precisa ser positivo."})
        if self.status in {
            self.Status.AWAITING_APPROVAL,
            self.Status.APPROVAL_SUBMITTED,
            self.Status.APPROVED,
            self.Status.COMPLETED,
            self.Status.CANCEL_SUBMITTED,
            self.Status.CANCELED,
        } and self.chain_batch_id is None:
            raise ValidationError("Um lote criado na rede precisa ter seu ID confirmado pelo evento.")
        saved_issuance_values = (
            self.issued_amount_sjc,
            self.participant_amount_sjc,
            self.token_decimals,
        )
        if any(value is None for value in saved_issuance_values) and not all(
            value is None for value in saved_issuance_values
        ):
            raise ValidationError("As quantidades e a precisão do token precisam ser registradas juntas.")
        if self.issued_amount_sjc is not None:
            if self.issued_amount_sjc < 1 or self.participant_amount_sjc > self.issued_amount_sjc:
                raise ValidationError("As quantidades emitida e recebida são inconsistentes.")
            if self.token_decimals > 77:
                raise ValidationError({"token_decimals": "A precisão do token é inválida."})
            if self.status not in {self.Status.COMPLETED, self.Status.NEEDS_REVIEW}:
                raise ValidationError("As quantidades só podem ser salvas após a confirmação da emissão.")
        if self.status == self.Status.COMPLETED and (
            self.chain_batch_id is None or self.issued_amount_sjc is None or self.token_decimals is None
        ):
            raise ValidationError("Um lote concluído precisa de ID, emissão e precisão confirmados na rede.")

        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "company_id",
                "participant_id",
                "unit_id",
                "network_id",
                "soja_coin_id",
                "unit_registry_id",
                "batches_contract_id",
                "created_by_id",
                "creator_address",
                "total_net_weight_kg",
                "approval_deadline",
                "summary_hash",
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("O conteúdo e a intenção inicial do lote são imutáveis.")
            if previous.chain_batch_id is not None and previous.chain_batch_id != self.chain_batch_id:
                raise ValidationError("O ID confirmado do lote não pode ser substituído.")
            if previous.review_reason and previous.review_reason != self.review_reason:
                raise ValidationError("O motivo da conferência não pode ser substituído.")
            for field_name in ("issued_amount_sjc", "participant_amount_sjc", "token_decimals"):
                previous_amount = getattr(previous, field_name)
                if previous_amount is not None and previous_amount != getattr(self, field_name):
                    raise ValidationError("As quantidades confirmadas da emissão não podem ser substituídas.")
            allowed = {
                (self.Status.CREATION_PREPARED, self.Status.CREATION_SUBMITTED),
                (self.Status.CREATION_PREPARED, self.Status.NEEDS_REVIEW),
                (self.Status.CREATION_SUBMITTED, self.Status.CREATION_PREPARED),
                (self.Status.CREATION_SUBMITTED, self.Status.AWAITING_APPROVAL),
                (self.Status.CREATION_SUBMITTED, self.Status.NEEDS_REVIEW),
                (self.Status.AWAITING_APPROVAL, self.Status.APPROVAL_SUBMITTED),
                (self.Status.AWAITING_APPROVAL, self.Status.CANCEL_SUBMITTED),
                (self.Status.AWAITING_APPROVAL, self.Status.NEEDS_REVIEW),
                (self.Status.APPROVAL_SUBMITTED, self.Status.AWAITING_APPROVAL),
                (self.Status.APPROVAL_SUBMITTED, self.Status.APPROVED),
                (self.Status.APPROVAL_SUBMITTED, self.Status.COMPLETED),
                (self.Status.APPROVAL_SUBMITTED, self.Status.NEEDS_REVIEW),
                (self.Status.CANCEL_SUBMITTED, self.Status.AWAITING_APPROVAL),
                (self.Status.CANCEL_SUBMITTED, self.Status.CANCELED),
                (self.Status.CANCEL_SUBMITTED, self.Status.NEEDS_REVIEW),
                (self.Status.CANCELED, self.Status.NEEDS_REVIEW),
                (self.Status.APPROVED, self.Status.NEEDS_REVIEW),
                (self.Status.COMPLETED, self.Status.NEEDS_REVIEW),
                (self.Status.NEEDS_REVIEW, self.Status.NEEDS_REVIEW),
            }
            if previous.status != self.status and (previous.status, self.status) not in allowed:
                raise ValidationError(f"Transição de lote inválida: {previous.status} → {self.status}.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Normalize and validate the immutable batch snapshot on each save."""

        self.creator_address = normalize_wallet_address(self.creator_address)
        self.summary_hash = normalize_bytes32_hash(self.summary_hash)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Keep batch state and transaction history available for audit."""

        raise ValidationError("Lotes de emissão não podem ser apagados.")

    @property
    def approval_expired(self) -> bool:
        """Return whether the chain deadline passed without releasing its deposit lock."""

        return self.status == self.Status.AWAITING_APPROVAL and self.approval_deadline <= int(
            timezone.now().timestamp()
        )

    @property
    def approval_deadline_at(self) -> datetime:
        """Expose the Unix contract deadline as a timezone-aware display value."""

        return datetime.fromtimestamp(
            self.approval_deadline,
            tz=timezone.get_current_timezone(),
        )

    def __str__(self) -> str:
        """Return a stable label for admin and log records."""

        return f"Lote #{self.chain_batch_id or 'pendente'} — {self.participant.name}"


class IssuanceBatchReceipt(NonDeletableModel):
    """Preserve each receipt's exact on-chain data as it entered a batch."""

    batch = models.ForeignKey(
        IssuanceBatch,
        on_delete=models.PROTECT,
        related_name="receipt_rows",
        verbose_name="Lote",
    )
    receipt = models.ForeignKey(
        Receipt,
        on_delete=models.PROTECT,
        related_name="issuance_batch_rows",
        verbose_name="Recebimento",
    )
    deposit_id = models.PositiveBigIntegerField(
        "ID do recebimento na rede",
        help_text="ID emitido pela SojaCoin e usado pelo lote; não é o ID local do recebimento.",
    )
    unit_chain_id = models.PositiveBigIntegerField("ID da unidade na rede")
    crop_year_bytes32 = models.CharField(
        "Safra codificada", max_length=66, help_text="Bytes32 da safra exatamente como gravados no depósito."
    )
    invoice_key_hash = models.CharField(
        "Hash da chave da NF-e", max_length=66, help_text="Hash bytes32 vinculada à carga na SojaCoin."
    )
    weighing_ticket_hash = models.CharField(
        "Hash do romaneio", max_length=66, help_text="Hash bytes32 da pesagem confirmada na SojaCoin."
    )
    net_weight_kg = models.PositiveBigIntegerField(
        "Peso líquido (kg)", help_text="Peso inteiro em kg aprovado pelo contrato para este depósito."
    )
    is_current = models.BooleanField(
        "Mantém o recebimento reservado",
        default=True,
        help_text="Fica falso após cancelamento confirmado, preservando o histórico do lote.",
    )

    class Meta:
        """Preserve a receipt snapshot once per batch and one active reservation."""

        ordering = ("pk",)
        verbose_name = "recebimento do lote"
        verbose_name_plural = "recebimentos do lote"
        constraints = [
            models.UniqueConstraint(fields=("batch", "receipt"), name="uniq_receipt_in_issue_batch"),
            models.UniqueConstraint(
                fields=("receipt",),
                condition=Q(is_current=True),
                name="uniq_current_issuance_batch_receipt",
            ),
        ]

    def clean(self) -> None:
        """Ensure this historical row matches its receipt and batch ownership."""

        super().clean()
        self.crop_year_bytes32 = normalize_bytes32_hash(self.crop_year_bytes32)
        self.invoice_key_hash = normalize_bytes32_hash(self.invoice_key_hash)
        self.weighing_ticket_hash = normalize_bytes32_hash(self.weighing_ticket_hash)
        for field_name in ("crop_year_bytes32", "invoice_key_hash", "weighing_ticket_hash"):
            if not is_valid_bytes32_hash(getattr(self, field_name)):
                raise ValidationError({field_name: "Informe uma hash bytes32 válida."})
        if self.deposit_id < 1 or self.unit_chain_id < 1 or self.net_weight_kg < 1:
            raise ValidationError("Os IDs e o peso líquido do recebimento precisam ser positivos.")
        if self.batch_id and self.receipt_id:
            if self.batch.company_id != self.receipt.company_id:
                raise ValidationError("O recebimento e o lote precisam pertencer à mesma empresa.")
            if self.batch.participant_id != self.receipt.participant_id:
                raise ValidationError("O recebimento precisa pertencer ao participante do lote.")
            if self.batch.unit_id != self.receipt.unit_id:
                raise ValidationError("O recebimento precisa pertencer à unidade do lote.")
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "batch_id",
                "receipt_id",
                "deposit_id",
                "unit_chain_id",
                "crop_year_bytes32",
                "invoice_key_hash",
                "weighing_ticket_hash",
                "net_weight_kg",
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("O snapshot do recebimento não pode ser alterado.")
            if not previous.is_current and self.is_current:
                raise ValidationError("Um recebimento liberado não pode ser reservado por um lote antigo.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate the immutable receipt snapshot before saving it."""

        self.crop_year_bytes32 = normalize_bytes32_hash(self.crop_year_bytes32)
        self.invoice_key_hash = normalize_bytes32_hash(self.invoice_key_hash)
        self.weighing_ticket_hash = normalize_bytes32_hash(self.weighing_ticket_hash)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Preserve receipt membership even after a batch is canceled."""

        raise ValidationError("O histórico dos recebimentos em lotes não pode ser apagado.")


class IssuanceBatchApprovalLink(NonDeletableModel):
    """One-time bearer access to a single participant approval page."""

    batch = models.ForeignKey(
        IssuanceBatch,
        on_delete=models.PROTECT,
        related_name="approval_links",
        verbose_name="Lote",
    )
    token_hash = models.CharField(
        "Hash do código",
        max_length=64,
        unique=True,
        help_text="SHA-256 do segredo aleatório exibido uma única vez ao operador.",
    )
    expires_at = models.DateTimeField(
        "Expira em",
        help_text=(
            "Validade limitada do link. O prazo da aprovação é separado e continua sendo "
            "validado pelo contrato."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_issuance_batch_links",
        verbose_name="Criado por",
    )
    used_at = models.DateTimeField("Usado em", null=True, blank=True)
    canceled_at = models.DateTimeField("Cancelado em", null=True, blank=True)

    class Meta:
        """Allow one unused, uncanceled participant link for a batch at a time."""

        ordering = ("-created_at", "-pk")
        verbose_name = "link de aprovação do lote"
        verbose_name_plural = "links de aprovação dos lotes"
        constraints = [
            models.UniqueConstraint(
                fields=("batch",),
                condition=Q(used_at__isnull=True, canceled_at__isnull=True),
                name="uniq_open_issuance_batch_link",
            ),
        ]

    def clean(self) -> None:
        """Validate link secrecy and its one-way use/cancellation state."""

        super().clean()
        if not re.fullmatch(r"[0-9a-f]{64}", self.token_hash or ""):
            raise ValidationError({"token_hash": "A hash do link é inválida."})
        if self.used_at and self.canceled_at:
            raise ValidationError("Um link usado não pode ser cancelado.")
        if self.batch_id and self.batch.status not in {
            IssuanceBatch.Status.AWAITING_APPROVAL,
            IssuanceBatch.Status.APPROVAL_SUBMITTED,
            IssuanceBatch.Status.APPROVED,
            IssuanceBatch.Status.COMPLETED,
            IssuanceBatch.Status.NEEDS_REVIEW,
            IssuanceBatch.Status.CANCELED,
        }:
            raise ValidationError("O link só pode estar associado a um lote criado na rede.")
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = ("batch_id", "token_hash", "expires_at", "created_by_id")
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("A identidade e a validade original do link são imutáveis.")
            for field in ("used_at", "canceled_at"):
                if getattr(previous, field) and getattr(previous, field) != getattr(self, field):
                    raise ValidationError("As datas de uso e cancelamento não podem ser substituídas.")

    @property
    def is_usable(self) -> bool:
        """Return whether the link can open or submit an approval."""

        return (
            self.used_at is None
            and self.canceled_at is None
            and self.expires_at > timezone.now()
            and self.batch.status in {
                IssuanceBatch.Status.AWAITING_APPROVAL,
                IssuanceBatch.Status.APPROVAL_SUBMITTED,
            }
            and not self.batch.approval_expired
        )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate one-way link lifecycle changes before persistence."""

        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Preserve access history and consumed link evidence."""

        raise ValidationError("Links de aprovação devem permanecer no histórico.")


class IssuanceBatchAuthorizationChallenge(NonDeletableModel):
    """Persist the exact EIP-712 message shown to the participant's wallet."""

    class Status(models.TextChoices):
        """Track whether an exact typed-data challenge is pending, signed or void."""

        PENDING = "pending", _( "Aguardando assinatura" )
        SIGNED = "signed", _( "Assinado" )
        INVALIDATED = "invalidated", _( "Invalidado" )

    link = models.ForeignKey(
        IssuanceBatchApprovalLink,
        on_delete=models.PROTECT,
        related_name="challenges",
        verbose_name="Link de aprovação",
    )
    chain_nonce = models.PositiveBigIntegerField(
        "Nonce do participante", help_text="Nonce lido do contrato no momento da preparação da assinatura."
    )
    deadline = models.PositiveBigIntegerField(
        "Prazo assinado", help_text="Prazo do lote incorporado ao struct EIP-712."
    )
    digest = models.CharField(
        "Digest EIP-712", max_length=66, help_text="Digest recalculado e comparado ao digest do contrato."
    )
    typed_data = models.JSONField(
        "Dados tipados apresentados", help_text="Snapshot imutável do domínio e struct enviados à carteira."
    )
    status = models.CharField("Situação", max_length=16, choices=Status.choices, default=Status.PENDING)
    signature = models.CharField("Assinatura EIP-712", max_length=132, blank=True)
    signed_at = models.DateTimeField("Assinado em", null=True, blank=True)

    class Meta:
        """Keep the newest participant authorization challenges easy to inspect."""

        ordering = ("-created_at", "-pk")
        verbose_name = "desafio EIP-712 do lote"
        verbose_name_plural = "desafios EIP-712 dos lotes"

    def clean(self) -> None:
        """Keep the signed challenge tied to its link, nonce and exact digest."""

        super().clean()
        self.digest = normalize_bytes32_hash(self.digest)
        if not is_valid_bytes32_hash(self.digest):
            raise ValidationError({"digest": "O digest EIP-712 é inválido."})
        if self.deadline < 1 or self.chain_nonce < 0:
            raise ValidationError("O nonce e o prazo da assinatura são inválidos.")
        if self.status == self.Status.SIGNED and (not self.signature or self.signed_at is None):
            raise ValidationError("Um desafio assinado precisa guardar assinatura e horário.")
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = ("link_id", "chain_nonce", "deadline", "digest", "typed_data")
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Os dados tipados apresentados não podem ser alterados.")
            if previous.signature and previous.signature != self.signature:
                raise ValidationError("Uma assinatura EIP-712 salva não pode ser substituída.")
            allowed = {
                (self.Status.PENDING, self.Status.SIGNED),
                (self.Status.PENDING, self.Status.INVALIDATED),
                (self.Status.SIGNED, self.Status.INVALIDATED),
            }
            if previous.status != self.status and (previous.status, self.status) not in allowed:
                raise ValidationError("O desafio assinado ou invalidado não pode ser reaberto.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate the one-way challenge lifecycle before persistence."""

        self.digest = normalize_bytes32_hash(self.digest)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Keep signature challenges available for approval audit."""

        raise ValidationError("Desafios de assinatura devem permanecer no histórico.")


class IssuanceBatchAttempt(NonDeletableModel):
    """Record immutable calldata and the transaction lifecycle for one batch action."""

    class Kind(models.TextChoices):
        """Distinguish transaction actions while sharing one recovery lifecycle."""

        CREATE = "create", _( "Criar lote" )
        APPROVE = "approve", _( "Aprovar lote" )
        APPROVE_AND_ISSUE = "approve_issue", _( "Aprovar e emitir tokens" )
        CANCEL = "cancel", _( "Cancelar lote" )

    batch = models.ForeignKey(
        IssuanceBatch,
        on_delete=models.PROTECT,
        related_name="attempts",
        verbose_name="Lote",
    )
    transaction = models.OneToOneField(
        BlockchainTransaction,
        on_delete=models.PROTECT,
        related_name="issuance_batch_attempt",
        verbose_name="Transação blockchain",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_issuance_batch_attempts",
        verbose_name="Solicitado por",
        null=True,
        blank=True,
    )
    link = models.ForeignKey(
        IssuanceBatchApprovalLink,
        on_delete=models.PROTECT,
        related_name="attempts",
        null=True,
        blank=True,
        verbose_name="Link participante",
    )
    challenge = models.ForeignKey(
        IssuanceBatchAuthorizationChallenge,
        on_delete=models.PROTECT,
        related_name="attempts",
        null=True,
        blank=True,
        verbose_name="Desafio assinado",
    )
    kind = models.CharField("Ação", max_length=16, choices=Kind.choices)
    sender_address = models.CharField("Carteira que envia", max_length=42)
    prepared_data = models.TextField(
        "Calldata preparada", help_text="Chamada ABI imutável que o servidor compara com a transação retornada."
    )
    expected_arguments = models.JSONField(
        "Argumentos e snapshot esperados",
        help_text="IDs, hashes, prazo e valores usados para validar o evento e o estado on-chain.",
    )
    signature = models.CharField(
        "Assinatura do participante", max_length=132, blank=True, help_text="Assinatura EIP-712 usada na chamada de aprovação."
    )
    submission_started_at = models.DateTimeField(
        "Envio iniciado em",
        null=True,
        blank=True,
        help_text="Marca que a carteira pode ter transmitido; ausência permite retomar sem ambiguidade.",
    )
    is_open = models.BooleanField("Tentativa aberta", default=True)

    class Meta:
        """Prevent simultaneous open creation, approval or cancellation attempts."""

        ordering = ("-created_at", "-pk")
        verbose_name = "tentativa de transação do lote"
        verbose_name_plural = "tentativas de transação dos lotes"
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "kind"),
                condition=Q(is_open=True),
                name="uniq_open_issuance_batch_action",
            ),
        ]

    def clean(self) -> None:
        """Require matching company, network, contract, sender and prepared call."""

        super().clean()
        self.sender_address = normalize_wallet_address(self.sender_address)
        self.prepared_data = (self.prepared_data or "").strip().lower()
        self.signature = (self.signature or "").strip().lower()
        if not is_valid_wallet_address(self.sender_address):
            raise ValidationError({"sender_address": "A carteira que envia é inválida."})
        if not re.fullmatch(r"0x(?:[0-9a-f]{2})+", self.prepared_data):
            raise ValidationError({"prepared_data": "O calldata preparado é inválido."})
        if not isinstance(self.expected_arguments, dict):
            raise ValidationError({"expected_arguments": "Os argumentos esperados são inválidos."})
        if self.kind == self.Kind.APPROVE:
            if self.link_id is None or self.challenge_id is None or not self.signature:
                raise ValidationError("A aprovação precisa de link, desafio e assinatura do participante.")
            if self.link.batch_id != self.batch_id or self.challenge.link_id != self.link_id:
                raise ValidationError("O desafio e o link precisam pertencer a este lote.")
            if self.sender_address != self.batch.participant.wallet_address_normalized:
                raise ValidationError("A aprovação deve ser enviada pela carteira do participante.")
        elif self.kind == self.Kind.APPROVE_AND_ISSUE:
            if self.link_id is None or self.challenge_id is not None or self.signature:
                raise ValidationError("A aprovação com emissão precisa do link, sem assinatura separada.")
            if self.link.batch_id != self.batch_id:
                raise ValidationError("O link precisa pertencer a este lote.")
            if self.sender_address != self.batch.participant.wallet_address_normalized:
                raise ValidationError("A aprovação deve ser enviada pela carteira do participante.")
        elif self.kind == self.Kind.CANCEL:
            if self.challenge_id is not None or self.signature:
                raise ValidationError("O cancelamento não pode conter desafio ou assinatura de aprovação.")
            if self.link_id is not None and self.link.batch_id != self.batch_id:
                raise ValidationError("O link de cancelamento precisa pertencer ao lote da tentativa.")
        elif self.link_id is not None or self.challenge_id is not None or self.signature:
            raise ValidationError("Somente a tentativa de aprovação guarda dados do participante.")
        record = self.transaction if self.transaction_id else None
        if record is not None:
            if (
                record.operation_type != ISSUANCE_BATCH_OPERATION_TYPE
                or record.company_id != self.batch.company_id
                or record.network_id != self.batch.network_id
                or record.contract_id != self.batch.batches_contract_id
                or record.sender_address != self.sender_address
            ):
                raise ValidationError("A transação precisa corresponder ao lote, rede e carteira.")
            if self.submission_started_at is None and record.status != BlockchainTransaction.Status.PREPARED:
                raise ValidationError("Uma transação transmitida precisa de horário de início salvo.")
            if record.status in {
                BlockchainTransaction.Status.CONFIRMED,
                BlockchainTransaction.Status.FAILED,
                BlockchainTransaction.Status.REORGED,
            } and self.is_open:
                raise ValidationError("Uma tentativa encerrada não pode continuar aberta.")
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "batch_id",
                "transaction_id",
                "requested_by_id",
                "link_id",
                "challenge_id",
                "kind",
                "sender_address",
                "prepared_data",
                "expected_arguments",
                "signature",
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Os dados preparados de uma tentativa são imutáveis.")
            if previous.submission_started_at and previous.submission_started_at != self.submission_started_at:
                raise ValidationError("O início do envio não pode ser alterado.")
            if not previous.is_open and self.is_open:
                raise ValidationError("Uma tentativa encerrada não pode ser reaberta.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Normalize the sender and validate immutable attempt data."""

        self.sender_address = normalize_wallet_address(self.sender_address)
        self.prepared_data = (self.prepared_data or "").strip().lower()
        self.signature = (self.signature or "").strip().lower()
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Preserve submitted, rejected and recovered transaction attempts."""

        raise ValidationError("As tentativas de lote não podem ser apagadas.")

    @property
    def status(self) -> str:
        """Expose the canonical transaction state without duplicating it."""

        return self.transaction.status

    def __str__(self) -> str:
        """Return an admin-friendly description of this transaction action."""

        return f"{self.get_kind_display()} — lote #{self.batch.chain_batch_id or self.batch_id}"
