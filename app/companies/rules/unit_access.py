"""Unit ownership and unit-level access rules."""

from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from ..models import CompanyMembership, Unit, UnitAccess


UNIT_ACCESS_FIELDS: frozenset[str] = frozenset(
    {"can_view", "can_receive", "can_issue", "can_audit"}
)


def get_unit_for_membership(
    membership: CompanyMembership,
    unit_id: int,
) -> Unit:
    """Return a unit owned by the membership's company or raise 404."""

    return get_object_or_404(
        Unit,
        pk=unit_id,
        company_id=membership.company_id,
    )


def require_active_unit(unit: Unit) -> Unit:
    """Reject new operations for a deactivated unit."""

    if not unit.active:
        raise PermissionDenied("This unit is inactive.")

    return unit


def require_unit_access(
    membership: CompanyMembership,
    unit_id: int,
    access_field: str,
    *,
    for_operation: bool = False,
) -> tuple[Unit, UnitAccess]:
    """Return a unit grant after checking company, membership and unit state."""

    if access_field not in UNIT_ACCESS_FIELDS:
        raise ValueError(f"Unknown unit access field: {access_field}")

    unit = get_unit_for_membership(membership, unit_id)
    if for_operation:
        require_active_unit(unit)

    grant = UnitAccess.objects.filter(
        membership=membership,
        unit=unit,
        active=True,
    ).first()
    if grant is None or not getattr(grant, access_field):
        raise PermissionDenied("The user is not authorized for this unit.")

    return unit, grant
