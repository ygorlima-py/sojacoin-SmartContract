"""Database records used to reconcile the SaaS with the blockchain."""

from __future__ import annotations

import re
from typing import Any, NoReturn
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from companies.models import (
    Company,
    CompanyMembership,
    ImmutableHistoryModel,
    TimestampedModel,
    Unit,
)
from utils.identifiers import (
    is_valid_bytes32_hash,
    is_valid_transaction_hash,
    is_valid_wallet_address,
    normalize_transaction_hash,
    normalize_wallet_address,
)


DEPLOYMENT_OPERATION_TYPE = "contract_deployment"
CONTRACT_CONFIGURATION_OPERATION_TYPE = "contract_configuration"
UNIT_REGISTRATION_OPERATION_TYPE = "unit_registration"
UNIT_WALLET_PERMISSION_OPERATION_TYPE = "unit_wallet_permission"
PARTICIPANT_REGISTRATION_OPERATION_TYPE = "participant_registration"
RECEIPT_OPEN_OPERATION_TYPE = "receipt_open"
RECEIPT_INVOICE_LINK_OPERATION_TYPE = "receipt_invoice_link"
RECEIPT_WEIGHING_CONFIRMATION_OPERATION_TYPE = "receipt_weighing_confirmation"


class NonDeletableQuerySet(models.QuerySet):
    """QuerySet that preserves configuration and transaction records."""

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Reject bulk deletion; records are deactivated instead."""

        raise ValidationError("Blockchain records cannot be deleted.")

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Reject bulk updates that could bypass lifecycle validation."""

        raise ValidationError("Use a model save or lifecycle service to update records.")

    def bulk_update(self, *args: Any, **kwargs: Any) -> NoReturn:
        """Reject bulk updates for the same lifecycle-safety reason."""

        raise ValidationError("Use a model save or lifecycle service to update records.")


class NonDeletableManager(models.Manager):
    """Manager used by records whose history must remain available."""

    def get_queryset(self) -> NonDeletableQuerySet:
        """Return a queryset that rejects bulk deletion."""

        return NonDeletableQuerySet(self.model, using=self._db)


class NonDeletableModel(TimestampedModel):
    """Timestamped record that can be deactivated but never deleted."""

    objects = NonDeletableManager()

    class Meta:
        abstract = True

    def delete(
        self,
        using: str | None = None,
        keep_parents: bool = False,
    ) -> tuple[int, dict[str, int]]:
        """Reject deletion of one blockchain record."""

        raise ValidationError("Blockchain records cannot be deleted.")


class Network(NonDeletableModel):
    """A blockchain network configured for the SaaS."""

    # Human-readable network name shown to the operator.
    name = models.CharField("Name", max_length=120)
    # EVM chain identifier used to prevent signing on the wrong network.
    chain_id = models.PositiveBigIntegerField("Chain ID", unique=True)
    # Each saved network selects its own endpoint instead of sharing one project-wide URL.
    rpc_url = models.URLField(
        "RPC URL",
        max_length=500,
        blank=True,
        help_text=(
            "HTTP or HTTPS JSON-RPC endpoint. Treat credentials in this URL as "
            "secrets; network settings are restricted to superusers."
        ),
    )
    # Disabled networks must not receive new transactions.
    active = models.BooleanField("Active", default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "network"
        verbose_name_plural = "networks"

    def clean(self) -> None:
        """Validate the network label, RPC URL, and chain identifier."""

        super().clean()
        self.name = (self.name or "").strip()
        self.rpc_url = (self.rpc_url or "").strip()
        if not self.name:
            raise ValidationError({"name": "Enter a network name."})
        if not self.rpc_url:
            raise ValidationError(
                {"rpc_url": "Configure the RPC URL for this network."}
            )
        try:
            parsed_url = urlsplit(self.rpc_url)
        except ValueError as error:
            raise ValidationError(
                {"rpc_url": "Enter a valid HTTP or HTTPS RPC URL."}
            ) from error
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValidationError(
                {"rpc_url": "Use a complete HTTP or HTTPS RPC URL."}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate the network before saving it."""

        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Return the configured network name."""

        return self.name


class ContractInstance(NonDeletableModel):
    """A deployed contract belonging to one company and one network."""

    class Kind(models.TextChoices):
        SOJA_COIN = "sojacoin", _("SojaCoin")
        UNIT_REGISTRY = "unit_registry", _("Unit registry")
        ISSUANCE_BATCHES = "issuance_batches", _("Issuance batches")
        SOJA_COIN_ADMINISTRATION = (
            "sojacoin_administration",
            _("SojaCoin administration module"),
        )
        SOJA_COIN_DEPOSITS = "sojacoin_deposits", _("SojaCoin deposits module")
        SOJA_COIN_CPR = "sojacoin_cpr", _("SojaCoin CPR module")
        SOJA_COIN_SERVICES = "sojacoin_services", _("SojaCoin services module")
        SOJA_COIN_LOCKS = "sojacoin_locks", _("SojaCoin locks module")
        SOJA_COIN_REDEMPTIONS = (
            "sojacoin_redemptions",
            _("SojaCoin redemptions module"),
        )

    # Company that owns this deployment and its operational state.
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="contract_instances",
        verbose_name="Company",
    )
    # Network where this address is deployed.
    network = models.ForeignKey(
        Network,
        on_delete=models.PROTECT,
        related_name="contract_instances",
        verbose_name="Network",
    )
    # Contract type used when routing a blockchain operation.
    kind = models.CharField("Kind", max_length=32, choices=Kind.choices)
    # Address as entered by the operator or deployment process.
    address = models.CharField("Address", max_length=42)
    # Lowercase address used for comparisons and uniqueness.
    address_normalized = models.CharField(
        "Normalized address",
        max_length=42,
        editable=False,
    )
    # Application or contract version that produced this deployment.
    # Artifact SHA-256 labels use the prefix plus all 64 hexadecimal digits.
    version = models.CharField("Version", max_length=71)
    # Deployment transaction, when the deployment has been reconciled.
    deployment_transaction_hash = models.CharField(
        "Deployment transaction hash",
        max_length=66,
        blank=True,
    )
    # Inactive deployments remain available for historical lookups.
    active = models.BooleanField("Active", default=True)

    class Meta:
        ordering = ["company", "network", "kind"]
        verbose_name = "contract instance"
        verbose_name_plural = "contract instances"
        constraints = [
            models.UniqueConstraint(
                fields=("network", "address_normalized"),
                name="uniq_contract_network_address",
            ),
            models.UniqueConstraint(
                fields=("company", "network", "kind"),
                condition=Q(active=True),
                name="uniq_active_contract_company_kind",
            ),
        ]

    def clean(self) -> None:
        """Normalize addresses and keep the deployment tied to its network."""

        super().clean()
        self.address = (self.address or "").strip()
        self.address_normalized = normalize_wallet_address(self.address)
        self.version = (self.version or "").strip()
        self.deployment_transaction_hash = normalize_transaction_hash(
            self.deployment_transaction_hash
        )
        if not is_valid_wallet_address(self.address_normalized):
            raise ValidationError({"address": "Enter a valid EVM contract address."})
        if not self.version:
            raise ValidationError({"version": "Enter the deployed contract version."})
        if self.deployment_transaction_hash and not is_valid_transaction_hash(
            self.deployment_transaction_hash
        ):
            raise ValidationError(
                {"deployment_transaction_hash": "Enter a valid transaction hash."}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep public blockchain references normalized on every write path."""

        self.address = (self.address or "").strip()
        self.address_normalized = normalize_wallet_address(self.address)
        self.deployment_transaction_hash = normalize_transaction_hash(
            self.deployment_transaction_hash
        )
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Return the contract type and address."""

        return f"{self.get_kind_display()} — {self.address_normalized}"


class BlockchainTransaction(NonDeletableModel):
    """A local lifecycle record for one blockchain transaction."""

    class Status(models.TextChoices):
        PREPARED = "prepared", _("Prepared")
        SUBMITTED = "submitted", _("Submitted")
        CONFIRMED = "confirmed", _("Confirmed")
        FAILED = "failed", _("Failed")
        REORGED = "reorged", _("Confirmation reverted")

    # Company scope for every operation and history query.
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="blockchain_transactions",
        verbose_name="Company",
    )
    # Network used by the operation.
    network = models.ForeignKey(
        Network,
        on_delete=models.PROTECT,
        related_name="transactions",
        verbose_name="Network",
    )
    # Contract called by the transaction, when known.
    contract = models.ForeignKey(
        ContractInstance,
        on_delete=models.PROTECT,
        related_name="transactions",
        verbose_name="Contract",
        null=True,
        blank=True,
    )
    # Stable application name such as ``unit_registration`` or ``issuance``.
    operation_type = models.CharField("Operation type", max_length=64)
    # Public wallet that submitted the transaction.
    sender_address = models.CharField("Sender address", max_length=42)
    # Empty until the network accepts the transaction.
    transaction_hash = models.CharField(
        "Transaction hash",
        max_length=66,
        blank=True,
        null=True,
    )
    # A prepared or submitted transaction is not yet a completed operation.
    status = models.CharField(
        "Status",
        max_length=16,
        choices=Status.choices,
        default=Status.PREPARED,
    )
    # Block data is filled only after confirmation is observed.
    block_number = models.PositiveBigIntegerField(
        "Block number",
        null=True,
        blank=True,
    )
    block_hash = models.CharField("Block hash", max_length=66, blank=True)
    submitted_at = models.DateTimeField("Submitted at", null=True, blank=True)
    confirmed_at = models.DateTimeField("Confirmed at", null=True, blank=True)
    error_message = models.TextField("Error message", blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "blockchain transaction"
        verbose_name_plural = "blockchain transactions"
        constraints = [
            models.UniqueConstraint(
                fields=("network", "transaction_hash"),
                condition=Q(transaction_hash__isnull=False)
                & ~Q(transaction_hash=""),
                name="uniq_network_transaction_hash",
            ),
        ]

    @property
    def is_confirmed(self) -> bool:
        """Return whether the transaction is safe to show as completed."""

        return self.status == self.Status.CONFIRMED

    def _validate_status_transition(self) -> None:
        """Prevent direct saves from moving a transaction backwards."""

        if self.pk is None or self._state.adding:
            return

        previous = type(self).objects.get(pk=self.pk)
        if previous.status == self.status:
            if previous.status == self.Status.CONFIRMED and (
                previous.transaction_hash != self.transaction_hash
                or previous.block_number != self.block_number
                or previous.block_hash != self.block_hash
                or previous.confirmed_at != self.confirmed_at
            ):
                raise ValidationError("Confirmation evidence cannot be changed.")
            return

        allowed = {
            (self.Status.PREPARED, self.Status.SUBMITTED),
            (self.Status.PREPARED, self.Status.FAILED),
            (self.Status.SUBMITTED, self.Status.CONFIRMED),
            (self.Status.SUBMITTED, self.Status.FAILED),
            (self.Status.CONFIRMED, self.Status.REORGED),
        }
        if (previous.status, self.status) not in allowed:
            raise ValidationError(
                f"Invalid transaction status transition: {previous.status} -> {self.status}."
            )

    def clean(self) -> None:
        """Enforce lifecycle data required by each transaction status."""

        super().clean()
        self.operation_type = (self.operation_type or "").strip()
        self.sender_address = normalize_wallet_address(self.sender_address)
        self.transaction_hash = normalize_transaction_hash(self.transaction_hash) or None
        self.block_hash = normalize_transaction_hash(self.block_hash)
        self.error_message = (self.error_message or "").strip()

        if not self.operation_type:
            raise ValidationError({"operation_type": "Enter an operation type."})
        if not is_valid_wallet_address(self.sender_address):
            raise ValidationError({"sender_address": "Enter a valid sender address."})
        if self.transaction_hash and not is_valid_transaction_hash(self.transaction_hash):
            raise ValidationError({"transaction_hash": "Enter a valid transaction hash."})
        if self.block_hash and not is_valid_transaction_hash(self.block_hash):
            raise ValidationError({"block_hash": "Enter a valid block hash."})
        if self.contract_id and (
            self.contract.company_id != self.company_id
            or self.contract.network_id != self.network_id
        ):
            raise ValidationError(
                "The contract must belong to the transaction company and network."
            )

        if self.status == self.Status.PREPARED:
            if self.transaction_hash or self.submitted_at or self.block_number is not None:
                raise ValidationError(
                    "A prepared transaction cannot contain network confirmation data."
                )
            if self.block_hash or self.confirmed_at or self.error_message:
                raise ValidationError(
                    "A prepared transaction cannot contain completion data."
                )
        elif self.status == self.Status.SUBMITTED:
            if not self.transaction_hash or self.submitted_at is None:
                raise ValidationError(
                    "A submitted transaction needs a hash and submission time."
                )
            if self.block_number is not None or self.block_hash or self.confirmed_at:
                raise ValidationError(
                    "A submitted transaction cannot contain confirmation data."
                )
            if self.error_message:
                raise ValidationError(
                    "A submitted transaction cannot contain an error message."
                )
        elif self.status == self.Status.CONFIRMED:
            if not self.transaction_hash or self.submitted_at is None:
                raise ValidationError(
                    "A confirmed transaction needs a submitted transaction."
                )
            if self.block_number is None or not self.block_hash or self.confirmed_at is None:
                raise ValidationError(
                    "A confirmed transaction needs block data and confirmation time."
                )
            if self.error_message:
                raise ValidationError(
                    "A confirmed transaction cannot contain an error message."
                )
        elif self.status == self.Status.FAILED:
            if not self.error_message:
                raise ValidationError(
                    "A failed transaction needs an error message."
                )
            if self.confirmed_at is not None or self.block_number is not None or self.block_hash:
                raise ValidationError(
                    "A failed transaction cannot contain confirmation data."
                )
        elif self.status == self.Status.REORGED:
            if not self.transaction_hash or not self.error_message:
                raise ValidationError(
                    "A reverted confirmation needs a transaction hash and reason."
                )

        self._validate_status_transition()

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Normalize references and validate before persisting the lifecycle state."""

        self.sender_address = normalize_wallet_address(self.sender_address)
        self.transaction_hash = normalize_transaction_hash(self.transaction_hash) or None
        self.block_hash = normalize_transaction_hash(self.block_hash)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Return the operation type and current status."""

        return f"{self.operation_type} — {self.get_status_display()}"


class UnitRegistrationAttempt(NonDeletableModel):
    """Preserve the exact wallet call used to register one local unit.

    The linked transaction owns the changing network state. This record keeps
    the local unit and exact call arguments together so a retry can continue
    the same operation and a confirmed event can be matched safely.
    """

    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        related_name="blockchain_registration_attempts",
        verbose_name="Unit",
    )
    transaction = models.OneToOneField(
        BlockchainTransaction,
        on_delete=models.PROTECT,
        related_name="unit_registration_attempt",
        verbose_name="Blockchain transaction",
    )
    # Keep the exact values sent to the contract, even if local details change later.
    unit_code = models.CharField("Unit code", max_length=64)
    registration_hash = models.CharField("Registration hash", max_length=66)
    prepared_data = models.TextField("Prepared contract call data")
    # One open attempt per unit prevents duplicate sends while a wallet result
    # or network confirmation is still uncertain.
    is_open = models.BooleanField("Attempt is open", default=True)
    submission_started_at = models.DateTimeField(
        "Wallet submission started at",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ("created_at", "pk")
        verbose_name = "unit registration attempt"
        verbose_name_plural = "unit registration attempts"
        constraints = [
            models.UniqueConstraint(
                fields=("unit",),
                condition=Q(is_open=True),
                name="uniq_open_unit_registration_attempt",
            ),
        ]

    def clean(self) -> None:
        """Validate the unit, transaction scope and immutable wallet payload."""

        super().clean()
        self.unit_code = (self.unit_code or "").strip().upper()
        self.registration_hash = (self.registration_hash or "").strip().lower()
        self.prepared_data = (self.prepared_data or "").strip().lower()

        if (
            not self.unit_code
            or "\x00" in self.unit_code
            or len(self.unit_code.encode("utf-8")) > 32
        ):
            raise ValidationError(
                {"unit_code": "O código deve ocupar de 1 a 32 bytes UTF-8 e não conter byte nulo."}
            )
        if not is_valid_bytes32_hash(self.registration_hash):
            raise ValidationError(
                {"registration_hash": "Informe uma hash bytes32 válida."}
            )
        if not re.fullmatch(r"0x(?:[0-9a-f]{2})+", self.prepared_data):
            raise ValidationError(
                {"prepared_data": "Os dados preparados da chamada são inválidos."}
            )

        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                previous.unit_id == self.unit_id,
                previous.transaction_id == self.transaction_id,
                previous.unit_code == self.unit_code,
                previous.registration_hash == self.registration_hash,
                previous.prepared_data == self.prepared_data,
            )
            if not all(immutable_fields):
                raise ValidationError(
                    "A unidade e os dados preparados de uma tentativa não podem ser alterados."
                )
            if previous.submission_started_at and (
                previous.submission_started_at != self.submission_started_at
            ):
                raise ValidationError("O início do envio não pode ser alterado.")
            if not previous.is_open and self.is_open:
                raise ValidationError("Uma tentativa encerrada não pode ser reaberta.")

        if self.unit_id and self.transaction_id:
            record = self.transaction
            if record.operation_type != UNIT_REGISTRATION_OPERATION_TYPE:
                raise ValidationError(
                    {"transaction": "A transação precisa ser de registro de unidade."}
                )
            contract = record.contract if record.contract_id else None
            if (
                record.company_id != self.unit.company_id
                or contract is None
                or contract.company_id != self.unit.company_id
                or contract.network_id != record.network_id
                or contract.kind != ContractInstance.Kind.UNIT_REGISTRY
            ):
                raise ValidationError(
                    {
                        "transaction": (
                            "A transação precisa usar o UnitRegistry da empresa e rede corretas."
                        )
                    }
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate before storing the attempt as an immutable audit record."""

        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def status(self) -> str:
        """Expose the transaction lifecycle state on the unit attempt."""

        return self.transaction.status

    def __str__(self) -> str:
        """Identify the unit and transaction represented by this attempt."""

        return f"{self.unit} — {self.transaction}"


class UnitWalletPermissionAttempt(NonDeletableModel):
    """Preserve one prepared grant or revocation for a member's unit wallet.

    The immutable payload lets the server verify the exact call returned by the
    owner's wallet. ``is_open`` prevents a second call while the first wallet
    response or network confirmation is still uncertain.
    """

    class PermissionKind(models.TextChoices):
        RECEIVE = "receive", _("Recebimento")
        ISSUE = "issue", _("Emissão")

    class Action(models.TextChoices):
        GRANT = "grant", _("Conceder")
        REVOKE = "revoke", _("Revogar")

    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        related_name="wallet_permission_attempts",
        verbose_name="Unidade",
    )
    membership = models.ForeignKey(
        CompanyMembership,
        on_delete=models.PROTECT,
        related_name="wallet_permission_attempts",
        verbose_name="Pessoa autorizada",
    )
    # Keep the verified account-to-wallet link used when this call was prepared.
    user_wallet = models.ForeignKey(
        "accounts.UserWallet",
        on_delete=models.PROTECT,
        related_name="unit_permission_attempts",
        verbose_name="Carteira verificada",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_unit_wallet_permissions",
        verbose_name="Administrador responsável",
    )
    transaction = models.OneToOneField(
        BlockchainTransaction,
        on_delete=models.PROTECT,
        related_name="unit_wallet_permission_attempt",
        verbose_name="Transação blockchain",
    )
    wallet_address = models.CharField("Carteira autorizada", max_length=42)
    permission_kind = models.CharField(
        "Permissão",
        max_length=16,
        choices=PermissionKind.choices,
    )
    action = models.CharField("Ação", max_length=8, choices=Action.choices)
    # Solidity bit read from the saved UnitRegistry ABI at preparation time.
    permission_bit = models.DecimalField(
        "Bit da permissão",
        max_digits=78,
        decimal_places=0,
    )
    previous_permission_mask = models.DecimalField(
        "Permissões anteriores na unidade",
        max_digits=78,
        decimal_places=0,
    )
    permission_mask = models.DecimalField(
        "Permissões enviadas ao contrato",
        max_digits=78,
        decimal_places=0,
    )
    prepared_data = models.TextField("Dados preparados da chamada")
    is_open = models.BooleanField("Tentativa aberta", default=True)
    submission_started_at = models.DateTimeField(
        "Solicitação aberta na carteira em",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ("-created_at", "-pk")
        verbose_name = "tentativa de permissão de carteira"
        verbose_name_plural = "tentativas de permissões de carteira"
        constraints = [
            models.UniqueConstraint(
                fields=("unit", "wallet_address"),
                condition=Q(is_open=True),
                name="uniq_open_unit_wallet_permission_attempt",
            ),
        ]

    def clean(self) -> None:
        """Check the member, verified wallet, transaction and saved call scope."""

        super().clean()
        self.wallet_address = normalize_wallet_address(self.wallet_address)
        self.prepared_data = (self.prepared_data or "").strip().lower()

        if not is_valid_wallet_address(self.wallet_address):
            raise ValidationError({"wallet_address": "Informe uma carteira EVM válida."})
        if not re.fullmatch(r"0x(?:[0-9a-f]{2})+", self.prepared_data):
            raise ValidationError({"prepared_data": "Os dados preparados são inválidos."})
        if self.permission_kind not in self.PermissionKind.values:
            raise ValidationError({"permission_kind": "Escolha uma permissão válida."})
        if self.action not in self.Action.values:
            raise ValidationError({"action": "Escolha uma ação válida."})

        for field_name in (
            "permission_bit",
            "previous_permission_mask",
            "permission_mask",
        ):
            value = getattr(self, field_name)
            if value < 0 or value >= 2**256:
                raise ValidationError({field_name: "A máscara deve caber em uint256."})

        if self.unit_id and self.membership_id:
            if self.unit.company_id != self.membership.company_id:
                raise ValidationError("A pessoa e a unidade devem pertencer à mesma empresa.")
        if self.membership_id and self.user_wallet_id:
            if self.membership.user_id != self.user_wallet.user_id:
                raise ValidationError("A carteira verificada não pertence à pessoa selecionada.")
            if self.wallet_address != self.user_wallet.wallet.address_normalized:
                raise ValidationError("O endereço da tentativa difere da carteira verificada.")

        if self.transaction_id and self.unit_id:
            record = self.transaction
            contract = record.contract if record.contract_id else None
            if (
                record.operation_type != UNIT_WALLET_PERMISSION_OPERATION_TYPE
                or record.company_id != self.unit.company_id
                or contract is None
                or contract.company_id != self.unit.company_id
                or contract.network_id != record.network_id
                or contract.kind != ContractInstance.Kind.UNIT_REGISTRY
            ):
                raise ValidationError(
                    "A transação deve usar o UnitRegistry da empresa e da rede corretas."
                )

        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                previous.unit_id == self.unit_id,
                previous.membership_id == self.membership_id,
                previous.user_wallet_id == self.user_wallet_id,
                previous.requested_by_id == self.requested_by_id,
                previous.transaction_id == self.transaction_id,
                previous.wallet_address == self.wallet_address,
                previous.permission_kind == self.permission_kind,
                previous.action == self.action,
                previous.permission_bit == self.permission_bit,
                previous.previous_permission_mask == self.previous_permission_mask,
                previous.permission_mask == self.permission_mask,
                previous.prepared_data == self.prepared_data,
            )
            if not all(immutable_fields):
                raise ValidationError("Os dados preparados de uma tentativa não podem mudar.")
            if previous.submission_started_at and (
                previous.submission_started_at != self.submission_started_at
            ):
                raise ValidationError("O início do envio não pode ser alterado.")
            if not previous.is_open and self.is_open:
                raise ValidationError("Uma tentativa encerrada não pode ser reaberta.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Normalize references and validate before preserving the attempt."""

        self.wallet_address = normalize_wallet_address(self.wallet_address)
        self.prepared_data = (self.prepared_data or "").strip().lower()
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def status(self) -> str:
        """Expose the linked blockchain transaction lifecycle state."""

        return self.transaction.status

    def __str__(self) -> str:
        """Identify the member, wallet permission and transaction."""

        return f"{self.unit} — {self.wallet_address} — {self.transaction}"


class DeploymentProcess(NonDeletableModel):
    """Track the deployment of one company's nine contracts on one network."""

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", _("Em andamento")
        DEPLOYED = "deployed", _("Contratos implantados")
        CANCELED = "canceled", _("Cancelado")
        NEEDS_REVIEW = "needs_review", _("Precisa de revisão")

    # The company and network determine the scope of every deployment step.
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="deployment_processes")
    network = models.ForeignKey(Network, on_delete=models.PROTECT, related_name="deployment_processes")
    # Public address assigned ownership of SojaCoin and UnitRegistry.
    owner_address = models.CharField("Carteira do dono", max_length=42)
    status = models.CharField("Situação", max_length=16, choices=Status.choices, default=Status.IN_PROGRESS)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "processo de implantação"
        verbose_name_plural = "processos de implantação"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "network"),
                condition=Q(status__in=("in_progress", "needs_review")),
                name="uniq_active_deployment_process",
            ),
        ]

    def clean(self) -> None:
        """Normalize and validate the public owner address."""

        super().clean()
        self.owner_address = normalize_wallet_address(self.owner_address)
        if not is_valid_wallet_address(self.owner_address) or self.owner_address == "0x" + "0" * 40:
            raise ValidationError({"owner_address": "Informe um endereço de carteira válido."})
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if (previous.company_id != self.company_id or previous.network_id != self.network_id
                or previous.owner_address != self.owner_address):
                raise ValidationError("Empresa, rede e carteira do processo não podem ser alteradas.")
            allowed_transitions = {
                (self.Status.IN_PROGRESS, self.Status.DEPLOYED),
                (self.Status.IN_PROGRESS, self.Status.CANCELED),
                (self.Status.IN_PROGRESS, self.Status.NEEDS_REVIEW),
                (self.Status.DEPLOYED, self.Status.NEEDS_REVIEW),
            }
            if previous.status != self.status and (
                previous.status,
                self.status,
            ) not in allowed_transitions:
                raise ValidationError("A situação deste processo não pode ser alterada.")
        if self.status == self.Status.DEPLOYED:
            if not self.pk or self.steps.count() != 9 or self.confirmed_step_count != 9:
                raise ValidationError("Os nove contratos precisam estar confirmados.")

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate the process before storing it."""

        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Identify the company and network for an operator."""

        return f"{self.company} — {self.network}"

    @property
    def confirmed_step_count(self) -> int:
        """Count steps whose linked contract still has a confirmed deployment."""

        steps = self.steps.select_related("contract").prefetch_related(
            "attempts__transaction"
        )
        return sum(step.is_confirmed for step in steps)


class DeploymentStep(NonDeletableModel):
    """Represent one required contract within a deployment process."""

    # Steps are ordered so dependent contracts are created after their inputs.
    process = models.ForeignKey(DeploymentProcess, on_delete=models.PROTECT, related_name="steps")
    kind = models.CharField("Contrato", max_length=32, choices=ContractInstance.Kind.choices)
    position = models.PositiveSmallIntegerField("Ordem")
    # Populated only by the future receipt reconciliation flow.
    contract = models.OneToOneField(
        ContractInstance, on_delete=models.PROTECT, related_name="deployment_step",
        null=True, blank=True,
    )

    class Meta:
        ordering = ("position",)
        constraints = [
            models.UniqueConstraint(fields=("process", "kind"), name="uniq_deployment_step_kind"),
            models.UniqueConstraint(fields=("process", "position"), name="uniq_deployment_step_position"),
            models.CheckConstraint(condition=Q(position__gte=1) & Q(position__lte=9), name="deployment_step_position_range"),
        ]

    def clean(self) -> None:
        """Prevent linking a deployment from another company, network or kind."""

        super().clean()
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if (previous.process_id != self.process_id or previous.kind != self.kind
                or previous.position != self.position):
                raise ValidationError("O processo, tipo e ordem da etapa não podem ser alterados.")
            if previous.contract_id and previous.contract_id != self.contract_id:
                raise ValidationError("Um contrato confirmado não pode ser substituído.")
        if self.contract_id and self.process_id:
            if (self.contract.company_id != self.process.company_id
                or self.contract.network_id != self.process.network_id
                or self.contract.kind != self.kind):
                raise ValidationError({"contract": "O contrato não corresponde a esta etapa."})
            if not self.pk or not self.attempts.filter(
                transaction__operation_type=DEPLOYMENT_OPERATION_TYPE,
                transaction__status=BlockchainTransaction.Status.CONFIRMED,
                transaction__transaction_hash=self.contract.deployment_transaction_hash,
            ).exists() or not self.contract.deployment_transaction_hash:
                raise ValidationError({"contract": "É necessária uma transação de implantação confirmada."})

    @property
    def linked_deployment_attempt(self) -> DeploymentAttempt | None:
        """Find the attempt whose transaction hash created the linked contract."""

        if self.contract_id is None or not self.contract.deployment_transaction_hash:
            return None
        return next(
            (
                attempt
                for attempt in self.attempts.all()
                if attempt.transaction.transaction_hash
                == self.contract.deployment_transaction_hash
                and attempt.transaction.company_id == self.process.company_id
                and attempt.transaction.network_id == self.process.network_id
            ),
            None,
        )

    @property
    def is_confirmed(self) -> bool:
        """Return whether this linked contract has a currently confirmed attempt."""

        attempt = self.linked_deployment_attempt
        return (
            attempt is not None
            and attempt.transaction.status == BlockchainTransaction.Status.CONFIRMED
        )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate the step and its linked contract before saving."""

        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Show the position and contract name."""

        return f"{self.position}. {self.get_kind_display()}"


class DeploymentAttempt(NonDeletableModel):
    """Preserve each transaction used to attempt a deployment step."""

    # Multiple attempts can be kept if an earlier transaction fails.
    step = models.ForeignKey(DeploymentStep, on_delete=models.PROTECT, related_name="attempts")
    transaction = models.OneToOneField(
        BlockchainTransaction, on_delete=models.PROTECT, related_name="deployment_attempt"
    )
    # Store the exact unsigned creation payload so the RPC result can be checked
    # against what the operator approved in the wallet.
    prepared_data = models.TextField("Prepared contract creation data", blank=True)
    # Persist this before opening MetaMask so an uncertain browser response blocks
    # another send and gives the operator a safe recovery path.
    submission_started_at = models.DateTimeField(
        "Wallet submission started at", null=True, blank=True
    )
    # A database-level slot prevents two unresolved attempts for the same step.
    # It remains open after confirmation until that contract is linked to the step.
    is_open = models.BooleanField("Attempt is open", default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("step",),
                condition=Q(is_open=True),
                name="uniq_open_deployment_attempt_step",
            ),
        ]

    def clean(self) -> None:
        """Require the transaction and step to belong to the same scope."""

        super().clean()
        self.prepared_data = (self.prepared_data or "").strip().lower()
        if self.prepared_data and not re.fullmatch(
            r"0x(?:[0-9a-f]{2})+", self.prepared_data
        ):
            raise ValidationError({"prepared_data": "Enter valid contract creation data."})
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if previous.step_id != self.step_id or previous.transaction_id != self.transaction_id:
                raise ValidationError("Uma tentativa registrada não pode ser alterada.")
            if previous.prepared_data != self.prepared_data:
                raise ValidationError("Os dados preparados de uma tentativa não podem ser alterados.")
            if previous.submission_started_at and (
                previous.submission_started_at != self.submission_started_at
            ):
                raise ValidationError("O início do envio de uma tentativa não pode ser alterado.")
            if not previous.is_open and self.is_open:
                raise ValidationError("Uma tentativa encerrada não pode ser reaberta.")
        if self.step_id and self.transaction_id:
            if self.transaction.operation_type != DEPLOYMENT_OPERATION_TYPE:
                raise ValidationError(
                    {"transaction": "A tentativa precisa ser uma transação de implantação."}
                )
            if (self.transaction.company_id != self.step.process.company_id
                or self.transaction.network_id != self.step.process.network_id):
                raise ValidationError({"transaction": "A transação pertence a outra empresa ou rede."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate the attempt before preserving it."""

        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Identify the step and its transaction."""

        return f"{self.step} — {self.transaction}"


class DeploymentConfigurationAttempt(NonDeletableModel):
    """Preserve one attempt to connect a deployed helper to SojaCoin.

    The related transaction owns the changing network state: sender, hash,
    submission time, receipt and failure reason. This record stores the
    process-specific target and exact call data that the wallet must approve.
    """

    class Kind(models.TextChoices):
        UNIT_REGISTRY = "unit_registry", _("Ligar registro de unidades")
        ISSUANCE_BATCHES = "issuance_batches", _("Ligar lotes de emissão")

    process = models.ForeignKey(
        DeploymentProcess,
        on_delete=models.PROTECT,
        related_name="configuration_attempts",
        verbose_name="Deployment process",
    )
    kind = models.CharField("Configuration", max_length=32, choices=Kind.choices)
    transaction = models.OneToOneField(
        BlockchainTransaction,
        on_delete=models.PROTECT,
        related_name="configuration_attempt",
        verbose_name="Blockchain transaction",
    )
    # The server chooses this target from a confirmed step, never from browser data.
    expected_address = models.CharField("Expected contract address", max_length=42)
    # Save the exact ABI call so the RPC can compare it with the wallet transaction.
    prepared_data = models.TextField("Prepared call data")
    # This slot remains occupied while a wallet send may be unresolved.
    is_open = models.BooleanField("Attempt is open", default=True)
    submission_started_at = models.DateTimeField(
        "Wallet submission started at", null=True, blank=True
    )

    class Meta:
        ordering = ("created_at", "pk")
        verbose_name = "contract configuration attempt"
        verbose_name_plural = "contract configuration attempts"
        constraints = [
            models.UniqueConstraint(
                fields=("process", "kind"),
                condition=Q(is_open=True),
                name="uniq_open_deployment_configuration",
            ),
        ]

    def clean(self) -> None:
        """Validate process scope and keep the signed call immutable."""

        super().clean()
        self.expected_address = normalize_wallet_address(self.expected_address)
        self.prepared_data = (self.prepared_data or "").strip().lower()

        if not is_valid_wallet_address(self.expected_address) or self.expected_address == "0x" + "0" * 40:
            raise ValidationError({"expected_address": "Informe um endereço de contrato válido."})
        if not re.fullmatch(r"0x(?:[0-9a-f]{2}){36}", self.prepared_data):
            raise ValidationError({"prepared_data": "Os dados preparados da chamada são inválidos."})

        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if (
                previous.process_id != self.process_id
                or previous.kind != self.kind
                or previous.transaction_id != self.transaction_id
                or previous.expected_address != self.expected_address
                or previous.prepared_data != self.prepared_data
            ):
                raise ValidationError(
                    "O processo, tipo e dados preparados de uma tentativa não podem ser alterados."
                )
            if previous.submission_started_at and (
                previous.submission_started_at != self.submission_started_at
            ):
                raise ValidationError("O início do envio não pode ser alterado.")
            if not previous.is_open and self.is_open:
                raise ValidationError("Uma tentativa encerrada não pode ser reaberta.")

        if self.process_id and self.transaction_id:
            record = self.transaction
            if record.operation_type != CONTRACT_CONFIGURATION_OPERATION_TYPE:
                raise ValidationError(
                    {"transaction": "A transação precisa ser de configuração de contrato."}
                )
            if (
                record.company_id != self.process.company_id
                or record.network_id != self.process.network_id
            ):
                raise ValidationError(
                    {"transaction": "A transação pertence a outra empresa ou rede."}
                )
            if record.contract_id is None or record.contract.kind != ContractInstance.Kind.SOJA_COIN:
                raise ValidationError(
                    {"transaction": "A configuração precisa chamar o contrato SojaCoin."}
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Validate before preserving the configuration attempt."""

        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def status(self) -> str:
        """Expose transaction lifecycle state on the process configuration row."""

        return self.transaction.status

    def __str__(self) -> str:
        """Identify which process configuration this attempt belongs to."""

        return f"{self.process} — {self.get_kind_display()} — {self.transaction}"


class ProcessedEvent(ImmutableHistoryModel):
    """An event already applied to local state, recorded exactly once."""

    # Network and contract identify the source of the event.
    network = models.ForeignKey(
        Network,
        on_delete=models.PROTECT,
        related_name="processed_events",
        verbose_name="Network",
    )
    contract = models.ForeignKey(
        ContractInstance,
        on_delete=models.PROTECT,
        related_name="processed_events",
        verbose_name="Contract",
    )
    # Transaction hash plus log index is the event's stable identity on a network.
    transaction_hash = models.CharField("Transaction hash", max_length=66)
    log_index = models.PositiveIntegerField("Log index")
    block_number = models.PositiveBigIntegerField("Block number")
    block_hash = models.CharField("Block hash", max_length=66)
    event_name = models.CharField("Event name", max_length=128)
    processed_at = models.DateTimeField("Processed at", default=timezone.now)

    class Meta:
        ordering = ["-processed_at"]
        verbose_name = "processed blockchain event"
        verbose_name_plural = "processed blockchain events"
        constraints = [
            models.UniqueConstraint(
                fields=("network", "transaction_hash", "log_index"),
                name="uniq_processed_event_identity",
            ),
        ]

    def clean(self) -> None:
        """Validate the event source and canonical blockchain references."""

        super().clean()
        self.transaction_hash = normalize_transaction_hash(self.transaction_hash)
        self.block_hash = normalize_transaction_hash(self.block_hash)
        self.event_name = (self.event_name or "").strip()
        if not is_valid_transaction_hash(self.transaction_hash):
            raise ValidationError({"transaction_hash": "Enter a valid transaction hash."})
        if not is_valid_transaction_hash(self.block_hash):
            raise ValidationError({"block_hash": "Enter a valid block hash."})
        if self.contract.network_id != self.network_id:
            raise ValidationError("The event contract must belong to its network.")
        if not self.event_name:
            raise ValidationError({"event_name": "Enter an event name."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Normalize references before the append-only base validates the record."""

        self.transaction_hash = normalize_transaction_hash(self.transaction_hash)
        self.block_hash = normalize_transaction_hash(self.block_hash)
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        """Return the event identity used by the idempotent processor."""

        return f"{self.event_name} — {self.transaction_hash}:{self.log_index}"
