"""Prepare and reconcile unit-specific wallet permissions on UnitRegistry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import TransactionNotFound, Web3Exception

from accounts.models import UserWallet
from accounts.rules.account_access import is_active_account
from accounts.rules.wallet_access import require_verified_wallet
from blockchain.models import (
    BlockchainTransaction,
    ContractInstance,
    ProcessedEvent,
    UNIT_REGISTRATION_OPERATION_TYPE,
    UNIT_WALLET_PERMISSION_OPERATION_TYPE,
    UnitRegistrationAttempt,
    UnitWalletPermissionAttempt,
)
from blockchain.services.contracts import bind_contract_to_web3
from blockchain.services.rpc import connect_to_rpc
from blockchain.services.transaction_confirmation import (
    DecodedContractEvent,
    confirm_transaction_from_chain,
)
from blockchain.services.transactions import fail_transaction, submit_transaction
from utils.identifiers import (
    normalize_transaction_hash,
    normalize_wallet_address,
    is_valid_transaction_hash,
)

from ..models import (
    AccessChange,
    CompanyMembership,
    MembershipRole,
    Unit,
    UnitAccess,
    UnitWalletPermission,
)
from ..rules.role_access import require_administrator_access
from .access_history import record_access_change


PermissionKind = Literal["receive", "issue"]
PermissionAction = Literal["grant", "revoke"]


class WalletPermissionCandidate(TypedDict):
    """Describe one verified member wallet available for a permission grant."""

    membership_id: int
    membership_label: str
    user_wallet_id: int
    wallet_address: str
    wallet_label: str

RECEIVE_PERMISSION = "receive"
ISSUE_PERMISSION = "issue"


@dataclass(frozen=True)
class PreparedWalletPermission:
    """Return the saved call details the connected owner wallet must send."""

    status: Literal["ready", "recover", "submitted"]
    attempt: UnitWalletPermissionAttempt
    chain_id: int
    owner_address: str
    contract_address: str
    transaction_data: str


@dataclass(frozen=True)
class WalletPermissionConfirmation:
    """Describe the network result for one saved permission attempt."""

    status: Literal["pending", "failed", "confirmed"]
    attempt: UnitWalletPermissionAttempt
    transaction: BlockchainTransaction


def _require_member_wallet_for_grant(
    *,
    membership: CompanyMembership,
    unit: Unit,
    user_wallet: UserWallet,
    permission_kind: PermissionKind,
) -> None:
    """Require the member, SaaS role, unit access and wallet to remain valid."""

    if not membership.active:
        raise ValidationError("A pessoa precisa ter um vínculo ativo com a empresa.")
    if not is_active_account(membership.user):
        raise ValidationError("A conta da pessoa selecionada está inativa.")

    required_role = (
        MembershipRole.Role.RECEIVING
        if permission_kind == RECEIVE_PERMISSION
        else MembershipRole.Role.ISSUANCE
    )
    if not membership.roles.filter(role=required_role, active=True).exists():
        raise ValidationError("A pessoa precisa ter a função correspondente no SaaS.")

    required_access = "can_receive" if permission_kind == RECEIVE_PERMISSION else "can_issue"
    if not UnitAccess.objects.filter(
        membership=membership,
        unit=unit,
        active=True,
        **{required_access: True},
    ).exists():
        raise ValidationError(
            "Conceda primeiro o acesso correspondente a esta unidade no SaaS."
        )

    try:
        require_verified_wallet(user_wallet)
    except PermissionDenied as error:
        raise ValidationError(
            "A pessoa precisa ter uma carteira ativa e verificada no SaaS."
        ) from error


def _permission_bit(contract: Contract, permission_kind: PermissionKind) -> int:
    """Read the Solidity permission bit instead of duplicating its numeric value."""

    if permission_kind == RECEIVE_PERMISSION:
        return int(contract.functions.PERMISSION_RECEIVE_SOYBEANS().call())
    if permission_kind == ISSUE_PERMISSION:
        return int(contract.functions.PERMISSION_ISSUE_SJC().call())
    raise ValidationError("Escolha recebimento ou emissão.")


def _get_member_wallet(
    *,
    company_id: int,
    unit: Unit,
    membership_id: int,
    user_wallet_id: int,
    permission_kind: PermissionKind,
    action: PermissionAction,
) -> tuple[CompanyMembership, UserWallet]:
    """Resolve the target person and wallet, requiring active checks for grants."""

    try:
        membership = CompanyMembership.objects.select_related("user", "company").get(
            pk=membership_id,
            company_id=company_id,
        )
    except CompanyMembership.DoesNotExist as error:
        raise ValidationError(
            "A pessoa selecionada não pertence à equipe ativa da empresa."
        ) from error

    try:
        user_wallet = UserWallet.objects.select_related("user", "wallet").get(
            pk=user_wallet_id,
            user_id=membership.user_id,
        )
    except UserWallet.DoesNotExist as error:
        raise ValidationError("A carteira não pertence à pessoa selecionada.") from error

    if action == "grant":
        _require_member_wallet_for_grant(
            membership=membership,
            unit=unit,
            user_wallet=user_wallet,
            permission_kind=permission_kind,
        )

    return membership, user_wallet


def get_unit_wallet_permission_candidates(
    unit: Unit,
    permission_kind: PermissionKind,
) -> list[WalletPermissionCandidate]:
    """List verified wallets of active members already authorized in the SaaS."""

    role = (
        MembershipRole.Role.RECEIVING
        if permission_kind == RECEIVE_PERMISSION
        else MembershipRole.Role.ISSUANCE
    )
    access_field = "can_receive" if permission_kind == RECEIVE_PERMISSION else "can_issue"
    memberships = (
        CompanyMembership.objects.filter(
            company_id=unit.company_id,
            active=True,
            roles__role=role,
            roles__active=True,
            unit_accesses__unit=unit,
            unit_accesses__active=True,
            **{f"unit_accesses__{access_field}": True},
        )
        .select_related("user")
        .distinct()
        .order_by("user__last_name", "user__first_name", "user__username")
    )

    candidates: list[WalletPermissionCandidate] = []
    for membership in memberships:
        if not is_active_account(membership.user):
            continue
        wallet_links = (
            UserWallet.objects.filter(
                user_id=membership.user_id,
                verification_status=UserWallet.VerificationStatus.VERIFIED,
                wallet__active=True,
            )
            .select_related("wallet")
            .order_by("wallet__label", "wallet__address_normalized")
        )
        for user_wallet in wallet_links:
            candidates.append(
                {
                    "membership_id": membership.pk,
                    "membership_label": str(membership),
                    "user_wallet_id": user_wallet.pk,
                    "wallet_address": user_wallet.wallet.address_normalized,
                    "wallet_label": user_wallet.wallet.label,
                }
            )

    return candidates


def _get_confirmed_unit_registration(
    *,
    company_id: int,
    unit: Unit,
) -> UnitRegistrationAttempt:
    """Find the confirmed UnitRegistry and network that assigned this unit ID."""

    if not unit.active or not unit.company.active:
        raise ValidationError("A empresa e a unidade precisam estar ativas.")
    if unit.chain_unit_id is None:
        raise ValidationError("Registre a unidade na blockchain antes de configurar carteiras.")

    attempt = (
        UnitRegistrationAttempt.objects.select_related(
            "transaction__network",
            "transaction__contract",
        )
        .filter(
            unit=unit,
            transaction__company_id=company_id,
            transaction__operation_type=UNIT_REGISTRATION_OPERATION_TYPE,
            transaction__status=BlockchainTransaction.Status.CONFIRMED,
            transaction__contract__kind=ContractInstance.Kind.UNIT_REGISTRY,
        )
        .order_by("-transaction__confirmed_at", "-pk")
        .first()
    )
    if attempt is None or not attempt.transaction.transaction_hash:
        raise ValidationError("Não foi encontrado um registro confirmado para esta unidade.")

    transaction_record = attempt.transaction
    contract = transaction_record.contract
    if (
        contract.company_id != company_id
        or contract.network_id != transaction_record.network_id
        or not contract.active
        or not transaction_record.network.active
    ):
        raise ValidationError("O UnitRegistry ou a rede do registro não estão ativos.")

    event_exists = ProcessedEvent.objects.filter(
        network_id=transaction_record.network_id,
        contract_id=contract.pk,
        transaction_hash=transaction_record.transaction_hash,
        event_name="UnitRegistered",
    ).exists()
    if not event_exists:
        raise ValidationError("O registro da unidade não tem evento de confirmação válido.")
    if unit.code.strip().upper() != attempt.unit_code:
        raise ValidationError("O código local mudou depois do registro na blockchain.")
    if unit.registration_hash.lower() != attempt.registration_hash:
        raise ValidationError("Os dados locais mudaram depois do registro na blockchain.")

    return attempt


def _load_registry_contract(
    registration: UnitRegistrationAttempt,
    *,
    unit: Unit,
    wallet_address: str,
) -> tuple[Contract, str]:
    """Connect to the registered contract and verify the unit and its owner."""

    transaction_record = registration.transaction
    web3 = connect_to_rpc(transaction_record.network)
    registry = transaction_record.contract
    try:
        contract_address = Web3.to_checksum_address(registry.address_normalized)
        if not web3.eth.get_code(contract_address):
            raise ValidationError("Não há código do UnitRegistry no endereço cadastrado.")
        contract = bind_contract_to_web3(web3, registry)
        owner_address = normalize_wallet_address(str(contract.functions.owner().call()))
        registered_unit = contract.functions.units(unit.chain_unit_id).call()
    except ValidationError:
        raise
    except Web3Exception as error:
        raise ConnectionError("Não foi possível consultar a unidade no RPC.") from error

    expected_code = registration.unit_code.encode("utf-8").ljust(32, b"\x00")
    expected_hash = bytes.fromhex(registration.registration_hash[2:])
    if (
        bytes(registered_unit[0]) != expected_code
        or bytes(registered_unit[1]) != expected_hash
        or not bool(registered_unit[5])
    ):
        raise ValidationError(
            "O UnitRegistry não confirma os mesmos dados da unidade local ativa."
        )
    if owner_address != wallet_address:
        raise ValidationError("Conecte a carteira dona do UnitRegistry para continuar.")

    return contract, owner_address


def prepare_unit_wallet_permission(
    *,
    company_id: int,
    unit_id: int,
    membership_id: int,
    user_wallet_id: int,
    permission_kind: PermissionKind,
    action: PermissionAction,
    owner_address: str,
    wallet_chain_id: int,
    actor: AbstractBaseUser,
) -> PreparedWalletPermission:
    """Prepare one grant or revocation without sending it to the blockchain."""

    admin_membership = require_administrator_access(actor, company_id)
    if not is_active_account(actor):
        raise ValidationError("A conta do administrador precisa estar ativa.")
    if permission_kind not in {RECEIVE_PERMISSION, ISSUE_PERMISSION}:
        raise ValidationError("Escolha recebimento ou emissão.")
    if action not in {"grant", "revoke"}:
        raise ValidationError("Ação de permissão inválida.")
    if not owner_address or not Web3.is_address(owner_address):
        raise ValidationError("Conecte a carteira pública dona do contrato.")

    try:
        unit = Unit.objects.select_related("company").get(
            pk=unit_id,
            company_id=company_id,
        )
    except Unit.DoesNotExist as error:
        raise ValidationError("A unidade não pertence à empresa selecionada.") from error

    membership, user_wallet = _get_member_wallet(
        company_id=company_id,
        unit=unit,
        membership_id=membership_id,
        user_wallet_id=user_wallet_id,
        permission_kind=permission_kind,
        action=action,
    )
    target_wallet = user_wallet.wallet.address_normalized
    registration = _get_confirmed_unit_registration(company_id=company_id, unit=unit)
    if wallet_chain_id != registration.transaction.network.chain_id:
        raise ValidationError(
            f"A carteira está na rede {wallet_chain_id}; troque para "
            f"{registration.transaction.network.chain_id} e tente novamente."
        )
    try:
        contract, current_owner = _load_registry_contract(
            registration,
            unit=unit,
            wallet_address=normalize_wallet_address(owner_address),
        )
        unit_id_on_chain = int(unit.chain_unit_id or 0)
        general_mask = int(
            contract.functions.permissionsGeneral(
                Web3.to_checksum_address(target_wallet)
            ).call()
        )
        current_mask = int(
            contract.functions.permissionsByUnit(
                unit_id_on_chain,
                Web3.to_checksum_address(target_wallet),
            ).call()
        )
        current_auditor = normalize_wallet_address(
            str(contract.functions.auditorByUnit(unit_id_on_chain).call())
        )
        bit = _permission_bit(contract, permission_kind)
        receive_bit = _permission_bit(contract, RECEIVE_PERMISSION)
        issue_bit = _permission_bit(contract, ISSUE_PERMISSION)
    except Web3Exception as error:
        raise ConnectionError("Não foi possível consultar as permissões no RPC.") from error

    effective_mask = general_mask | current_mask
    target_is_active = bool(effective_mask & bit)
    if action == "grant":
        if target_is_active:
            raise ValidationError("Esta carteira já tem essa permissão na unidade ou na empresa.")
        other_bit = issue_bit if permission_kind == RECEIVE_PERMISSION else receive_bit
        if effective_mask & other_bit:
            raise ValidationError(
                "A carteira já tem a permissão da outra categoria. Use carteiras diferentes."
            )
        if current_auditor == target_wallet:
            raise ValidationError("A carteira designada como auditora não pode operar o estoque.")
        desired_mask = current_mask | bit
    else:
        if general_mask & bit:
            raise ValidationError(
                "Essa permissão vem do acesso geral da empresa e não pode ser retirada "
                "só desta unidade."
            )
        if not current_mask & bit:
            raise ValidationError("Esta carteira não tem essa permissão ativa nesta unidade.")
        desired_mask = current_mask & ~bit

    registry_record = registration.transaction.contract
    existing_open = UnitWalletPermissionAttempt.objects.filter(
        unit=unit,
        wallet_address=target_wallet,
        is_open=True,
    ).select_related("transaction", "membership", "user_wallet__wallet").first()
    if existing_open:
        if (
            existing_open.permission_kind != permission_kind
            or existing_open.action != action
            or existing_open.membership_id != membership.pk
            or existing_open.user_wallet_id != user_wallet.pk
        ):
            raise ValidationError(
                "Esta carteira já tem uma alteração aguardando conclusão. Resolva-a primeiro."
            )
        if existing_open.transaction.status == BlockchainTransaction.Status.SUBMITTED:
            status: Literal["ready", "recover", "submitted"] = "submitted"
        elif existing_open.submission_started_at is None:
            status = "ready"
        else:
            status = "recover"
        return PreparedWalletPermission(
            status=status,
            attempt=existing_open,
            chain_id=existing_open.transaction.network.chain_id,
            owner_address=existing_open.transaction.sender_address,
            contract_address=existing_open.transaction.contract.address_normalized,
            transaction_data=existing_open.prepared_data,
        )

    try:
        prepared_data = contract.functions.setPermissions(
            unit_id_on_chain,
            Web3.to_checksum_address(target_wallet),
            desired_mask,
        )._encode_transaction_data()
    except (AttributeError, TypeError, ValueError) as error:
        raise ValidationError("Não foi possível preparar a chamada setPermissions.") from error

    transaction_record = BlockchainTransaction(
        company_id=company_id,
        network=registration.transaction.network,
        contract=registry_record,
        operation_type=UNIT_WALLET_PERMISSION_OPERATION_TYPE,
        sender_address=current_owner,
        status=BlockchainTransaction.Status.PREPARED,
    )

    try:
        with db_transaction.atomic():
            locked_unit = Unit.objects.select_for_update().get(
                pk=unit.pk,
                company_id=company_id,
                active=True,
                company__active=True,
            )
            if UnitWalletPermissionAttempt.objects.filter(
                unit=locked_unit,
                wallet_address=target_wallet,
                is_open=True,
            ).exists():
                raise ValidationError(
                    "Esta carteira já tem uma alteração aguardando conclusão. Recarregue a página."
                )

            # Save the transaction and exact call together before opening MetaMask.
            transaction_record.save()
            attempt = UnitWalletPermissionAttempt(
                unit=locked_unit,
                membership=membership,
                user_wallet=user_wallet,
                requested_by=admin_membership.user,
                transaction=transaction_record,
                wallet_address=target_wallet,
                permission_kind=permission_kind,
                action=action,
                permission_bit=bit,
                previous_permission_mask=current_mask,
                permission_mask=desired_mask,
                prepared_data=prepared_data,
            )
            attempt.save()
    except Unit.DoesNotExist as error:
        raise ValidationError(
            "A empresa ou unidade foi desativada. Recarregue a página."
        ) from error
    except IntegrityError as error:
        raise ValidationError(
            "Já existe uma alteração aberta para esta carteira. Recarregue a página."
        ) from error

    return PreparedWalletPermission(
        status="ready",
        attempt=attempt,
        chain_id=registration.transaction.network.chain_id,
        owner_address=current_owner,
        contract_address=registry_record.address_normalized,
        transaction_data=prepared_data,
    )


def mark_wallet_permission_submission_started(
    attempt_id: int,
) -> UnitWalletPermissionAttempt:
    """Persist that the owner wallet prompt is about to open."""

    try:
        with db_transaction.atomic():
            attempt = UnitWalletPermissionAttempt.objects.select_for_update().select_related(
                "unit__company",
                "membership__user",
                "user_wallet__wallet",
                "transaction__network",
                "transaction__contract",
            ).get(pk=attempt_id)
            transaction_record = BlockchainTransaction.objects.select_for_update().get(
                pk=attempt.transaction_id
            )
            if not attempt.is_open:
                raise ValidationError("Esta tentativa já foi encerrada.")
            if transaction_record.status == BlockchainTransaction.Status.SUBMITTED:
                return attempt
            if transaction_record.status != BlockchainTransaction.Status.PREPARED:
                raise ValidationError("Esta tentativa não pode abrir a carteira.")
            if (
                not attempt.unit.active
                or not attempt.unit.company.active
                or not attempt.transaction.network.active
                or not attempt.transaction.contract.active
            ):
                raise ValidationError(
                    "A empresa, unidade, rede e UnitRegistry precisam estar ativos."
                )
            if attempt.user_wallet.wallet.address_normalized != attempt.wallet_address:
                raise ValidationError("A carteira vinculada à tentativa foi alterada.")
            if attempt.action == UnitWalletPermissionAttempt.Action.GRANT:
                _require_member_wallet_for_grant(
                    membership=attempt.membership,
                    unit=attempt.unit,
                    user_wallet=attempt.user_wallet,
                    permission_kind=cast(PermissionKind, attempt.permission_kind),
                )
            if attempt.submission_started_at is None:
                attempt.submission_started_at = timezone.now()
                attempt.save(update_fields=("submission_started_at", "updated_at"))
            return attempt
    except UnitWalletPermissionAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de permissão não encontrada.") from error


def reject_wallet_permission_attempt(
    attempt_id: int,
) -> UnitWalletPermissionAttempt:
    """Close an attempt only after the wallet explicitly rejects the request."""

    try:
        attempt = UnitWalletPermissionAttempt.objects.select_related("transaction").get(
            pk=attempt_id
        )
    except UnitWalletPermissionAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de permissão não encontrada.") from error
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("Esta tentativa não está aguardando resposta da carteira.")
    if attempt.transaction.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Uma transação enviada não pode ser marcada como recusada.")

    fail_transaction(attempt.transaction, "A carteira dona do UnitRegistry recusou a chamada.")
    attempt.refresh_from_db()
    return attempt


def register_wallet_permission_transaction_hash(
    *,
    attempt_id: int,
    transaction_hash: str,
) -> BlockchainTransaction | None:
    """Validate the RPC transaction against the saved owner call and submit it."""

    canonical_hash = normalize_transaction_hash(transaction_hash)
    if not is_valid_transaction_hash(canonical_hash):
        raise ValidationError("A hash informada pela carteira é inválida.")
    try:
        attempt = UnitWalletPermissionAttempt.objects.select_related(
            "transaction__network",
            "transaction__contract",
        ).get(pk=attempt_id)
    except UnitWalletPermissionAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de permissão não encontrada.") from error

    record = attempt.transaction
    if not attempt.is_open or attempt.submission_started_at is None:
        raise ValidationError("Esta tentativa não está aguardando o resultado da carteira.")
    if record.transaction_hash:
        if record.transaction_hash == canonical_hash:
            return record
        raise ValidationError("Esta tentativa já está ligada a outra hash.")
    if record.status != BlockchainTransaction.Status.PREPARED:
        raise ValidationError("Somente uma tentativa preparada pode receber uma hash.")

    web3 = connect_to_rpc(record.network)
    try:
        chain_transaction = web3.eth.get_transaction(canonical_hash)
    except TransactionNotFound:
        return None
    except Web3Exception as error:
        raise ConnectionError("Não foi possível consultar a transação no RPC.") from error

    found_hash = normalize_transaction_hash(Web3.to_hex(chain_transaction["hash"]))
    if found_hash != canonical_hash:
        raise ValidationError("O RPC retornou uma transação com outra hash.")
    if normalize_wallet_address(str(chain_transaction["from"])) != record.sender_address:
        raise ValidationError(
            "A transação foi enviada por uma carteira diferente da dona do contrato."
        )
    if normalize_wallet_address(str(chain_transaction.get("to") or "")) != (
        record.contract.address_normalized
    ):
        raise ValidationError("A transação chama um contrato diferente do UnitRegistry esperado.")
    if (
        chain_transaction.get("chainId") is None
        or int(chain_transaction["chainId"]) != record.network.chain_id
    ):
        raise ValidationError("A transação pertence a uma rede diferente da tentativa.")
    if int(chain_transaction.get("value", 0)) != 0:
        raise ValidationError("A alteração de permissão não pode transferir saldo nativo.")

    raw_data = chain_transaction.get("input", chain_transaction.get("data"))
    if isinstance(raw_data, (bytes, bytearray)):
        actual_data = Web3.to_hex(raw_data).lower()
    elif isinstance(raw_data, str):
        actual_data = raw_data.lower()
    else:
        raise ValidationError("O RPC retornou dados de chamada inválidos.")
    if actual_data != attempt.prepared_data:
        raise ValidationError("A chamada enviada difere dos dados preparados pelo servidor.")

    return submit_transaction(record, canonical_hash)


def handle_permissions_set_event(
    transaction_record: BlockchainTransaction,
    event: DecodedContractEvent,
) -> None:
    """Match PermissionsSet to the saved request and update local state once."""

    if event.event_name != "PermissionsSet":
        raise ValidationError("O evento não corresponde a uma alteração de permissão.")
    try:
        attempt = UnitWalletPermissionAttempt.objects.select_related(
            "unit__company",
            "membership",
            "requested_by",
            "transaction",
        ).get(transaction_id=transaction_record.pk)
        event_unit_id = int(event.args["unitId"])
        event_wallet = normalize_wallet_address(str(event.args["wallet"]))
        event_mask = int(event.args["permissions"])
        event_owner = normalize_wallet_address(str(event.args["definidasBy"]))
    except (UnitWalletPermissionAttempt.DoesNotExist, KeyError, TypeError, ValueError) as error:
        raise ValidationError(
            "O evento PermissionsSet está incompleto ou sem tentativa."
        ) from error

    if (
        event_unit_id != attempt.unit.chain_unit_id
        or event_wallet != attempt.wallet_address
        or event_mask != int(attempt.permission_mask)
        or event_owner != transaction_record.sender_address
    ):
        raise ValidationError("O evento PermissionsSet não corresponde à alteração preparada.")
    if transaction_record.contract_id != attempt.transaction.contract_id:
        raise ValidationError("O evento veio de um contrato diferente do UnitRegistry esperado.")

    permission_record, _ = UnitWalletPermission.objects.select_for_update().get_or_create(
        unit=attempt.unit,
        wallet=attempt.wallet_address,
        defaults={
            "membership": attempt.membership,
            "permission_mask": attempt.permission_mask,
            "transaction": transaction_record.transaction_hash,
            "sync_status": (
                UnitWalletPermission.SyncStatus.REVOKED
                if int(attempt.permission_mask) == 0
                else UnitWalletPermission.SyncStatus.CONFIRMED
            ),
        },
    )
    previous_value = {
        "wallet": attempt.wallet_address,
        "permission_mask": int(attempt.previous_permission_mask),
        "permission": attempt.get_permission_kind_display(),
        "active": bool(
            int(attempt.previous_permission_mask) & int(attempt.permission_bit)
        ),
    }
    permission_record.membership = attempt.membership
    permission_record.permission_mask = attempt.permission_mask
    permission_record.transaction = transaction_record.transaction_hash
    permission_record.sync_status = (
        UnitWalletPermission.SyncStatus.REVOKED
        if int(attempt.permission_mask) == 0
        else UnitWalletPermission.SyncStatus.CONFIRMED
    )
    permission_record.save()

    record_access_change(
        company_id=attempt.unit.company_id,
        membership=attempt.membership,
        actor=attempt.requested_by,
        unit=attempt.unit,
        action=AccessChange.Action.WALLET_PERMISSION_UPDATED,
        previous_value=previous_value,
        new_value={
            "wallet": attempt.wallet_address,
            "permission_mask": int(attempt.permission_mask),
            "permission": attempt.get_permission_kind_display(),
            "active": bool(
                int(attempt.permission_mask) & int(attempt.permission_bit)
            ),
            "person": str(attempt.membership),
            "sync_status": permission_record.sync_status,
            "action": attempt.action,
        },
        transaction_hash=transaction_record.transaction_hash or "",
        occurred_at=timezone.now(),
    )

    if attempt.is_open:
        attempt.is_open = False
        attempt.save(update_fields=("is_open", "updated_at"))


def confirm_wallet_permission_attempt(
    attempt_id: int,
) -> WalletPermissionConfirmation:
    """Confirm one PermissionsSet receipt and apply its local snapshot atomically."""

    try:
        attempt = UnitWalletPermissionAttempt.objects.select_related(
            "transaction__network",
            "transaction__contract",
        ).get(pk=attempt_id)
    except UnitWalletPermissionAttempt.DoesNotExist as error:
        raise ValidationError("Tentativa de permissão não encontrada.") from error

    record = attempt.transaction
    if record.status == BlockchainTransaction.Status.FAILED:
        return WalletPermissionConfirmation("failed", attempt, record)
    if record.status not in {
        BlockchainTransaction.Status.SUBMITTED,
        BlockchainTransaction.Status.CONFIRMED,
    }:
        raise ValidationError("A carteira ainda não enviou uma transação para consultar.")

    try:
        confirmed_record = confirm_transaction_from_chain(
            record.pk,
            event_handler=handle_permissions_set_event,
            required_event_names=("PermissionsSet",),
        )
    except Web3Exception as error:
        raise ConnectionError("Não foi possível consultar o recibo da transação no RPC.") from error

    attempt.refresh_from_db()
    transaction_result = confirmed_record or record
    if confirmed_record is None:
        status: Literal["pending", "failed", "confirmed"] = "pending"
    elif confirmed_record.status == BlockchainTransaction.Status.FAILED:
        status = "failed"
    elif confirmed_record.status == BlockchainTransaction.Status.CONFIRMED:
        status = "confirmed"
    else:
        raise ValidationError("O RPC retornou uma situação de transação inesperada.")

    return WalletPermissionConfirmation(status, attempt, transaction_result)
