"""Automated tests for producer registration and remote terms acceptance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from eth_account import Account
from web3 import Web3

from blockchain.models import (
    PARTICIPANT_REGISTRATION_OPERATION_TYPE,
    BlockchainTransaction,
    ContractInstance,
    Network,
)
from blockchain.services.transaction_confirmation import DecodedContractEvent
from blockchain.services.transactions import (
    confirm_transaction,
    fail_transaction,
    mark_transaction_reorged,
    submit_transaction,
)
from companies.models import Company, CompanyMembership, MembershipRole

from . import services
from .models import (
    Participant,
    ParticipantAcceptanceChallenge,
    ParticipantAcceptanceLink,
    ParticipantRegistrationAttempt,
    ParticipantTerms,
)


User = get_user_model()
OWNER_ACCOUNT = Account.from_key("0x" + "01" * 32)
PRODUCER_ACCOUNT = Account.from_key("0x" + "02" * 32)
OTHER_ACCOUNT = Account.from_key("0x" + "03" * 32)
PROPOSAL_HASH = "0x" + "a1" * 32
ACCEPTANCE_HASH = "0x" + "a2" * 32
BLOCK_HASH = "0x" + "b1" * 32
PREPARED_PROPOSAL_DATA = "0x12345678"
PREPARED_ACCEPTANCE_DATA = "0xabcdef01"


class FakeContractCall:
    """Provide the small Web3 call interface needed by service unit tests."""

    def __init__(self, value: object, encoded_data: str) -> None:
        self.value = value
        self.encoded_data = encoded_data

    def call(self) -> object:
        """Return a configured RPC read result."""

        return self.value() if callable(self.value) else self.value

    def _encode_transaction_data(self) -> str:
        """Return the saved calldata for a prepared wallet request."""

        return self.encoded_data


class FakeContractFunctions:
    """Return callable fake contract functions with configurable state reads."""

    def __init__(self, values: dict[str, object], encoded_data: dict[str, str]) -> None:
        self.values = values
        self.encoded_data = encoded_data

    def __getattr__(self, name: str):  # noqa: ANN204 - mirrors Web3's dynamic API.
        def call_function(*args: object) -> FakeContractCall:
            value = self.values.get(name)
            if callable(value):
                value = lambda: self.values[name](*args)
            return FakeContractCall(
                value,
                self.encoded_data.get(name, PREPARED_PROPOSAL_DATA),
            )

        return call_function


class ParticipantFlowTestCase(TestCase):
    """Build one authorized company, one SojaCoin and two test wallets."""

    def setUp(self) -> None:
        self.company = Company.objects.create(
            legal_name="Campo Claro Armazens Ltda.",
            trade_name="Campo Claro",
            cnpj="11222333000181",
        )
        self.grantor = User.objects.create_user(
            username="company-admin",
            password="test-password",
        )
        self.operator = User.objects.create_user(
            username="registration-operator",
            password="test-password",
        )
        membership = CompanyMembership.objects.create(
            company=self.company,
            user=self.operator,
        )
        MembershipRole.objects.create(
            membership=membership,
            role=MembershipRole.Role.REGISTRATION,
            granted_by=self.grantor,
        )
        self.network = Network.objects.create(
            name="Anvil local",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )
        self.contract_instance = ContractInstance.objects.create(
            company=self.company,
            network=self.network,
            kind=ContractInstance.Kind.SOJA_COIN,
            address="0x" + "11" * 20,
            version="test-artifact-v1",
        )
        self.participant, self.terms = services.create_participant(
            user=self.operator,
            company_id=self.company.pk,
            name="Produtor de Teste",
            wallet_address=PRODUCER_ACCOUNT.address,
        )
        self.web3 = Mock()
        self.web3.eth.get_code.return_value = b""
        self.contract = self.make_contract()

    def make_contract(
        self,
        *,
        owner_address: str | None = None,
        registered: bool = False,
        pending: bool = False,
        nonce: int = 7,
        deposit_authorized: bool = False,
        terms_hash: str | None = None,
    ) -> SimpleNamespace:
        """Create a fake contract whose read calls mirror the named state."""

        saved_terms_hash = terms_hash or self.terms.terms_hash

        def acceptance_digest(wallet: str, deadline: int) -> bytes:
            typed_data = services._build_acceptance_typed_data(
                participant_address=wallet,
                terms_hash=saved_terms_hash,
                nonce=nonce,
                deadline=deadline,
                chain_id=self.network.chain_id,
                contract_address=self.contract_instance.address_normalized,
            )
            _signable, digest = services._typed_data_digest(typed_data)
            return bytes.fromhex(digest[2:])

        values: dict[str, object] = {
            "owner": owner_address or OWNER_ACCOUNT.address,
            "participantRegistered": registered,
            "participantRegistrationPending": pending,
            "participationTermsHashByParticipant": bytes.fromhex(saved_terms_hash[2:]),
            "participationTermsNonces": nonce,
            "participationTermsAcceptanceHash": acceptance_digest,
            "depositAuthorizationActive": deposit_authorized,
        }
        encoded = {
            "proposeParticipantRegistration": PREPARED_PROPOSAL_DATA,
            "acceptParticipationTerms": PREPARED_ACCEPTANCE_DATA,
        }
        return SimpleNamespace(
            functions=FakeContractFunctions(values, encoded),
            address=self.contract_instance.address_normalized,
        )

    def mock_rpc(self):  # noqa: ANN201 - returns patch context managers for a test fixture.
        """Patch network access and contract binding for deterministic service tests."""

        return (
            patch("participants.services.connect_to_rpc", return_value=self.web3),
            patch("participants.services._get_contract", return_value=self.contract),
        )

    def prepare_proposal(self):  # noqa: ANN201 - test helper returns service result.
        """Prepare the owner proposal with the company's active deployment."""

        connect_patch, contract_patch = self.mock_rpc()
        with connect_patch, contract_patch:
            return services.prepare_participant_proposal(
                user=self.operator,
                company_id=self.company.pk,
                participant_id=self.participant.pk,
                terms_id=self.terms.pk,
                network_id=self.network.pk,
                owner_address=OWNER_ACCOUNT.address,
                wallet_chain_id=self.network.chain_id,
            )

    def mark_proposal_as_confirmed(self):
        """Create chain-confirmed proposal evidence for acceptance-link tests."""

        prepared = self.prepare_proposal()
        attempt = prepared.attempt
        services.mark_participant_submission_started(attempt.pk)
        submitted = submit_transaction(attempt.transaction, PROPOSAL_HASH)
        confirm_transaction(submitted, block_number=1, block_hash=BLOCK_HASH)
        self.terms.refresh_from_db()
        self.contract.functions.values["participantRegistrationPending"] = True
        self.terms.status = ParticipantTerms.Status.AWAITING_ACCEPTANCE
        self.terms.proposal_confirmed_at = timezone.now()
        self.terms.save(update_fields=("status", "proposal_confirmed_at", "updated_at"))
        return attempt

    def create_link(self) -> tuple[ParticipantAcceptanceLink, str]:
        """Create the producer's one-use acceptance link after proposal evidence."""

        return services.create_participant_acceptance_link(
            user=self.operator,
            company_id=self.company.pk,
            participant_id=self.participant.pk,
            terms_id=self.terms.pk,
        )

    def create_challenge(self, token: str) -> ParticipantAcceptanceChallenge:
        """Prepare and persist an EIP-712 signature challenge for the producer."""

        connect_patch, contract_patch = self.mock_rpc()
        with connect_patch, contract_patch:
            return services.create_participant_acceptance_challenge(
                token=token,
                wallet_address=PRODUCER_ACCOUNT.address,
                wallet_chain_id=self.network.chain_id,
            ).challenge

    def sign_challenge(self, challenge: ParticipantAcceptanceChallenge) -> str:
        """Sign the exact stored typed-data challenge using the test producer key."""

        signature = Account.sign_typed_data(
            PRODUCER_ACCOUNT.key,
            full_message=challenge.typed_data,
        ).signature.hex()
        return signature if signature.startswith("0x") else f"0x{signature}"

    def prepare_acceptance(self, token: str, challenge: ParticipantAcceptanceChallenge):
        """Validate a producer signature and persist its acceptance transaction."""

        connect_patch, contract_patch = self.mock_rpc()
        with connect_patch, contract_patch:
            return services.prepare_participant_acceptance(
                token=token,
                challenge_id=challenge.pk,
                signature=self.sign_challenge(challenge),
                wallet_address=PRODUCER_ACCOUNT.address,
                wallet_chain_id=self.network.chain_id,
            )

    def attach_valid_chain_transaction(
        self,
        attempt: ParticipantRegistrationAttempt,
        transaction_hash: str,
    ) -> ParticipantRegistrationAttempt:
        """Mark a wallet prompt and register an RPC transaction matching calldata."""

        # Public acceptance actions prove the bearer link; the raw token exists only
        # as transient test input and is never persisted in a model field.
        token = getattr(attempt, "_test_access_token", None)
        if attempt.kind == ParticipantRegistrationAttempt.Kind.ACCEPTANCE:
            services.mark_participant_submission_started(attempt.pk, access_token=token)
        else:
            services.mark_participant_submission_started(attempt.pk)

        self.web3.eth.get_transaction.return_value = {
            "chainId": self.network.chain_id,
            "from": attempt.sender_address,
            "to": attempt.transaction.contract.address_normalized,
            "input": attempt.prepared_data,
            "value": 0,
        }
        with patch("participants.services.connect_to_rpc", return_value=self.web3):
            return services.register_participant_transaction_hash(
                attempt.pk,
                transaction_hash,
                access_token=token if attempt.kind == ParticipantRegistrationAttempt.Kind.ACCEPTANCE else None,
            )


class ParticipantRecordsAndViewsTests(ParticipantFlowTestCase):
    """Cover company scoping, wallet uniqueness, immutable terms and public links."""

    def test_create_saves_normalized_wallet_and_reproducible_test_term_hash(self) -> None:
        """The text users see hashes to the exact value kept for the proposal."""

        self.assertEqual(
            self.participant.wallet_address_normalized,
            PRODUCER_ACCOUNT.address.lower(),
        )
        self.assertEqual(
            self.terms.terms_hash,
            Web3.to_hex(Web3.keccak(text=self.terms.content)),
        )
        self.assertIn("TERMO DE TESTE — SEM VALIDADE JURÍDICA", self.terms.content)

    def test_same_wallet_is_rejected_case_insensitively_within_company(self) -> None:
        """A checksum variation cannot create a second company producer."""

        with self.assertRaises(ValidationError):
            Participant.objects.create(
                company=self.company,
                name="Duplicate Producer",
                wallet_address=PRODUCER_ACCOUNT.address.upper().replace("0X", "0x"),
                created_by=self.operator,
            )

    def test_same_wallet_can_belong_to_a_different_company(self) -> None:
        """Producer identity is scoped to the company, not globally shared."""

        other_company = Company.objects.create(
            legal_name="Other Company Ltda.",
            cnpj="11444777000323",
        )
        other = Participant.objects.create(
            company=other_company,
            name="Same Wallet, Different Company",
            wallet_address=PRODUCER_ACCOUNT.address,
            created_by=self.operator,
        )
        self.assertNotEqual(other.company_id, self.participant.company_id)

    def test_terms_text_and_hash_are_immutable(self) -> None:
        """An issued hash cannot silently point to changed text."""

        self.terms.content += "Texto editado depois da proposta."
        with self.assertRaisesRegex(ValidationError, "imutáveis"):
            self.terms.save()

    def test_company_pages_require_membership_and_scope_the_list(self) -> None:
        """A user sees only participants for the company in the authorized URL."""

        client = Client()
        client.force_login(self.operator)
        response = client.get(reverse("participants:list", args=[self.company.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.participant.name)

        other_company = Company.objects.create(
            legal_name="Private Company Ltda.",
            cnpj="11444777000404",
        )
        self.assertEqual(
            client.get(reverse("participants:list", args=[other_company.pk])).status_code,
            404,
        )

    def test_user_without_registration_role_cannot_create_producer(self) -> None:
        """A company membership alone does not authorize producer creation."""

        unprivileged = User.objects.create_user(username="read-only", password="test-password")
        CompanyMembership.objects.create(company=self.company, user=unprivileged)
        client = Client()
        client.force_login(unprivileged)
        response = client.post(
            reverse("participants:create", args=[self.company.pk]),
            {"name": "Unauthorized", "wallet_address": OTHER_ACCOUNT.address},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Participant.objects.filter(name="Unauthorized").exists())

    def test_public_invalid_link_shows_only_an_unavailable_message(self) -> None:
        """A guessed code cannot expose another producer's company or terms."""

        response = Client().get(reverse("participants:accept", args=["x" * 48]))
        self.assertEqual(response.status_code, 410)
        self.assertNotIn(self.company.trade_name.encode(), response.content)

    def test_csrf_protects_public_acceptance_post(self) -> None:
        """Producer wallet actions are POST-only and require the normal CSRF token."""

        client = Client(enforce_csrf_checks=True)
        response = client.post(
            reverse("participants:acceptance_challenge", args=["x" * 48]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)


class ParticipantProposalTests(ParticipantFlowTestCase):
    """Cover proposal preparation, wallet-origin data and chain reconciliation."""

    def test_owner_proposal_is_saved_before_wallet_send_and_reused_once(self) -> None:
        """A repeated prepare request returns its open attempt instead of duplicating it."""

        prepared = self.prepare_proposal()
        repeated = self.prepare_proposal()
        self.assertEqual(prepared.attempt.pk, repeated.attempt.pk)
        self.assertEqual(ParticipantRegistrationAttempt.objects.count(), 1)
        self.assertEqual(prepared.attempt.transaction.operation_type, PARTICIPANT_REGISTRATION_OPERATION_TYPE)
        self.assertEqual(prepared.attempt.transaction.status, BlockchainTransaction.Status.PREPARED)
        self.terms.refresh_from_db()
        self.assertEqual(self.terms.status, ParticipantTerms.Status.PROPOSAL_PREPARED)

    def test_wrong_wallet_owner_and_wrong_network_are_rejected(self) -> None:
        """The current SojaCoin owner and saved chain must authorize the proposal."""

        with self.assertRaises(ValidationError):
            services.prepare_participant_proposal(
                user=self.operator,
                company_id=self.company.pk,
                participant_id=self.participant.pk,
                terms_id=self.terms.pk,
                network_id=self.network.pk,
                owner_address=OWNER_ACCOUNT.address,
                wallet_chain_id=1,
            )

        self.contract.functions.values["owner"] = OTHER_ACCOUNT.address
        connect_patch, contract_patch = self.mock_rpc()
        with connect_patch, contract_patch, self.assertRaises(PermissionDenied):
            self.prepare_proposal()
        self.assertFalse(ParticipantRegistrationAttempt.objects.exists())

    def test_registered_or_pending_wallet_cannot_receive_another_proposal(self) -> None:
        """Existing chain registration state prevents a second proposal."""

        for state_name in ("participantRegistered", "participantRegistrationPending"):
            self.contract.functions.values["participantRegistered"] = False
            self.contract.functions.values["participantRegistrationPending"] = False
            self.contract.functions.values[state_name] = True
            with self.subTest(state=state_name), self.assertRaises(ValidationError):
                self.prepare_proposal()
            self.assertFalse(ParticipantRegistrationAttempt.objects.exists())

    def test_explicit_rejection_releases_attempt_but_uncertain_send_does_not(self) -> None:
        """Only a clear wallet rejection closes the prepared attempt for retry."""

        uncertain = self.prepare_proposal().attempt
        services.mark_participant_submission_started(uncertain.pk)
        same_attempt, send_allowed = services.mark_participant_submission_started(uncertain.pk)
        self.assertEqual(same_attempt.pk, uncertain.pk)
        self.assertFalse(send_allowed)
        self.assertTrue(uncertain.__class__.objects.get(pk=uncertain.pk).is_open)

        services.reject_participant_attempt(uncertain.pk)
        uncertain.refresh_from_db()
        self.terms.refresh_from_db()
        self.assertFalse(uncertain.is_open)
        self.assertEqual(uncertain.transaction.status, BlockchainTransaction.Status.FAILED)
        self.assertEqual(self.terms.status, ParticipantTerms.Status.FAILED)

        retry = self.prepare_proposal()
        self.assertNotEqual(retry.attempt.pk, uncertain.pk)

    def test_hash_requires_exact_rpc_sender_destination_data_and_chain(self) -> None:
        """A hash is stored only when RPC transaction fields match the prepared call."""

        attempt = self.prepare_proposal().attempt
        services.mark_participant_submission_started(attempt.pk)
        self.web3.eth.get_transaction.return_value = {
            "chainId": self.network.chain_id,
            "from": attempt.sender_address,
            "to": attempt.transaction.contract.address_normalized,
            "input": "0xdeadbeef",
            "value": 0,
        }
        with patch("participants.services.connect_to_rpc", return_value=self.web3):
            with self.assertRaisesRegex(ValidationError, "dados enviados"):
                services.register_participant_transaction_hash(attempt.pk, PROPOSAL_HASH)
        attempt.transaction.refresh_from_db()
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.PREPARED)

        self.web3.eth.get_transaction.return_value["input"] = attempt.prepared_data
        with patch("participants.services.connect_to_rpc", return_value=self.web3):
            saved = services.register_participant_transaction_hash(attempt.pk, PROPOSAL_HASH)
            repeated = services.register_participant_transaction_hash(attempt.pk, PROPOSAL_HASH)
        self.assertEqual(saved.transaction.status, BlockchainTransaction.Status.SUBMITTED)
        self.assertEqual(repeated.transaction.transaction_hash, PROPOSAL_HASH)

    def test_wrong_rpc_sender_or_chain_is_rejected(self) -> None:
        """The RPC response cannot attach a transaction from another wallet or chain."""

        attempt = self.prepare_proposal().attempt
        services.mark_participant_submission_started(attempt.pk)
        fields = {
            "chainId": self.network.chain_id,
            "from": OTHER_ACCOUNT.address,
            "to": attempt.transaction.contract.address_normalized,
            "input": attempt.prepared_data,
            "value": 0,
        }
        self.web3.eth.get_transaction.return_value = fields
        with patch("participants.services.connect_to_rpc", return_value=self.web3):
            with self.assertRaisesRegex(ValidationError, "carteira que enviou"):
                services.register_participant_transaction_hash(attempt.pk, PROPOSAL_HASH)
            fields["from"] = attempt.sender_address
            fields["chainId"] = 1
            with self.assertRaisesRegex(ValidationError, "outra rede"):
                services.register_participant_transaction_hash(attempt.pk, PROPOSAL_HASH)

    def test_proposal_confirmation_requires_matching_event_and_is_idempotent(self) -> None:
        """Only a matching event plus saved contract state opens the producer link step."""

        attempt = self.prepare_proposal().attempt
        self.attach_valid_chain_transaction(attempt, PROPOSAL_HASH)
        terms_hash_bytes = bytes.fromhex(self.terms.terms_hash[2:])
        event = DecodedContractEvent(
            event_name="ParticipantRegistrationProposed",
            transaction_hash=PROPOSAL_HASH,
            log_index=0,
            block_number=1,
            block_hash=BLOCK_HASH,
            args={
                "walletAddress": PRODUCER_ACCOUNT.address,
                "participationTermsHash": terms_hash_bytes,
                "proposedBy": OWNER_ACCOUNT.address,
            },
        )
        self.contract.functions.values["participantRegistrationPending"] = True

        def confirm_with_event(transaction_id, *, event_handler, required_event_names):
            self.assertEqual(required_event_names, ("ParticipantRegistrationProposed",))
            transaction_record = BlockchainTransaction.objects.get(pk=transaction_id)
            event_handler(transaction_record, event)
            return confirm_transaction(
                transaction_record,
                block_number=1,
                block_hash=BLOCK_HASH,
            )

        with (
            patch("participants.services.connect_to_rpc", return_value=self.web3),
            patch("participants.services._get_contract", return_value=self.contract),
            patch("participants.services.confirm_transaction_from_chain", side_effect=confirm_with_event),
        ):
            result = services.confirm_participant_attempt(attempt.pk)
            self.assertEqual(result.status, "proposal_confirmed")
            self.terms.refresh_from_db()
            self.assertEqual(self.terms.status, ParticipantTerms.Status.AWAITING_ACCEPTANCE)

            # The existing confirmation is safe to query again; no second row is created.
            attempts_before = ParticipantRegistrationAttempt.objects.count()
            transactions_before = BlockchainTransaction.objects.count()
            with patch(
                "participants.services.confirm_transaction_from_chain",
                return_value=result.transaction,
            ):
                repeated = services.confirm_participant_attempt(attempt.pk)
            self.assertEqual(repeated.status, "proposal_confirmed")
            self.assertEqual(ParticipantRegistrationAttempt.objects.count(), attempts_before)
            self.assertEqual(BlockchainTransaction.objects.count(), transactions_before)

    def test_pending_and_reverted_proposal_receipts_keep_clear_states(self) -> None:
        """A missing receipt stays pending; a reverted receipt frees a new attempt."""

        pending_attempt = self.prepare_proposal().attempt
        services.mark_participant_submission_started(pending_attempt.pk)
        submit_transaction(pending_attempt.transaction, PROPOSAL_HASH)
        with (
            patch("participants.services.connect_to_rpc", return_value=self.web3),
            patch("participants.services._get_contract", return_value=self.contract),
            patch("participants.services.confirm_transaction_from_chain", return_value=None),
        ):
            result = services.confirm_participant_attempt(pending_attempt.pk)
        self.assertEqual(result.status, "pending")
        self.assertTrue(pending_attempt.__class__.objects.get(pk=pending_attempt.pk).is_open)

        with (
            patch("participants.services.connect_to_rpc", return_value=self.web3),
            patch("participants.services._get_contract", return_value=self.contract),
            patch(
                "participants.services.confirm_transaction_from_chain",
                side_effect=lambda transaction_id, **kwargs: fail_transaction(
                    BlockchainTransaction.objects.get(pk=transaction_id),
                    "reverted receipt",
                ),
            ),
        ):
            failed = services.confirm_participant_attempt(pending_attempt.pk)
        self.assertEqual(failed.status, "failed")
        self.terms.refresh_from_db()
        self.assertEqual(self.terms.status, ParticipantTerms.Status.FAILED)


class ParticipantAcceptanceTests(ParticipantFlowTestCase):
    """Cover one-use links, EIP-712 ownership and producer-sent transactions."""

    def setUp(self) -> None:
        super().setUp()
        self.proposal_attempt = self.mark_proposal_as_confirmed()
        self.link, self.token = self.create_link()

    def test_link_stores_only_a_digest_and_exposes_exact_saved_test_terms(self) -> None:
        """The raw bearer secret is returned once and the page reveals one proposal."""

        self.assertNotEqual(self.link.token_hash, self.token)
        self.assertEqual(self.link.token_hash, services._token_digest(self.token))
        link = services.get_participant_acceptance_link(self.token)
        self.assertEqual(link.terms.content, self.terms.content)
        response = Client().get(reverse("participants:accept", args=[self.token]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.terms.content.splitlines()[0])
        self.assertContains(response, PRODUCER_ACCOUNT.address.lower())

    def test_cancelled_and_expired_links_cannot_be_opened(self) -> None:
        """An operator can revoke an unused link and an expired link is rejected."""

        services.cancel_participant_acceptance_link(
            user=self.operator,
            company_id=self.company.pk,
            participant_id=self.participant.pk,
            link_id=self.link.pk,
        )
        with self.assertRaisesRegex(ValidationError, "cancelado"):
            services.get_participant_acceptance_link(self.token)

        expired_token = "expired-test-token-12345678901234567890"
        expired = ParticipantAcceptanceLink.objects.create(
            terms=self.terms,
            token_hash=services._token_digest(expired_token),
            expires_at=timezone.now() - timedelta(seconds=1),
            created_by=self.operator,
        )
        self.assertFalse(expired.is_usable)
        with self.assertRaises(ValidationError):
            services.get_participant_acceptance_link(expired_token)

    def test_new_link_cancels_previous_unused_link(self) -> None:
        """Only the newest bearer link remains valid for this terms version."""

        new_link, new_token = self.create_link()
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.canceled_at)
        with self.assertRaises(ValidationError):
            services.get_participant_acceptance_link(self.token)
        self.assertEqual(services.get_participant_acceptance_link(new_token).pk, new_link.pk)

    def test_eip712_digest_matches_contract_view_and_acceptance_is_prepared(self) -> None:
        """The producer signs the contract's digest, then a separate tx is saved."""

        challenge = self.create_challenge(self.token)
        self.assertEqual(
            services._typed_data_digest(challenge.typed_data)[1],
            challenge.digest,
        )
        prepared = self.prepare_acceptance(self.token, challenge)
        self.terms.refresh_from_db()
        self.assertEqual(prepared.attempt.kind, ParticipantRegistrationAttempt.Kind.ACCEPTANCE)
        self.assertEqual(prepared.attempt.sender_address, PRODUCER_ACCOUNT.address.lower())
        self.assertEqual(prepared.attempt.transaction.operation_type, PARTICIPANT_REGISTRATION_OPERATION_TYPE)
        self.assertEqual(prepared.attempt.transaction.status, BlockchainTransaction.Status.PREPARED)
        self.assertEqual(self.terms.status, ParticipantTerms.Status.ACCEPTANCE_PREPARED)

    def test_wrong_wallet_and_signature_from_another_account_are_rejected(self) -> None:
        """A bearer URL alone cannot authorize a different Ethereum account."""

        connect_patch, contract_patch = self.mock_rpc()
        with connect_patch, contract_patch, self.assertRaises(PermissionDenied):
            services.create_participant_acceptance_challenge(
                token=self.token,
                wallet_address=OTHER_ACCOUNT.address,
                wallet_chain_id=self.network.chain_id,
            )

        challenge = self.create_challenge(self.token)
        wrong_signature = Account.sign_typed_data(
            OTHER_ACCOUNT.key,
            full_message=challenge.typed_data,
        ).signature.hex()
        if not wrong_signature.startswith("0x"):
            wrong_signature = f"0x{wrong_signature}"
        connect_patch, contract_patch = self.mock_rpc()
        with connect_patch, contract_patch, self.assertRaises(PermissionDenied):
            services.prepare_participant_acceptance(
                token=self.token,
                challenge_id=challenge.pk,
                signature=wrong_signature,
                wallet_address=PRODUCER_ACCOUNT.address,
                wallet_chain_id=self.network.chain_id,
            )

    def test_contract_wallet_receives_clear_unsupported_wallet_message(self) -> None:
        """Safe and other contract wallets are not misreported as bad EOA signatures."""

        self.web3.eth.get_code.return_value = b"\x60\x00"
        connect_patch, contract_patch = self.mock_rpc()
        with (
            connect_patch,
            contract_patch,
            self.assertRaisesRegex(ValidationError, "Carteiras de contrato, como Safe"),
        ):
            services.create_participant_acceptance_challenge(
                token=self.token,
                wallet_address=PRODUCER_ACCOUNT.address,
                wallet_chain_id=self.network.chain_id,
            )

    def test_expired_challenge_and_changed_chain_nonce_are_rejected(self) -> None:
        """The signed payload cannot be reused after time or nonce changes."""

        challenge = self.create_challenge(self.token)
        expired_time = datetime.fromtimestamp(
            challenge.deadline + 1,
            tz=datetime_timezone.utc,
        )
        connect_patch, contract_patch = self.mock_rpc()
        with (
            connect_patch,
            contract_patch,
            patch("participants.services.timezone.now", return_value=expired_time),
            self.assertRaisesRegex(ValidationError, "expirou"),
        ):
            services.prepare_participant_acceptance(
                token=self.token,
                challenge_id=challenge.pk,
                signature=self.sign_challenge(challenge),
                wallet_address=PRODUCER_ACCOUNT.address,
                wallet_chain_id=self.network.chain_id,
            )

        fresh_challenge = self.create_challenge(self.token)
        self.contract.functions.values["participationTermsNonces"] = 8
        connect_patch, contract_patch = self.mock_rpc()
        with connect_patch, contract_patch, self.assertRaisesRegex(ValidationError, "nonce mudou"):
            services.prepare_participant_acceptance(
                token=self.token,
                challenge_id=fresh_challenge.pk,
                signature=self.sign_challenge(fresh_challenge),
                wallet_address=PRODUCER_ACCOUNT.address,
                wallet_chain_id=self.network.chain_id,
            )

    def test_acceptance_link_must_match_the_attempt_and_hash_rpc_data(self) -> None:
        """The link token and exact calldata protect the producer transaction record."""

        challenge = self.create_challenge(self.token)
        attempt = self.prepare_acceptance(self.token, challenge).attempt
        attempt._test_access_token = self.token
        services.mark_participant_submission_started(attempt.pk, access_token=self.token)
        self.web3.eth.get_transaction.return_value = {
            "chainId": self.network.chain_id,
            "from": attempt.sender_address,
            "to": attempt.transaction.contract.address_normalized,
            "input": "0xdeadbeef",
            "value": 0,
        }
        with patch("participants.services.connect_to_rpc", return_value=self.web3):
            with self.assertRaises(PermissionDenied):
                services.register_participant_transaction_hash(
                    attempt.pk,
                    ACCEPTANCE_HASH,
                    access_token="wrong-token-that-is-long-enough-to-be-checked",
                )
            with self.assertRaisesRegex(ValidationError, "dados enviados"):
                services.register_participant_transaction_hash(
                    attempt.pk,
                    ACCEPTANCE_HASH,
                    access_token=self.token,
                )
            self.web3.eth.get_transaction.return_value["input"] = attempt.prepared_data
            accepted = services.register_participant_transaction_hash(
                attempt.pk,
                ACCEPTANCE_HASH,
                access_token=self.token,
            )
            repeated = services.register_participant_transaction_hash(
                attempt.pk,
                ACCEPTANCE_HASH,
                access_token=self.token,
            )
        self.assertEqual(accepted.pk, repeated.pk)
        self.assertEqual(accepted.transaction.status, BlockchainTransaction.Status.SUBMITTED)

    def test_confirmed_acceptance_requires_both_events_and_reorg_hides_confirmation(self) -> None:
        """Only both expected events and live contract flags complete enrollment."""

        challenge = self.create_challenge(self.token)
        attempt = self.prepare_acceptance(self.token, challenge).attempt
        attempt._test_access_token = self.token
        self.attach_valid_chain_transaction(attempt, ACCEPTANCE_HASH)
        self.contract.functions.values["participantRegistered"] = True
        self.contract.functions.values["depositAuthorizationActive"] = True
        terms_hash_bytes = bytes.fromhex(self.terms.terms_hash[2:])
        events = [
            DecodedContractEvent(
                event_name="ParticipantRegistered",
                transaction_hash=ACCEPTANCE_HASH,
                log_index=0,
                block_number=2,
                block_hash=BLOCK_HASH,
                args={
                    "walletAddress": PRODUCER_ACCOUNT.address,
                    "participationTermsHash": terms_hash_bytes,
                    "acceptedBy": PRODUCER_ACCOUNT.address,
                },
            ),
            DecodedContractEvent(
                event_name="DepositAuthorizationChanged",
                transaction_hash=ACCEPTANCE_HASH,
                log_index=1,
                block_number=2,
                block_hash=BLOCK_HASH,
                args={
                    "producer": PRODUCER_ACCOUNT.address,
                    "authorized": True,
                    "participationTermsHash": terms_hash_bytes,
                    "changedBy": PRODUCER_ACCOUNT.address,
                },
            ),
        ]

        def confirm_with_events(transaction_id, *, event_handler, required_event_names):
            self.assertEqual(
                set(required_event_names),
                {"ParticipantRegistered", "DepositAuthorizationChanged"},
            )
            transaction_record = BlockchainTransaction.objects.get(pk=transaction_id)
            for event in events:
                event_handler(transaction_record, event)
            return confirm_transaction(
                transaction_record,
                block_number=2,
                block_hash=BLOCK_HASH,
            )

        with (
            patch("participants.services.connect_to_rpc", return_value=self.web3),
            patch("participants.services._get_contract", return_value=self.contract),
            patch("participants.services.confirm_transaction_from_chain", side_effect=confirm_with_events),
        ):
            result = services.confirm_participant_attempt(attempt.pk, access_token=self.token)
        self.assertEqual(result.status, "confirmed")
        self.terms.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(self.terms.status, ParticipantTerms.Status.CONFIRMED)
        self.assertIsNotNone(self.terms.accepted_at)
        self.assertIsNotNone(self.link.used_at)

        # A second check is read-only and does not create another attempt or tx.
        with (
            patch("participants.services.connect_to_rpc", return_value=self.web3),
            patch("participants.services._get_contract", return_value=self.contract),
            patch(
                "participants.services.confirm_transaction_from_chain",
                return_value=result.transaction,
            ),
        ):
            repeated = services.confirm_participant_attempt(attempt.pk, access_token=self.token)
        self.assertEqual(repeated.status, "confirmed")
        self.assertEqual(ParticipantRegistrationAttempt.objects.count(), 2)

        reorged = mark_transaction_reorged(result.transaction, "test reorganization")
        self.terms.refresh_from_db()
        self.assertEqual(reorged.status, BlockchainTransaction.Status.REORGED)
        self.assertEqual(self.terms.status, ParticipantTerms.Status.NEEDS_REVIEW)
        with patch("participants.services.connect_to_rpc") as rpc:
            review = services.confirm_participant_attempt(attempt.pk, access_token=self.token)
        self.assertEqual(review.status, "review")
        rpc.assert_not_called()

    def test_receipt_failure_returns_terms_to_waiting_and_closes_attempt(self) -> None:
        """A reverted acceptance is visible but does not register the producer."""

        challenge = self.create_challenge(self.token)
        attempt = self.prepare_acceptance(self.token, challenge).attempt
        attempt._test_access_token = self.token
        self.attach_valid_chain_transaction(attempt, ACCEPTANCE_HASH)
        with (
            patch("participants.services.connect_to_rpc", return_value=self.web3),
            patch("participants.services._get_contract", return_value=self.contract),
            patch(
                "participants.services.confirm_transaction_from_chain",
                side_effect=lambda transaction_id, **kwargs: fail_transaction(
                    BlockchainTransaction.objects.get(pk=transaction_id),
                    "reverted receipt",
                ),
            ),
        ):
            result = services.confirm_participant_attempt(attempt.pk, access_token=self.token)
        self.terms.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.terms.status, ParticipantTerms.Status.AWAITING_ACCEPTANCE)
        self.assertFalse(attempt.is_open)


class ParticipantReorganizationAndRoutingTests(ParticipantFlowTestCase):
    """Verify shared blockchain lifecycle hooks and route boundaries."""

    def test_proposal_reorganization_moves_term_to_review(self) -> None:
        """A previously confirmed proposal is never represented as current after reorg."""

        attempt = self.mark_proposal_as_confirmed()
        reorged = mark_transaction_reorged(attempt.transaction, "proposal left canonical chain")
        self.terms.refresh_from_db()
        self.assertEqual(reorged.status, BlockchainTransaction.Status.REORGED)
        self.assertEqual(self.terms.status, ParticipantTerms.Status.NEEDS_REVIEW)

    def test_attempt_routes_are_post_only_and_company_scoped(self) -> None:
        """A valid participant URL cannot operate a different participant's attempt."""

        attempt = self.prepare_proposal().attempt
        client = Client()
        client.force_login(self.operator)
        started_url = reverse(
            "participants:attempt_started",
            args=[self.company.pk, self.participant.pk, attempt.pk],
        )
        self.assertEqual(client.get(started_url).status_code, 405)

        other_company = Company.objects.create(
            legal_name="Another SaaS Company Ltda.",
            cnpj="11444777000404",
        )
        CompanyMembership.objects.create(company=other_company, user=self.operator)
        wrong_company_url = reverse(
            "participants:attempt_started",
            args=[other_company.pk, self.participant.pk, attempt.pk],
        )
        self.assertEqual(client.post(wrong_company_url, data="{}", content_type="application/json").status_code, 403)


