"""Services for creating company staff memberships."""

from __future__ import annotations

from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from accounts.rules.account_access import is_active_account

from ..models import AccessChange, Company, CompanyMembership
from .access_history import record_access_change


def add_company_membership(
    *,
    company: Company,
    user: AbstractBaseUser,
    actor: AbstractBaseUser,
) -> CompanyMembership:
    """Add an existing active account to a company and record the change.

    Authorization is performed by ``CompanyAdministratorRequiredMixin`` in
    the view. This service still validates the target account and duplicate
    constraint so it is safe to reuse from another authorized entry point.
    """

    if not company.active:
        raise ValidationError(
            "Não é possível adicionar membros a uma empresa inativa."
        )
    if not is_active_account(user):
        raise ValidationError("A conta selecionada está inativa.")

    if CompanyMembership.objects.filter(company=company, user=user).exists():
        raise ValidationError("Este usuário já está vinculado a esta empresa.")

    try:
        with transaction.atomic():
            membership = CompanyMembership.objects.create(
                company=company,
                user=user,
                active=True,
            )
            record_access_change(
                company_id=company.pk,
                membership=membership,
                actor=actor,
                action=AccessChange.Action.MEMBERSHIP_GRANTED,
                new_value={"active": True, "user_id": user.pk},
            )
    except IntegrityError as error:
        # The database constraint is the final protection against a race
        # between two administrators submitting the same user.
        raise ValidationError(
            "Este usuário já está vinculado a esta empresa."
        ) from error

    return membership
