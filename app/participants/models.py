"""Participant records, immutable terms, acceptance links and chain attempts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from web3 import Web3

from blockchain.models import (
    PARTICIPANT_REGISTRATION_OPERATION_TYPE,
    BlockchainTransaction,
    ContractInstance,
    Network,
)
from companies.models import Company
from utils.identifiers import (
    is_valid_bytes32_hash,
    is_valid_transaction_hash,
    is_valid_wallet_address,
    normalize_bytes32_hash,
    normalize_transaction_hash,
    normalize_wallet_address,
)

from .documents import participant_terms_pdf_hash, read_valid_participant_terms_pdf


class TimestampedModel(models.Model):
    """Provide creation and update timestamps for participant records."""

    created_at = models.DateTimeField("Criado em", auto_now_add=True)
    updated_at = models.DateTimeField("Atualizado em", auto_now=True)

    class Meta:
        abstract = True


class Participant(TimestampedModel):
    """A company participant identified by a public wallet address."""

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="participants",
        verbose_name="Empresa",
    )
    name = models.CharField("Nome do participante", max_length=200)
    wallet_address = models.CharField("Carteira", max_length=42)
    wallet_address_normalized = models.CharField(
        "Carteira normalizada",
        max_length=42,
        editable=False,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_participants",
        verbose_name="Criado por",
    )

    class Meta:
        ordering = ("name", "pk")
        verbose_name = "participante"
        verbose_name_plural = "participantes"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "wallet_address_normalized"),
                name="uniq_company_participant_wallet",
            ),
        ]

    def clean(self) -> None:
        """Normalize the public address and validate its EVM format."""

        super().clean()
        self.name = (self.name or "").strip()
        self.wallet_address = (self.wallet_address or "").strip()
        self.wallet_address_normalized = normalize_wallet_address(self.wallet_address)
        if not self.name:
            raise ValidationError({"name": "Informe o nome do participante."})
        if not is_valid_wallet_address(self.wallet_address_normalized):
            raise ValidationError({"wallet_address": "Informe uma carteira EVM válida."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep the normalized address correct on every save path."""

        self.wallet_address = (self.wallet_address or "").strip()
        self.wallet_address_normalized = normalize_wallet_address(self.wallet_address)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Keep participants with terms or transactions in the audit trail."""

        raise ValidationError("Registros de participantes não podem ser apagados.")

    @property
    def latest_terms(self) -> ParticipantTerms | None:
        """Return the newest saved terms version, when one exists."""

        return self.terms_versions.order_by("-version", "-pk").first()

    def __str__(self) -> str:
        return f"{self.name} — {self.company}"


def participant_terms_upload_path(instance: ParticipantTermsDocument, filename: str) -> str:
    """Store each uploaded terms PDF under a non-guessable, company-scoped name."""

    return f"participant_terms/company_{instance.company_id}/{uuid4().hex}.pdf"


class ParticipantTermsDocumentQuerySet(models.QuerySet):
    """Prevent bulk operations from changing or removing published terms."""

    def update(self, **kwargs: Any) -> NoReturn:
        raise ValidationError("Versões publicadas de termos não podem ser alteradas em lote.")

    def delete(self) -> NoReturn:
        raise ValidationError("Versões publicadas de termos não podem ser apagadas.")


class ParticipantTermsDocumentManager(models.Manager):
    """Use the guarded queryset for all terms-document operations."""

    def get_queryset(self) -> ParticipantTermsDocumentQuerySet:
        return ParticipantTermsDocumentQuerySet(self.model, using=self._db)


class ParticipantTermsDocument(TimestampedModel):
    """An immutable, company-approved PDF version used by participant proposals."""

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="participant_terms_documents",
        verbose_name="Empresa",
    )
    version = models.PositiveIntegerField("Versão")
    original_filename = models.CharField("Nome original do arquivo", max_length=255)
    pdf_file = models.FileField(
        "Arquivo PDF",
        upload_to=participant_terms_upload_path,
        max_length=300,
    )
    terms_hash = models.CharField("Hash Keccak-256 do PDF", max_length=66)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="published_participant_terms",
        verbose_name="Publicado por",
    )
    company_approval_confirmed = models.BooleanField(
        "Aprovação da empresa confirmada", default=False
    )
    published_at = models.DateTimeField("Publicado em", default=timezone.now)
    is_current = models.BooleanField("Versão atual", default=True)

    objects = ParticipantTermsDocumentManager()

    class Meta:
        ordering = ("company", "-version", "-pk")
        verbose_name = "versão do termo do participante"
        verbose_name_plural = "versões dos termos do participante"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "version"),
                name="uniq_company_participant_terms_version",
            ),
            models.UniqueConstraint(
                fields=("company",),
                condition=Q(is_current=True),
                name="uniq_current_company_participant_terms",
            ),
        ]

    def clean(self) -> None:
        """Verify that the stored bytes are a PDF and match the recorded hash."""

        super().clean()
        self.original_filename = Path(self.original_filename or "").name.strip()
        self.terms_hash = normalize_bytes32_hash(self.terms_hash)
        if not self.original_filename.lower().endswith(".pdf"):
            raise ValidationError({"original_filename": "O arquivo dos termos precisa ser PDF."})
        if not self.company_approval_confirmed:
            raise ValidationError(
                {"company_approval_confirmed": "Confirme que a empresa aprovou este PDF."}
            )
        if not is_valid_bytes32_hash(self.terms_hash):
            raise ValidationError({"terms_hash": "A hash do PDF precisa ser bytes32."})
        if not self.pdf_file:
            raise ValidationError({"pdf_file": "Envie o PDF dos termos do participante."})

        pdf_bytes = read_valid_participant_terms_pdf(self.pdf_file.file)
        if participant_terms_pdf_hash(pdf_bytes) != self.terms_hash:
            raise ValidationError({"terms_hash": "A hash não corresponde aos bytes do PDF enviado."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep every published PDF and its identity immutable after upload."""

        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "company_id",
                "version",
                "original_filename",
                "terms_hash",
                "uploaded_by_id",
                "company_approval_confirmed",
                "published_at",
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("O arquivo e os dados de uma versão publicada são imutáveis.")
            if previous.pdf_file.name != self.pdf_file.name:
                raise ValidationError("O arquivo de uma versão publicada não pode ser trocado.")
            if previous.is_current is False and self.is_current:
                raise ValidationError("Uma versão antiga não pode voltar a ser a versão atual.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Preserve each published source file for existing acceptance records."""

        raise ValidationError("Versões publicadas de termos não podem ser apagadas.")

    def __str__(self) -> str:
        return f"{self.company} — termos do participante v{self.version}"


class ParticipantTerms(TimestampedModel):
    """Store one exact, versioned terms document and its on-chain hash."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Cadastro criado")
        PROPOSAL_PREPARED = "proposal_prepared", _("Proposta preparada")
        PROPOSAL_SUBMITTED = "proposal_submitted", _("Proposta enviada")
        AWAITING_ACCEPTANCE = "awaiting_acceptance", _("Aguardando aceite do participante")
        ACCEPTANCE_PREPARED = "acceptance_prepared", _("Aceite preparado")
        ACCEPTANCE_SUBMITTED = "acceptance_submitted", _("Aceite enviado")
        CONFIRMED = "confirmed", _("Cadastro confirmado")
        FAILED = "failed", _("Falha na transação")
        NEEDS_REVIEW = "needs_review", _("Precisa de conferência")

    participant = models.ForeignKey(
        Participant,
        on_delete=models.PROTECT,
        related_name="terms_versions",
        verbose_name="Participante",
    )
    document = models.ForeignKey(
        ParticipantTermsDocument,
        on_delete=models.PROTECT,
        related_name="participant_terms",
        null=True,
        blank=True,
        verbose_name="PDF publicado",
    )
    version = models.PositiveIntegerField("Versão")
    content = models.TextField("Conteúdo legado do termo", blank=True, default="")
    terms_hash = models.CharField("Hash do termo", max_length=66)
    status = models.CharField(
        "Situação",
        max_length=24,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    network = models.ForeignKey(
        Network,
        on_delete=models.PROTECT,
        related_name="participant_terms",
        null=True,
        blank=True,
        verbose_name="Rede usada",
    )
    contract = models.ForeignKey(
        ContractInstance,
        on_delete=models.PROTECT,
        related_name="participant_terms",
        null=True,
        blank=True,
        verbose_name="SojaCoin usado",
    )
    proposal_confirmed_at = models.DateTimeField(
        "Proposta confirmada em", null=True, blank=True
    )
    accepted_at = models.DateTimeField("Aceite confirmado em", null=True, blank=True)

    class Meta:
        ordering = ("participant", "-version")
        verbose_name = "termo de participação"
        verbose_name_plural = "termos de participação"
        constraints = [
            models.UniqueConstraint(
                fields=("participant", "version"),
                name="uniq_participant_terms_version",
            ),
        ]

    def clean(self) -> None:
        """Validate the hash and keep each terms version tied to one deployment."""

        super().clean()
        self.terms_hash = normalize_bytes32_hash(self.terms_hash)
        if not is_valid_bytes32_hash(self.terms_hash):
            raise ValidationError({"terms_hash": "A hash do termo precisa ser bytes32."})
        if self.document_id:
            if (
                self.document.company_id != self.participant.company_id
                or self.document.version != self.version
                or self.document.terms_hash != self.terms_hash
                or self.content
            ):
                raise ValidationError(
                    "O participante precisa usar a mesma versão PDF e hash publicadas para sua empresa."
                )
        else:
            if not self.content.strip():
                raise ValidationError({"content": "O termo legado não pode ficar vazio."})
            calculated_hash = Web3.to_hex(Web3.keccak(text=self.content))
            if calculated_hash != self.terms_hash:
                raise ValidationError({"terms_hash": "A hash não corresponde ao texto salvo."})
        if self.contract_id and self.network_id:
            if (
                self.contract.company_id != self.participant.company_id
                or self.contract.network_id != self.network_id
                or self.contract.kind != ContractInstance.Kind.SOJA_COIN
            ):
                raise ValidationError("O contrato e a rede precisam pertencer à empresa do participante.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Reject edits to the content or hash after a terms version is created."""

        self.terms_hash = normalize_bytes32_hash(self.terms_hash)
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "participant_id",
                "document_id",
                "version",
                "content",
                "terms_hash",
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("O conteúdo e a hash de uma versão do termo são imutáveis.")
            if previous.network_id and (
                previous.network_id != self.network_id
                or previous.contract_id != self.contract_id
            ):
                raise ValidationError("A rede e a SojaCoin vinculadas à proposta são imutáveis.")
            allowed_transitions = {
                (self.Status.DRAFT, self.Status.PROPOSAL_PREPARED),
                (self.Status.FAILED, self.Status.PROPOSAL_PREPARED),
                (self.Status.PROPOSAL_PREPARED, self.Status.PROPOSAL_SUBMITTED),
                (self.Status.PROPOSAL_PREPARED, self.Status.AWAITING_ACCEPTANCE),
                (self.Status.PROPOSAL_PREPARED, self.Status.FAILED),
                (self.Status.PROPOSAL_PREPARED, self.Status.NEEDS_REVIEW),
                (self.Status.PROPOSAL_SUBMITTED, self.Status.AWAITING_ACCEPTANCE),
                (self.Status.PROPOSAL_SUBMITTED, self.Status.FAILED),
                (self.Status.PROPOSAL_SUBMITTED, self.Status.NEEDS_REVIEW),
                (self.Status.AWAITING_ACCEPTANCE, self.Status.ACCEPTANCE_PREPARED),
                (self.Status.AWAITING_ACCEPTANCE, self.Status.NEEDS_REVIEW),
                (self.Status.ACCEPTANCE_PREPARED, self.Status.ACCEPTANCE_SUBMITTED),
                (self.Status.ACCEPTANCE_PREPARED, self.Status.AWAITING_ACCEPTANCE),
                (self.Status.ACCEPTANCE_PREPARED, self.Status.CONFIRMED),
                (self.Status.ACCEPTANCE_PREPARED, self.Status.NEEDS_REVIEW),
                (self.Status.ACCEPTANCE_SUBMITTED, self.Status.AWAITING_ACCEPTANCE),
                (self.Status.ACCEPTANCE_SUBMITTED, self.Status.CONFIRMED),
                (self.Status.ACCEPTANCE_SUBMITTED, self.Status.NEEDS_REVIEW),
                (self.Status.CONFIRMED, self.Status.NEEDS_REVIEW),
            }
            if previous.status != self.status and (previous.status, self.status) not in allowed_transitions:
                raise ValidationError(
                    f"Transição inválida para o termo: {previous.status} → {self.status}."
                )
            for field in ("proposal_confirmed_at", "accepted_at"):
                if getattr(previous, field) and getattr(previous, field) != getattr(self, field):
                    raise ValidationError("Datas de confirmação já registradas não podem ser alteradas.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise ValidationError("Versões de termos não podem ser apagadas.")

    def __str__(self) -> str:
        return f"{self.participant.name} — versão {self.version}"


class ParticipantAcceptanceLink(TimestampedModel):
    """A short-lived bearer link whose raw random token is never stored."""

    terms = models.ForeignKey(
        ParticipantTerms,
        on_delete=models.PROTECT,
        related_name="acceptance_links",
        verbose_name="Termo",
    )
    token_hash = models.CharField("Hash do código", max_length=64, unique=True)
    expires_at = models.DateTimeField("Expira em")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_participant_links",
        verbose_name="Criado por",
    )
    used_at = models.DateTimeField("Usado em", null=True, blank=True)
    canceled_at = models.DateTimeField("Cancelado em", null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "link de aceite de termo"
        verbose_name_plural = "links de aceite de termos"

    def clean(self) -> None:
        super().clean()
        if not re.fullmatch(r"[0-9a-f]{64}", self.token_hash or ""):
            raise ValidationError({"token_hash": "A hash do link é inválida."})
        if self.used_at and self.canceled_at:
            raise ValidationError("Um link usado não pode estar cancelado.")

    @property
    def is_usable(self) -> bool:
        """Return whether the bearer link is still valid for its participant."""

        return (
            self.used_at is None
            and self.canceled_at is None
            and self.expires_at > timezone.now()
        )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep link identity fixed and allow only one-way lifecycle timestamps."""

        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = ("terms_id", "token_hash", "expires_at", "created_by_id")
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Os dados de identificação do link são imutáveis.")
            if previous.used_at and previous.used_at != self.used_at:
                raise ValidationError("A data de uso do link não pode ser alterada.")
            if previous.canceled_at and previous.canceled_at != self.canceled_at:
                raise ValidationError("A data de cancelamento do link não pode ser alterada.")
            if previous.used_at and self.canceled_at:
                raise ValidationError("Um link usado não pode ser cancelado.")
            if previous.canceled_at and self.used_at:
                raise ValidationError("Um link cancelado não pode ser usado.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise ValidationError("Links de aceite devem ser preservados no histórico.")


class ParticipantAcceptanceChallenge(TimestampedModel):
    """Persist the exact EIP-712 challenge shown to the participant's wallet."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Aguardando assinatura")
        SIGNED = "signed", _("Assinado")
        INVALIDATED = "invalidated", _("Invalidado")

    link = models.ForeignKey(
        ParticipantAcceptanceLink,
        on_delete=models.PROTECT,
        related_name="challenges",
        verbose_name="Link de aceite",
    )
    chain_nonce = models.PositiveBigIntegerField("Nonce do contrato")
    deadline = models.PositiveBigIntegerField("Prazo da assinatura")
    digest = models.CharField("Digest EIP-712", max_length=66)
    typed_data = models.JSONField("Dados tipados apresentados")
    status = models.CharField(
        "Situação", max_length=16, choices=Status.choices, default=Status.PENDING
    )
    signature = models.CharField("Assinatura", max_length=132, blank=True)
    signed_at = models.DateTimeField("Assinado em", null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "desafio EIP-712 de aceite"
        verbose_name_plural = "desafios EIP-712 de aceite"

    def clean(self) -> None:
        super().clean()
        self.digest = normalize_bytes32_hash(self.digest)
        if not is_valid_bytes32_hash(self.digest):
            raise ValidationError({"digest": "Informe um digest EIP-712 válido."})
        if self.status == self.Status.SIGNED and (not self.signature or not self.signed_at):
            raise ValidationError("Um desafio assinado precisa guardar a assinatura e o horário.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep the typed-data payload fixed after issuing its one-use challenge."""

        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = ("link_id", "chain_nonce", "deadline", "digest", "typed_data")
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Os dados EIP-712 emitidos não podem ser alterados.")
            if previous.signature and previous.signature != self.signature:
                raise ValidationError("A assinatura guardada não pode ser substituída.")
            if previous.signed_at and previous.signed_at != self.signed_at:
                raise ValidationError("A data da assinatura não pode ser alterada.")
            allowed_transitions = {
                (self.Status.PENDING, self.Status.SIGNED),
                (self.Status.PENDING, self.Status.INVALIDATED),
                (self.Status.SIGNED, self.Status.INVALIDATED),
            }
            if previous.status != self.status and (previous.status, self.status) not in allowed_transitions:
                raise ValidationError("O desafio assinado ou invalidado não pode ser reaberto.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise ValidationError("Desafios de aceite devem ser preservados no histórico.")


class ParticipantRegistrationAttempt(TimestampedModel):
    """Preserve each unsigned call and its blockchain transaction lifecycle."""

    class Kind(models.TextChoices):
        PROPOSAL = "proposal", _("Proposta de cadastro")
        ACCEPTANCE = "acceptance", _("Aceite do participante")

    terms = models.ForeignKey(
        ParticipantTerms,
        on_delete=models.PROTECT,
        related_name="attempts",
        verbose_name="Termo",
    )
    kind = models.CharField("Etapa", max_length=16, choices=Kind.choices)
    transaction = models.OneToOneField(
        BlockchainTransaction,
        on_delete=models.PROTECT,
        related_name="participant_registration_attempt",
        verbose_name="Transação blockchain",
    )
    acceptance_link = models.ForeignKey(
        ParticipantAcceptanceLink,
        on_delete=models.PROTECT,
        related_name="attempts",
        null=True,
        blank=True,
        verbose_name="Link usado no aceite",
    )
    acceptance_challenge = models.ForeignKey(
        ParticipantAcceptanceChallenge,
        on_delete=models.PROTECT,
        related_name="attempts",
        null=True,
        blank=True,
        verbose_name="Desafio assinado",
    )
    sender_address = models.CharField("Carteira que envia", max_length=42)
    prepared_data = models.TextField("Dados preparados da chamada")
    signature = models.CharField("Assinatura do participante", max_length=132, blank=True)
    deadline = models.PositiveBigIntegerField("Prazo da assinatura", null=True, blank=True)
    is_open = models.BooleanField("Tentativa aberta", default=True)
    submission_started_at = models.DateTimeField(
        "Solicitação de envio aberta em", null=True, blank=True
    )

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "tentativa de cadastro de participante"
        verbose_name_plural = "tentativas de cadastro de participantes"
        constraints = [
            models.UniqueConstraint(
                fields=("terms", "kind"),
                condition=Q(is_open=True),
                name="uniq_open_participant_registration_attempt",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self.sender_address = normalize_wallet_address(self.sender_address)
        self.prepared_data = (self.prepared_data or "").strip().lower()
        self.signature = (self.signature or "").strip().lower()
        if not is_valid_wallet_address(self.sender_address):
            raise ValidationError({"sender_address": "A carteira que envia é inválida."})
        if not re.fullmatch(r"0x(?:[0-9a-f]{2})+", self.prepared_data):
            raise ValidationError({"prepared_data": "Os dados preparados são inválidos."})
        if self.kind == self.Kind.PROPOSAL and (self.acceptance_link_id or self.signature):
            raise ValidationError("A proposta não pode conter dados do aceite do participante.")
        if self.kind == self.Kind.ACCEPTANCE:
            if not self.acceptance_link_id or not self.acceptance_challenge_id:
                raise ValidationError("O aceite precisa estar ligado ao link e ao desafio usados.")
            if not self.signature or self.deadline is None:
                raise ValidationError("O aceite precisa guardar assinatura e prazo.")
            if self.sender_address != self.terms.participant.wallet_address_normalized:
                raise ValidationError("O remetente do aceite precisa ser a carteira do participante.")
        if self.transaction_id and self.terms.network_id:
            if (
                self.transaction.operation_type != PARTICIPANT_REGISTRATION_OPERATION_TYPE
                or self.transaction.company_id != self.terms.participant.company_id
                or self.transaction.network_id != self.terms.network_id
                or self.transaction.contract_id != self.terms.contract_id
            ):
                raise ValidationError("A transação e o termo precisam usar a mesma empresa, rede e SojaCoin.")
            if self.transaction.sender_address != self.sender_address:
                raise ValidationError("A carteira remetente deve corresponder à transação.")
        if self.acceptance_link_id and self.acceptance_link.terms_id != self.terms_id:
            raise ValidationError("O link de aceite precisa pertencer ao mesmo termo.")
        if self.acceptance_challenge_id and (
            self.acceptance_challenge.link_id != self.acceptance_link_id
        ):
            raise ValidationError("O desafio precisa pertencer ao link usado nesta tentativa.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep the prepared call fixed and allow a one-way open-to-closed change."""

        self.sender_address = normalize_wallet_address(self.sender_address)
        self.prepared_data = (self.prepared_data or "").strip().lower()
        self.signature = (self.signature or "").strip().lower()
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "terms_id",
                "kind",
                "transaction_id",
                "acceptance_link_id",
                "acceptance_challenge_id",
                "sender_address",
                "prepared_data",
                "signature",
                "deadline",
            )
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Os dados preparados da tentativa são imutáveis.")
            if not previous.is_open and self.is_open:
                raise ValidationError("Uma tentativa encerrada não pode ser reaberta.")
            if previous.submission_started_at and previous.submission_started_at != self.submission_started_at:
                raise ValidationError("O início do envio não pode ser alterado.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise ValidationError("Tentativas de cadastro devem ser preservadas no histórico.")

    @property
    def transaction_hash(self) -> str:
        """Expose the linked transaction hash for templates without duplicating it."""

        return self.transaction.transaction_hash or ""
