from typing import Any, NoReturn

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from utils.identifiers import (
    is_valid_bytes32_hash,
    is_valid_cnpj,
    is_valid_transaction_hash,
    is_valid_wallet_address,
    normalize_cnpj,
    normalize_transaction_hash,
    normalize_wallet_address,
)


class TimestampedModel(models.Model):
    """Abstract base that gives every persisted record audit timestamps."""

    # Timestamp set once when this record is first inserted.
    created_at = models.DateTimeField("Criada em", auto_now_add=True)
    # Timestamp refreshed whenever this record is saved by Django.
    updated_at = models.DateTimeField("Atualizada em", auto_now=True)

    class Meta:
        abstract = True


class ImmutableHistoryQuerySet(models.QuerySet):
    """QuerySet that cannot remove or mutate an audit record in bulk."""

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Reject bulk deletion so history cannot disappear accidentally."""

        raise ValidationError("History records cannot be deleted.")

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Reject bulk updates so an audit record remains immutable."""

        raise ValidationError("History records cannot be updated.")

    def bulk_update(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Reject bulk updates for the same reason as ``update``."""

        raise ValidationError("History records cannot be updated.")


class ImmutableHistoryManager(models.Manager):
    """Manager used by records that must be append-only."""

    def get_queryset(self) -> ImmutableHistoryQuerySet:
        """Return a queryset that rejects destructive bulk operations."""

        return ImmutableHistoryQuerySet(self.model, using=self._db)


class ImmutableHistoryModel(TimestampedModel):
    """Append-only base for local audit records."""

    objects = ImmutableHistoryManager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Allow creation only and validate the complete audit record."""

        if not self._state.adding:
            raise ValidationError("History records cannot be updated.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(
        self,
        using: str | None = None,
        keep_parents: bool = False,
    ) -> tuple[int, dict[str, int]]:
        """Reject deletion of an individual audit record."""

        raise ValidationError("History records cannot be deleted.")


class Company(TimestampedModel):
    """A SaaS customer company, isolated from other companies.

    The CNPJ is stored in canonical form: 14 alphanumeric characters without
    punctuation. Full validation runs through ``full_clean()``, which Django
    forms and the Django Admin call before saving.
    """

    # Registered legal name used in official and contractual records.
    legal_name = models.CharField("Razão social", max_length=200)
    # Optional public-facing name used in the SaaS interface.
    trade_name = models.CharField("Nome fantasia", max_length=200, blank=True)
    # Unique business identifier. Input punctuation is removed before storage.
    cnpj = models.CharField(
        "CNPJ",
        max_length=18,
        unique=True,
        help_text="Pode ser informado com ou sem pontuação; será armazenado normalizado.",
    )
    # Disabled companies keep their records but cannot operate in the SaaS.
    active = models.BooleanField("Ativa", default=True)

    class Meta:
        ordering = ["legal_name"]
        verbose_name = "empresa"
        verbose_name_plural = "empresas"

    def clean(self) -> None:
        """Normalize the CNPJ and validate its format and check digits."""
        super().clean()
        self.cnpj = normalize_cnpj(self.cnpj or "")
        if len(self.cnpj) != 14 or not is_valid_cnpj(self.cnpj):
            raise ValidationError({"cnpj": "Informe um CNPJ válido com 14 caracteres."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep the CNPJ normalized for direct saves as well.

        Code that does not use a ModelForm or the Django Admin should call
        ``full_clean()`` before ``save()`` to run full validation.
        """
        self.cnpj = normalize_cnpj(self.cnpj or "")
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        name = self.trade_name or self.legal_name
        return f"{name} ({self.cnpj})"


class CompanyMembership(TimestampedModel):
    """Connect a Django user to one company without sharing company scope."""

    # Company that owns this membership and all records accessed through it.
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="memberships",
        verbose_name="Empresa",
    )
    # Django account that receives this company's SaaS access.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="company_memberships",
        verbose_name="Usuário",
    )
    # Deactivation removes application access without deleting the audit trail.
    active = models.BooleanField("Ativo", default=True)

    class Meta:
        ordering = ["company", "user"]
        verbose_name = "vínculo com empresa"
        verbose_name_plural = "vínculos com empresas"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "user"),
                name="uniq_company_membership_user",
            ),
        ]

    def __str__(self):
        return f"{self.user} — {self.company}"


class MembershipRole(TimestampedModel):
    """Assign one or more SaaS functions to a company membership."""

    class Role(models.TextChoices):
        # Company-level administrator who manages staff and configuration.
        ADMINISTRATOR = "administrator", "Administrator"
        # User who prepares participant and CPR records.
        REGISTRATION = "registration", "Registration operator"
        # User who receives and confirms physical loads.
        RECEIVING = "receiving", "Receiving operator"
        # User who creates issuance batches and sends issuance operations.
        ISSUANCE = "issuance", "Issuance operator"
        # User who records stock audits and cannot operate inventory flows.
        AUDITOR = "auditor", "Auditor"

    # Membership to which this role belongs.
    membership = models.ForeignKey(
        CompanyMembership,
        on_delete=models.PROTECT,
        related_name="roles",
        verbose_name="Vínculo",
    )
    # SaaS function assigned to the membership.
    role = models.CharField("Função", max_length=32, choices=Role.choices)
    # Inactive roles remain stored for audit but grant no application access.
    active = models.BooleanField("Ativa", default=True)
    # User who granted or last re-granted this role.
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="granted_membership_roles",
        verbose_name="Concedida por",
    )
    # Time at which an inactive role was revoked; null while active.
    revoked_at = models.DateTimeField("Revogada em", null=True, blank=True)

    class Meta:
        ordering = ["membership", "role"]
        verbose_name = "função do vínculo"
        verbose_name_plural = "funções dos vínculos"
        constraints = [
            models.UniqueConstraint(
                fields=("membership", "role"),
                condition=Q(active=True),
                name="uniq_active_membership_role",
            ),
        ]

    def clean(self) -> None:
        """Keep the active flag and revocation timestamp consistent."""
        super().clean()
        if self.membership_id and self.granted_by_id:
            if self.membership.user_id == self.granted_by_id:
                raise ValidationError(
                    {"granted_by": "A user cannot grant a function to themselves."}
                )
        if self.active:
            self.revoked_at = None
        elif self.revoked_at is None:
            self.revoked_at = timezone.now()

    def save(self, *args, **kwargs):
        """Validate the grant and preserve revocation history."""
        self.full_clean()
        if self.active:
            self.revoked_at = None
        elif self.revoked_at is None:
            self.revoked_at = timezone.now()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.membership} — {self.get_role_display()}"


class Unit(TimestampedModel):
    """A warehouse or other registered physical unit of a company."""

    # Company that owns this unit and its operational history.
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="units",
        verbose_name="Empresa",
    )
    # Human-readable name shown in the SaaS.
    name = models.CharField("Nome", max_length=200)
    # Company-scoped code used by operators and integrations.
    code = models.CharField("Código", max_length=64)
    # Address entered in the unit registration form and included in its hash.
    address = models.CharField("Endereço", max_length=300, blank=True)
    # Canonical CNPJ uniquely identifies the unit throughout the SaaS.
    cnpj = models.CharField("CNPJ da unidade", max_length=14, unique=True)
    # State registration entered in the form and included in its hash.
    state_registration = models.CharField(
        "Inscrição estadual",
        max_length=32,
        blank=True,
    )
    # Keccak-256 of the canonical registration fields sent to the unit registry.
    registration_hash = models.CharField(
        "Hash do registro",
        max_length=66,
        blank=True,
        editable=False,
    )
    # Inactive units remain queryable but cannot receive new operations.
    active = models.BooleanField("Ativa", default=True)
    # Unit ID returned by the on-chain unit registry after confirmation.
    chain_unit_id = models.PositiveBigIntegerField(
        "ID da unidade na rede",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["company", "name"]
        verbose_name = "unidade"
        verbose_name_plural = "unidades"
        constraints = [
            models.CheckConstraint(
                condition=Q(cnpj__regex=r"^[A-Z0-9]{12}[0-9]{2}$"),
                name="unit_cnpj_canonical_format",
            ),
            models.UniqueConstraint(
                fields=("company", "code"),
                name="uniq_company_unit_code",
            ),
            models.UniqueConstraint(
                fields=("company", "chain_unit_id"),
                condition=Q(chain_unit_id__isnull=False),
                name="uniq_company_chain_unit",
            ),
        ]

    def clean(self):
        """Normalize and validate the unit's code, CNPJ and registration hash."""
        super().clean()
        self.code = (self.code or "").strip().upper()
        self.cnpj = normalize_cnpj(self.cnpj or "")
        if not self.code:
            raise ValidationError({"code": "Informe o código da unidade."})
        if not is_valid_cnpj(self.cnpj):
            raise ValidationError({"cnpj": "Informe um CNPJ válido."})
        self.registration_hash = (self.registration_hash or "").strip().lower()
        if self.registration_hash and not is_valid_bytes32_hash(self.registration_hash):
            raise ValidationError(
                {"registration_hash": "Informe uma hash hexadecimal de 32 bytes válida."}
            )

    def save(self, *args, **kwargs):
        """Keep identifiers normalized for direct saves as well."""
        self.code = (self.code or "").strip().upper()
        self.cnpj = normalize_cnpj(self.cnpj or "")
        self.registration_hash = (self.registration_hash or "").strip().lower()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.code})"


class UnitAccess(TimestampedModel):
    """Grant a membership scoped access to one physical unit."""

    # Staff membership receiving this unit-level grant.
    membership = models.ForeignKey(
        CompanyMembership,
        on_delete=models.PROTECT,
        related_name="unit_accesses",
        verbose_name="Vínculo",
    )
    # Unit to which the grant applies.
    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        related_name="access_grants",
        verbose_name="Unidade",
    )
    # Allows reading data without authorizing an operational blockchain call.
    can_view = models.BooleanField("Pode visualizar", default=False)
    # Allows preparation of receiving operations in the SaaS; the wallet rule is separate.
    can_receive = models.BooleanField("Pode receber", default=False)
    # Allows preparation of issuance operations in the SaaS; the wallet rule is separate.
    can_issue = models.BooleanField("Pode emitir", default=False)
    # Allows reading and submitting audit records for this unit.
    can_audit = models.BooleanField("Pode auditar", default=False)
    # Inactive grants are preserved but do not authorize application actions.
    active = models.BooleanField("Ativo", default=True)

    class Meta:
        ordering = ["unit", "membership"]
        verbose_name = "acesso à unidade"
        verbose_name_plural = "acessos às unidades"
        constraints = [
            models.UniqueConstraint(
                fields=("membership", "unit"),
                name="uniq_membership_unit_access",
            ),
        ]

    def clean(self):
        """Prevent a membership from crossing company boundaries."""
        super().clean()
        if self.membership_id and self.unit_id:
            if self.membership.company_id != self.unit.company_id:
                raise ValidationError(
                    "O vínculo e a unidade devem pertencer à mesma empresa."
                )

    def save(self, *args, **kwargs):
        """Validate the company boundary on every persistence path."""

        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.membership} — {self.unit}"


class UnitWalletPermission(TimestampedModel):
    """Track the latest unit permission mask synchronized with the blockchain."""

    class SyncStatus(models.TextChoices):
        # Permission change is prepared locally and has not been confirmed.
        PENDING = "pending", "Pending"
        # Permission transaction was confirmed on the configured network.
        CONFIRMED = "confirmed", "Confirmed"
        # Permission transaction was rejected or reverted.
        FAILED = "failed", "Failed"
        # Permission was revoked while retaining the previous audit record.
        REVOKED = "revoked", "Revoked"

    # Unit whose on-chain permission mask is being tracked.
    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        related_name="wallet_permissions",
        verbose_name="Unidade",
    )
    # Public EVM address; private keys are never stored in this model.
    wallet = models.CharField("Carteira", max_length=42)
    # Complete uint256 mask sent to the contract, stored as an exact decimal value.
    permission_mask = models.DecimalField(
        "Máscara de permissão",
        max_digits=78,
        decimal_places=0,
        default=0,
    )
    # Latest configuration transaction hash, when a transaction exists.
    transaction = models.CharField("Transação", max_length=66, blank=True)
    # Local synchronization state; pending values are not treated as effective.
    sync_status = models.CharField(
        "Status da sincronização",
        max_length=16,
        choices=SyncStatus.choices,
        default=SyncStatus.PENDING,
    )

    class Meta:
        ordering = ["unit", "wallet"]
        verbose_name = "permissão de carteira na unidade"
        verbose_name_plural = "permissões de carteiras nas unidades"
        constraints = [
            models.UniqueConstraint(
                fields=("unit", "wallet"),
                name="uniq_unit_wallet_permission",
            ),
        ]

    @property
    def is_effective(self) -> bool:
        """Return whether the permission is confirmed on the blockchain."""

        return self.sync_status == self.SyncStatus.CONFIRMED and bool(self.transaction)

    def clean(self) -> None:
        """Normalize and validate public blockchain references."""
        super().clean()
        self.wallet = normalize_wallet_address(self.wallet)
        self.transaction = normalize_transaction_hash(self.transaction)
        if not is_valid_wallet_address(self.wallet):
            raise ValidationError({"wallet": "Informe um endereço EVM válido."})
        if self.transaction and not is_valid_transaction_hash(self.transaction):
            raise ValidationError({"transaction": "Informe um hash de transação válido."})
        if self.permission_mask < 0:
            raise ValidationError({"permission_mask": "A máscara não pode ser negativa."})
        if self.sync_status in {
            self.SyncStatus.CONFIRMED,
            self.SyncStatus.REVOKED,
        } and not self.transaction:
            raise ValidationError(
                {"transaction": "A permissão só pode ser efetiva após uma transação."}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep public blockchain references normalized for direct saves."""
        self.wallet = normalize_wallet_address(self.wallet)
        self.transaction = normalize_transaction_hash(self.transaction)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.unit} — {self.wallet}"


class AccessChange(ImmutableHistoryModel):
    """Immutable-style audit record for SaaS and wallet access changes."""

    class Action(models.TextChoices):
        # A company membership was created or reactivated.
        MEMBERSHIP_GRANTED = "membership_granted", "Membership granted"
        # A company membership was deactivated.
        MEMBERSHIP_REVOKED = "membership_revoked", "Membership revoked"
        # A role was granted to a membership.
        ROLE_GRANTED = "role_granted", "Role granted"
        # A role was revoked from a membership.
        ROLE_REVOKED = "role_revoked", "Role revoked"
        # Unit-level SaaS access flags changed.
        UNIT_ACCESS_UPDATED = "unit_access_updated", "Unit access updated"
        # An on-chain wallet permission mask changed or was synchronized.
        WALLET_PERMISSION_UPDATED = "wallet_permission_updated", "Wallet permission updated"

    # Company scope used to prevent cross-company audit records.
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="access_changes",
        verbose_name="Empresa",
    )
    # Membership affected by this access event.
    membership = models.ForeignKey(
        CompanyMembership,
        on_delete=models.PROTECT,
        related_name="access_changes",
        verbose_name="Vínculo",
    )
    # Optional unit scope for unit-specific changes.
    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        related_name="access_changes",
        verbose_name="Unidade",
        null=True,
        blank=True,
    )
    # Django user who initiated or recorded the change.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="performed_access_changes",
        verbose_name="Responsável",
    )
    # Kind of access change represented by this record.
    action = models.CharField("Ação", max_length=40, choices=Action.choices)
    # Previous serialized values, kept for an auditable before/after view.
    previous_value = models.JSONField("Valor anterior", default=dict, blank=True)
    # New serialized values, kept for an auditable before/after view.
    new_value = models.JSONField("Novo valor", default=dict, blank=True)
    # Blockchain transaction hash when this event tracks an on-chain change.
    transaction = models.CharField("Transação", max_length=66, blank=True)
    # Business time of the access event; unlike updated_at, it is not refreshed on edits.
    occurred_at = models.DateTimeField("Ocorrido em", default=timezone.now)

    class Meta:
        ordering = ["-occurred_at"]
        verbose_name = "alteração de acesso"
        verbose_name_plural = "alterações de acesso"
        indexes = [
            models.Index(fields=("company", "occurred_at"), name="access_change_company_time"),
            models.Index(fields=("membership", "occurred_at"), name="access_change_member_time"),
        ]

    def clean(self) -> None:
        """Ensure the recorded company matches the affected objects."""
        super().clean()
        self.transaction = normalize_transaction_hash(self.transaction)
        if self.transaction and not is_valid_transaction_hash(self.transaction):
            raise ValidationError({"transaction": "Informe um hash de transação válido."})
        if self.membership_id and self.company_id:
            if self.membership.company_id != self.company_id:
                raise ValidationError(
                    "O vínculo do histórico deve pertencer à empresa informada."
                )
        if self.unit_id and self.company_id:
            if self.unit.company_id != self.company_id:
                raise ValidationError(
                    "A unidade do histórico deve pertencer à empresa informada."
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep an optional transaction hash normalized for direct saves."""
        self.transaction = normalize_transaction_hash(self.transaction)
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.get_action_display()} — {self.company}"
