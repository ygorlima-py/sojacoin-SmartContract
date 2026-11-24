from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils import timezone
from datetime import timedelta

from .models import (
    UserProfile,
    UserWallet,
    Wallet,
    WalletVerificationChallenge,
)
from .forms import UserProfileForm
from .rules.account_access import is_active_account
from .rules.operation_rules import (
    BLOCKCHAIN_TRANSACTION_CONFIRMED,
    ISSUANCE_BATCH_APPROVED,
    BatchReceiptCandidate,
    calculate_net_weight_kg,
    can_display_issued_tokens,
    require_accepted_terms,
    require_approved_batch_for_issuance,
    require_compatible_batch,
    require_confirmed_issuance,
    require_cpr_balance_unchanged,
    require_registered_participants,
    require_unique_weighing_ticket,
)
from .rules.wallet_access import (
    consume_wallet_challenge,
    get_user_challenge,
    get_user_wallet,
    require_consumable_challenge,
    require_verified_wallet,
    require_verified_wallet_for_operation,
)


User = get_user_model()


class PasswordManagementTests(TestCase):
    """Exercise password change and reset with Django's token handling."""

    def setUp(self):
        self.user = User.objects.create_user(username="password-user", email="user@example.com", password="Old-secure-pass123!")

    def test_change_requires_login_and_old_password(self):
        url = reverse("accounts:password_change")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user)
        response = self.client.post(url, {"old_password": "wrong", "new_password1": "New-secure-pass456!", "new_password2": "New-secure-pass456!"})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Old-secure-pass123!"))
        response = self.client.post(url, {"old_password": "Old-secure-pass123!", "new_password1": "New-secure-pass456!", "new_password2": "New-secure-pass456!"})
        self.assertRedirects(response, reverse("accounts:password_change_done"))
        self.assertIn("_auth_user_id", self.client.session)

    def test_reset_generic_response_and_email(self):
        url = reverse("accounts:password_reset")
        self.assertRedirects(self.client.post(url, {"email": "missing@example.com"}), reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)
        self.assertRedirects(self.client.post(url, {"email": "user@example.com"}), reverse("accounts:password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/accounts/reset/", mail.outbox[0].body)

    def test_reset_valid_token_once_and_invalid_token(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        url = reverse("accounts:password_reset_confirm", kwargs={"uidb64": uid, "token": default_token_generator.make_token(self.user)})
        response = self.client.get(url)
        response = self.client.post(response.url, {"new_password1": "New-secure-pass456!", "new_password2": "New-secure-pass456!"})
        self.assertRedirects(response, reverse("accounts:password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("New-secure-pass456!"))
        self.assertContains(self.client.get(url), "Link inválido ou expirado")


class AccountAccessTests(TestCase):
    """Exercise authentication, profile isolation and session protections."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="operator", password="secure-test-password")

    def test_login_and_safe_redirect(self):
        response = self.client.post(reverse("accounts:login"), {
            "username": "operator", "password": "secure-test-password",
            "next": "https://example.com/steal",
        })
        self.assertRedirects(response, reverse("accounts:profile"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_login_rejects_wrong_password_and_inactive_profile(self):
        response = self.client.post(reverse("accounts:login"), {"username": "operator", "password": "wrong"})
        self.assertContains(response, "Usuário ou senha inválidos.")
        self.user.profile.active = False
        self.user.profile.save()
        response = self.client.post(reverse("accounts:login"), {"username": "operator", "password": "secure-test-password"})
        self.assertContains(response, "Usuário ou senha inválidos.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_rejects_inactive_user(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.post(reverse("accounts:login"), {"username": "operator", "password": "secure-test-password"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_account_rule_requires_an_active_user_and_profile(self):
        self.assertTrue(is_active_account(self.user))

        self.user.profile.active = False
        self.user.profile.save()
        self.assertFalse(is_active_account(self.user))

        self.user.profile.active = True
        self.user.profile.save()
        self.user.is_active = False
        self.user.save()
        self.assertFalse(is_active_account(self.user))

    def test_missing_profile_cannot_login(self):
        UserProfile.objects.filter(user=self.user).delete()

        response = self.client.post(reverse("accounts:login"), {
            "username": "operator",
            "password": "secure-test-password",
        })

        self.assertContains(response, "Usuário ou senha inválidos.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_inactive_user_ends_an_existing_session(self):
        self.client.force_login(self.user)
        self.user.is_active = False
        self.user.save()

        response = self.client.get(reverse("accounts:profile"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url.split("?", 1)[0], reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_profile_requires_login_and_edits_only_permitted_fields(self):
        self.assertEqual(self.client.get(reverse("accounts:profile")).status_code, 302)
        self.client.force_login(self.user)
        response = self.client.post(reverse("accounts:profile"), {"full_name": "Maria", "phone": "123", "active": "false"})
        self.assertRedirects(response, reverse("accounts:profile"))
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.full_name, "Maria")
        self.assertTrue(self.user.profile.active)

    def test_profile_form_does_not_expose_status_or_role_fields(self):
        form = UserProfileForm(instance=self.user.profile)

        self.assertEqual(set(form.fields), {"full_name", "phone"})

    def test_public_signup_is_not_available(self):
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)

    def test_inactive_profile_ends_existing_session(self):
        self.client.force_login(self.user)
        self.user.profile.active = False
        self.user.profile.save()
        self.assertRedirects(self.client.get(reverse("accounts:profile")), reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_requires_post_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.get(reverse("accounts:logout")).status_code, 405)
        self.assertEqual(client.post(reverse("accounts:logout")).status_code, 403)
        client.get(reverse("accounts:profile"))
        response = client.post(reverse("accounts:logout"), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertRedirects(response, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", client.session)


class UserProfileModelTests(TestCase):
    """Verify the profile lifecycle and its one-to-one relationship."""

    def test_profile_is_created_for_a_new_user(self):
        user = User.objects.create_user(
            username="alice",
            password="a-secure-development-password",
        )

        profile = UserProfile.objects.get(user=user)

        self.assertEqual(profile.user, user)
        self.assertTrue(profile.active)
        self.assertEqual(profile.full_name, "")
        self.assertEqual(profile.phone, "")
        self.assertIsNotNone(profile.created_at)
        self.assertIsNotNone(profile.updated_at)

    def test_profile_is_created_again_if_an_existing_user_lacks_one(self):
        user = User.objects.create_user(
            username="without-profile",
            password="a-secure-development-password",
        )
        UserProfile.objects.filter(user=user).delete()

        user.save()

        self.assertTrue(UserProfile.objects.filter(user=user).exists())

    def test_each_user_can_have_only_one_profile(self):
        user = User.objects.create_user(
            username="single-profile",
            password="a-secure-development-password",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UserProfile.objects.create(user=user)

        self.assertEqual(UserProfile.objects.filter(user=user).count(), 1)

    def test_profile_fields_can_be_updated_without_changing_the_user(self):
        user = User.objects.create_user(
            username="profile-fields",
            password="a-secure-development-password",
        )
        profile = user.profile
        profile.full_name = "Alice Producer"
        profile.phone = "+55 11 99999-0000"
        profile.active = False
        profile.save()

        profile.refresh_from_db()
        user.refresh_from_db()

        self.assertEqual(profile.full_name, "Alice Producer")
        self.assertEqual(profile.phone, "+55 11 99999-0000")
        self.assertFalse(profile.active)
        self.assertEqual(user.username, "profile-fields")

    def test_deleting_the_user_deletes_the_profile(self):
        user = User.objects.create_user(
            username="cascade-profile",
            password="a-secure-development-password",
        )
        profile_id = user.profile.pk

        user.delete()

        self.assertFalse(UserProfile.objects.filter(pk=profile_id).exists())


class WalletModelTests(TestCase):
    """Verify public address normalization and wallet classification."""

    def test_wallet_normalizes_and_preserves_the_display_address(self):
        wallet = Wallet.objects.create(
            address="  0xAbCdEf0123456789AbCdEf0123456789AbCdEf01  ",
            label="Settlement wallet",
        )

        self.assertEqual(wallet.address, "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01")
        self.assertEqual(
            wallet.address_normalized,
            "0xabcdef0123456789abcdef0123456789abcdef01",
        )
        self.assertEqual(str(wallet), "Settlement wallet")

    def test_wallet_rejects_an_invalid_address(self):
        with self.assertRaises(ValidationError):
            Wallet.objects.create(address="not-an-evm-address")

    def test_normalized_address_is_unique_case_insensitively(self):
        Wallet.objects.create(address="0xabcdef0123456789abcdef0123456789abcdef01")

        with self.assertRaises(ValidationError):
            Wallet.objects.create(address="0xABCDEF0123456789ABCDEF0123456789ABCDEF01")

    def test_contract_wallet_is_a_declared_classification_only(self):
        wallet = Wallet.objects.create(
            address="0x1234567890123456789012345678901234567890",
            wallet_type=Wallet.WalletType.CONTRACT,
        )

        self.assertEqual(wallet.wallet_type, Wallet.WalletType.CONTRACT)
        self.assertTrue(wallet.active)


class UserWalletModelTests(TestCase):
    """Verify user-specific wallet links and their audit states."""

    def setUp(self):
        self.wallet = Wallet.objects.create(
            address="0x1234567890123456789012345678901234567890",
            label="Shared corporate wallet",
            wallet_type=Wallet.WalletType.CONTRACT,
        )

    def create_user(self, username):
        return User.objects.create_user(
            username=username,
            password="a-secure-development-password",
        )

    def test_link_starts_pending(self):
        user = self.create_user("pending-link")

        link = UserWallet.objects.create(user=user, wallet=self.wallet)

        self.assertEqual(
            link.verification_status,
            UserWallet.VerificationStatus.PENDING,
        )
        self.assertIsNone(link.verified_at)
        self.assertIsNone(link.revoked_at)

    def test_same_wallet_can_be_linked_to_multiple_users(self):
        first = self.create_user("first-wallet-user")
        second = self.create_user("second-wallet-user")

        UserWallet.objects.create(user=first, wallet=self.wallet)
        UserWallet.objects.create(user=second, wallet=self.wallet)

        self.assertEqual(self.wallet.user_links.count(), 2)

    def test_same_user_cannot_repeat_the_same_wallet_link(self):
        user = self.create_user("duplicate-wallet-link")
        UserWallet.objects.create(user=user, wallet=self.wallet)

        with self.assertRaises(ValidationError):
            UserWallet.objects.create(user=user, wallet=self.wallet)

    def test_verified_link_records_the_verification_time(self):
        user = self.create_user("verified-wallet-link")

        link = UserWallet.objects.create(
            user=user,
            wallet=self.wallet,
            verification_status=UserWallet.VerificationStatus.VERIFIED,
        )

        self.assertIsNotNone(link.verified_at)
        self.assertIsNone(link.revoked_at)

    def test_revoked_link_records_date_and_reason(self):
        user = self.create_user("revoked-wallet-link")

        link = UserWallet.objects.create(
            user=user,
            wallet=self.wallet,
            verification_status=UserWallet.VerificationStatus.REVOKED,
            revocation_reason="The account no longer controls this wallet.",
        )

        self.assertIsNotNone(link.revoked_at)
        self.assertEqual(
            link.revocation_reason,
            "The account no longer controls this wallet.",
        )

    def test_revoked_link_requires_a_reason(self):
        user = self.create_user("revoked-without-reason")

        with self.assertRaises(ValidationError):
            UserWallet.objects.create(
                user=user,
                wallet=self.wallet,
                verification_status=UserWallet.VerificationStatus.REVOKED,
            )


class WalletVerificationChallengeModelTests(TestCase):
    """Verify network binding and one-time challenge lifecycle data."""

    def setUp(self):
        user = User.objects.create_user(
            username="challenge-user",
            password="a-secure-development-password",
        )
        wallet = Wallet.objects.create(
            address="0x1234567890123456789012345678901234567890",
        )
        self.user_wallet = UserWallet.objects.create(user=user, wallet=wallet)
        self.expires_at = timezone.now() + timedelta(minutes=10)

    def create_challenge(self, **overrides):
        values = {
            "user_wallet": self.user_wallet,
            "message": "SojaCoin wallet ownership challenge v1",
            "digest": "0x" + "ab" * 32,
            "domain": "sojacoin.local",
            "chain_id": 31337,
            "expires_at": self.expires_at,
        }
        values.update(overrides)
        return WalletVerificationChallenge.objects.create(**values)

    def test_pending_challenge_is_bound_to_a_network_and_can_expire(self):
        challenge = self.create_challenge()

        self.assertEqual(challenge.chain_id, 31337)
        self.assertEqual(challenge.digest, "0x" + "ab" * 32)
        self.assertFalse(challenge.is_expired())
        self.assertTrue(challenge.can_be_consumed())

    def test_invalid_digest_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_challenge(digest="not-a-bytes32-hash")

    def test_chain_id_is_required_and_positive(self):
        with self.assertRaises(ValidationError):
            self.create_challenge(chain_id=0)

    def test_nonce_is_unique(self):
        first = self.create_challenge()

        with self.assertRaises(ValidationError):
            self.create_challenge(nonce=first.nonce)

    def test_expired_challenge_cannot_be_consumed(self):
        challenge = self.create_challenge(
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        self.assertTrue(challenge.is_expired())
        self.assertFalse(challenge.can_be_consumed())

    def test_consumed_challenge_requires_and_records_a_signature(self):
        challenge = self.create_challenge(
            status=WalletVerificationChallenge.Status.CONSUMED,
            signature="0x" + "11" * 65,
        )

        self.assertIsNotNone(challenge.consumed_at)
        self.assertTrue(challenge.signature)
        self.assertFalse(challenge.can_be_consumed())

    def test_consumed_challenge_cannot_be_reopened(self) -> None:
        challenge = self.create_challenge(
            status=WalletVerificationChallenge.Status.CONSUMED,
            signature="0x" + "11" * 65,
        )
        challenge.status = WalletVerificationChallenge.Status.PENDING
        challenge.signature = ""
        challenge.consumed_at = None

        with self.assertRaises(ValidationError):
            challenge.save()

    def test_cancelled_challenge_requires_and_records_a_reason(self):
        challenge = self.create_challenge(
            status=WalletVerificationChallenge.Status.CANCELLED,
            cancellation_reason="The user rejected the wallet request.",
        )

        self.assertIsNotNone(challenge.cancelled_at)
        self.assertEqual(
            challenge.cancellation_reason,
            "The user rejected the wallet request.",
        )
        self.assertFalse(challenge.can_be_consumed())

    def test_cancelled_challenge_requires_a_reason(self):
        with self.assertRaises(ValidationError):
            self.create_challenge(
                status=WalletVerificationChallenge.Status.CANCELLED,
            )


class WalletAccessRuleTests(TestCase):
    """Verify wallet ownership, verification and one-time challenge rules."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="wallet-rule-user",
            password="a-secure-development-password",
        )
        self.other_user = User.objects.create_user(
            username="other-wallet-rule-user",
            password="a-secure-development-password",
        )
        self.wallet = Wallet.objects.create(
            address="0x1234567890123456789012345678901234567890",
        )
        self.user_wallet = UserWallet.objects.create(
            user=self.user,
            wallet=self.wallet,
        )
        self.expires_at = timezone.now() + timedelta(minutes=10)

    def create_challenge(
        self,
        **overrides: object,
    ) -> WalletVerificationChallenge:
        """Create a valid pending challenge for the test wallet link."""

        values: dict[str, object] = {
            "user_wallet": self.user_wallet,
            "message": "SojaCoin wallet ownership challenge v1",
            "digest": "0x" + "ab" * 32,
            "domain": "sojacoin.local",
            "chain_id": 31337,
            "expires_at": self.expires_at,
        }
        values.update(overrides)
        return WalletVerificationChallenge.objects.create(**values)

    def test_wallet_link_is_scoped_to_the_authenticated_user(self) -> None:
        with self.assertRaises(Http404):
            get_user_wallet(self.other_user, self.user_wallet.pk)

    def test_pending_wallet_cannot_be_used_for_an_operation(self) -> None:
        with self.assertRaises(PermissionDenied):
            require_verified_wallet_for_operation(self.user, self.user_wallet.pk)

    def test_verified_wallet_can_be_used_when_account_and_wallet_are_active(self) -> None:
        self.user_wallet.verification_status = UserWallet.VerificationStatus.VERIFIED
        self.user_wallet.save()

        result = require_verified_wallet_for_operation(self.user, self.user_wallet.pk)

        self.assertEqual(result.pk, self.user_wallet.pk)

    def test_inactive_wallet_cannot_be_used_even_when_link_is_verified(self) -> None:
        self.user_wallet.verification_status = UserWallet.VerificationStatus.VERIFIED
        self.user_wallet.save()
        self.wallet.active = False
        self.wallet.save()

        with self.assertRaises(PermissionDenied):
            require_verified_wallet(self.user_wallet)

    def test_expired_challenge_cannot_be_consumed(self) -> None:
        challenge = self.create_challenge(
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        with self.assertRaises(PermissionDenied):
            require_consumable_challenge(challenge)

    def test_invalid_signature_keeps_challenge_and_link_pending(self) -> None:
        challenge = self.create_challenge()

        def reject_signature(
            current_challenge: WalletVerificationChallenge,
            signature: str,
        ) -> bool:
            return False

        with self.assertRaises(ValidationError):
            consume_wallet_challenge(
                self.user,
                challenge.pk,
                "0x" + "11" * 65,
                signature_validator=reject_signature,
            )

        challenge.refresh_from_db()
        self.user_wallet.refresh_from_db()
        self.assertEqual(
            challenge.status,
            WalletVerificationChallenge.Status.PENDING,
        )
        self.assertEqual(
            self.user_wallet.verification_status,
            UserWallet.VerificationStatus.PENDING,
        )

    def test_valid_signature_consumes_challenge_and_verifies_link(self) -> None:
        challenge = self.create_challenge()

        def accept_signature(
            current_challenge: WalletVerificationChallenge,
            signature: str,
        ) -> bool:
            return current_challenge.digest == "0x" + "ab" * 32 and bool(signature)

        consumed = consume_wallet_challenge(
            self.user,
            challenge.pk,
            "0x" + "11" * 65,
            signature_validator=accept_signature,
        )

        consumed.refresh_from_db()
        self.user_wallet.refresh_from_db()
        self.assertEqual(
            consumed.status,
            WalletVerificationChallenge.Status.CONSUMED,
        )
        self.assertEqual(
            self.user_wallet.verification_status,
            UserWallet.VerificationStatus.VERIFIED,
        )
        self.assertIsNotNone(consumed.consumed_at)
        self.assertIsNotNone(self.user_wallet.verified_at)

    def test_consumed_challenge_cannot_be_replayed(self) -> None:
        challenge = self.create_challenge()

        def accept_signature(
            current_challenge: WalletVerificationChallenge,
            signature: str,
        ) -> bool:
            return True

        consume_wallet_challenge(
            self.user,
            challenge.pk,
            "0x" + "11" * 65,
            signature_validator=accept_signature,
        )

        with self.assertRaises(PermissionDenied):
            consume_wallet_challenge(
                self.user,
                challenge.pk,
                "0x" + "22" * 65,
                signature_validator=accept_signature,
            )

    def test_inactive_account_cannot_read_wallet_or_challenge(self) -> None:
        self.user.profile.active = False
        self.user.profile.save()
        challenge = self.create_challenge()

        with self.assertRaises(Http404):
            get_user_wallet(self.user, self.user_wallet.pk)
        with self.assertRaises(Http404):
            get_user_challenge(self.user, challenge.pk)

    def test_challenge_lookup_is_scoped_to_the_authenticated_user(self) -> None:
        challenge = self.create_challenge()

        with self.assertRaises(Http404):
            get_user_challenge(self.other_user, challenge.pk)


class OperationRuleTests(TestCase):
    """Verify shared invariants used by future operation apps."""

    def test_cpr_requires_registered_producer_and_creditor(self) -> None:
        require_registered_participants(True, True)

        with self.assertRaises(ValidationError):
            require_registered_participants(True, False)

    def test_terms_are_required_only_when_the_operation_uses_them(self) -> None:
        require_accepted_terms(True, operation_name="CPR")

        with self.assertRaises(ValidationError):
            require_accepted_terms(False, operation_name="CPR")

    def test_cpr_balance_cannot_be_changed_manually(self) -> None:
        require_cpr_balance_unchanged(400_000_000, 400_000_000)

        with self.assertRaises(ValidationError):
            require_cpr_balance_unchanged(400_000_000, 300_000_000)

    def test_net_weight_is_calculated_from_gross_tare_and_deductions(self) -> None:
        self.assertEqual(
            calculate_net_weight_kg(
                gross_weight_kg=1_000,
                tare_weight_kg=100,
                moisture_deduction_kg=20,
                impurities_deduction_kg=10,
                damaged_grain_deduction_kg=5,
                other_deduction_kg=15,
            ),
            850,
        )

        with self.assertRaises(ValidationError):
            calculate_net_weight_kg(100, 90, other_deduction_kg=11)

    def test_weighing_ticket_hash_is_normalized_and_single_use(self) -> None:
        used_hashes: set[str] = set()

        def is_used(ticket_hash: str) -> bool:
            return ticket_hash in used_hashes

        ticket_hash = "0x" + "AB" * 32
        normalized_hash = require_unique_weighing_ticket(
            ticket_hash,
            is_used=is_used,
        )
        used_hashes.add(normalized_hash)

        self.assertEqual(normalized_hash, "0x" + "ab" * 32)
        with self.assertRaises(ValidationError):
            require_unique_weighing_ticket(ticket_hash, is_used=is_used)

    def test_batch_requires_eligible_receipts_from_one_producer(self) -> None:
        producer = "0x1234567890123456789012345678901234567890"
        result = require_compatible_batch(
            [
                BatchReceiptCandidate(1, producer),
                BatchReceiptCandidate(2, producer.upper()),
            ]
        )
        self.assertEqual(result, producer.lower())

        with self.assertRaises(ValidationError):
            require_compatible_batch(
                [
                    BatchReceiptCandidate(1, producer),
                    BatchReceiptCandidate(
                        2,
                        "0xabcdef0123456789abcdef0123456789abcdef01",
                    ),
                ]
            )

    def test_batch_rejects_duplicate_or_ineligible_receipts(self) -> None:
        producer = "0x1234567890123456789012345678901234567890"

        with self.assertRaises(ValidationError):
            require_compatible_batch(
                [
                    BatchReceiptCandidate(1, producer),
                    BatchReceiptCandidate(1, producer),
                ]
            )
        with self.assertRaises(ValidationError):
            require_compatible_batch(
                [BatchReceiptCandidate(1, producer, eligible=False)]
            )

    def test_issuance_requires_approved_batch(self) -> None:
        require_approved_batch_for_issuance(ISSUANCE_BATCH_APPROVED)

        with self.assertRaises(ValidationError):
            require_approved_batch_for_issuance("pending_approval")

    def test_tokens_are_visible_only_after_confirmed_transaction(self) -> None:
        self.assertTrue(can_display_issued_tokens(BLOCKCHAIN_TRANSACTION_CONFIRMED))
        self.assertFalse(can_display_issued_tokens("submitted"))

        with self.assertRaises(ValidationError):
            require_confirmed_issuance("failed")
