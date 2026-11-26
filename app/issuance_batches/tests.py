"""Focused tests for Solidity-compatible batch hashes and guarded lifecycles."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from web3 import Web3

from blockchain.models import BlockchainTransaction, ContractInstance, Network
from blockchain.services.transactions import (
    confirm_transaction,
    fail_transaction,
    mark_transaction_reorged,
    submit_transaction,
)
from blockchain.services.transaction_confirmation import DecodedContractEvent
from companies.models import Company, CompanyMembership, MembershipRole, Unit, UnitAccess
from participants.models import Participant
from receipts.models import Receipt
from receipts.views import ReceiptBrowsingAccessMixin, ReceiptListView

from . import services
from .models import (
    ISSUANCE_BATCH_OPERATION_TYPE,
    IssuanceBatch,
    IssuanceBatchApprovalLink,
    IssuanceBatchAttempt,
    IssuanceBatchAuthorizationChallenge,
    IssuanceBatchReceipt,
)


User = get_user_model()
TEST_ACCOUNT = Account.from_key("0x" + "01" * 32)
OTHER_ACCOUNT = Account.from_key("0x" + "02" * 32)
TEST_SUMMARY_HASH = "0x" + "ab" * 32
TEST_BLOCK_HASH = "0x" + "cd" * 32
TEST_TX_HASH = "0x" + "ef" * 32


class IssuanceBatchEncodingTests(SimpleTestCase):
    """Keep summary hashing and EIP-712 payloads aligned with Solidity."""

    def test_single_receipt_summary_matches_solidity_abi_encoding(self) -> None:
        """Hash the length and every batch field in the exact Solidity order."""

        invoice_hash = "0x" + "11" * 32
        ticket_hash = "0x" + "22" * 32
        crop_year = "0x" + "33" * 32
        first_hash = keccak(abi_encode(["uint256"], [1]))
        expected = keccak(
            abi_encode(
                ["bytes32", "uint256", "uint256", "bytes32", "bytes32", "bytes32", "uint256"],
                [first_hash, 17, 4, bytes.fromhex(crop_year[2:]), bytes.fromhex(invoice_hash[2:]),
                 bytes.fromhex(ticket_hash[2:]), 24_750],
            )
        )

        actual = services.calculate_summary_hash(
            deposit_id=17,
            unit_id=4,
            crop_year=crop_year,
            invoice_key_hash=invoice_hash,
            weighing_ticket_hash=ticket_hash,
            net_weight_kg=24_750,
        )

        self.assertEqual(actual, Web3.to_hex(expected).lower())
        self.assertNotEqual(
            actual,
            services.calculate_summary_hash(
                deposit_id=18,
                unit_id=4,
                crop_year=crop_year,
                invoice_key_hash=invoice_hash,
                weighing_ticket_hash=ticket_hash,
                net_weight_kg=24_750,
            ),
        )

    def test_typed_data_digest_matches_manual_eip712_domain_and_struct_hash(self) -> None:
        """Calculate the same EIP-712 digest using independent ABI primitives."""

        batch = SimpleNamespace(
            network=SimpleNamespace(chain_id=31_337),
            batches_contract=SimpleNamespace(address_normalized="0x" + "44" * 20),
            chain_batch_id=9,
            participant=SimpleNamespace(wallet_address_normalized=TEST_ACCOUNT.address.lower()),
            summary_hash=TEST_SUMMARY_HASH,
            total_net_weight_kg=24_750,
            approval_deadline=1_900_000_000,
        )
        nonce = 7
        typed_data = services._build_typed_data(batch=batch, nonce=nonce)
        signable, actual_digest = services._typed_data_digest(typed_data)

        domain_type_hash = keccak(
            text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
        )
        domain_separator = keccak(
            abi_encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                [
                    domain_type_hash,
                    keccak(text="SojaCoin Token Issuance Batches"),
                    keccak(text="1"),
                    31_337,
                    batch.batches_contract.address_normalized,
                ],
            )
        )
        struct_type_hash = keccak(
            text=(
                "IssuanceBatchAuthorization(uint256 batchId,address producer,bytes32 summaryHash,"
                "uint256 totalNetWeightKg,uint256 nonce,uint256 deadline)"
            )
        )
        struct_hash = keccak(
            abi_encode(
                ["bytes32", "uint256", "address", "bytes32", "uint256", "uint256", "uint256"],
                [
                    struct_type_hash,
                    batch.chain_batch_id,
                    batch.participant.wallet_address_normalized,
                    bytes.fromhex(TEST_SUMMARY_HASH[2:]),
                    batch.total_net_weight_kg,
                    nonce,
                    batch.approval_deadline,
                ],
            )
        )
        expected_digest = Web3.to_hex(keccak(b"\x19\x01" + domain_separator + struct_hash)).lower()

        self.assertEqual(actual_digest, expected_digest)
        signature = "0x" + Account.sign_message(signable, private_key=TEST_ACCOUNT.key).signature.hex()
        self.assertEqual(services._recover_typed_data_signer(typed_data, signature), TEST_ACCOUNT.address.lower())
        wrong_signature = Account.sign_message(
            encode_typed_data(full_message=typed_data),
            private_key=OTHER_ACCOUNT.key,
        ).signature.hex()
        wrong_signature = "0x" + wrong_signature
        self.assertNotEqual(services._recover_typed_data_signer(typed_data, wrong_signature), TEST_ACCOUNT.address.lower())

    def test_summary_rejects_non_positive_ids_or_weight(self) -> None:
        """Reject values that cannot represent an eligible contract receipt."""

        with self.assertRaises(ValidationError):
            services.calculate_summary_hash(
                deposit_id=0,
                unit_id=1,
                crop_year="0x" + "00" * 32,
                invoice_key_hash="0x" + "11" * 32,
                weighing_ticket_hash="0x" + "22" * 32,
                net_weight_kg=1,
            )

    def test_token_amount_formatting_uses_integer_units_without_float_rounding(self) -> None:
        """Render six-decimal ERC-20 values exactly for the Portuguese UI."""

        self.assertEqual(services._format_token_units(412_500_000, 6), "412,5")
        self.assertEqual(services._format_token_units(123_456_789, 6), "123,456789")
        self.assertEqual(services._format_token_units(0, 6), "0")
        self.assertEqual(
            services._format_token_units(2**256 - 1, 18),
            "115792089237316195423570985008687907853269984665640564039457,584007913129639935",
        )

    def test_participant_script_requests_one_transaction_and_no_typed_data_signature(self) -> None:
        """Keep the remote approval page on its single transaction path."""

        script_path = (
            Path(__file__).parent
            / "static"
            / "issuance_batches"
            / "participant_approval.js"
        )
        script = script_path.read_text(encoding="utf-8")
        self.assertIn('method: "eth_sendTransaction"', script)
        self.assertNotIn("eth_signTypedData_v4", script)

    def test_approved_batch_remains_a_receipt_reservation(self) -> None:
        """Keep an approved receipt locked until a later issuance or review flow."""

        self.assertIn(IssuanceBatch.Status.APPROVED, services.ACTIVE_BATCH_STATUSES)


class IssuanceBatchPersistenceTests(TestCase):
    """Exercise protected database rows and batch transaction transitions."""

    def setUp(self) -> None:
        """Build an isolated company, unit and saved contract deployment set."""

        self.company = Company.objects.create(
            legal_name="Batch Test Company Ltda.",
            trade_name="Batch Test",
            cnpj="11222333000181",
        )
        self.user = User.objects.create_user(username="batch-test-user", password="test-password")
        self.network = Network.objects.create(
            name="Anvil Test",
            chain_id=31_337,
            rpc_url="http://127.0.0.1:8545",
        )
        self.unit = Unit.objects.create(
            company=self.company,
            name="Test warehouse",
            code="TEST-WH",
            cnpj="11222333000181",
        )
        self.participant = Participant.objects.create(
            company=self.company,
            name="Test participant",
            wallet_address=TEST_ACCOUNT.address,
            created_by=self.user,
        )
        self.soja_coin = self._contract(ContractInstance.Kind.SOJA_COIN, "11")
        self.registry = self._contract(ContractInstance.Kind.UNIT_REGISTRY, "22")
        self.batches_contract = self._contract(ContractInstance.Kind.ISSUANCE_BATCHES, "33")

    def _contract(self, kind: str, address_byte: str) -> ContractInstance:
        """Create one network-scoped deployment fixture with a unique address."""

        return ContractInstance.objects.create(
            company=self.company,
            network=self.network,
            kind=kind,
            address="0x" + address_byte * 20,
            version="unit-test-artifact-v1",
        )

    def _batch(
        self,
        *,
        status: str = IssuanceBatch.Status.CREATION_PREPARED,
        approval_deadline: int | None = None,
    ) -> IssuanceBatch:
        """Build a saved batch snapshot for persistence and lifecycle tests."""

        return IssuanceBatch.objects.create(
            company=self.company,
            participant=self.participant,
            unit=self.unit,
            network=self.network,
            soja_coin=self.soja_coin,
            unit_registry=self.registry,
            batches_contract=self.batches_contract,
            created_by=self.user,
            creator_address=OTHER_ACCOUNT.address,
            chain_batch_id=1 if status != IssuanceBatch.Status.CREATION_PREPARED else None,
            total_net_weight_kg=24_750,
            approval_deadline=approval_deadline or int(timezone.now().timestamp()) + 86_400,
            summary_hash=TEST_SUMMARY_HASH,
            status=status,
        )

    def _approval_link(self, batch: IssuanceBatch) -> IssuanceBatchApprovalLink:
        """Create a valid private link fixture for one pending batch."""

        return IssuanceBatchApprovalLink.objects.create(
            batch=batch,
            token_hash=hashlib.sha256(f"batch-link-{batch.pk}".encode("utf-8")).hexdigest(),
            expires_at=timezone.now() + timedelta(days=2),
            created_by=self.user,
        )

    def _batch_receipt_row(
        self, batch: IssuanceBatch, *, deposit_id: int = 7
    ) -> IssuanceBatchReceipt:
        """Create the local receipt membership required by a one-load batch."""

        receipt = Receipt.objects.create(
            company=self.company,
            participant=self.participant,
            unit=self.unit,
            crop_year="2026/2027",
            created_by=self.user,
            status=Receipt.Status.BLOCKCHAIN_CONFIRMED,
        )
        return IssuanceBatchReceipt.objects.create(
            batch=batch,
            receipt=receipt,
            deposit_id=deposit_id,
            unit_chain_id=1,
            crop_year_bytes32="0x" + "11" * 32,
            invoice_key_hash="0x" + "22" * 32,
            weighing_ticket_hash="0x" + "33" * 32,
            net_weight_kg=batch.total_net_weight_kg,
        )

    def _challenge(
        self,
        *,
        batch: IssuanceBatch,
        link: IssuanceBatchApprovalLink,
        deadline: int | None = None,
    ) -> IssuanceBatchAuthorizationChallenge:
        """Persist one challenge fixture with a stable nonce and digest."""

        return IssuanceBatchAuthorizationChallenge.objects.create(
            link=link,
            chain_nonce=7,
            deadline=deadline or batch.approval_deadline,
            digest=TEST_SUMMARY_HASH,
            typed_data={"message": {"batchId": batch.chain_batch_id, "nonce": 7}},
        )

    def _transaction(self, batch: IssuanceBatch) -> BlockchainTransaction:
        """Create a prepared transaction for the batch module."""

        return BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            contract=batch.batches_contract,
            operation_type=ISSUANCE_BATCH_OPERATION_TYPE,
            sender_address=OTHER_ACCOUNT.address,
            status=BlockchainTransaction.Status.PREPARED,
        )

    def test_approval_attempt_cannot_mix_another_batch_link_or_challenge(self) -> None:
        """Bind signatures, links, challenges and calls to one immutable batch."""

        batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        other_batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        expires_at = timezone.now() + timedelta(days=2)
        link = IssuanceBatchApprovalLink.objects.create(
            batch=batch,
            token_hash="a1" * 32,
            expires_at=expires_at,
            created_by=self.user,
        )
        other_link = IssuanceBatchApprovalLink.objects.create(
            batch=other_batch,
            token_hash="a2" * 32,
            expires_at=expires_at,
            created_by=self.user,
        )
        challenge = IssuanceBatchAuthorizationChallenge.objects.create(
            link=other_link,
            chain_nonce=0,
            deadline=batch.approval_deadline,
            digest=TEST_SUMMARY_HASH,
            typed_data={"message": {"batchId": batch.chain_batch_id}},
        )
        attempt = IssuanceBatchAttempt(
            batch=batch,
            transaction=self._transaction(batch),
            link=link,
            challenge=challenge,
            kind=IssuanceBatchAttempt.Kind.APPROVE,
            sender_address=self.participant.wallet_address_normalized,
            prepared_data="0x12345678",
            expected_arguments={"batch_id": batch.chain_batch_id},
            signature="0x" + "11" * 65,
        )

        with self.assertRaises(ValidationError):
            attempt.save()

    def test_shared_transaction_lifecycle_closes_attempt_and_reorg_requires_review(self) -> None:
        """Guard sends against unattached attempts and hide a reorganized batch."""

        batch = self._batch()
        record = self._transaction(batch)
        attempt = IssuanceBatchAttempt.objects.create(
            batch=batch,
            transaction=record,
            requested_by=self.user,
            kind=IssuanceBatchAttempt.Kind.CREATE,
            sender_address=OTHER_ACCOUNT.address,
            prepared_data="0x12345678",
            expected_arguments={"deposit_ids": [4], "deadline": batch.approval_deadline},
        )

        with self.assertRaises(ValidationError):
            submit_transaction(record, TEST_TX_HASH)

        attempt.submission_started_at = timezone.now()
        attempt.save(update_fields=("submission_started_at", "updated_at"))
        submitted = submit_transaction(record, TEST_TX_HASH)
        confirmed = confirm_transaction(submitted, 5, TEST_BLOCK_HASH)
        attempt.refresh_from_db()
        self.assertFalse(attempt.is_open)

        mark_transaction_reorged(confirmed, "Test reorganization")
        batch.refresh_from_db()
        self.assertEqual(batch.status, IssuanceBatch.Status.NEEDS_REVIEW)
        self.assertEqual(batch.review_reason, "Test reorganization")

    def test_failed_wallet_attempt_is_closed_and_preserved_for_retry(self) -> None:
        """A definitive wallet refusal releases the open slot without deleting history."""

        batch = self._batch()
        record = self._transaction(batch)
        attempt = IssuanceBatchAttempt.objects.create(
            batch=batch,
            transaction=record,
            requested_by=self.user,
            kind=IssuanceBatchAttempt.Kind.CREATE,
            sender_address=OTHER_ACCOUNT.address,
            prepared_data="0x12345678",
            expected_arguments={"deposit_ids": [4], "deadline": batch.approval_deadline},
            submission_started_at=timezone.now(),
        )

        fail_transaction(record, "Wallet rejected")
        attempt.refresh_from_db()
        record.refresh_from_db()

        self.assertFalse(attempt.is_open)
        self.assertEqual(record.status, BlockchainTransaction.Status.FAILED)
        self.assertEqual(IssuanceBatchAttempt.objects.filter(batch=batch).count(), 1)

    def test_issuance_staff_can_browse_unit_without_receiving_role(self) -> None:
        """The receipt browsing gate accepts issuance access independently."""

        issuer = User.objects.create_user(username="batch-issuer", password="test-password")
        membership = CompanyMembership.objects.create(company=self.company, user=issuer)
        MembershipRole.objects.create(
            membership=membership,
            role=MembershipRole.Role.ISSUANCE,
            granted_by=self.user,
        )
        UnitAccess.objects.create(membership=membership, unit=self.unit, can_issue=True)
        view = ReceiptBrowsingAccessMixin()
        view.company = self.company
        view.request = SimpleNamespace(user=issuer)

        view.check_company_permissions(view.request)
        view.check_unit_browsing_access(self.unit.pk)

        self.assertIsNone(view.receiving_membership)
        self.assertEqual(view.issuance_membership, membership)
        self.unit.chain_unit_id = 1
        self.unit.save(update_fields=("chain_unit_id", "updated_at"))
        receipt_list = ReceiptListView()
        receipt_list.receiving_membership = None
        receipt_list.issuance_membership = membership
        self.assertTrue(receipt_list._has_accessible_registered_units())

        UnitAccess.objects.filter(membership=membership, unit=self.unit).update(can_issue=False)
        self.assertFalse(receipt_list._has_accessible_registered_units())

        other_unit = Unit.objects.create(
            company=self.company,
            name="Unassigned warehouse",
            code="UNASSIGNED-WH",
            cnpj="11444777000100",
        )
        with self.assertRaises(PermissionDenied):
            services.require_issuance_access(issuer, self.company.pk, other_unit.pk)

    def test_transaction_hash_must_match_chain_sender_target_and_calldata(self) -> None:
        """Reject a transaction hash unless every RPC identity field is prepared."""

        batch = self._batch()
        record = self._transaction(batch)
        attempt = IssuanceBatchAttempt.objects.create(
            batch=batch,
            transaction=record,
            requested_by=self.user,
            kind=IssuanceBatchAttempt.Kind.CREATE,
            sender_address=OTHER_ACCOUNT.address,
            prepared_data="0x12345678",
            expected_arguments={"deposit_ids": [4], "deadline": batch.approval_deadline},
            submission_started_at=timezone.now(),
        )
        rpc = MagicMock()
        rpc.eth.get_transaction.return_value = {
            "hash": bytes.fromhex(TEST_TX_HASH[2:]),
            "from": OTHER_ACCOUNT.address,
            "to": batch.batches_contract.address_normalized,
            "input": "0x12345678",
            "value": 0,
            "chainId": self.network.chain_id,
        }

        with patch("issuance_batches.services.connect_to_rpc", return_value=rpc):
            self.assertTrue(services._verify_transaction(attempt, TEST_TX_HASH))
            rpc.eth.get_transaction.return_value["input"] = "0xdeadbeef"
            with self.assertRaises(ValidationError):
                services._verify_transaction(attempt, TEST_TX_HASH)

    def test_uncertain_hash_keeps_the_same_attempt_available_for_recovery(self) -> None:
        """Keep the prepared call when RPC has not indexed its wallet transaction yet."""

        batch = self._batch()
        record = self._transaction(batch)
        attempt = IssuanceBatchAttempt.objects.create(
            batch=batch,
            transaction=record,
            requested_by=self.user,
            kind=IssuanceBatchAttempt.Kind.CREATE,
            sender_address=OTHER_ACCOUNT.address,
            prepared_data="0x12345678",
            expected_arguments={"deposit_ids": [4], "deadline": batch.approval_deadline},
            submission_started_at=timezone.now(),
        )
        with (
            patch("issuance_batches.services.require_issuance_access"),
            patch(
                "issuance_batches.services._verified_operator_wallet",
                return_value=OTHER_ACCOUNT.address.lower(),
            ),
            patch("issuance_batches.services._verify_transaction", side_effect=(False, True)),
        ):
            not_indexed = services.register_batch_transaction_hash(
                attempt=attempt,
                transaction_hash=TEST_TX_HASH,
                user=self.user,
            )
            record.refresh_from_db()
            self.assertIsNone(not_indexed)
            self.assertEqual(record.status, BlockchainTransaction.Status.PREPARED)
            self.assertIsNone(record.transaction_hash)

            submitted = services.register_batch_transaction_hash(
                attempt=attempt,
                transaction_hash=TEST_TX_HASH,
                user=self.user,
            )

        record.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(submitted.status, BlockchainTransaction.Status.SUBMITTED)
        self.assertEqual(record.transaction_hash, TEST_TX_HASH)
        self.assertEqual(batch.status, IssuanceBatch.Status.CREATION_SUBMITTED)

    def test_automatic_approval_prepares_one_recoverable_transaction(self) -> None:
        """Persist approveAndIssue calldata once before the participant wallet opens."""

        batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        row = self._batch_receipt_row(batch)
        raw_token = "single-transaction-approval-link-" + "x" * 40
        link = IssuanceBatchApprovalLink.objects.create(
            batch=batch,
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=timezone.now() + timedelta(days=2),
            created_by=self.user,
        )
        web3 = Mock()
        web3.eth.get_block.return_value = {"timestamp": batch.approval_deadline - 1}
        batch_contract = Mock()
        batch_contract.functions.getBatchDepositIds.return_value.call.return_value = [row.deposit_id]
        transaction_function = Mock()
        transaction_function._encode_transaction_data.return_value = "0x1234abcd"
        batch_contract.functions.approveAndIssueBatch.return_value = transaction_function
        transaction_function.call.return_value = None

        with (
            patch(
                "issuance_batches.services._get_batch_chain",
                return_value=(web3, batch_contract, Mock(), Mock()),
            ),
            patch("issuance_batches.services._check_pending_batch"),
        ):
            prepared = services.prepare_participant_approval_and_issue(
                token=raw_token,
                wallet_address=self.participant.wallet_address,
                wallet_chain_id=self.network.chain_id,
            )
            recovered = services.prepare_participant_approval_and_issue(
                token=raw_token,
                wallet_address=self.participant.wallet_address,
                wallet_chain_id=self.network.chain_id,
            )

        self.assertEqual(prepared.attempt.pk, recovered.attempt.pk)
        self.assertEqual(prepared.attempt.kind, IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE)
        self.assertEqual(prepared.attempt.prepared_data, "0x1234abcd")
        self.assertEqual(prepared.attempt.expected_arguments["deposit_id"], row.deposit_id)
        self.assertIsNone(prepared.attempt.challenge_id)
        self.assertEqual(prepared.attempt.signature, "")
        self.assertEqual(
            BlockchainTransaction.objects.filter(
                contract=self.batches_contract,
                sender_address=self.participant.wallet_address_normalized,
            ).count(),
            1,
        )
        batch_contract.functions.approveAndIssueBatch.assert_called_once_with(batch.chain_batch_id)

    def test_automatic_approval_rejects_wrong_participant_wallet_or_network(self) -> None:
        """Reject a mismatched participant or chain before preparing any wallet request."""

        batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        raw_token = "wrong-wallet-approval-link-" + "y" * 40
        IssuanceBatchApprovalLink.objects.create(
            batch=batch,
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=timezone.now() + timedelta(days=2),
            created_by=self.user,
        )

        with self.assertRaises(PermissionDenied):
            services.prepare_participant_approval_and_issue(
                token=raw_token,
                wallet_address=OTHER_ACCOUNT.address,
                wallet_chain_id=self.network.chain_id,
            )
        with self.assertRaises(ValidationError):
            services.prepare_participant_approval_and_issue(
                token=raw_token,
                wallet_address=self.participant.wallet_address,
                wallet_chain_id=self.network.chain_id + 1,
            )
        self.assertFalse(batch.attempts.exists())

    def test_completed_batch_display_separates_minted_amount_from_wallet_balance(self) -> None:
        """Show confirmed mint totals separately from the producer's latest balance."""

        batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        link = self._approval_link(batch)
        record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            contract=self.batches_contract,
            operation_type=ISSUANCE_BATCH_OPERATION_TYPE,
            sender_address=self.participant.wallet_address_normalized,
        )
        attempt = IssuanceBatchAttempt.objects.create(
            batch=batch,
            transaction=record,
            link=link,
            kind=IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE,
            sender_address=self.participant.wallet_address_normalized,
            prepared_data="0x1234abcd",
            expected_arguments={"batch_id": batch.chain_batch_id},
            submission_started_at=timezone.now(),
        )
        submit_transaction(record, TEST_TX_HASH)
        confirm_transaction(record, 42, TEST_BLOCK_HASH)

        batch.status = IssuanceBatch.Status.APPROVAL_SUBMITTED
        batch.save(update_fields=("status", "updated_at"))
        batch.issued_amount_sjc = 412_500_000
        batch.participant_amount_sjc = 300_000_000
        batch.token_decimals = 6
        batch.status = IssuanceBatch.Status.COMPLETED
        batch.save(
            update_fields=(
                "issued_amount_sjc",
                "participant_amount_sjc",
                "token_decimals",
                "status",
                "updated_at",
            )
        )

        web3 = Mock()
        web3.eth.block_number = 51
        token = Mock()
        token.functions.decimals.return_value.call.return_value = 6
        token.functions.balanceOf.return_value.call.return_value = 123_456_789
        with (
            patch("issuance_batches.services.connect_to_rpc", return_value=web3),
            patch("issuance_batches.services.bind_contract_to_web3", return_value=token),
        ):
            display = services.get_batch_issuance_display(batch)

        self.assertEqual(display["issued_amount"], "412,5")
        self.assertEqual(display["participant_amount"], "300")
        self.assertEqual(display["balance"], "123,456789")
        self.assertEqual(display["balance_block_number"], 51)
        self.assertEqual(display["transaction_hash"], TEST_TX_HASH)
        self.assertEqual(display["block_number"], 42)

    def test_automatic_issuance_confirmation_checks_both_contract_event_streams(self) -> None:
        """Complete only when batch and token events plus historical storage agree."""

        batch = self._batch(status=IssuanceBatch.Status.APPROVAL_SUBMITTED)
        row = self._batch_receipt_row(batch)
        link = self._approval_link(batch)
        transaction_record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            contract=self.batches_contract,
            operation_type=ISSUANCE_BATCH_OPERATION_TYPE,
            sender_address=self.participant.wallet_address_normalized,
        )
        attempt = IssuanceBatchAttempt.objects.create(
            batch=batch,
            transaction=transaction_record,
            link=link,
            kind=IssuanceBatchAttempt.Kind.APPROVE_AND_ISSUE,
            sender_address=self.participant.wallet_address_normalized,
            prepared_data="0x1234abcd",
            expected_arguments={
                "batch_id": batch.chain_batch_id,
                "producer": self.participant.wallet_address_normalized,
                "created_by": batch.creator_address,
                "deposit_id": row.deposit_id,
                "amount_deposits": 1,
                "total_net_weight_kg": row.net_weight_kg,
                "deadline": batch.approval_deadline,
                "summary_hash": batch.summary_hash,
            },
        )

        batch_events = self.batches_contract
        token_events = self.soja_coin
        zero_address = "0x" + "00" * 20

        def event(
            name: str,
            index: int,
            args: dict[str, object],
            contract: ContractInstance,
        ) -> DecodedContractEvent:
            return DecodedContractEvent(
                event_name=name,
                transaction_hash=TEST_TX_HASH,
                log_index=index,
                block_number=42,
                block_hash=TEST_BLOCK_HASH,
                args=args,
                contract_instance=contract,
            )

        observed_events = [
            event(
                "BatchApproved",
                0,
                {
                    "batchId": batch.chain_batch_id,
                    "producer": self.participant.wallet_address_normalized,
                    "recordedBy": self.participant.wallet_address_normalized,
                    "summaryHash": bytes.fromhex(batch.summary_hash[2:]),
                },
                batch_events,
            ),
            event(
                "Transfer",
                1,
                {"from": zero_address, "to": OTHER_ACCOUNT.address, "value": 112_500_000},
                token_events,
            ),
            event(
                "Transfer",
                2,
                {
                    "from": zero_address,
                    "to": self.participant.wallet_address_normalized,
                    "value": 300_000_000,
                },
                token_events,
            ),
            event(
                "BatchIssuanceRecorded",
                3,
                {
                    "batchId": batch.chain_batch_id,
                    "depositId": row.deposit_id,
                    "amountIssued": 1,
                    "amountDeposits": 1,
                },
                batch_events,
            ),
            event(
                "BatchCompleted",
                4,
                {
                    "batchId": batch.chain_batch_id,
                    "producer": self.participant.wallet_address_normalized,
                    "amountDeposits": 1,
                },
                batch_events,
            ),
            event(
                "TokensIssued",
                5,
                {
                    "depositId": row.deposit_id,
                    "wallet": self.participant.wallet_address_normalized,
                    "issuedBy": batch.creator_address,
                    "amount": 412_500_000,
                },
                token_events,
            ),
        ]

        chain_state = (
            self.participant.wallet_address_normalized,
            batch.creator_address,
            row.net_weight_kg,
            1,
            1,
            batch.approval_deadline,
            bytes.fromhex(batch.summary_hash[2:]),
            3,
        )
        batch_contract = Mock()
        batch_contract.functions.batches.return_value.call.return_value = chain_state
        batch_contract.functions.getBatchDepositIds.return_value.call.return_value = [row.deposit_id]
        batch_contract.functions.batchByDeposit.return_value.call.return_value = batch.chain_batch_id
        batch_contract.functions.depositIssued.return_value.call.return_value = True
        soja_contract = Mock()
        soja_contract.functions.deposits.return_value.call.return_value = [
            self.participant.wallet_address_normalized,
            bytes.fromhex(row.invoice_key_hash[2:]),
            row.unit_chain_id,
            bytes.fromhex(row.crop_year_bytes32[2:]),
            0,
            412_500_000,
            True,
            True,
            False,
            False,
            0,
            row.net_weight_kg,
            0,
            bytes.fromhex(row.weighing_ticket_hash[2:]),
        ]
        soja_contract.functions.decimals.return_value.call.return_value = 6

        services._validate_automatic_issuance_events(
            attempt=attempt,
            token_event=observed_events[-1],
            observed_events=observed_events,
            batch_contract=batch_contract,
            soja_contract=soja_contract,
        )

        batch.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(batch.status, IssuanceBatch.Status.COMPLETED)
        self.assertEqual(batch.issued_amount_sjc, 412_500_000)
        self.assertEqual(batch.participant_amount_sjc, 300_000_000)
        self.assertEqual(batch.token_decimals, 6)
        self.assertIsNotNone(link.used_at)

    def test_signature_validation_rejects_changed_nonce_and_wrong_signer(self) -> None:
        """Invalidate stale EIP-712 data and reject a valid signature from another EOA."""

        batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        link = self._approval_link(batch)
        challenge = self._challenge(batch=batch, link=link)
        contract = MagicMock()
        contract.functions.issuanceBatchAuthorizationHash.return_value.call.return_value = bytes.fromhex(
            TEST_SUMMARY_HASH[2:]
        )
        typed_data = challenge.typed_data
        signature = "0x" + "11" * 65
        web3 = MagicMock()
        web3.eth.get_block.return_value = {"timestamp": challenge.deadline - 1}

        with patch("issuance_batches.services._check_pending_batch", return_value=8):
            with self.assertRaises(ValidationError):
                services._validate_signature(
                    batch=batch,
                    challenge=challenge,
                    signature=signature,
                    web3=web3,
                    batch_contract=contract,
                )
        challenge.refresh_from_db()
        self.assertEqual(challenge.status, IssuanceBatchAuthorizationChallenge.Status.INVALIDATED)

        challenge = self._challenge(batch=batch, link=link)
        with (
            patch("issuance_batches.services._check_pending_batch", return_value=7),
            patch("issuance_batches.services._build_typed_data", return_value=typed_data),
            patch("issuance_batches.services._typed_data_digest", return_value=(None, TEST_SUMMARY_HASH)),
            patch("issuance_batches.services._require_individual_wallet"),
            patch("issuance_batches.services._recover_typed_data_signer", return_value=OTHER_ACCOUNT.address.lower()),
        ):
            with self.assertRaises(PermissionDenied):
                services._validate_signature(
                    batch=batch,
                    challenge=challenge,
                    signature=signature,
                    web3=web3,
                    batch_contract=contract,
                )

    def test_reissuing_approval_challenge_invalidates_previous_with_model_lifecycle(self) -> None:
        """Replace a pending challenge through model saves, preserving lifecycle checks."""

        batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        raw_token = "participant-approval-link-secret-for-test"
        link = IssuanceBatchApprovalLink.objects.create(
            batch=batch,
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=timezone.now() + timedelta(days=3),
            created_by=self.user,
        )
        web3 = MagicMock()
        web3.eth.get_block.return_value = {"timestamp": batch.approval_deadline - 1}
        batch_contract = MagicMock()
        typed_data = services._build_typed_data(batch=batch, nonce=7)
        _signable, expected_digest = services._typed_data_digest(typed_data)
        batch_contract.functions.issuanceBatchAuthorizationHash.return_value.call.return_value = bytes.fromhex(
            expected_digest[2:]
        )

        with (
            patch("issuance_batches.services._get_batch_chain", return_value=(web3, batch_contract, MagicMock(), MagicMock())),
            patch("issuance_batches.services._require_individual_wallet"),
            patch("issuance_batches.services._check_pending_batch", return_value=7),
        ):
            first = services.create_approval_challenge(
                token=raw_token,
                wallet_address=self.participant.wallet_address,
                wallet_chain_id=self.network.chain_id,
            )
            second = services.create_approval_challenge(
                token=raw_token,
                wallet_address=self.participant.wallet_address,
                wallet_chain_id=self.network.chain_id,
            )

        first.challenge.refresh_from_db()
        self.assertEqual(first.challenge.status, IssuanceBatchAuthorizationChallenge.Status.INVALIDATED)
        self.assertEqual(second.challenge.status, IssuanceBatchAuthorizationChallenge.Status.PENDING)
        self.assertEqual(
            IssuanceBatchAuthorizationChallenge.objects.filter(
                link=link,
                status=IssuanceBatchAuthorizationChallenge.Status.PENDING,
            ).count(),
            1,
        )

    def test_signature_validation_uses_chain_timestamp_for_expiry(self) -> None:
        """Invalidate a signature challenge at the deadline reported by the chain."""

        batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        link = self._approval_link(batch)
        challenge = self._challenge(batch=batch, link=link)
        web3 = MagicMock()
        web3.eth.get_block.return_value = {"timestamp": challenge.deadline}

        with self.assertRaises(ValidationError):
            services._validate_signature(
                batch=batch,
                challenge=challenge,
                signature="0x" + "11" * 65,
                web3=web3,
                batch_contract=MagicMock(),
            )

        challenge.refresh_from_db()
        self.assertEqual(challenge.status, IssuanceBatchAuthorizationChallenge.Status.INVALIDATED)

    def test_valid_participant_eip712_signature_is_accepted(self) -> None:
        """Accept a genuine EIP-712 signature from the wallet saved on the participant."""

        batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        link = self._approval_link(batch)
        typed_data = services._build_typed_data(batch=batch, nonce=7)
        signable, digest = services._typed_data_digest(typed_data)
        challenge = IssuanceBatchAuthorizationChallenge.objects.create(
            link=link,
            chain_nonce=7,
            deadline=batch.approval_deadline,
            digest=digest,
            typed_data=typed_data,
        )
        signature = "0x" + Account.sign_message(signable, private_key=TEST_ACCOUNT.key).signature.hex()
        web3 = MagicMock()
        web3.eth.get_block.return_value = {"timestamp": batch.approval_deadline - 1}
        web3.eth.get_code.return_value = b""
        contract = MagicMock()
        contract.functions.issuanceBatchAuthorizationHash.return_value.call.return_value = bytes.fromhex(
            digest[2:]
        )

        with patch("issuance_batches.services._check_pending_batch", return_value=7):
            accepted = services._validate_signature(
                batch=batch,
                challenge=challenge,
                signature=signature,
                web3=web3,
                batch_contract=contract,
            )

        self.assertEqual(accepted, signature.lower())

    def test_expired_and_canceled_links_are_rejected(self) -> None:
        """Reject bearer links after their own expiry or an explicit revocation."""

        expired_batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        expired_token = "expired-link-" + "c" * 40
        expired_link = IssuanceBatchApprovalLink.objects.create(
            batch=expired_batch,
            token_hash=hashlib.sha256(expired_token.encode("utf-8")).hexdigest(),
            expires_at=timezone.now() - timedelta(seconds=1),
            created_by=self.user,
        )
        canceled_token = "canceled-link-" + "d" * 40
        canceled_batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        canceled_link = IssuanceBatchApprovalLink.objects.create(
            batch=canceled_batch,
            token_hash=hashlib.sha256(canceled_token.encode("utf-8")).hexdigest(),
            expires_at=timezone.now() + timedelta(days=1),
            canceled_at=timezone.now(),
            created_by=self.user,
        )

        for token in ("invalid-link-" + "e" * 40, expired_token, canceled_token):
            with self.subTest(token=token[:12]), self.assertRaises(ValidationError):
                services.get_approval_link(token)

        self.assertFalse(expired_link.is_usable)
        self.assertFalse(canceled_link.is_usable)

    def test_participant_can_poll_cancel_and_link_cannot_be_revoked_mid_send(self) -> None:
        """Keep the participant link available until an uncertain wallet action resolves."""

        batch = self._batch(status=IssuanceBatch.Status.AWAITING_APPROVAL)
        raw_token = f"test-batch-link-{batch.pk}-" + "x" * 40
        link = IssuanceBatchApprovalLink.objects.create(
            batch=batch,
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=timezone.now() + timedelta(days=2),
            created_by=self.user,
        )

        transaction_record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            contract=batch.batches_contract,
            operation_type=ISSUANCE_BATCH_OPERATION_TYPE,
            sender_address=self.participant.wallet_address_normalized,
            status=BlockchainTransaction.Status.PREPARED,
        )
        IssuanceBatchAttempt.objects.create(
            batch=batch,
            transaction=transaction_record,
            link=link,
            kind=IssuanceBatchAttempt.Kind.CANCEL,
            sender_address=self.participant.wallet_address_normalized,
            prepared_data="0x12345678",
            expected_arguments={"batch_id": batch.chain_batch_id},
            submission_started_at=timezone.now(),
        )

        with patch("issuance_batches.services.require_issuance_access"):
            with self.assertRaises(ValidationError):
                services.cancel_approval_link(
                    user=self.user,
                    company_id=self.company.pk,
                    batch_id=batch.pk,
                    link_id=link.pk,
                )
        link.refresh_from_db()
        self.assertIsNone(link.canceled_at)

        batch.status = IssuanceBatch.Status.CANCEL_SUBMITTED
        batch.save(update_fields=("status", "updated_at"))
        resolved = services.get_approval_link(raw_token, allow_cancel=True)
        self.assertEqual(resolved.pk, link.pk)


    def test_expired_contract_deadline_allows_bounded_link_access_only_for_cancel(self) -> None:
        """A participant can still review/cancel an expired batch during link lifetime."""

        batch = self._batch(
            status=IssuanceBatch.Status.AWAITING_APPROVAL,
            approval_deadline=int(timezone.now().timestamp()) - 1,
        )
        raw_token = "test-token-" + "b" * 40
        link = IssuanceBatchApprovalLink.objects.create(
            batch=batch,
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=timezone.now() + timedelta(days=3),
            created_by=self.user,
        )

        resolved = services.get_approval_link(raw_token, allow_cancel=True)

        self.assertEqual(resolved.pk, link.pk)
        self.assertFalse(link.is_usable)
        with self.assertRaises(ValidationError):
            services.get_approval_link(raw_token)
