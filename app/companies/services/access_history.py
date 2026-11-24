"""Services that deactivate access records and append their audit entries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone

from ..models import (
    AccessChange,
    CompanyMembership,
    MembershipRole,
    Unit,
    UnitAccess,
)
from ..rules.role_access import (
    require_administrator_access,
    require_role_grant_access,
)


def record_access_change(
    *,
    company_id: int,
    membership: CompanyMembership,
    actor: AbstractBaseUser,
    action: str,
    unit: Unit | None = None,
    previous_value: Mapping[str, object] | None = None,
    new_value: Mapping[str, object] | None = None,
    transaction_hash: str = "",
    occurred_at: datetime | None = None,
) -> AccessChange:
    """Append one company-scoped access change to the immutable history."""

    if membership.company_id != company_id:
        raise ValidationError("The membership does not belong to this company.")
    if unit is not None and unit.company_id != company_id:
        raise ValidationError("The unit does not belong to this company.")

    return AccessChange.objects.create(
        company_id=company_id,
        membership=membership,
        unit=unit,
        actor=actor,
        action=action,
        previous_value=dict(previous_value or {}),
        new_value=dict(new_value or {}),
        transaction=transaction_hash,
        occurred_at=occurred_at or timezone.now(),
    )


def deactivate_membership(
    membership: CompanyMembership,
    actor: AbstractBaseUser,
) -> AccessChange:
    """Deactivate a company membership and record who performed the change."""

    if not membership.active:
        raise ValidationError("The membership is already inactive.")

    with db_transaction.atomic():
        membership.active = False
        membership.save(update_fields=("active", "updated_at"))
        return record_access_change(
            company_id=membership.company_id,
            membership=membership,
            actor=actor,
            action=AccessChange.Action.MEMBERSHIP_REVOKED,
            previous_value={"active": True},
            new_value={"active": False},
        )


def grant_membership_role(
    membership: CompanyMembership,
    role: str,
    actor: AbstractBaseUser,
) -> MembershipRole:
    """Grant one active SaaS function and append its immutable history row."""

    require_role_grant_access(actor, membership)
    if not membership.active:
        raise ValidationError("Não é possível conceder função a um membro inativo.")

    valid_roles = {value for value, _label in MembershipRole.Role.choices}
    if role not in valid_roles:
        raise ValidationError({"role": "Função inválida."})

    if MembershipRole.objects.filter(
        membership=membership,
        role=role,
        active=True,
    ).exists():
        raise ValidationError("Esta função já está ativa para este membro.")

    try:
        with db_transaction.atomic():
            membership_role = MembershipRole.objects.create(
                membership=membership,
                role=role,
                active=True,
                granted_by=actor,
            )
            record_access_change(
                company_id=membership.company_id,
                membership=membership,
                actor=actor,
                action=AccessChange.Action.ROLE_GRANTED,
                new_value={"active": True, "role": role},
            )
    except IntegrityError as error:
        # The conditional unique constraint protects against concurrent grants.
        raise ValidationError(
            "Esta função já está ativa para este membro."
        ) from error

    return membership_role


def revoke_membership_role(
    role: MembershipRole,
    actor: AbstractBaseUser,
) -> AccessChange:
    """Revoke a role without deleting it and append the role change history."""

    if not role.active:
        raise ValidationError("The role is already inactive.")

    revoked_at = timezone.now()
    with db_transaction.atomic():
        role.active = False
        role.revoked_at = revoked_at
        role.save(update_fields=("active", "revoked_at", "updated_at"))
        return record_access_change(
            company_id=role.membership.company_id,
            membership=role.membership,
            actor=actor,
            action=AccessChange.Action.ROLE_REVOKED,
            previous_value={"active": True, "role": role.role},
            new_value={"active": False, "revoked_at": revoked_at.isoformat()},
            occurred_at=revoked_at,
        )


def set_unit_access(
    membership: CompanyMembership,
    unit: Unit,
    actor: AbstractBaseUser,
    *,
    can_view: bool,
    can_receive: bool,
    can_issue: bool,
    can_audit: bool,
) -> UnitAccess | None:
    """Create, update or deactivate one company-scoped unit grant.

    The SaaS grant is intentionally separate from any wallet permission sent
    to the blockchain. When all flags are false, an existing grant is marked
    inactive instead of being deleted.
    """

    if membership.company_id != unit.company_id:
        raise ValidationError("O vínculo e a unidade devem pertencer à mesma empresa.")

    require_administrator_access(actor, membership.company_id)
    if not membership.active:
        raise ValidationError("Não é possível conceder acesso a um membro inativo.")

    values = {
        "can_view": can_view,
        "can_receive": can_receive,
        "can_issue": can_issue,
        "can_audit": can_audit,
    }
    access = UnitAccess.objects.filter(
        membership=membership,
        unit=unit,
    ).first()

    if not any(values.values()):
        if access is None or not access.active:
            return access
        return deactivate_unit_access(access, actor)

    if access is not None and access.active and all(
        getattr(access, field_name) == value
        for field_name, value in values.items()
    ):
        return access

    previous_value = (
        {
            "active": access.active,
            **{
                field_name: getattr(access, field_name)
                for field_name in values
            },
        }
        if access is not None
        else {}
    )

    try:
        with db_transaction.atomic():
            if access is None:
                access = UnitAccess.objects.create(
                    membership=membership,
                    unit=unit,
                    active=True,
                    **values,
                )
            else:
                for field_name, value in values.items():
                    setattr(access, field_name, value)
                access.active = True
                access.save(
                    update_fields=(
                        "can_view",
                        "can_receive",
                        "can_issue",
                        "can_audit",
                        "active",
                        "updated_at",
                    )
                )

            record_access_change(
                company_id=membership.company_id,
                membership=membership,
                unit=unit,
                actor=actor,
                action=AccessChange.Action.UNIT_ACCESS_UPDATED,
                previous_value=previous_value,
                new_value={"active": True, **values},
            )
    except IntegrityError as error:
        raise ValidationError(
            "Já existe um acesso para este membro e esta unidade."
        ) from error

    return access


def deactivate_unit_access(
    access: UnitAccess,
    actor: AbstractBaseUser,
) -> AccessChange:
    """Deactivate a unit grant and preserve its previous permission flags."""

    if not access.active:
        raise ValidationError("The unit access is already inactive.")

    previous_value = {
        "active": access.active,
        "can_view": access.can_view,
        "can_receive": access.can_receive,
        "can_issue": access.can_issue,
        "can_audit": access.can_audit,
    }
    with db_transaction.atomic():
        access.active = False
        access.save(update_fields=("active", "updated_at"))
        return record_access_change(
            company_id=access.unit.company_id,
            membership=access.membership,
            unit=access.unit,
            actor=actor,
            action=AccessChange.Action.UNIT_ACCESS_UPDATED,
            previous_value=previous_value,
            new_value={"active": False},
        )
