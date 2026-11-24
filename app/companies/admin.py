from django.contrib import admin
from django.forms import ModelForm
from django.http import HttpRequest

from .forms import UnitForm
from .models import (
    AccessChange,
    Company,
    CompanyMembership,
    MembershipRole,
    Unit,
    UnitAccess,
    UnitWalletPermission,
)
from .services.access_history import record_access_change
from .services.unit_registration import save_unit_registration


class CompanyUnitInlineForm(UnitForm):
    """Save inline units through the same local registration service.

    The inline receives its ``company`` from the parent CompanyAdmin form. The
    hash and blockchain-controlled fields are therefore handled by the
    application instead of by submitted admin values.
    """

    def save(self, commit: bool = True) -> Unit:
        """Persist the unit and calculate its deterministic registration hash."""

        if not commit:
            return super().save(commit=False)
        return save_unit_registration(self.instance, self.cleaned_data)


class CompanyUnitInline(admin.TabularInline):
    """Allow units to be created directly while editing their company."""

    model = Unit
    form = CompanyUnitInlineForm
    fields = ("name", "code", "address", "cnpj", "state_registration")
    extra = 1
    show_change_link = True


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("legal_name", "trade_name", "cnpj", "active", "created_at")
    list_filter = ("active",)
    search_fields = ("legal_name", "trade_name", "cnpj")
    readonly_fields = ("created_at", "updated_at")
    inlines = (CompanyUnitInline,)


@admin.register(CompanyMembership)
class CompanyMembershipAdmin(admin.ModelAdmin):
    list_display = ("company", "user", "active", "created_at", "updated_at")
    list_filter = ("active", "company")
    search_fields = (
        "company__legal_name",
        "company__trade_name",
        "user__username",
        "user__email",
    )
    readonly_fields = ("created_at", "updated_at")

    def get_readonly_fields(
        self,
        request: HttpRequest,
        obj: CompanyMembership | None = None,
    ) -> tuple[str, ...]:
        """Keep a membership's company and user identity immutable after creation."""

        fields = ("created_at", "updated_at")
        if obj is not None:
            return (*fields, "company", "user")
        return fields

    def save_model(
        self,
        request: HttpRequest,
        obj: CompanyMembership,
        form: ModelForm,
        change: bool,
    ) -> None:
        """Record assisted membership creation and activation changes."""

        previous = (
            CompanyMembership.objects.filter(pk=obj.pk).only("active").first()
            if change
            else None
        )
        super().save_model(request, obj, form, change)

        if previous is None:
            action = AccessChange.Action.MEMBERSHIP_GRANTED
            previous_value: dict[str, object] = {}
        elif previous.active == obj.active:
            return
        else:
            action = (
                AccessChange.Action.MEMBERSHIP_GRANTED
                if obj.active
                else AccessChange.Action.MEMBERSHIP_REVOKED
            )
            previous_value = {"active": previous.active}

        record_access_change(
            company_id=obj.company_id,
            membership=obj,
            actor=request.user,
            action=action,
            previous_value=previous_value,
            new_value={"active": obj.active, "user_id": obj.user_id},
        )

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: CompanyMembership | None = None,
    ) -> bool:
        """Preserve memberships so their access history remains traceable."""

        return False


@admin.register(MembershipRole)
class MembershipRoleAdmin(admin.ModelAdmin):
    list_display = ("membership", "role", "active", "granted_by", "revoked_at")
    list_filter = ("role", "active", "membership__company")
    search_fields = (
        "membership__company__legal_name",
        "membership__user__username",
        "membership__user__email",
        "granted_by__username",
    )
    readonly_fields = ("created_at", "updated_at", "revoked_at")

    def get_readonly_fields(
        self,
        request: HttpRequest,
        obj: MembershipRole | None = None,
    ) -> tuple[str, ...]:
        """Prevent changing a role's target after its first audit entry."""

        fields = ("created_at", "updated_at", "revoked_at")
        if obj is not None:
            return (*fields, "membership", "role", "granted_by")
        return fields

    def save_model(
        self,
        request: HttpRequest,
        obj: MembershipRole,
        form: ModelForm,
        change: bool,
    ) -> None:
        """Record assisted role creation and activation changes."""

        previous = (
            MembershipRole.objects.filter(pk=obj.pk)
            .only("active", "role")
            .first()
            if change
            else None
        )
        super().save_model(request, obj, form, change)

        if previous is not None and previous.active == obj.active:
            return

        action = (
            AccessChange.Action.ROLE_GRANTED
            if obj.active
            else AccessChange.Action.ROLE_REVOKED
        )
        previous_value = (
            {"active": previous.active, "role": previous.role}
            if previous is not None
            else {}
        )
        new_value: dict[str, object] = {"active": obj.active, "role": obj.role}
        if not obj.active and obj.revoked_at is not None:
            new_value["revoked_at"] = obj.revoked_at.isoformat()

        record_access_change(
            company_id=obj.membership.company_id,
            membership=obj.membership,
            actor=request.user,
            action=action,
            previous_value=previous_value,
            new_value=new_value,
        )

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: MembershipRole | None = None,
    ) -> bool:
        """Preserve revoked roles instead of allowing administrative deletion."""

        return False


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "code",
        "company",
        "chain_unit_id",
        "registration_hash",
        "active",
    )
    list_filter = ("active", "company")
    search_fields = (
        "name",
        "code",
        "cnpj",
        "registration_hash",
        "company__legal_name",
        "company__trade_name",
    )
    readonly_fields = ("created_at", "updated_at", "registration_hash")


@admin.register(UnitAccess)
class UnitAccessAdmin(admin.ModelAdmin):
    list_display = (
        "membership",
        "unit",
        "can_view",
        "can_receive",
        "can_issue",
        "can_audit",
        "active",
    )
    list_filter = (
        "active",
        "can_view",
        "can_receive",
        "can_issue",
        "can_audit",
        "unit__company",
    )
    search_fields = (
        "membership__company__legal_name",
        "membership__user__username",
        "membership__user__email",
        "unit__name",
        "unit__code",
    )
    readonly_fields = ("created_at", "updated_at")

    def get_readonly_fields(
        self,
        request: HttpRequest,
        obj: UnitAccess | None = None,
    ) -> tuple[str, ...]:
        """Keep a grant tied to the same membership and unit after creation."""

        fields = ("created_at", "updated_at")
        if obj is not None:
            return (*fields, "membership", "unit")
        return fields

    def save_model(
        self,
        request: HttpRequest,
        obj: UnitAccess,
        form: ModelForm,
        change: bool,
    ) -> None:
        """Record assisted unit-grant creation and flag changes."""

        previous = (
            UnitAccess.objects.filter(pk=obj.pk)
            .only("active", "can_view", "can_receive", "can_issue", "can_audit")
            .first()
            if change
            else None
        )
        super().save_model(request, obj, form, change)

        current_value = {
            "active": obj.active,
            "can_view": obj.can_view,
            "can_receive": obj.can_receive,
            "can_issue": obj.can_issue,
            "can_audit": obj.can_audit,
        }
        previous_value = (
            {
                "active": previous.active,
                "can_view": previous.can_view,
                "can_receive": previous.can_receive,
                "can_issue": previous.can_issue,
                "can_audit": previous.can_audit,
            }
            if previous is not None
            else {}
        )
        if previous_value == current_value:
            return

        record_access_change(
            company_id=obj.unit.company_id,
            membership=obj.membership,
            unit=obj.unit,
            actor=request.user,
            action=AccessChange.Action.UNIT_ACCESS_UPDATED,
            previous_value=previous_value,
            new_value=current_value,
        )

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: UnitAccess | None = None,
    ) -> bool:
        """Preserve inactive grants and their access history."""

        return False


@admin.register(UnitWalletPermission)
class UnitWalletPermissionAdmin(admin.ModelAdmin):
    list_display = ("unit", "wallet", "permission_mask", "sync_status", "transaction")
    list_filter = ("sync_status", "unit__company")
    search_fields = ("wallet", "transaction", "unit__name", "unit__code")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AccessChange)
class AccessChangeAdmin(admin.ModelAdmin):
    list_display = ("company", "membership", "unit", "action", "actor", "occurred_at")
    list_filter = ("action", "company", "occurred_at")
    search_fields = (
        "company__legal_name",
        "membership__user__username",
        "membership__user__email",
        "actor__username",
        "transaction",
    )
    readonly_fields = (
        "company",
        "membership",
        "unit",
        "actor",
        "action",
        "previous_value",
        "new_value",
        "transaction",
        "created_at",
        "updated_at",
        "occurred_at",
    )

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: AccessChange | None = None,
    ) -> bool:
        """Keep access history read-only after it is appended."""

        return obj is None

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Require application services to create auditable history entries."""

        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: AccessChange | None = None,
    ) -> bool:
        """Prevent removing access history from the admin."""

        return False
