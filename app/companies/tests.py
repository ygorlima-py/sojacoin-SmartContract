from django.contrib.auth import get_user_model
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest
from django.test import RequestFactory, TestCase
from django.urls import reverse

from blockchain.models import BlockchainTransaction

from .admin import (
    AccessChangeAdmin,
    CompanyMembershipAdmin,
    MembershipRoleAdmin,
    UnitAccessAdmin,
)
from .forms import (
    CompanyForm,
    CompanyMembershipForm,
    MembershipRoleForm,
    UnitAccessForm,
    UnitForm,
)
from .models import (
    AccessChange,
    Company,
    CompanyMembership,
    MembershipRole,
    Unit,
    UnitAccess,
    UnitWalletPermission,
)
from .rules.company_access import get_membership_for_user, require_active_company
from .rules.role_access import (
    require_administrator_access,
    require_auditor_access,
    require_issuance_access,
    require_receiving_access,
    require_registration_access,
    require_role_grant_access,
)
from .rules.unit_access import get_unit_for_membership
from .services.unit_registration import (
    calculate_unit_registration_hash,
    save_unit_registration,
)


User = get_user_model()


class CompanyFormTests(TestCase):
    """Verify the assisted company form's validation boundaries."""

    def test_form_normalizes_a_valid_cnpj(self) -> None:
        form = CompanyForm(
            {
                "legal_name": "Campo Verde Armazéns Ltda.",
                "trade_name": "Campo Verde",
                "cnpj": "11.222.333/0001-81",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        company = form.save()

        self.assertEqual(company.cnpj, "11222333000181")

    def test_form_rejects_invalid_cnpj_digits(self) -> None:
        form = CompanyForm(
            {
                "legal_name": "Invalid Company",
                "trade_name": "Invalid",
                "cnpj": "11.222.333/0001-80",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("cnpj", form.errors)

    def test_form_rejects_duplicate_normalized_cnpj(self) -> None:
        Company.objects.create(
            legal_name="Existing Company",
            trade_name="Existing",
            cnpj="11222333000181",
        )
        form = CompanyForm(
            {
                "legal_name": "Duplicate Company",
                "trade_name": "Duplicate",
                "cnpj": "11.222.333/0001-81",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("cnpj", form.errors)

    def test_editing_same_company_keeps_cnpj_unique(self) -> None:
        company = Company.objects.create(
            legal_name="Existing Company",
            trade_name="Existing",
            cnpj="11222333000181",
        )
        form = CompanyForm(
            {
                "legal_name": "Updated Company",
                "trade_name": "Updated",
                "cnpj": "11.222.333/0001-81",
            },
            instance=company,
        )

        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()
        self.assertEqual(updated.pk, company.pk)

    def test_active_is_not_a_form_field_and_is_preserved(self) -> None:
        company = Company.objects.create(
            legal_name="Inactive Company",
            trade_name="Inactive",
            cnpj="11222333000181",
            active=False,
        )
        form = CompanyForm(
            {
                "legal_name": "Updated Company",
                "trade_name": "Updated",
                "cnpj": "11.222.333/0001-81",
                "active": True,
            },
            instance=company,
        )

        self.assertNotIn("active", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()

        self.assertFalse(updated.active)


class UnitFormTests(TestCase):
    """Verify that the unit form exposes only editable registration data."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.company = Company.objects.create(
            legal_name="Unit Form Company",
            trade_name="Unit Form",
            cnpj="11444777000100",
        )

    def test_form_contains_only_user_editable_fields(self) -> None:
        form = UnitForm(instance=Unit(company=self.company))

        self.assertEqual(
            set(form.fields),
            {"name", "code", "address", "cnpj", "state_registration"},
        )
        self.assertNotIn("company", form.fields)
        self.assertNotIn("registration_hash", form.fields)
        self.assertNotIn("chain_unit_id", form.fields)
        self.assertNotIn("active", form.fields)

    def test_form_normalizes_code_and_cnpj(self) -> None:
        form = UnitForm(
            {
                "name": "Armazém Primavera",
                "code": " prim ",
                "address": "Rua das Flores, 10",
                "cnpj": "11.222.333/0001-81",
                "state_registration": "123456789",
            },
            instance=Unit(company=self.company),
        )

        self.assertTrue(form.is_valid(), form.errors)
        unit = form.save()

        self.assertEqual(unit.code, "PRIM")
        self.assertEqual(unit.cnpj, "11222333000181")

    def test_form_rejects_invalid_cnpj(self) -> None:
        form = UnitForm(
            {
                "name": "Invalid Unit",
                "code": "INVALID",
                "address": "Rua das Flores, 10",
                "cnpj": "11.222.333/0001-80",
                "state_registration": "123456789",
            },
            instance=Unit(company=self.company),
        )

        self.assertFalse(form.is_valid())
        self.assertIn("cnpj", form.errors)

    def test_form_rejects_duplicate_normalized_cnpj(self) -> None:
        Unit.objects.create(
            company=self.company,
            name="Existing Unit",
            code="EXISTING",
            cnpj="11222333000181",
        )
        form = UnitForm(
            {
                "name": "Duplicate Unit",
                "code": "DUPLICATE",
                "address": "Rua das Flores, 10",
                "cnpj": "11.222.333/0001-81",
                "state_registration": "123456789",
            },
            instance=Unit(company=self.company),
        )

        self.assertFalse(form.is_valid())
        self.assertIn("cnpj", form.errors)

    def test_system_fields_are_preserved_when_editing(self) -> None:
        unit = Unit.objects.create(
            company=self.company,
            name="Existing Unit",
            code="EXISTING",
            address="Rua antiga, 1",
            cnpj="11222333000181",
            state_registration="987654321",
            registration_hash="0x" + ("a" * 64),
            chain_unit_id=42,
            active=False,
        )
        form = UnitForm(
            {
                "name": "Updated Unit",
                "code": "UPDATED",
                "address": "Rua nova, 2",
                "cnpj": "11.222.333/0001-81",
                "state_registration": "123456789",
                "registration_hash": "0x" + ("b" * 64),
                "chain_unit_id": 99,
                "active": True,
            },
            instance=unit,
        )

        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()

        self.assertEqual(updated.registration_hash, "0x" + ("a" * 64))
        self.assertEqual(updated.chain_unit_id, 42)
        self.assertFalse(updated.active)


class UnitRegistrationPersistenceTests(TestCase):
    """Verify local unit persistence without starting a blockchain operation."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.company = Company.objects.create(
            legal_name="Registration Company",
            trade_name="Registration",
            cnpj="11444777000242",
        )

    def registration_data(self) -> dict[str, str]:
        """Return form-shaped data with values that need canonicalization."""

        return {
            "name": "  Armazém   Primavera  ",
            "code": " prim ",
            "address": " Rua das Flores, 10 ",
            "cnpj": "11.222.333/0001-81",
            "state_registration": " sp-123 ",
        }

    def test_new_unit_is_saved_with_hash_and_no_chain_id(self) -> None:
        data = self.registration_data()
        unit = Unit(company=self.company, chain_unit_id=99)
        form = UnitForm(data, instance=unit)

        self.assertTrue(form.is_valid(), form.errors)
        saved = save_unit_registration(form.instance, form.cleaned_data)
        saved.refresh_from_db()

        self.assertEqual(saved.name, "Armazém Primavera")
        self.assertEqual(saved.code, "PRIM")
        self.assertEqual(saved.address, "Rua das Flores, 10")
        self.assertEqual(saved.state_registration, "SP-123")
        self.assertEqual(saved.registration_hash, calculate_unit_registration_hash(data))
        self.assertIsNone(saved.chain_unit_id)

    def test_edit_preserves_chain_id_and_recalculates_hash(self) -> None:
        old_data = self.registration_data()
        unit = Unit(
            company=self.company,
            name="Armazém Antigo",
            code="OLD",
            address="Rua antiga, 1",
            cnpj="11444777000323",
            state_registration="RJ-456",
        )
        save_unit_registration(unit, old_data)
        unit.chain_unit_id = 42
        unit.save()
        old_hash = unit.registration_hash

        data = self.registration_data()
        data["name"] = " Armazém Atualizado "
        form = UnitForm(data, instance=unit)

        self.assertTrue(form.is_valid(), form.errors)
        saved = save_unit_registration(form.instance, form.cleaned_data)

        self.assertEqual(saved.chain_unit_id, 42)
        self.assertNotEqual(saved.registration_hash, old_hash)
        self.assertEqual(saved.registration_hash, calculate_unit_registration_hash(data))

    def test_same_registration_data_produces_the_same_hash(self) -> None:
        """The canonical payload must produce a deterministic hash."""

        data = self.registration_data()

        first_hash = calculate_unit_registration_hash(data)
        second_hash = calculate_unit_registration_hash(dict(data))

        self.assertEqual(first_hash, second_hash)

    def test_invalid_form_is_not_persisted(self) -> None:
        data = self.registration_data()
        data["cnpj"] = "11.222.333/0001-80"
        form = UnitForm(data, instance=Unit(company=self.company))

        self.assertFalse(form.is_valid())
        self.assertEqual(Unit.objects.filter(company=self.company).count(), 0)


class UnitCnpjTests(TestCase):
    def test_cnpj_is_unique_across_companies_even_with_different_formatting(self):
        first = Company.objects.create(legal_name="First", cnpj="11222333000181")
        second = Company.objects.create(legal_name="Second", cnpj="11444777000161")
        Unit.objects.create(company=first, name="Unit A", code="A", cnpj="11.222.333/0001-81")
        duplicate = Unit(company=second, name="Unit B", code="B", cnpj="11222333000181")
        with self.assertRaises(ValidationError):
            duplicate.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            duplicate.save()

    def test_bulk_insert_cannot_bypass_canonical_format(self):
        company = Company.objects.create(legal_name="First", cnpj="11222333000181")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Unit.objects.bulk_create([
                Unit(company=company, name="Unit", code="A", cnpj="11.222.333/0001-81")
            ])


class CompanyContextTests(TestCase):
    """Verify company selection and server-side company isolation."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="company-user",
            password="secure-company-password",
        )
        cls.other_user = User.objects.create_user(
            username="other-company-user",
            password="secure-company-password",
        )
        cls.first_company = Company.objects.create(
            legal_name="First Company",
            trade_name="First",
            cnpj="11222333000181",
        )
        cls.second_company = Company.objects.create(
            legal_name="Second Company",
            trade_name="Second",
            cnpj="11444777000161",
        )
        cls.inactive_company = Company.objects.create(
            legal_name="Inactive Company",
            trade_name="Inactive",
            cnpj="11444777000242",
            active=False,
        )
        cls.first_membership = CompanyMembership.objects.create(
            company=cls.first_company,
            user=cls.user,
        )
        cls.second_membership = CompanyMembership.objects.create(
            company=cls.second_company,
            user=cls.user,
        )
        cls.inactive_company_membership = CompanyMembership.objects.create(
            company=cls.inactive_company,
            user=cls.user,
        )
        CompanyMembership.objects.create(
            company=cls.second_company,
            user=cls.other_user,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_user_can_choose_each_membership_explicitly(self):
        response = self.client.get(
            reverse("companies:detail", kwargs={"company_id": self.first_company.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["company"], self.first_company)
        self.assertEqual(self.client.session["active_company_id"], self.first_company.pk)

        response = self.client.get(
            reverse("companies:detail", kwargs={"company_id": self.second_company.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["company"], self.second_company)
        self.assertEqual(self.client.session["active_company_id"], self.second_company.pk)

    def test_company_list_does_not_choose_a_context_implicitly(self):
        response = self.client.get(reverse("companies:list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["memberships"].count(), 3)
        self.assertNotIn("active_company_id", self.client.session)

    def test_url_id_from_another_company_returns_not_found(self):
        self.client.force_login(self.other_user)

        response = self.client.get(
            reverse("companies:detail", kwargs={"company_id": self.first_company.pk})
        )

        self.assertEqual(response.status_code, 404)

    def test_inactive_membership_cannot_open_company_context(self):
        self.first_membership.active = False
        self.first_membership.save()

        response = self.client.get(
            reverse("companies:detail", kwargs={"company_id": self.first_company.pk})
        )

        self.assertEqual(response.status_code, 404)

    def test_inactive_company_is_readable_but_blocks_new_operations(self):
        response = self.client.get(
            reverse("companies:detail", kwargs={"company_id": self.inactive_company.pk})
        )
        self.assertEqual(response.status_code, 200)

        with self.assertRaises(PermissionDenied):
            require_active_company(self.inactive_company_membership)

    def test_membership_lookup_is_scoped_to_the_user(self):
        membership = get_membership_for_user(self.user, self.first_company.pk)

        self.assertEqual(membership, self.first_membership)

        with self.assertRaises(Http404):
            get_membership_for_user(self.other_user, self.first_company.pk)


class CompanyUpdateViewTests(TestCase):
    """Verify company editing uses membership and administrator authorization."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.grantor = User.objects.create_user(
            username="company-update-grantor",
            password="company-update-password",
        )
        cls.admin_user = User.objects.create_user(
            username="company-update-admin",
            password="company-update-password",
        )
        cls.member_user = User.objects.create_user(
            username="company-update-member",
            password="company-update-password",
        )
        cls.company = Company.objects.create(
            legal_name="Update Company",
            trade_name="Update",
            cnpj="11444777000323",
        )
        cls.other_company = Company.objects.create(
            legal_name="Other Update Company",
            trade_name="Other Update",
            cnpj="11444777000676",
        )
        cls.admin_membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.admin_user,
        )
        CompanyMembership.objects.create(
            company=cls.company,
            user=cls.member_user,
        )
        MembershipRole.objects.create(
            membership=cls.admin_membership,
            role=MembershipRole.Role.ADMINISTRATOR,
            granted_by=cls.grantor,
        )

    def edit_url(self, company_id: int | None = None) -> str:
        """Return the company edit URL used by the tests."""

        return reverse(
            "companies:edit",
            kwargs={"company_id": company_id or self.company.pk},
        )

    def detail_url(self, company_id: int | None = None) -> str:
        """Return the company detail URL used by the tests."""

        return reverse(
            "companies:detail",
            kwargs={"company_id": company_id or self.company.pk},
        )

    def valid_company_data(self) -> dict[str, str]:
        """Return a valid payload for the company form."""

        return {
            "legal_name": "Updated Legal Company",
            "trade_name": "Updated Trade",
            "cnpj": "11.222.333/0001-81",
        }

    def test_administrator_can_open_company_edit(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(self.edit_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["company"], self.company)
        self.assertContains(response, "Salvar alterações")

    def test_administrator_can_update_company(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.post(self.edit_url(), self.valid_company_data())

        self.assertRedirects(response, self.detail_url())
        self.company.refresh_from_db()
        self.assertEqual(self.company.legal_name, "Updated Legal Company")
        self.assertEqual(self.company.trade_name, "Updated Trade")
        self.assertEqual(self.company.cnpj, "11222333000181")
        self.assertTrue(self.company.active)

    def test_invalid_company_data_is_not_saved(self) -> None:
        self.client.force_login(self.admin_user)
        data = self.valid_company_data()
        data["cnpj"] = "11.222.333/0001-80"

        response = self.client.post(self.edit_url(), data)

        self.assertEqual(response.status_code, 200)
        self.company.refresh_from_db()
        self.assertEqual(self.company.legal_name, "Update Company")
        self.assertIn("cnpj", response.context["form"].errors)

    def test_member_without_administrator_role_cannot_edit(self) -> None:
        self.client.force_login(self.member_user)

        response = self.client.get(self.edit_url())

        self.assertEqual(response.status_code, 403)

    def test_company_without_membership_returns_not_found(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(self.edit_url(self.other_company.pk))

        self.assertEqual(response.status_code, 404)

    def test_inactive_company_cannot_be_edited(self) -> None:
        self.company.active = False
        self.company.save()
        self.client.force_login(self.admin_user)

        response = self.client.get(self.edit_url())

        self.assertEqual(response.status_code, 403)

    def test_detail_edit_link_is_only_shown_to_administrator(self) -> None:
        self.client.force_login(self.admin_user)
        admin_response = self.client.get(self.detail_url())
        self.assertContains(admin_response, self.edit_url())

        self.client.force_login(self.member_user)
        member_response = self.client.get(self.detail_url())
        self.assertNotContains(member_response, self.edit_url())

    def test_edit_requires_login(self) -> None:
        response = self.client.get(self.edit_url())

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={self.edit_url()}",
        )


class UnitViewTests(TestCase):
    """Verify unit views enforce company scope and local persistence rules."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.grantor = User.objects.create_user(
            username="unit-view-grantor",
            password="unit-view-password",
        )
        cls.admin_user = User.objects.create_user(
            username="unit-view-admin",
            password="unit-view-password",
        )
        cls.member_user = User.objects.create_user(
            username="unit-view-member",
            password="unit-view-password",
        )
        cls.company = Company.objects.create(
            legal_name="Unit View Company",
            trade_name="Unit Views",
            cnpj="11444777000323",
        )
        cls.other_company = Company.objects.create(
            legal_name="Other Unit Company",
            trade_name="Other Units",
            cnpj="11444777000676",
        )
        cls.admin_membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.admin_user,
        )
        CompanyMembership.objects.create(
            company=cls.company,
            user=cls.member_user,
        )
        MembershipRole.objects.create(
            membership=cls.admin_membership,
            role=MembershipRole.Role.ADMINISTRATOR,
            granted_by=cls.grantor,
        )
        cls.unit = Unit.objects.create(
            company=cls.company,
            name="Main Unit",
            code="MAIN",
            address="Main Street, 1",
            cnpj="11444777000404",
            state_registration="SP-100",
        )
        cls.other_unit = Unit.objects.create(
            company=cls.other_company,
            name="Outside Unit",
            code="OUTSIDE",
            address="Outside Street, 2",
            cnpj="11444777000757",
            state_registration="SP-200",
        )

    def list_url(self, company_id: int | None = None) -> str:
        """Return the company-scoped unit list URL."""

        return reverse(
            "companies:unit_list",
            kwargs={"company_id": company_id or self.company.pk},
        )

    def detail_url(self, unit_id: int, company_id: int | None = None) -> str:
        """Return the company-scoped unit detail URL."""

        return reverse(
            "companies:unit_detail",
            kwargs={
                "company_id": company_id or self.company.pk,
                "unit_id": unit_id,
            },
        )

    def new_url(self, company_id: int | None = None) -> str:
        """Return the company-scoped unit creation URL."""

        return reverse(
            "companies:unit_new",
            kwargs={"company_id": company_id or self.company.pk},
        )

    def edit_url(self, unit_id: int, company_id: int | None = None) -> str:
        """Return the company-scoped unit edit URL."""

        return reverse(
            "companies:unit_edit",
            kwargs={
                "company_id": company_id or self.company.pk,
                "unit_id": unit_id,
            },
        )

    def valid_unit_data(self, **overrides: str) -> dict[str, str]:
        """Return valid unit form data with optional field overrides."""

        data = {
            "name": "New Unit",
            "code": "NEW",
            "address": "New Street, 3",
            "cnpj": "11.222.333/0001-81",
            "state_registration": "SP-300",
        }
        data.update(overrides)
        return data

    def test_member_list_shows_only_company_members_and_active_roles(self) -> None:
        """Show active and inactive company memberships without leaking other companies."""

        outside = CompanyMembership.objects.create(company=self.other_company, user=self.grantor)
        member = CompanyMembership.objects.get(company=self.company, user=self.member_user)
        member.active = False
        member.save()
        self.client.force_login(self.admin_user)
        url = reverse("companies:member_list", kwargs={"company_id": self.company.pk})

        response = self.client.get(url)

        self.assertContains(response, self.admin_user.username)
        self.assertContains(response, self.member_user.username)
        self.assertContains(response, "Administrador")
        self.assertContains(response, "Inativo")
        self.assertNotContains(response, outside.user.username)
        self.assertContains(response, reverse("companies:member_detail", kwargs={"company_id": self.company.pk, "membership_id": member.pk}))

    def test_member_pages_require_active_membership_and_scope_details(self) -> None:
        """Reject anonymous users, nonmembers, and cross-company detail IDs."""

        outside = CompanyMembership.objects.create(company=self.other_company, user=self.grantor)
        list_url = reverse("companies:member_list", kwargs={"company_id": self.company.pk})
        detail_url = reverse("companies:member_detail", kwargs={"company_id": self.company.pk, "membership_id": outside.pk})
        self.assertEqual(self.client.get(list_url).status_code, 302)
        self.client.force_login(self.grantor)
        self.assertEqual(self.client.get(list_url).status_code, 404)
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(detail_url).status_code, 404)
        self.admin_membership.active = False
        self.admin_membership.save()
        self.assertEqual(self.client.get(list_url).status_code, 404)

    def test_member_detail_shows_company_member(self) -> None:
        """Allow an active member to open a company-scoped member detail."""

        self.client.force_login(self.member_user)
        url = reverse("companies:member_detail", kwargs={"company_id": self.company.pk, "membership_id": self.admin_membership.pk})
        response = self.client.get(url)
        self.assertContains(response, self.admin_user.username)
        self.assertContains(response, "Administrador")

    def test_unit_list_is_scoped_to_the_company(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(self.list_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Main Unit")
        self.assertNotContains(response, "Outside Unit")

    def test_unit_detail_from_another_company_returns_not_found(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(self.detail_url(self.other_unit.pk))

        self.assertEqual(response.status_code, 404)

    def test_user_without_company_membership_cannot_access_units(self) -> None:
        self.client.force_login(self.grantor)

        response = self.client.get(self.list_url())

        self.assertEqual(response.status_code, 404)

    def test_unit_detail_shows_local_hash_and_pending_chain_state(self) -> None:
        """Show the local hash and pending state without inventing a transaction."""

        self.unit.registration_hash = "0x" + ("a" * 64)
        self.unit.save()
        self.client.force_login(self.admin_user)

        response = self.client.get(self.detail_url(self.unit.pk))

        self.assertContains(response, "Hash local calculada")
        self.assertContains(response, self.unit.registration_hash)
        self.assertContains(response, "Aguardando registro na blockchain")
        self.assertContains(response, "chain_unit_id")
        self.assertNotContains(response, "Transação associada")

    def test_unit_detail_shows_confirmed_chain_id(self) -> None:
        """Show confirmation only when the unit has an on-chain identifier."""

        self.unit.registration_hash = "0x" + ("b" * 64)
        self.unit.chain_unit_id = 17
        self.unit.save()
        self.client.force_login(self.admin_user)

        response = self.client.get(self.detail_url(self.unit.pk))

        self.assertContains(response, "Registro confirmado na blockchain")
        self.assertContains(response, "ID da unidade na rede")
        self.assertContains(response, "17")
        self.assertNotContains(response, "Aguardando registro na blockchain")

    def test_administrator_can_create_unit_locally(self) -> None:
        self.client.force_login(self.admin_user)
        transaction_count = BlockchainTransaction.objects.count()

        response = self.client.post(self.new_url(), self.valid_unit_data())

        created = Unit.objects.get(company=self.company, code="NEW")
        self.assertRedirects(response, self.detail_url(created.pk))
        self.assertEqual(created.registration_hash[:2], "0x")
        self.assertEqual(len(created.registration_hash), 66)
        self.assertIsNone(created.chain_unit_id)
        self.assertEqual(BlockchainTransaction.objects.count(), transaction_count)

    def test_administrator_can_update_only_unit_in_company(self) -> None:
        self.unit.chain_unit_id = 17
        self.unit.save()
        self.client.force_login(self.admin_user)
        transaction_count = BlockchainTransaction.objects.count()

        response = self.client.post(
            self.edit_url(self.unit.pk),
            self.valid_unit_data(
                name="Updated Unit",
                code="UPDATED",
                cnpj="11.222.333/0001-81",
            ),
        )

        self.assertRedirects(response, self.detail_url(self.unit.pk))
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.name, "Updated Unit")
        self.assertEqual(self.unit.code, "UPDATED")
        self.assertEqual(self.unit.chain_unit_id, 17)
        self.assertEqual(BlockchainTransaction.objects.count(), transaction_count)

    def test_member_without_administrator_role_cannot_create_or_edit(self) -> None:
        self.client.force_login(self.member_user)

        create_response = self.client.get(self.new_url())
        edit_response = self.client.get(self.edit_url(self.unit.pk))

        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(edit_response.status_code, 403)

    def test_inactive_company_blocks_create_and_edit(self) -> None:
        self.company.active = False
        self.company.save()
        self.client.force_login(self.admin_user)

        create_response = self.client.get(self.new_url())
        edit_response = self.client.get(self.edit_url(self.unit.pk))

        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(edit_response.status_code, 403)

    def test_invalid_unit_form_is_not_saved(self) -> None:
        self.client.force_login(self.admin_user)
        data = self.valid_unit_data(cnpj="11.222.333/0001-80")

        response = self.client.post(self.new_url(), data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Unit.objects.filter(company=self.company, code="NEW").exists())
        self.assertIn("cnpj", response.context["form"].errors)

    def test_unit_list_and_detail_are_readable_by_regular_member(self) -> None:
        self.client.force_login(self.member_user)

        list_response = self.client.get(self.list_url())
        detail_response = self.client.get(self.detail_url(self.unit.pk))

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self.assertNotContains(list_response, "Nova unidade")
        self.assertNotContains(detail_response, "Editar unidade")


class MemberCreateViewTests(TestCase):
    """Verify that administrators can add existing accounts to one company."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.grantor = User.objects.create_user(
            username="membership-grantor",
            password="secure-membership-password",
        )
        cls.admin_user = User.objects.create_user(
            username="membership-admin",
            password="secure-membership-password",
        )
        cls.member_user = User.objects.create_user(
            username="membership-existing",
            password="secure-membership-password",
        )
        cls.target_user = User.objects.create_user(
            username="membership-target",
            password="secure-membership-password",
        )
        cls.inactive_user = User.objects.create_user(
            username="membership-inactive",
            password="secure-membership-password",
            is_active=False,
        )
        cls.company = Company.objects.create(
            legal_name="Membership Company",
            trade_name="Membership",
            cnpj="11444777000323",
        )
        cls.other_company = Company.objects.create(
            legal_name="Other Membership Company",
            trade_name="Other Membership",
            cnpj="11444777000676",
        )
        cls.admin_membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.admin_user,
        )
        cls.member_membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.member_user,
        )
        MembershipRole.objects.create(
            membership=cls.admin_membership,
            role=MembershipRole.Role.ADMINISTRATOR,
            granted_by=cls.grantor,
        )

    def new_url(self, company_id: int | None = None) -> str:
        """Return the company-scoped add-member URL."""

        return reverse(
            "companies:member_new",
            kwargs={"company_id": company_id or self.company.pk},
        )

    def list_url(self, company_id: int | None = None) -> str:
        """Return the company-scoped team URL."""

        return reverse(
            "companies:member_list",
            kwargs={"company_id": company_id or self.company.pk},
        )

    def test_form_contains_only_user_and_excludes_existing_or_inactive_accounts(self) -> None:
        form = CompanyMembershipForm(company=self.company)

        self.assertEqual(set(form.fields), {"user"})
        self.assertIn(self.target_user, form.fields["user"].queryset)
        self.assertNotIn(self.admin_user, form.fields["user"].queryset)
        self.assertNotIn(self.member_user, form.fields["user"].queryset)
        self.assertNotIn(self.inactive_user, form.fields["user"].queryset)

    def test_administrator_can_add_existing_user_to_company(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.post(
            self.new_url(),
            {"user": self.target_user.pk},
        )

        self.assertRedirects(response, self.list_url())
        membership = CompanyMembership.objects.get(
            company=self.company,
            user=self.target_user,
        )
        self.assertTrue(membership.active)
        history = AccessChange.objects.get(membership=membership)
        self.assertEqual(history.actor, self.admin_user)
        self.assertEqual(history.action, AccessChange.Action.MEMBERSHIP_GRANTED)
        self.assertContains(self.client.get(self.list_url()), self.target_user.username)

    def test_duplicate_membership_is_rejected(self) -> None:
        CompanyMembership.objects.create(
            company=self.company,
            user=self.target_user,
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            self.new_url(),
            {"user": self.target_user.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            CompanyMembership.objects.filter(
                company=self.company,
                user=self.target_user,
            ).count(),
            1,
        )
        self.assertIn("user", response.context["form"].errors)

    def test_company_is_taken_from_url_not_from_posted_data(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.post(
            self.new_url(),
            {
                "user": self.target_user.pk,
                "company": self.other_company.pk,
            },
        )

        self.assertRedirects(response, self.list_url())
        self.assertTrue(
            CompanyMembership.objects.filter(
                company=self.company,
                user=self.target_user,
            ).exists()
        )
        self.assertFalse(
            CompanyMembership.objects.filter(
                company=self.other_company,
                user=self.target_user,
            ).exists()
        )

    def test_non_administrator_cannot_add_member(self) -> None:
        self.client.force_login(self.member_user)

        response = self.client.get(self.new_url())

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            CompanyMembership.objects.filter(
                company=self.company,
                user=self.target_user,
            ).exists()
        )

    def test_company_from_another_membership_returns_not_found(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(self.new_url(self.other_company.pk))

        self.assertEqual(response.status_code, 404)

    def test_inactive_company_blocks_member_creation(self) -> None:
        self.company.active = False
        self.company.save()
        self.client.force_login(self.admin_user)

        response = self.client.get(self.new_url())

        self.assertEqual(response.status_code, 403)

    def test_inactive_user_cannot_be_added(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.post(
            self.new_url(),
            {"user": self.inactive_user.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            CompanyMembership.objects.filter(
                company=self.company,
                user=self.inactive_user,
            ).exists()
        )


class MemberRoleViewTests(TestCase):
    """Verify administrator-only role grants and historical revocations."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.grantor = User.objects.create_user(
            username="role-view-grantor",
            password="secure-role-view-password",
        )
        cls.admin_user = User.objects.create_user(
            username="role-view-admin",
            password="secure-role-view-password",
        )
        cls.target_user = User.objects.create_user(
            username="role-view-target",
            password="secure-role-view-password",
        )
        cls.company = Company.objects.create(
            legal_name="Role View Company",
            trade_name="Role Views",
            cnpj="11444777000323",
        )
        cls.other_company = Company.objects.create(
            legal_name="Other Role View Company",
            trade_name="Other Role Views",
            cnpj="11444777000676",
        )
        cls.admin_membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.admin_user,
        )
        cls.target_membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.target_user,
        )
        cls.other_membership = CompanyMembership.objects.create(
            company=cls.other_company,
            user=cls.target_user,
        )
        MembershipRole.objects.create(
            membership=cls.admin_membership,
            role=MembershipRole.Role.ADMINISTRATOR,
            granted_by=cls.grantor,
        )

    def detail_url(
        self,
        membership_id: int | None = None,
        company_id: int | None = None,
    ) -> str:
        """Return the company-scoped member detail URL."""

        return reverse(
            "companies:member_detail",
            kwargs={
                "company_id": company_id or self.company.pk,
                "membership_id": membership_id or self.target_membership.pk,
            },
        )

    def role_new_url(
        self,
        membership_id: int | None = None,
        company_id: int | None = None,
    ) -> str:
        """Return the company-scoped role grant URL."""

        return reverse(
            "companies:member_role_new",
            kwargs={
                "company_id": company_id or self.company.pk,
                "membership_id": membership_id or self.target_membership.pk,
            },
        )

    def revoke_url(
        self,
        role_id: int,
        membership_id: int | None = None,
        company_id: int | None = None,
    ) -> str:
        """Return the company-scoped role revocation URL."""

        return reverse(
            "companies:member_role_revoke",
            kwargs={
                "company_id": company_id or self.company.pk,
                "membership_id": membership_id or self.target_membership.pk,
                "role_id": role_id,
            },
        )

    def test_role_form_has_only_role_and_checks_duplicate_active_roles(self) -> None:
        form = MembershipRoleForm(membership=self.target_membership)

        self.assertEqual(set(form.fields), {"role"})
        MembershipRole.objects.create(
            membership=self.target_membership,
            role=MembershipRole.Role.RECEIVING,
            granted_by=self.grantor,
        )
        duplicate_form = MembershipRoleForm(
            {"role": MembershipRole.Role.RECEIVING},
            membership=self.target_membership,
        )

        self.assertFalse(duplicate_form.is_valid())
        self.assertIn("role", duplicate_form.errors)

    def test_administrator_can_grant_role_and_history_is_recorded(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.post(
            self.role_new_url(),
            {"role": MembershipRole.Role.RECEIVING},
        )

        self.assertRedirects(response, self.detail_url())
        role = MembershipRole.objects.get(
            membership=self.target_membership,
            role=MembershipRole.Role.RECEIVING,
            active=True,
        )
        self.assertEqual(role.granted_by, self.admin_user)
        history = AccessChange.objects.get(
            membership=self.target_membership,
            action=AccessChange.Action.ROLE_GRANTED,
        )
        self.assertEqual(history.actor, self.admin_user)
        self.assertEqual(history.new_value["role"], MembershipRole.Role.RECEIVING)
        self.assertContains(self.client.get(self.detail_url()), "Recebimento")

    def test_duplicate_active_role_is_rejected(self) -> None:
        MembershipRole.objects.create(
            membership=self.target_membership,
            role=MembershipRole.Role.REGISTRATION,
            granted_by=self.grantor,
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            self.role_new_url(),
            {"role": MembershipRole.Role.REGISTRATION},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            MembershipRole.objects.filter(
                membership=self.target_membership,
                role=MembershipRole.Role.REGISTRATION,
                active=True,
            ).count(),
            1,
        )
        self.assertIn("role", response.context["form"].errors)

    def test_administrator_cannot_grant_role_to_themselves(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(
            self.role_new_url(membership_id=self.admin_membership.pk)
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            MembershipRole.objects.filter(
                membership=self.admin_membership,
                role=MembershipRole.Role.RECEIVING,
            ).exists()
        )

    def test_non_administrator_cannot_grant_or_revoke_roles(self) -> None:
        self.client.force_login(self.target_user)

        grant_response = self.client.get(self.role_new_url())

        self.assertEqual(grant_response.status_code, 403)

        role = MembershipRole.objects.create(
            membership=self.target_membership,
            role=MembershipRole.Role.AUDITOR,
            granted_by=self.grantor,
        )
        revoke_response = self.client.post(self.revoke_url(role.pk))

        self.assertEqual(revoke_response.status_code, 403)
        role.refresh_from_db()
        self.assertTrue(role.active)

    def test_member_from_another_company_returns_not_found(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(
            self.role_new_url(
                membership_id=self.other_membership.pk,
                company_id=self.company.pk,
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_administrator_can_revoke_role_without_deleting_it(self) -> None:
        role = MembershipRole.objects.create(
            membership=self.target_membership,
            role=MembershipRole.Role.AUDITOR,
            granted_by=self.grantor,
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(self.revoke_url(role.pk))

        self.assertRedirects(response, self.detail_url())
        role.refresh_from_db()
        self.assertFalse(role.active)
        self.assertIsNotNone(role.revoked_at)
        history = AccessChange.objects.get(
            membership=self.target_membership,
            action=AccessChange.Action.ROLE_REVOKED,
        )
        self.assertEqual(history.actor, self.admin_user)
        self.assertEqual(history.previous_value["role"], MembershipRole.Role.AUDITOR)
        self.assertTrue(MembershipRole.objects.filter(pk=role.pk).exists())

    def test_revoked_role_can_be_granted_again_as_a_new_active_record(self) -> None:
        role = MembershipRole.objects.create(
            membership=self.target_membership,
            role=MembershipRole.Role.ISSUANCE,
            granted_by=self.grantor,
        )
        role.active = False
        role.save()
        self.client.force_login(self.admin_user)

        response = self.client.post(
            self.role_new_url(),
            {"role": MembershipRole.Role.ISSUANCE},
        )

        self.assertRedirects(response, self.detail_url())
        self.assertEqual(
            MembershipRole.objects.filter(
                membership=self.target_membership,
                role=MembershipRole.Role.ISSUANCE,
                active=True,
            ).count(),
            1,
        )


class MemberUnitAccessViewTests(TestCase):
    """Verify company-scoped SaaS permissions for a member and unit."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.grantor = User.objects.create_user(
            username="unit-access-grantor",
            password="secure-unit-access-password",
        )
        cls.admin_user = User.objects.create_user(
            username="unit-access-admin",
            password="secure-unit-access-password",
        )
        cls.target_user = User.objects.create_user(
            username="unit-access-target",
            password="secure-unit-access-password",
        )
        cls.company = Company.objects.create(
            legal_name="Unit Access Company",
            trade_name="Unit Access",
            cnpj="11444777000323",
        )
        cls.other_company = Company.objects.create(
            legal_name="Other Unit Access Company",
            trade_name="Other Unit Access",
            cnpj="11444777000676",
        )
        cls.admin_membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.admin_user,
        )
        cls.target_membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.target_user,
        )
        cls.other_membership = CompanyMembership.objects.create(
            company=cls.other_company,
            user=cls.target_user,
        )
        MembershipRole.objects.create(
            membership=cls.admin_membership,
            role=MembershipRole.Role.ADMINISTRATOR,
            granted_by=cls.grantor,
        )
        cls.unit = Unit.objects.create(
            company=cls.company,
            name="Unit Access Warehouse",
            code="ACCESS",
            cnpj="11444777000404",
        )
        cls.other_unit = Unit.objects.create(
            company=cls.other_company,
            name="Other Company Warehouse",
            code="OTHER",
            cnpj="11444777000757",
        )

    def detail_url(self) -> str:
        """Return the target member detail URL."""

        return reverse(
            "companies:member_detail",
            kwargs={
                "company_id": self.company.pk,
                "membership_id": self.target_membership.pk,
            },
        )

    def access_url(
        self,
        unit_id: int | None = None,
        membership_id: int | None = None,
        company_id: int | None = None,
    ) -> str:
        """Return a company-scoped member-unit access URL."""

        return reverse(
            "companies:member_unit_access",
            kwargs={
                "company_id": company_id or self.company.pk,
                "membership_id": membership_id or self.target_membership.pk,
                "unit_id": unit_id or self.unit.pk,
            },
        )

    def test_form_exposes_only_four_saas_flags(self) -> None:
        form = UnitAccessForm()

        self.assertEqual(
            set(form.fields),
            {"can_view", "can_receive", "can_issue", "can_audit"},
        )
        self.assertFalse(form.initial)

    def test_administrator_can_create_access_for_company_unit(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.post(
            self.access_url(),
            {"can_view": "on", "can_receive": "on"},
        )

        self.assertRedirects(response, self.detail_url())
        access = UnitAccess.objects.get(
            membership=self.target_membership,
            unit=self.unit,
        )
        self.assertTrue(access.active)
        self.assertTrue(access.can_view)
        self.assertTrue(access.can_receive)
        self.assertFalse(access.can_issue)
        self.assertFalse(access.can_audit)
        history = AccessChange.objects.get(
            membership=self.target_membership,
            unit=self.unit,
            action=AccessChange.Action.UNIT_ACCESS_UPDATED,
        )
        self.assertEqual(history.actor, self.admin_user)
        self.assertEqual(history.new_value["can_receive"], True)

    def test_detail_shows_units_and_configuration_link_to_administrator(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(self.detail_url())

        self.assertContains(response, self.unit.name)
        self.assertContains(response, "Configurar")
        self.assertContains(response, self.access_url())

    def test_all_flags_false_deactivates_access_without_deleting_record(self) -> None:
        access = UnitAccess.objects.create(
            membership=self.target_membership,
            unit=self.unit,
            can_view=True,
            can_receive=True,
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(self.access_url(), {})

        self.assertRedirects(response, self.detail_url())
        access.refresh_from_db()
        self.assertFalse(access.active)
        self.assertTrue(UnitAccess.objects.filter(pk=access.pk).exists())
        history = AccessChange.objects.filter(
            membership=self.target_membership,
            unit=self.unit,
            action=AccessChange.Action.UNIT_ACCESS_UPDATED,
        ).latest("occurred_at")
        self.assertEqual(history.new_value, {"active": False})

    def test_existing_access_can_be_updated_and_reactivated(self) -> None:
        access = UnitAccess.objects.create(
            membership=self.target_membership,
            unit=self.unit,
            can_view=True,
            active=False,
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            self.access_url(),
            {"can_issue": "on"},
        )

        self.assertRedirects(response, self.detail_url())
        access.refresh_from_db()
        self.assertTrue(access.active)
        self.assertFalse(access.can_view)
        self.assertTrue(access.can_issue)

    def test_saas_access_changes_do_not_create_blockchain_permissions_or_transactions(self) -> None:
        """Changing SaaS access flags must not silently change wallet permissions."""

        self.client.force_login(self.admin_user)
        transaction_count = BlockchainTransaction.objects.count()

        grant_response = self.client.post(
            self.access_url(),
            {"can_view": "on", "can_receive": "on"},
        )
        self.assertRedirects(grant_response, self.detail_url())
        access = UnitAccess.objects.get(membership=self.target_membership, unit=self.unit)

        revoke_response = self.client.post(self.access_url(), {})
        self.assertRedirects(revoke_response, self.detail_url())

        self.assertEqual(BlockchainTransaction.objects.count(), transaction_count)
        self.assertFalse(UnitWalletPermission.objects.filter(unit=self.unit).exists())
        access.refresh_from_db()
        self.assertFalse(access.active)

    def test_unit_from_another_company_returns_not_found_and_is_not_assigned(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.post(
            self.access_url(unit_id=self.other_unit.pk),
            {"can_view": "on"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            UnitAccess.objects.filter(
                membership=self.target_membership,
                unit=self.other_unit,
            ).exists()
        )

    def test_membership_from_another_company_returns_not_found(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(
            self.access_url(membership_id=self.other_membership.pk)
        )

        self.assertEqual(response.status_code, 404)

    def test_non_administrator_cannot_manage_unit_access(self) -> None:
        self.client.force_login(self.target_user)

        response = self.client.get(self.access_url())

        self.assertEqual(response.status_code, 403)

    def test_inactive_membership_cannot_receive_active_unit_access(self) -> None:
        self.target_membership.active = False
        self.target_membership.save()
        self.client.force_login(self.admin_user)

        response = self.client.get(self.access_url())

        self.assertEqual(response.status_code, 403)


class AccessHistoryViewTests(TestCase):
    """Verify the company-scoped, read-only access-history screen."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.grantor = User.objects.create_user(
            username="history-grantor",
            password="secure-history-password",
        )
        cls.admin_user = User.objects.create_user(
            username="history-admin",
            password="secure-history-password",
        )
        cls.member_user = User.objects.create_user(
            username="history-member",
            password="secure-history-password",
        )
        cls.company = Company.objects.create(
            legal_name="History Company",
            trade_name="History",
            cnpj="11444777000323",
        )
        cls.other_company = Company.objects.create(
            legal_name="Other History Company",
            trade_name="Other History",
            cnpj="11444777000676",
        )
        cls.admin_membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.admin_user,
        )
        cls.member_membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.member_user,
        )
        MembershipRole.objects.create(
            membership=cls.admin_membership,
            role=MembershipRole.Role.ADMINISTRATOR,
            granted_by=cls.grantor,
        )
        cls.unit = Unit.objects.create(
            company=cls.company,
            name="History Warehouse",
            code="HISTORY",
            cnpj="11444777000404",
        )
        cls.outside_unit = Unit.objects.create(
            company=cls.other_company,
            name="Outside History Warehouse",
            code="OUTSIDE-HISTORY",
            cnpj="11444777000757",
        )
        AccessChange.objects.create(
            company=cls.company,
            membership=cls.member_membership,
            actor=cls.admin_user,
            action=AccessChange.Action.ROLE_GRANTED,
            new_value={"active": True, "role": MembershipRole.Role.RECEIVING},
        )
        AccessChange.objects.create(
            company=cls.company,
            membership=cls.member_membership,
            unit=cls.unit,
            actor=cls.admin_user,
            action=AccessChange.Action.UNIT_ACCESS_UPDATED,
            previous_value={"active": False},
            new_value={"active": True, "can_view": True},
        )
        AccessChange.objects.create(
            company=cls.other_company,
            membership=CompanyMembership.objects.create(
                company=cls.other_company,
                user=cls.grantor,
            ),
            unit=cls.outside_unit,
            actor=cls.grantor,
            action=AccessChange.Action.UNIT_ACCESS_UPDATED,
            new_value={"active": True},
        )

    def history_url(self, company_id: int | None = None) -> str:
        """Return the company-scoped history URL."""

        return reverse(
            "companies:history",
            kwargs={"company_id": company_id or self.company.pk},
        )

    def test_administrator_can_consult_company_history(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(self.history_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Função concedida")
        self.assertContains(response, "Acesso à unidade reativado")
        self.assertContains(response, self.member_user.username)
        self.assertContains(response, self.unit.name)
        self.assertContains(response, "Visualização")
        self.assertContains(response, "Concedida")
        self.assertNotContains(response, "can_view")
        self.assertNotContains(response, "previous_value")
        self.assertNotContains(response, self.outside_unit.name)

    def test_new_unit_access_shows_permissions_without_repeated_previous_state(self) -> None:
        """New access presents each recorded permission once, with no invented history."""
        AccessChange.objects.create(
            company=self.company,
            membership=self.member_membership,
            unit=self.unit,
            actor=self.admin_user,
            action=AccessChange.Action.UNIT_ACCESS_UPDATED,
            new_value={"active": True, "can_view": True, "can_receive": False,
                       "can_issue": False, "can_audit": False},
        )
        self.client.force_login(self.admin_user)
        response = self.client.get(self.history_url())
        self.assertContains(response, "Acesso à unidade criado")
        self.assertContains(response, "Não concedida")
        self.assertNotContains(response, "Não configurada")
        self.assertNotContains(response, "can_receive")

    def test_regular_member_cannot_consult_access_history(self) -> None:
        self.client.force_login(self.member_user)

        response = self.client.get(self.history_url())

        self.assertEqual(response.status_code, 403)

    def test_history_route_cannot_cross_company_scope(self) -> None:
        self.client.force_login(self.admin_user)

        response = self.client.get(self.history_url(self.other_company.pk))

        self.assertEqual(response.status_code, 404)


class AdminAccessHistoryTests(TestCase):
    """Verify that assisted Admin changes append immutable access history."""

    def setUp(self) -> None:
        """Create an isolated company, actor, member and unit for each test."""

        self.actor = User.objects.create_user(
            username="admin-history-actor",
            password="secure-admin-history-password",
        )
        self.member_user = User.objects.create_user(
            username="admin-history-member",
            password="secure-admin-history-password",
        )
        self.company = Company.objects.create(
            legal_name="Admin History Company",
            trade_name="Admin History",
            cnpj="11444777000323",
        )
        self.membership = CompanyMembership.objects.create(
            company=self.company,
            user=self.member_user,
        )
        self.unit = Unit.objects.create(
            company=self.company,
            name="Admin History Warehouse",
            code="ADMIN-HISTORY",
            cnpj="11444777000404",
        )
        self.site = AdminSite()

    def admin_request(self) -> HttpRequest:
        """Return a request carrying the user who performed the Admin action."""

        request = RequestFactory().post("/admin/")
        request.user = self.actor
        return request

    def test_admin_creation_and_updates_are_recorded_without_deletion(self) -> None:
        """Record membership, role and unit-grant changes with before/after data."""

        request = self.admin_request()
        membership_admin = CompanyMembershipAdmin(CompanyMembership, self.site)
        role_admin = MembershipRoleAdmin(MembershipRole, self.site)
        unit_access_admin = UnitAccessAdmin(UnitAccess, self.site)
        access_change_admin = AccessChangeAdmin(AccessChange, self.site)

        membership_admin.save_model(
            request,
            self.membership,
            form=None,  # type: ignore[arg-type]
            change=False,
        )
        membership_history = AccessChange.objects.get(
            membership=self.membership,
            action=AccessChange.Action.MEMBERSHIP_GRANTED,
        )
        self.assertEqual(membership_history.actor, self.actor)
        self.assertEqual(membership_history.new_value["active"], True)

        role = MembershipRole(
            membership=self.membership,
            role=MembershipRole.Role.RECEIVING,
            granted_by=self.actor,
        )
        role_admin.save_model(request, role, form=None, change=False)  # type: ignore[arg-type]
        role_history = AccessChange.objects.get(
            membership=self.membership,
            action=AccessChange.Action.ROLE_GRANTED,
        )
        self.assertEqual(role_history.new_value["role"], MembershipRole.Role.RECEIVING)

        unit_access = UnitAccess(
            membership=self.membership,
            unit=self.unit,
            can_view=True,
        )
        unit_access_admin.save_model(
            request,
            unit_access,
            form=None,  # type: ignore[arg-type]
            change=False,
        )
        unit_history = AccessChange.objects.get(
            membership=self.membership,
            unit=self.unit,
            action=AccessChange.Action.UNIT_ACCESS_UPDATED,
        )
        self.assertEqual(unit_history.new_value["can_view"], True)

        self.membership.active = False
        membership_admin.save_model(
            request,
            self.membership,
            form=None,  # type: ignore[arg-type]
            change=True,
        )
        self.assertTrue(
            AccessChange.objects.filter(
                membership=self.membership,
                action=AccessChange.Action.MEMBERSHIP_REVOKED,
            ).exists()
        )

        role.active = False
        role_admin.save_model(request, role, form=None, change=True)  # type: ignore[arg-type]
        self.assertTrue(
            AccessChange.objects.filter(
                membership=self.membership,
                action=AccessChange.Action.ROLE_REVOKED,
            ).exists()
        )

        unit_access.active = False
        unit_access_admin.save_model(
            request,
            unit_access,
            form=None,  # type: ignore[arg-type]
            change=True,
        )
        self.assertEqual(
            AccessChange.objects.filter(
                membership=self.membership,
                unit=self.unit,
                action=AccessChange.Action.UNIT_ACCESS_UPDATED,
            ).count(),
            2,
        )
        self.assertFalse(
            membership_admin.has_delete_permission(request, self.membership)
        )
        self.assertFalse(role_admin.has_delete_permission(request, role))
        self.assertFalse(unit_access_admin.has_delete_permission(request, unit_access))
        self.assertFalse(access_change_admin.has_add_permission(request))
        self.assertFalse(access_change_admin.has_delete_permission(request))


class RoleAccessTests(TestCase):
    """Verify staff functions and unit-level operational permissions."""

    @classmethod
    def setUpTestData(cls):
        cls.grantor = User.objects.create_user(
            username="platform-grantor",
            password="secure-role-password",
        )
        cls.company = Company.objects.create(
            legal_name="Role Company",
            trade_name="Roles",
            cnpj="11444777000323",
        )
        cls.outside_company = Company.objects.create(
            legal_name="Outside Company",
            trade_name="Outside",
            cnpj="11444777000676",
        )
        cls.admin_user = User.objects.create_user(
            username="company-admin",
            password="secure-role-password",
        )
        cls.registration_user = User.objects.create_user(
            username="registration-user",
            password="secure-role-password",
        )
        cls.receiving_user = User.objects.create_user(
            username="receiving-user",
            password="secure-role-password",
        )
        cls.issuance_user = User.objects.create_user(
            username="issuance-user",
            password="secure-role-password",
        )
        cls.auditor_user = User.objects.create_user(
            username="auditor-user",
            password="secure-role-password",
        )
        cls.target_user = User.objects.create_user(
            username="target-user",
            password="secure-role-password",
        )

        cls.admin_membership = CompanyMembership.objects.create(
            company=cls.company, user=cls.admin_user
        )
        cls.registration_membership = CompanyMembership.objects.create(
            company=cls.company, user=cls.registration_user
        )
        cls.receiving_membership = CompanyMembership.objects.create(
            company=cls.company, user=cls.receiving_user
        )
        cls.issuance_membership = CompanyMembership.objects.create(
            company=cls.company, user=cls.issuance_user
        )
        cls.auditor_membership = CompanyMembership.objects.create(
            company=cls.company, user=cls.auditor_user
        )
        cls.target_membership = CompanyMembership.objects.create(
            company=cls.company, user=cls.target_user
        )

        role_values = (
            (cls.admin_membership, MembershipRole.Role.ADMINISTRATOR),
            (cls.registration_membership, MembershipRole.Role.REGISTRATION),
            (cls.receiving_membership, MembershipRole.Role.RECEIVING),
            (cls.issuance_membership, MembershipRole.Role.ISSUANCE),
            (cls.auditor_membership, MembershipRole.Role.AUDITOR),
        )
        for membership, role in role_values:
            MembershipRole.objects.create(
                membership=membership,
                role=role,
                granted_by=cls.grantor,
            )

        cls.unit = Unit.objects.create(
            company=cls.company,
            name="Main warehouse",
            code="MAIN",
            cnpj="11444777000404",
        )
        cls.other_unit = Unit.objects.create(
            company=cls.company,
            name="Other warehouse",
            code="OTHER",
            cnpj="11444777000595",
        )
        cls.outside_unit = Unit.objects.create(
            company=cls.outside_company,
            name="Outside warehouse",
            code="OUTSIDE",
            cnpj="11444777000757",
        )
        cls.receiving_access = UnitAccess.objects.create(
            membership=cls.receiving_membership,
            unit=cls.unit,
            can_view=True,
            can_receive=True,
        )
        UnitAccess.objects.create(
            membership=cls.issuance_membership,
            unit=cls.unit,
            can_view=True,
            can_issue=True,
        )
        UnitAccess.objects.create(
            membership=cls.auditor_membership,
            unit=cls.unit,
            can_view=True,
            can_audit=True,
        )

    def test_each_staff_function_allows_only_its_operation(self):
        self.assertEqual(
            require_administrator_access(self.admin_user, self.company.pk),
            self.admin_membership,
        )
        self.assertEqual(
            require_registration_access(self.registration_user, self.company.pk),
            self.registration_membership,
        )
        self.assertEqual(
            require_receiving_access(
                self.receiving_user, self.company.pk, self.unit.pk
            )[1],
            self.unit,
        )
        self.assertEqual(
            require_issuance_access(
                self.issuance_user, self.company.pk, self.unit.pk
            )[1],
            self.unit,
        )
        self.assertEqual(
            require_auditor_access(self.auditor_user, self.company.pk, self.unit.pk)[1],
            self.unit,
        )

    def test_wrong_function_or_unit_grant_is_denied(self):
        with self.assertRaises(PermissionDenied):
            require_receiving_access(
                self.registration_user, self.company.pk, self.unit.pk
            )
        with self.assertRaises(PermissionDenied):
            require_receiving_access(
                self.receiving_user, self.company.pk, self.other_unit.pk
            )

        self.receiving_access.can_receive = False
        self.receiving_access.save()
        with self.assertRaises(PermissionDenied):
            require_receiving_access(
                self.receiving_user, self.company.pk, self.unit.pk
            )

    def test_inactive_role_no_longer_authorizes_its_operation(self):
        role = self.receiving_membership.roles.get(
            role=MembershipRole.Role.RECEIVING,
            active=True,
        )
        role.active = False
        role.save()

        with self.assertRaises(PermissionDenied):
            require_receiving_access(
                self.receiving_user,
                self.company.pk,
                self.unit.pk,
            )

    def test_inactive_membership_and_unit_grant_no_longer_authorize_operations(self):
        self.receiving_access.active = False
        self.receiving_access.save()
        with self.assertRaises(PermissionDenied):
            require_receiving_access(
                self.receiving_user,
                self.company.pk,
                self.unit.pk,
            )

        self.receiving_access.active = True
        self.receiving_access.save()
        self.receiving_membership.active = False
        self.receiving_membership.save()
        with self.assertRaises(Http404):
            require_receiving_access(
                self.receiving_user,
                self.company.pk,
                self.unit.pk,
            )

    def test_inactive_company_blocks_operational_functions(self):
        self.company.active = False
        self.company.save()

        with self.assertRaises(PermissionDenied):
            require_issuance_access(
                self.issuance_user, self.company.pk, self.unit.pk
            )

    def test_inactive_unit_blocks_new_operations(self):
        self.unit.active = False
        self.unit.save()

        with self.assertRaises(PermissionDenied):
            require_receiving_access(
                self.receiving_user, self.company.pk, self.unit.pk
            )

    def test_unit_from_another_company_cannot_be_used(self):
        with self.assertRaises(Http404):
            get_unit_for_membership(self.receiving_membership, self.outside_unit.pk)

    def test_unit_access_cannot_cross_company_boundaries(self):
        with self.assertRaises(ValidationError):
            UnitAccess.objects.create(
                membership=self.receiving_membership,
                unit=self.outside_unit,
                can_receive=True,
            )

    def test_user_cannot_grant_a_function_to_themselves(self):
        with self.assertRaises(ValidationError):
            MembershipRole.objects.create(
                membership=self.admin_membership,
                role=MembershipRole.Role.REGISTRATION,
                granted_by=self.admin_user,
            )
        with self.assertRaises(PermissionDenied):
            require_role_grant_access(self.admin_user, self.admin_membership)

    def test_only_an_administrator_can_grant_a_function_to_another_user(self):
        self.assertEqual(
            require_role_grant_access(self.admin_user, self.target_membership),
            self.admin_membership,
        )
        with self.assertRaises(PermissionDenied):
            require_role_grant_access(self.registration_user, self.target_membership)
