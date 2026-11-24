"""Role and unit-permission rules for company operations."""

from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import PermissionDenied

from .company_access import (
    get_membership_for_user,
    require_active_company,
)
from .unit_access import require_unit_access
from ..models import CompanyMembership, MembershipRole, Unit, UnitAccess


class CompanyAction:
    """Names of company actions that require an active role."""

    MANAGE_TEAM = "manage_team"
    MANAGE_PERMISSIONS = "manage_permissions"
    PREPARE_PARTICIPANT = "prepare_participant"
    PREPARE_CPR = "prepare_cpr"
    RECEIVE_LOAD = "receive_load"
    CREATE_ISSUANCE = "create_issuance"
    RECORD_AUDIT = "record_audit"


ACTION_ROLES: dict[str, tuple[str, ...]] = {
    CompanyAction.MANAGE_TEAM: (MembershipRole.Role.ADMINISTRATOR,),
    CompanyAction.MANAGE_PERMISSIONS: (MembershipRole.Role.ADMINISTRATOR,),
    CompanyAction.PREPARE_PARTICIPANT: (
        MembershipRole.Role.ADMINISTRATOR,
        MembershipRole.Role.REGISTRATION,
    ),
    CompanyAction.PREPARE_CPR: (
        MembershipRole.Role.ADMINISTRATOR,
        MembershipRole.Role.REGISTRATION,
    ),
    CompanyAction.RECEIVE_LOAD: (MembershipRole.Role.RECEIVING,),
    CompanyAction.CREATE_ISSUANCE: (MembershipRole.Role.ISSUANCE,),
    CompanyAction.RECORD_AUDIT: (MembershipRole.Role.AUDITOR,),
}


def require_company_action(
    user: AbstractBaseUser | None,
    company_id: int,
    action: str,
) -> CompanyMembership:
    """Return a membership when the user has the role for an operation."""

    allowed_roles = ACTION_ROLES.get(action)
    if allowed_roles is None:
        raise ValueError(f"Unknown company action: {action}")

    membership = get_membership_for_user(user, company_id)
    has_role = membership.roles.filter(role__in=allowed_roles, active=True).exists()
    if not has_role:
        raise PermissionDenied("The user does not have this company function.")

    # Every action in this map starts or changes an operation, so inactive
    # companies cannot execute it.
    require_active_company(membership)
    return membership


def require_administrator_access(
    user: AbstractBaseUser | None,
    company_id: int,
) -> CompanyMembership:
    """Authorize team and company-permission management."""

    return require_company_action(user, company_id, CompanyAction.MANAGE_TEAM)


def require_registration_access(
    user: AbstractBaseUser | None,
    company_id: int,
) -> CompanyMembership:
    """Authorize participant and CPR preparation."""

    return require_company_action(user, company_id, CompanyAction.PREPARE_CPR)


def require_receiving_access(
    user: AbstractBaseUser | None,
    company_id: int,
    unit_id: int,
) -> tuple[CompanyMembership, Unit, UnitAccess]:
    """Authorize receiving work for one unit."""

    return _require_unit_action(
        user,
        company_id,
        unit_id,
        CompanyAction.RECEIVE_LOAD,
        "can_receive",
    )


def require_issuance_access(
    user: AbstractBaseUser | None,
    company_id: int,
    unit_id: int,
) -> tuple[CompanyMembership, Unit, UnitAccess]:
    """Authorize issuance work for one unit."""

    return _require_unit_action(
        user,
        company_id,
        unit_id,
        CompanyAction.CREATE_ISSUANCE,
        "can_issue",
    )


def require_auditor_access(
    user: AbstractBaseUser | None,
    company_id: int,
    unit_id: int,
) -> tuple[CompanyMembership, Unit, UnitAccess]:
    """Authorize audit work for one designated unit."""

    return _require_unit_action(
        user,
        company_id,
        unit_id,
        CompanyAction.RECORD_AUDIT,
        "can_audit",
    )


def require_role_grant_access(
    actor: AbstractBaseUser,
    target_membership: CompanyMembership,
) -> CompanyMembership:
    """Authorize an administrator to grant a role to another user."""

    if actor.pk == target_membership.user_id:
        raise PermissionDenied("A user cannot grant a function to themselves.")

    return require_administrator_access(actor, target_membership.company_id)


def _require_unit_action(
    user: AbstractBaseUser | None,
    company_id: int,
    unit_id: int,
    action: str,
    access_field: str,
) -> tuple[CompanyMembership, Unit, UnitAccess]:
    """Apply company role and unit grant checks for an operational action."""

    membership = require_company_action(user, company_id, action)
    unit, grant = require_unit_access(
        membership,
        unit_id,
        access_field,
        for_operation=True,
    )
    return membership, unit, grant
