from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib import admin
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import DatabaseError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from web3 import Web3
from web3.exceptions import TransactionNotFound, Web3Exception

from companies.models import (
    AccessChange,
    Company,
    CompanyMembership,
    Unit,
    UnitAccess,
    UnitWalletPermission,
)
from companies.services.access_history import (
    deactivate_membership,
    deactivate_unit_access,
    record_access_change,
    revoke_membership_role,
)

from .admin import DeploymentStepInline
from .models import (
    CONTRACT_CONFIGURATION_OPERATION_TYPE,
    DEPLOYMENT_OPERATION_TYPE,
    BlockchainTransaction,
    ContractInstance,
    DeploymentAttempt,
    DeploymentConfigurationAttempt,
    DeploymentProcess,
    DeploymentStep,
    Network,
    ProcessedEvent,
)
from .services.deployment import (
    DEPLOYMENT_KIND_LABELS,
    DEPLOYMENT_ORDER,
    cancel_deployment_process,
    fail_deployment_attempt,
    mark_deployment_submission_started,
    prepare_deployment_step,
    register_deployment_transaction_hash,
    start_deployment_process,
    start_deployment_attempt,
)
from .services.deployment_confirmation import confirm_deployment_attempt
from .services.deployment_confirmation import DeploymentConfirmationResult
from .services.deployment_configuration import (
    ConfigurationConfirmation,
    PreparedConfiguration,
    _configuration_function_data,
    confirm_deployment_configuration,
    mark_configuration_submission_started,
    prepare_deployment_configuration,
    register_configuration_transaction_hash,
    reject_configuration_attempt,
)
from .services.events import process_event_once
from .services.rpc import connect_to_rpc
from .services.transactions import (
    confirm_transaction,
    fail_transaction,
    mark_transaction_reorged,
    submit_transaction,
)


User = get_user_model()


class RpcConnectionTests(TestCase):
    """Verify RPC connections use the endpoint saved on the selected network."""

    def test_connects_to_network_rpc_url_and_checks_its_chain_id(self) -> None:
        """The database network controls both the endpoint and expected chain."""

        network = Network.objects.create(
            name="Anvil local",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )
        with patch("blockchain.services.rpc.Web3") as web3_class:
            client = web3_class.return_value
            client.is_connected.return_value = True
            client.eth.chain_id = network.chain_id

            connected_client = connect_to_rpc(network)

        web3_class.HTTPProvider.assert_called_once_with(
            "http://127.0.0.1:8545",
            request_kwargs={"timeout": 10},
        )
        web3_class.assert_called_once_with(web3_class.HTTPProvider.return_value)
        self.assertIs(connected_client, client)

    def test_rejects_rpc_that_reports_a_different_chain_id(self) -> None:
        """A saved network cannot silently connect to a different chain."""

        network = Network.objects.create(
            name="Anvil local",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )
        with patch("blockchain.services.rpc.Web3") as web3_class:
            client = web3_class.return_value
            client.is_connected.return_value = True
            client.eth.chain_id = 1

            with self.assertRaisesRegex(ConnectionError, "expected chain 31337"):
                connect_to_rpc(network)

    def test_network_requires_an_http_or_https_rpc_url(self) -> None:
        """Non-HTTP protocols are rejected when a network is saved."""

        network = Network(
            name="Invalid network",
            chain_id=31337,
            rpc_url="file:///tmp/rpc",
        )

        with self.assertRaises(ValidationError) as error:
            network.full_clean()

        self.assertIn("rpc_url", error.exception.message_dict)


class BlockchainHistoryTests(TestCase):
    """Verify transaction lifecycle and append-only blockchain history."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.company = Company.objects.create(
            legal_name="Blockchain Company",
            cnpj="11222333000181",
        )
        cls.user = User.objects.create_user(
            username="history-actor",
            password="history-password",
        )
        cls.membership = CompanyMembership.objects.create(
            company=cls.company,
            user=cls.user,
        )
        cls.unit = Unit.objects.create(
            company=cls.company,
            name="Main warehouse",
            code="MAIN",
            cnpj="11444777000404",
        )
        cls.network = Network.objects.create(
            name="Local test network",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )
        cls.contract = ContractInstance.objects.create(
            company=cls.company,
            network=cls.network,
            kind=ContractInstance.Kind.SOJA_COIN,
            address="0x1111111111111111111111111111111111111111",
            version="test-1",
        )

    def create_transaction(self) -> BlockchainTransaction:
        """Create a fresh prepared transaction for one test."""

        return BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            contract=self.contract,
            operation_type="test_operation",
            sender_address="0x2222222222222222222222222222222222222222",
        )

    def test_pending_transaction_is_not_complete(self) -> None:
        """A prepared record cannot be saved as confirmed without evidence."""

        record = self.create_transaction()
        self.assertFalse(record.is_confirmed)

        record.status = BlockchainTransaction.Status.CONFIRMED
        with self.assertRaises(ValidationError):
            record.save()

    def test_transaction_lifecycle_requires_network_confirmation(self) -> None:
        """Only the explicit confirmation transition exposes completion."""

        record = self.create_transaction()
        submitted = submit_transaction(
            record,
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertEqual(submitted.status, BlockchainTransaction.Status.SUBMITTED)
        self.assertFalse(submitted.is_confirmed)

        confirmed = confirm_transaction(
            submitted,
            10,
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        self.assertEqual(confirmed.status, BlockchainTransaction.Status.CONFIRMED)
        self.assertTrue(confirmed.is_confirmed)

    def test_failed_transaction_never_appears_complete(self) -> None:
        """A failed submission remains visible as failed, never confirmed."""

        record = fail_transaction(self.create_transaction(), "RPC rejected the call")
        self.assertEqual(record.status, BlockchainTransaction.Status.FAILED)
        self.assertFalse(record.is_confirmed)
        with self.assertRaises(ValidationError):
            confirm_transaction(record, 11, "0x" + "c" * 64)

    def test_pending_unit_permission_is_not_effective(self) -> None:
        """A wallet permission becomes effective only with a confirmed hash."""

        permission = UnitWalletPermission.objects.create(
            unit=self.unit,
            wallet="0x3333333333333333333333333333333333333333",
            permission_mask=1,
        )
        self.assertFalse(permission.is_effective)

        permission.sync_status = UnitWalletPermission.SyncStatus.CONFIRMED
        with self.assertRaises(ValidationError):
            permission.save()

        permission.transaction = "0x" + "f" * 64
        permission.save()
        self.assertTrue(permission.is_effective)

    def test_reorged_confirmation_is_not_complete(self) -> None:
        """A later reorganization removes the completed status from the UI."""

        record = submit_transaction(
            self.create_transaction(),
            "0x" + "d" * 64,
        )
        record = confirm_transaction(record, 12, "0x" + "e" * 64)
        record = mark_transaction_reorged(record, "Block was reorganized")
        self.assertEqual(record.status, BlockchainTransaction.Status.REORGED)
        self.assertFalse(record.is_confirmed)

    def test_transaction_records_cannot_be_deleted(self) -> None:
        """Transaction history is preserved by both object and queryset deletes."""

        record = self.create_transaction()
        with self.assertRaises(ValidationError):
            record.delete()
        with self.assertRaises(ValidationError):
            BlockchainTransaction.objects.filter(pk=record.pk).delete()
        with self.assertRaises(ValidationError):
            BlockchainTransaction.objects.filter(pk=record.pk).update(
                status=BlockchainTransaction.Status.CONFIRMED,
            )

    def test_event_handler_runs_once_for_duplicate_logs(self) -> None:
        """The unique event identity prevents a replay from applying twice."""

        calls: list[int] = []

        def handler(event: ProcessedEvent) -> None:
            calls.append(event.pk)

        arguments = {
            "network": self.network,
            "contract": self.contract,
            "transaction_hash": "0x" + "1" * 64,
            "log_index": 0,
            "block_number": 20,
            "block_hash": "0x" + "2" * 64,
            "event_name": "UnitRegistered",
            "handler": handler,
        }
        first, created = process_event_once(**arguments)
        second, duplicate = process_event_once(**arguments)

        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(calls, [first.pk])
        self.assertEqual(ProcessedEvent.objects.count(), 1)

    def test_failed_event_handler_does_not_leave_marker(self) -> None:
        """A failed application rolls back the marker so a retry is safe."""

        def handler(event: ProcessedEvent) -> None:
            raise RuntimeError("projection failed")

        with self.assertRaises(RuntimeError):
            process_event_once(
                network=self.network,
                contract=self.contract,
                transaction_hash="0x" + "3" * 64,
                log_index=1,
                block_number=21,
                block_hash="0x" + "4" * 64,
                event_name="UnitRegistered",
                handler=handler,
            )

        self.assertFalse(ProcessedEvent.objects.exists())

    def test_processed_events_are_immutable(self) -> None:
        """An event marker cannot be edited or removed after processing."""

        event, _ = process_event_once(
            network=self.network,
            contract=self.contract,
            transaction_hash="0x" + "5" * 64,
            log_index=2,
            block_number=22,
            block_hash="0x" + "6" * 64,
            event_name="UnitRegistered",
        )
        event.event_name = "Changed"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            ProcessedEvent.objects.filter(pk=event.pk).delete()

    def test_access_change_is_immutable_and_scoped(self) -> None:
        """Access history records the actor and cannot be edited or deleted."""

        change = record_access_change(
            company_id=self.company.pk,
            membership=self.membership,
            actor=self.user,
            action=AccessChange.Action.MEMBERSHIP_GRANTED,
            previous_value={"active": False},
            new_value={"active": True},
        )
        self.assertEqual(change.actor_id, self.user.pk)
        self.assertEqual(change.company_id, self.company.pk)
        self.assertIsNotNone(change.occurred_at)

        change.new_value = {"active": False}
        with self.assertRaises(ValidationError):
            change.save()
        with self.assertRaises(ValidationError):
            change.delete()
        with self.assertRaises(ValidationError):
            AccessChange.objects.filter(pk=change.pk).delete()

    def test_membership_deactivation_keeps_audit_record(self) -> None:
        """Deactivation changes access state without deleting the membership."""

        change = deactivate_membership(self.membership, self.user)
        self.membership.refresh_from_db()

        self.assertFalse(self.membership.active)
        self.assertEqual(change.action, AccessChange.Action.MEMBERSHIP_REVOKED)
        self.assertTrue(AccessChange.objects.filter(pk=change.pk).exists())

    def test_role_revoke_keeps_the_role_and_records_the_actor(self) -> None:
        """Role revocation is an inactive state plus an immutable audit row."""

        # The role model rejects self-grants, so use a separate actor for this test.
        actor = User.objects.create_user(
            username="role-revoker",
            password="history-password",
        )
        role = self.membership.roles.create(
            role="registration",
            granted_by=actor,
        )

        change = revoke_membership_role(role, actor)
        role.refresh_from_db()
        self.assertFalse(role.active)
        self.assertEqual(change.action, AccessChange.Action.ROLE_REVOKED)

    def test_unit_access_deactivation_keeps_previous_flags_in_history(self) -> None:
        """A unit grant is deactivated without losing its permission snapshot."""

        access = UnitAccess.objects.create(
            membership=self.membership,
            unit=self.unit,
            can_view=True,
            can_receive=True,
        )
        change = deactivate_unit_access(access, self.user)
        access.refresh_from_db()

        self.assertFalse(access.active)
        self.assertEqual(change.previous_value["can_receive"], True)
        self.assertEqual(change.new_value, {"active": False})

    def test_duplicate_transaction_hash_is_rejected_per_network(self) -> None:
        """The same network cannot store two lifecycle records for one hash."""

        first = submit_transaction(self.create_transaction(), "0x" + "7" * 64)
        second = self.create_transaction()
        second.transaction_hash = first.transaction_hash
        second.submitted_at = first.submitted_at
        second.status = BlockchainTransaction.Status.SUBMITTED
        with self.assertRaises(ValidationError), transaction.atomic():
            second.save()


class DeploymentWorkflowTests(TestCase):
    """Cover cancellation, pre-existing contracts and reorged deployments."""

    owner_address = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    sender_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def setUp(self) -> None:
        self.company = Company.objects.create(
            legal_name="Deployment Company",
            cnpj="11222333000181",
        )
        self.network = Network.objects.create(
            name="Deployment Test Network",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )

    def start_process(self) -> DeploymentProcess:
        """Start a deployment using valid local test records."""

        return start_deployment_process(
            company=self.company,
            network=self.network,
            owner_address=self.owner_address,
        )

    def create_attempt(
        self,
        step: DeploymentStep,
        transaction_hash: str,
        *,
        confirmed: bool = False,
    ) -> BlockchainTransaction:
        """Create a transaction attempt, optionally confirmed with block data."""

        record = BlockchainTransaction.objects.create(
            company=step.process.company,
            network=step.process.network,
            operation_type=DEPLOYMENT_OPERATION_TYPE,
            sender_address=self.sender_address,
        )
        DeploymentAttempt.objects.create(step=step, transaction=record)
        record = submit_transaction(record, transaction_hash)
        if confirmed:
            record = confirm_transaction(
                record,
                block_number=int(transaction_hash[-4:], 16),
                block_hash="0x" + f"{int(transaction_hash[-4:], 16) + 100:064x}",
            )
        return record

    def link_confirmed_contract(
        self,
        step: DeploymentStep,
        index: int,
    ) -> tuple[BlockchainTransaction, ContractInstance]:
        """Link a contract only after its matching deployment transaction confirms."""

        transaction_hash = "0x" + f"{index + 1000:064x}"
        contract = ContractInstance.objects.create(
            company=step.process.company,
            network=step.process.network,
            kind=step.kind,
            address="0x" + f"{index:040x}",
            version="test-1",
            deployment_transaction_hash=transaction_hash,
        )
        record = self.create_attempt(step, transaction_hash, confirmed=True)
        step.contract = contract
        step.save()
        return record, contract

    def create_deployed_process(
        self,
    ) -> tuple[DeploymentProcess, BlockchainTransaction, ContractInstance]:
        """Create all nine confirmed steps and return the first deployment pair."""

        process = self.start_process()
        first_transaction: BlockchainTransaction | None = None
        first_contract: ContractInstance | None = None
        for index, step in enumerate(process.steps.order_by("position"), start=1):
            record, contract = self.link_confirmed_contract(step, index)
            if index == 1:
                first_transaction = record
                first_contract = contract

        process.refresh_from_db()
        process.status = DeploymentProcess.Status.DEPLOYED
        process.save()
        assert first_transaction is not None
        assert first_contract is not None
        return process, first_transaction, first_contract

    def confirm_steps_through(
        self,
        process: DeploymentProcess,
        last_position: int,
    ) -> dict[str, ContractInstance]:
        """Create matching confirmed contracts through one ordered step."""

        contracts: dict[str, ContractInstance] = {}
        for index, step in enumerate(
            process.steps.filter(position__lte=last_position).order_by("position"),
            start=1,
        ):
            _, contract = self.link_confirmed_contract(step, index)
            contracts[step.kind] = contract
        return contracts

    def test_first_module_step_returns_constructor_data(self) -> None:
        """The first step can prepare using the saved process context."""

        process = self.start_process()
        first_step = process.steps.get(position=1)

        data = prepare_deployment_step(first_step.pk)

        self.assertTrue(data.startswith("0x"))
        process.refresh_from_db()
        first_step.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertIsNone(first_step.contract_id)
        self.assertEqual(first_step.attempts.count(), 0)

    def test_sojacoin_preparation_uses_six_confirmed_modules_from_process(self) -> None:
        """SojaCoin receives only the six addresses linked to this process."""

        process = self.start_process()
        contracts = self.confirm_steps_through(process, last_position=6)
        token_step = process.steps.get(position=7)

        with patch(
            "blockchain.services.deployment.prepare_deployment_data",
            return_value="0x1234",
        ) as prepare_data:
            result = prepare_deployment_step(token_step.pk)

        self.assertEqual(result, "0x1234")
        self.assertEqual(
            prepare_data.call_args.kwargs["dependency_addresses"],
            {
                kind: Web3.to_checksum_address(contract.address_normalized)
                for kind, contract in contracts.items()
            },
        )
        self.assertEqual(prepare_data.call_args.kwargs["company"], self.company)
        self.assertEqual(prepare_data.call_args.kwargs["network"], self.network)
        self.assertEqual(
            prepare_data.call_args.kwargs["owner_address"], process.owner_address
        )

    def test_registry_and_issuance_preparation_use_process_sojacoin(self) -> None:
        """Both final contracts receive the confirmed SojaCoin address."""

        process = self.start_process()
        contracts = self.confirm_steps_through(process, last_position=7)
        soja_coin_address = Web3.to_checksum_address(
            contracts[ContractInstance.Kind.SOJA_COIN].address_normalized
        )

        registry_step = process.steps.get(position=8)
        with patch(
            "blockchain.services.deployment.prepare_deployment_data",
            return_value="0x1234",
        ) as prepare_registry_data:
            prepare_deployment_step(registry_step.pk)
        self.assertEqual(
            prepare_registry_data.call_args.kwargs["dependency_addresses"],
            {ContractInstance.Kind.SOJA_COIN: soja_coin_address},
        )

        self.link_confirmed_contract(registry_step, 8)
        issuance_step = process.steps.get(position=9)
        with patch(
            "blockchain.services.deployment.prepare_deployment_data",
            return_value="0x5678",
        ) as prepare_issuance_data:
            prepare_deployment_step(issuance_step.pk)
        self.assertEqual(
            prepare_issuance_data.call_args.kwargs["dependency_addresses"],
            {ContractInstance.Kind.SOJA_COIN: soja_coin_address},
        )

    def test_step_waits_until_every_previous_step_is_confirmed(self) -> None:
        """An incomplete earlier step blocks preparation without changing state."""

        process = self.start_process()
        next_step = process.steps.get(position=2)
        step_ids = list(process.steps.values_list("pk", flat=True))

        with self.assertRaisesMessage(ValidationError, "não tem contrato confirmado"):
            prepare_deployment_step(next_step.pk)

        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertEqual(list(process.steps.values_list("pk", flat=True)), step_ids)
        self.assertEqual(DeploymentAttempt.objects.count(), 0)

    def test_preparation_rejects_missing_or_invalid_step_ids(self) -> None:
        """Unknown and non-positive IDs produce clear validation errors."""

        with self.assertRaisesMessage(ValidationError, "não encontrada"):
            prepare_deployment_step(999999)
        with self.assertRaisesMessage(ValidationError, "positivo"):
            prepare_deployment_step(0)

    def test_preparation_rejects_closed_process_states(self) -> None:
        """Canceled, deployed and review processes cannot prepare new steps."""

        canceled = self.start_process()
        cancel_deployment_process(canceled.pk)

        review = self.start_process()
        review.status = DeploymentProcess.Status.NEEDS_REVIEW
        review.save()

        self.company = Company.objects.create(
            legal_name="Completed Deployment Company",
            cnpj="55222333000180",
        )
        self.network = Network.objects.create(
            name="Completed Deployment Network",
            chain_id=31338,
            rpc_url="http://127.0.0.1:8545",
        )
        deployed, _, _ = self.create_deployed_process()

        for process in (canceled, review, deployed):
            with self.subTest(status=process.status):
                step = process.steps.get(position=1)
                with self.assertRaisesMessage(ValidationError, "em andamento"):
                    prepare_deployment_step(step.pk)

    def test_preparation_rejects_inactive_company_or_network(self) -> None:
        """The process scope must still be active when a step is prepared."""

        process = self.start_process()
        first_step = process.steps.get(position=1)
        self.company.active = False
        self.company.save()
        with self.assertRaisesMessage(ValidationError, "empresa do processo está inativa"):
            prepare_deployment_step(first_step.pk)

        self.company.active = True
        self.company.save()
        self.network.active = False
        self.network.save()
        with self.assertRaisesMessage(ValidationError, "inativa"):
            prepare_deployment_step(first_step.pk)

        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertEqual(process.steps.count(), 9)

    def test_preparation_rejects_already_confirmed_step(self) -> None:
        """An already deployed contract cannot be prepared a second time."""

        process = self.start_process()
        first_step = process.steps.get(position=1)
        self.link_confirmed_contract(first_step, 1)

        with self.assertRaisesMessage(ValidationError, "já tem um contrato confirmado"):
            prepare_deployment_step(first_step.pk)

    def test_preparation_rejects_inactive_dependency(self) -> None:
        """Inactive earlier contracts cannot be passed to a later constructor."""

        process = self.start_process()
        contracts = self.confirm_steps_through(process, last_position=6)
        inactive_contract = contracts[ContractInstance.Kind.SOJA_COIN_LOCKS]
        inactive_contract.active = False
        inactive_contract.save()
        token_step = process.steps.get(position=7)

        with self.assertRaisesMessage(ValidationError, "está inativo"):
            prepare_deployment_step(token_step.pk)

        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertEqual(process.steps.count(), 9)

    def test_preparation_does_not_borrow_dependencies_from_other_company(self) -> None:
        """An active contract owned by another company cannot satisfy a step."""

        process = self.start_process()
        self.confirm_steps_through(process, last_position=5)
        other_company = Company.objects.create(
            legal_name="Unrelated Deployment Company",
            cnpj="11444777000404",
        )
        ContractInstance.objects.create(
            company=other_company,
            network=self.network,
            kind=ContractInstance.Kind.SOJA_COIN_LOCKS,
            address="0x" + f"{301:040x}",
            version="external-1",
        )
        token_step = process.steps.get(position=7)

        with self.assertRaises(ValidationError):
            prepare_deployment_step(token_step.pk)

        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertFalse(token_step.contract_id)

    def test_preparation_reports_existing_active_contract_of_same_kind(self) -> None:
        """An active unlinked instance prevents duplicate deployment data."""

        process = self.start_process()
        ContractInstance.objects.create(
            company=self.company,
            network=self.network,
            kind=ContractInstance.Kind.SOJA_COIN_ADMINISTRATION,
            address="0x" + f"{302:040x}",
            version="existing-1",
        )
        first_step = process.steps.get(position=1)

        with self.assertRaisesMessage(ValidationError, "já está cadastrado"):
            prepare_deployment_step(first_step.pk)

        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertIsNone(first_step.contract_id)

    def test_preparation_reports_missing_bytecode_without_changing_process(self) -> None:
        """A missing packaged bytecode file is reported before a step changes."""

        process = self.start_process()
        first_step = process.steps.get(position=1)
        with patch(
            "blockchain.services.deployment.BYTECODE_DIRECTORY",
            Path("/path/that/does/not/exist"),
        ):
            with self.assertRaisesMessage(ValidationError, "Código de implantação indisponível"):
                prepare_deployment_step(first_step.pk)

        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertIsNone(first_step.contract_id)

    def test_preparation_reports_invalid_bytecode(self) -> None:
        """Malformed bytecode is rejected before contract data is returned."""

        process = self.start_process()
        first_step = process.steps.get(position=1)
        with tempfile.TemporaryDirectory() as directory:
            bytecode_path = Path(directory) / "sojacoin_administration.json"
            bytecode_path.write_text(json.dumps("0xnot-bytecode"), encoding="utf-8")
            with patch(
                "blockchain.services.deployment.BYTECODE_DIRECTORY",
                Path(directory),
            ):
                with self.assertRaisesMessage(ValidationError, "Código de implantação inválido"):
                    prepare_deployment_step(first_step.pk)

    def test_preparation_reports_missing_or_invalid_abi(self) -> None:
        """ABI loading errors become a clear validation message."""

        process = self.start_process()
        first_step = process.steps.get(position=1)
        with patch(
            "blockchain.services.deployment.load_contract_abi",
            side_effect=ImproperlyConfigured("ABI missing"),
        ):
            with self.assertRaisesMessage(ValidationError, "ABI do contrato indisponível"):
                prepare_deployment_step(first_step.pk)

        with patch(
            "blockchain.services.deployment.load_contract_abi",
            return_value=["malformed ABI entry"],
        ):
            with self.assertRaisesMessage(
                ValidationError,
                "Não foi possível montar os dados de implantação",
            ):
                prepare_deployment_step(first_step.pk)

    def test_preparation_failure_preserves_process_steps_and_attempts(self) -> None:
        """A failed preparation does not create or remove process history."""

        process = self.start_process()
        initial_steps = list(process.steps.values_list("pk", flat=True))
        with patch(
            "blockchain.services.deployment.load_contract_abi",
            side_effect=ImproperlyConfigured("ABI missing"),
        ):
            with self.assertRaises(ValidationError):
                prepare_deployment_step(process.steps.get(position=1).pk)

        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertEqual(list(process.steps.values_list("pk", flat=True)), initial_steps)
        self.assertEqual(DeploymentAttempt.objects.count(), 0)

    def test_cancel_without_sent_transactions_preserves_process_and_allows_restart(self) -> None:
        """Canceling a local-only process preserves its steps and frees the scope."""

        process = self.start_process()
        step_ids = list(process.steps.values_list("pk", flat=True))

        canceled = cancel_deployment_process(process.pk)
        replacement = self.start_process()

        self.assertEqual(canceled.status, DeploymentProcess.Status.CANCELED)
        self.assertEqual(list(canceled.steps.values_list("pk", flat=True)), step_ids)
        self.assertEqual(DeploymentProcess.objects.count(), 2)
        self.assertEqual(replacement.status, DeploymentProcess.Status.IN_PROGRESS)

    def test_inactive_company_or_network_cannot_start_a_process(self) -> None:
        """An inactive company or network leaves no partial deployment process."""

        self.company.active = False
        self.company.save()
        with self.assertRaisesMessage(ValidationError, "ativas"):
            self.start_process()
        self.assertFalse(DeploymentProcess.objects.exists())

        self.company.active = True
        self.company.save()
        self.network.active = False
        self.network.save()
        with self.assertRaisesMessage(ValidationError, "ativas"):
            self.start_process()
        self.assertFalse(DeploymentProcess.objects.exists())

    def test_cancel_with_prepared_unsent_attempt_preserves_attempt(self) -> None:
        """A prepared transaction without a hash has not been broadcast yet."""

        process = self.start_process()
        step = process.steps.first()
        record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            operation_type="contract_deployment",
            sender_address=self.sender_address,
        )
        attempt = DeploymentAttempt.objects.create(step=step, transaction=record)

        canceled = cancel_deployment_process(process.pk)

        self.assertEqual(canceled.status, DeploymentProcess.Status.CANCELED)
        self.assertTrue(DeploymentAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertTrue(BlockchainTransaction.objects.filter(pk=record.pk).exists())
        with self.assertRaises(ValidationError):
            submit_transaction(record, "0x" + "c" * 64)

    def test_cancel_is_rejected_after_transaction_was_submitted(self) -> None:
        """A submitted transaction may still deploy, so its process stays open."""

        process = self.start_process()
        step = process.steps.first()
        record = self.create_attempt(step, "0x" + "d" * 64)

        with self.assertRaisesMessage(ValidationError, "transação enviada"):
            cancel_deployment_process(process.pk)

        process.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertEqual(record.status, BlockchainTransaction.Status.SUBMITTED)

    def test_deployment_transaction_cannot_be_submitted_without_an_attempt(self) -> None:
        """Every broadcast deployment transaction must be tied to its process step."""

        record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            operation_type=DEPLOYMENT_OPERATION_TYPE,
            sender_address=self.sender_address,
        )

        with self.assertRaisesMessage(ValidationError, "vinculada a uma etapa"):
            submit_transaction(record, "0x" + "6" * 64)

        record.refresh_from_db()
        self.assertEqual(record.status, BlockchainTransaction.Status.PREPARED)

    def test_deployment_attempt_rejects_a_non_deployment_transaction(self) -> None:
        """An unrelated transaction cannot be presented as a deployment attempt."""

        process = self.start_process()
        record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            operation_type="test_operation",
            sender_address=self.sender_address,
        )

        with self.assertRaisesMessage(ValidationError, "transação de implantação"):
            DeploymentAttempt.objects.create(step=process.steps.first(), transaction=record)

    def test_cancel_is_rejected_for_a_failed_transaction_that_was_broadcast(self) -> None:
        """A reverted or failed broadcast still has an on-chain history to inspect."""

        process = self.start_process()
        step = process.steps.first()
        record = self.create_attempt(step, "0x" + "e" * 64)
        record = fail_transaction(record, "A transaction was mined and reverted.")

        with self.assertRaisesMessage(ValidationError, "transação enviada"):
            cancel_deployment_process(process.pk)

        record.refresh_from_db()
        self.assertEqual(record.status, BlockchainTransaction.Status.FAILED)

    def test_cancel_rejects_deployed_and_review_processes(self) -> None:
        """Only an in-progress process without a broadcast can be canceled."""

        process, _, _ = self.create_deployed_process()
        with self.assertRaises(ValidationError):
            cancel_deployment_process(process.pk)
        process.status = DeploymentProcess.Status.NEEDS_REVIEW
        process.save()
        with self.assertRaises(ValidationError):
            cancel_deployment_process(process.pk)

    def test_process_cannot_be_marked_deployed_before_all_nine_confirm(self) -> None:
        """A process stays in progress until every step has confirmed evidence."""

        process = self.start_process()
        for index, step in enumerate(process.steps.order_by("position")[:8], start=1):
            self.link_confirmed_contract(step, index)

        process.status = DeploymentProcess.Status.DEPLOYED
        with self.assertRaisesMessage(ValidationError, "nove contratos"):
            process.save()

        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertEqual(process.confirmed_step_count, 8)

    def test_confirmed_transaction_without_contract_is_not_counted(self) -> None:
        """A receipt alone does not count until its deployed address is recorded."""

        process = self.start_process()
        step = process.steps.get(position=1)
        self.create_attempt(step, "0x" + "a" * 64, confirmed=True)

        process.refresh_from_db()
        step.refresh_from_db()
        inline = DeploymentStepInline(DeploymentProcess, admin.site)
        self.assertEqual(process.confirmed_step_count, 0)
        self.assertFalse(step.is_confirmed)
        self.assertEqual(inline.step_status(step), "Transação confirmada; endereço pendente")

    def test_step_rejects_contract_without_matching_confirmed_attempt(self) -> None:
        """A contract address is linked only to the confirmed attempt that created it."""

        process = self.start_process()
        step = process.steps.get(position=1)
        self.create_attempt(step, "0x" + "b" * 64, confirmed=True)
        contract = ContractInstance.objects.create(
            company=self.company,
            network=self.network,
            kind=step.kind,
            address="0x" + f"{201:040x}",
            version="mismatched-transaction-1",
            deployment_transaction_hash="0x" + "c" * 64,
        )

        step.contract = contract
        with self.assertRaisesMessage(ValidationError, "transação de implantação confirmada"):
            step.save()

        step.refresh_from_db()
        self.assertIsNone(step.contract_id)

    def test_cancel_is_idempotent_and_missing_process_has_a_clear_error(self) -> None:
        """Repeated cancellation preserves the same row; unknown IDs fail safely."""

        process = self.start_process()
        first_cancel = cancel_deployment_process(process.pk)
        second_cancel = cancel_deployment_process(process.pk)

        self.assertEqual(first_cancel.pk, second_cancel.pk)
        self.assertEqual(DeploymentProcess.objects.count(), 1)
        with self.assertRaisesMessage(ValidationError, "não encontrado"):
            cancel_deployment_process(999999)
        with self.assertRaisesMessage(ValidationError, "positivo"):
            cancel_deployment_process(0)

    def test_admin_cancel_is_post_only_and_keeps_history(self) -> None:
        """The process detail exposes a CSRF-protected POST cancel action."""

        process = self.start_process()
        admin_user = User.objects.create_superuser(
            username="deployment-admin",
            email="deployment-admin@example.test",
            password="test-password",
        )
        self.client.force_login(admin_user)
        change_url = reverse("admin:blockchain_deploymentprocess_change", args=(process.pk,))
        cancel_url = reverse("admin:blockchain_deploymentprocess_cancel", args=(process.pk,))

        detail = self.client.get(change_url)
        self.assertContains(detail, "Cancelar processo")
        self.assertContains(detail, "csrfmiddlewaretoken")
        self.assertEqual(self.client.get(cancel_url).status_code, 405)

        response = self.client.post(cancel_url, follow=True)
        process.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(process.status, DeploymentProcess.Status.CANCELED)
        self.assertEqual(process.steps.count(), 9)

    def test_admin_explains_why_a_sent_process_cannot_be_canceled(self) -> None:
        """The Admin reports the blocked action and leaves the process unchanged."""

        process = self.start_process()
        self.create_attempt(process.steps.first(), "0x" + "f" * 64)
        admin_user = User.objects.create_superuser(
            username="deployment-admin-blocked",
            email="deployment-admin-blocked@example.test",
            password="test-password",
        )
        self.client.force_login(admin_user)
        cancel_url = reverse("admin:blockchain_deploymentprocess_cancel", args=(process.pk,))

        response = self.client.post(cancel_url, follow=True)

        self.assertContains(response, "transação enviada")
        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)

    def test_admin_cancel_requires_permission_and_csrf(self) -> None:
        """Only an authorized, CSRF-protected Admin POST can cancel a process."""

        process = self.start_process()
        cancel_url = reverse("admin:blockchain_deploymentprocess_cancel", args=(process.pk,))
        staff_user = User.objects.create_user(
            username="deployment-viewer",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(staff_user)
        forbidden = self.client.post(cancel_url)
        self.assertEqual(forbidden.status_code, 403)

        admin_user = User.objects.create_superuser(
            username="deployment-csrf-admin",
            email="deployment-csrf-admin@example.test",
            password="test-password",
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(admin_user)
        csrf_forbidden = csrf_client.post(cancel_url)
        self.assertEqual(csrf_forbidden.status_code, 403)
        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)

    def test_start_rejects_each_preexisting_active_contract_kind(self) -> None:
        """Any active deployment kind blocks a fresh nine-contract process."""

        for index, kind in enumerate(DEPLOYMENT_ORDER, start=1):
            with self.subTest(kind=kind):
                contract = ContractInstance.objects.create(
                    company=self.company,
                    network=self.network,
                    kind=kind,
                    address="0x" + f"{index:040x}",
                    version="existing-1",
                )
                with self.assertRaises(ValidationError) as error:
                    self.start_process()

                self.assertIn(DEPLOYMENT_KIND_LABELS[kind], str(error.exception))
                self.assertEqual(DeploymentProcess.objects.count(), 0)
                self.assertTrue(ContractInstance.objects.filter(pk=contract.pk).exists())
                contract.active = False
                contract.save()

    def test_inactive_or_other_scope_contracts_do_not_block_start(self) -> None:
        """Only active contracts for the selected company and network conflict."""

        inactive = ContractInstance.objects.create(
            company=self.company,
            network=self.network,
            kind=ContractInstance.Kind.SOJA_COIN,
            address="0x" + f"{101:040x}",
            version="old-1",
            active=False,
        )
        other_company = Company.objects.create(
            legal_name="Another Deployment Company",
            cnpj="11444777000404",
        )
        other_company_contract = ContractInstance.objects.create(
            company=other_company,
            network=self.network,
            kind=ContractInstance.Kind.SOJA_COIN,
            address="0x" + f"{102:040x}",
            version="other-company-1",
        )
        other_network = Network.objects.create(
            name="Another Deployment Network",
            chain_id=31338,
            rpc_url="http://127.0.0.1:8545",
        )
        other_network_contract = ContractInstance.objects.create(
            company=self.company,
            network=other_network,
            kind=ContractInstance.Kind.SOJA_COIN,
            address="0x" + f"{103:040x}",
            version="other-network-1",
        )

        process = self.start_process()

        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)
        self.assertTrue(ContractInstance.objects.filter(pk=inactive.pk).exists())
        self.assertTrue(ContractInstance.objects.filter(pk=other_company_contract.pk).exists())
        self.assertTrue(ContractInstance.objects.filter(pk=other_network_contract.pk).exists())

    def test_existing_in_progress_or_needs_review_process_blocks_another_start(self) -> None:
        """A duplicate or unresolved process cannot begin for the same scope."""

        current = self.start_process()
        with self.assertRaisesMessage(ValidationError, "em andamento"):
            self.start_process()

        current.status = DeploymentProcess.Status.NEEDS_REVIEW
        current.save()
        with self.assertRaisesMessage(ValidationError, "revisão"):
            self.start_process()
        self.assertEqual(DeploymentProcess.objects.count(), 1)

    def test_reorged_deployment_updates_process_status_progress_and_admin_row(self) -> None:
        """A reorg removes the step from confirmed progress and preserves evidence."""

        process, transaction_record, contract = self.create_deployed_process()
        step = DeploymentStep.objects.get(process=process, position=1)
        attempt = DeploymentAttempt.objects.get(step=step)
        self.assertEqual(process.confirmed_step_count, 9)
        transaction_count = BlockchainTransaction.objects.count()

        mark_transaction_reorged(transaction_record, "The block left the canonical chain.")
        mark_transaction_reorged(transaction_record, "Repeated reorganization report.")

        process.refresh_from_db()
        transaction_record.refresh_from_db()
        step = DeploymentStep.objects.select_related("contract", "process").prefetch_related(
            "attempts__transaction"
        ).get(pk=step.pk)
        inline = DeploymentStepInline(DeploymentProcess, admin.site)
        self.assertEqual(process.status, DeploymentProcess.Status.NEEDS_REVIEW)
        self.assertEqual(process.confirmed_step_count, 8)
        self.assertEqual(inline.step_status(step), "Confirmação revertida")
        self.assertEqual(transaction_record.status, BlockchainTransaction.Status.REORGED)
        self.assertTrue(DeploymentProcess.objects.filter(pk=process.pk).exists())
        self.assertTrue(DeploymentStep.objects.filter(pk=step.pk, contract=contract).exists())
        self.assertTrue(DeploymentAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertTrue(BlockchainTransaction.objects.filter(pk=transaction_record.pk).exists())
        self.assertTrue(ContractInstance.objects.filter(pk=contract.pk).exists())
        self.assertEqual(BlockchainTransaction.objects.count(), transaction_count)


    def test_reorg_during_deployment_blocks_new_process_and_unsent_step(self) -> None:
        """An unresolved deployment cannot be restarted or submit another attempt."""

        process = self.start_process()
        current_step = process.steps.get(position=1)
        record, _ = self.link_confirmed_contract(current_step, 1)

        mark_transaction_reorged(record, "The block left the canonical chain.")

        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.NEEDS_REVIEW)
        with self.assertRaisesMessage(ValidationError, "revisão"):
            self.start_process()

        next_step = process.steps.get(position=2)
        pending = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            operation_type="contract_deployment",
            sender_address=self.sender_address,
        )
        DeploymentAttempt.objects.create(step=next_step, transaction=pending)
        with self.assertRaisesMessage(ValidationError, "em andamento"):
            submit_transaction(pending, "0x" + "7" * 64)
        pending.refresh_from_db()
        self.assertEqual(pending.status, BlockchainTransaction.Status.PREPARED)

    def test_reorged_deployment_is_shown_on_process_detail(self) -> None:
        """The detail page reports review status and the reduced confirmed count."""

        process, transaction_record, _ = self.create_deployed_process()
        mark_transaction_reorged(transaction_record, "The block left the canonical chain.")
        admin_user = User.objects.create_superuser(
            username="deployment-review-admin",
            email="deployment-review-admin@example.test",
            password="test-password",
        )
        self.client.force_login(admin_user)
        change_url = reverse("admin:blockchain_deploymentprocess_change", args=(process.pk,))

        response = self.client.get(change_url)

        self.assertContains(response, "Precisa de revisão")
        self.assertContains(response, "Confirmação revertida")
        self.assertContains(response, "8 de 9 contratos confirmados")

    def test_reorg_of_non_deployment_transaction_does_not_change_process(self) -> None:
        """Only a transaction attached to a deployment attempt affects that process."""

        process = self.start_process()
        unrelated = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            operation_type="test_operation",
            sender_address=self.sender_address,
        )
        unrelated = submit_transaction(unrelated, "0x" + "9" * 64)
        unrelated = confirm_transaction(unrelated, 50, "0x" + "8" * 64)

        mark_transaction_reorged(unrelated, "An unrelated operation was reorganized.")

        process.refresh_from_db()
        self.assertEqual(process.status, DeploymentProcess.Status.IN_PROGRESS)


class DeploymentWalletFlowTests(TestCase):
    """Exercise the persisted MetaMask deployment flow and its recovery paths."""

    owner_address = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    sender_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def setUp(self) -> None:
        self.company = Company.objects.create(
            legal_name="Wallet Flow Company",
            cnpj="11222333000181",
        )
        self.network = Network.objects.create(
            name="Wallet Flow Network",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )
        self.process = start_deployment_process(
            company=self.company,
            network=self.network,
            owner_address=self.owner_address,
        )
        self.step = self.process.steps.get(position=1)

    def create_prepared_attempt(self) -> DeploymentAttempt:
        """Prepare and persist one first-step attempt through the public service."""

        result = start_deployment_attempt(
            step_id=self.step.pk,
            sender_address=self.sender_address,
            wallet_chain_id=self.network.chain_id,
        )
        return DeploymentAttempt.objects.select_related("transaction").get(
            pk=result.attempt_id
        )

    def valid_rpc_transaction(
        self,
        attempt: DeploymentAttempt,
        transaction_hash: str,
        **changes: object,
    ) -> dict[str, object]:
        """Build an RPC transaction matching the saved attempt by default."""

        transaction = {
            "hash": bytes.fromhex(transaction_hash[2:]),
            "from": self.sender_address,
            "to": None,
            "chainId": self.network.chain_id,
            "input": bytes.fromhex(attempt.prepared_data[2:]),
        }
        transaction.update(changes)
        return transaction

    def rpc_client(self, transaction: dict[str, object]) -> Mock:
        """Return a small fake Web3 client for deterministic service tests."""

        client = Mock()
        client.eth.get_transaction.return_value = transaction
        return client

    def test_service_creates_transaction_and_attempt_atomically(self) -> None:
        """A prepared deployment stores its scope, signer and exact creation data."""

        prepared = start_deployment_attempt(
            step_id=self.step.pk,
            sender_address=self.sender_address.upper().replace("0X", "0x"),
            wallet_chain_id=self.network.chain_id,
        )
        attempt = DeploymentAttempt.objects.select_related("transaction").get(
            pk=prepared.attempt_id
        )

        self.assertEqual(prepared.chain_id, self.network.chain_id)
        self.assertTrue(prepared.transaction_data.startswith("0x"))
        self.assertEqual(attempt.step_id, self.step.pk)
        self.assertEqual(attempt.prepared_data, prepared.transaction_data.lower())
        self.assertTrue(attempt.is_open)
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.PREPARED)
        self.assertEqual(attempt.transaction.company_id, self.company.pk)
        self.assertEqual(attempt.transaction.network_id, self.network.pk)
        self.assertEqual(attempt.transaction.sender_address, self.sender_address)
        self.assertEqual(DeploymentAttempt.objects.count(), 1)
        self.assertEqual(BlockchainTransaction.objects.count(), 1)

    def test_second_preparation_reuses_only_the_same_untouched_attempt(self) -> None:
        """Retries before the wallet prompt reuse one attempt and other signers wait."""

        first = self.create_prepared_attempt()
        repeated = start_deployment_attempt(
            step_id=self.step.pk,
            sender_address=self.sender_address,
            wallet_chain_id=self.network.chain_id,
        )
        self.assertEqual(repeated.attempt_id, first.pk)
        self.assertEqual(DeploymentAttempt.objects.count(), 1)

        with self.assertRaisesMessage(ValidationError, "tentativa"):
            start_deployment_attempt(
                step_id=self.step.pk,
                sender_address="0xcccccccccccccccccccccccccccccccccccccccc",
                wallet_chain_id=self.network.chain_id,
            )
        self.assertEqual(DeploymentAttempt.objects.count(), 1)

    def test_wallet_network_mismatch_is_rejected_before_attempt_creation(self) -> None:
        """A wallet on another chain cannot create an attempt for this process."""

        with self.assertRaisesMessage(ValidationError, "usa"):
            start_deployment_attempt(
                step_id=self.step.pk,
                sender_address=self.sender_address,
                wallet_chain_id=1,
            )
        self.assertFalse(DeploymentAttempt.objects.exists())
        self.assertFalse(BlockchainTransaction.objects.exists())

    def test_wallet_rejection_is_recorded_and_allows_a_new_attempt(self) -> None:
        """Only a clear MetaMask rejection closes the attempt and releases its slot."""

        attempt = self.create_prepared_attempt()
        mark_deployment_submission_started(attempt.pk)
        rejected = fail_deployment_attempt(attempt.pk)
        rejected.refresh_from_db()
        rejected.transaction.refresh_from_db()

        self.assertFalse(rejected.is_open)
        self.assertEqual(rejected.transaction.status, BlockchainTransaction.Status.FAILED)
        self.assertIn("rejeitou", rejected.transaction.error_message)

        replacement = self.create_prepared_attempt()
        self.assertNotEqual(replacement.pk, rejected.pk)
        self.assertEqual(DeploymentAttempt.objects.count(), 2)

        canceled = cancel_deployment_process(self.process.pk)
        self.assertEqual(canceled.status, DeploymentProcess.Status.CANCELED)

    def test_duplicate_start_marker_is_rejected_to_prevent_a_second_wallet_send(self) -> None:
        """A persisted wallet prompt cannot be opened a second time for one attempt."""

        attempt = self.create_prepared_attempt()
        first = mark_deployment_submission_started(attempt.pk)

        with self.assertRaisesMessage(ValidationError, "já foi iniciado"):
            mark_deployment_submission_started(attempt.pk)

        first.refresh_from_db()
        self.assertIsNotNone(first.submission_started_at)
        self.assertTrue(first.is_open)

    def test_uncertain_send_keeps_attempt_open_and_blocks_cancel_and_retry(self) -> None:
        """An unknown wallet response cannot be treated as unsent or canceled."""

        attempt = self.create_prepared_attempt()
        mark_deployment_submission_started(attempt.pk)

        with self.assertRaisesMessage(ValidationError, "conferida"):
            start_deployment_attempt(
                step_id=self.step.pk,
                sender_address=self.sender_address,
                wallet_chain_id=self.network.chain_id,
            )
        with self.assertRaisesMessage(ValidationError, "transação enviada"):
            cancel_deployment_process(self.process.pk)

        attempt.refresh_from_db()
        attempt.transaction.refresh_from_db()
        self.process.refresh_from_db()
        self.assertTrue(attempt.is_open)
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.PREPARED)
        self.assertEqual(self.process.status, DeploymentProcess.Status.IN_PROGRESS)

    def test_hash_is_verified_and_repeated_hash_does_not_create_another_attempt(self) -> None:
        """A valid hash submits once, and repeating it returns the same transaction."""

        attempt = self.create_prepared_attempt()
        mark_deployment_submission_started(attempt.pk)
        tx_hash = "0x" + "c" * 64
        client = self.rpc_client(self.valid_rpc_transaction(attempt, tx_hash))

        with patch("blockchain.services.deployment.connect_to_rpc", return_value=client):
            first = register_deployment_transaction_hash(
                attempt_id=attempt.pk,
                transaction_hash=tx_hash,
            )
            repeated = register_deployment_transaction_hash(
                attempt_id=attempt.pk,
                transaction_hash=tx_hash,
            )

        self.assertIsNotNone(first)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(first.transaction_hash, tx_hash)
        self.assertEqual(first.status, BlockchainTransaction.Status.SUBMITTED)
        self.assertEqual(DeploymentAttempt.objects.count(), 1)
        self.assertEqual(BlockchainTransaction.objects.count(), 1)
        self.assertEqual(client.eth.get_transaction.call_count, 2)

    def test_invalid_hash_is_rejected_before_rpc_lookup(self) -> None:
        """Malformed wallet input cannot be submitted or sent to the RPC."""

        attempt = self.create_prepared_attempt()
        mark_deployment_submission_started(attempt.pk)
        with patch("blockchain.services.deployment.connect_to_rpc") as connect:
            with self.assertRaisesMessage(ValidationError, "inválida"):
                register_deployment_transaction_hash(
                    attempt_id=attempt.pk,
                    transaction_hash="0x1234",
                )
        connect.assert_not_called()

    def test_unknown_rpc_hash_can_be_retried_on_the_same_attempt(self) -> None:
        """A temporarily missing RPC transaction leaves the attempt open for retry."""

        attempt = self.create_prepared_attempt()
        mark_deployment_submission_started(attempt.pk)
        tx_hash = "0x" + "d" * 64
        client = self.rpc_client(self.valid_rpc_transaction(attempt, tx_hash))
        client.eth.get_transaction.side_effect = [
            TransactionNotFound(tx_hash),
            self.valid_rpc_transaction(attempt, tx_hash),
        ]

        with patch("blockchain.services.deployment.connect_to_rpc", return_value=client):
            first = register_deployment_transaction_hash(
                attempt_id=attempt.pk,
                transaction_hash=tx_hash,
            )
            attempt.refresh_from_db()
            second = register_deployment_transaction_hash(
                attempt_id=attempt.pk,
                transaction_hash=tx_hash,
            )

        self.assertIsNone(first)
        self.assertTrue(attempt.is_open)
        self.assertEqual(second.status, BlockchainTransaction.Status.SUBMITTED)
        self.assertEqual(DeploymentAttempt.objects.count(), 1)

    def test_database_failure_can_retry_the_same_hash_without_new_attempt(self) -> None:
        """A failed local write leaves the prepared attempt available for recovery."""

        attempt = self.create_prepared_attempt()
        mark_deployment_submission_started(attempt.pk)
        tx_hash = "0x" + "e" * 64
        client = self.rpc_client(self.valid_rpc_transaction(attempt, tx_hash))

        with (
            patch("blockchain.services.deployment.connect_to_rpc", return_value=client),
            patch(
                "blockchain.services.deployment.submit_transaction",
                side_effect=DatabaseError("temporary database error"),
            ),
        ):
            with self.assertRaises(DatabaseError):
                register_deployment_transaction_hash(
                    attempt_id=attempt.pk,
                    transaction_hash=tx_hash,
                )

        attempt.refresh_from_db()
        attempt.transaction.refresh_from_db()
        self.assertTrue(attempt.is_open)
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.PREPARED)
        self.assertIsNone(attempt.transaction.transaction_hash)

        with patch("blockchain.services.deployment.connect_to_rpc", return_value=client):
            recovered = register_deployment_transaction_hash(
                attempt_id=attempt.pk,
                transaction_hash=tx_hash,
            )
        self.assertEqual(recovered.status, BlockchainTransaction.Status.SUBMITTED)
        self.assertEqual(DeploymentAttempt.objects.count(), 1)

    def test_rpc_rejects_transaction_from_a_different_wallet(self) -> None:
        """The RPC sender must match the operator address saved at preparation."""

        attempt = self.create_prepared_attempt()
        mark_deployment_submission_started(attempt.pk)
        tx_hash = "0x" + "1" * 64
        client = self.rpc_client(
            self.valid_rpc_transaction(
                attempt,
                tx_hash,
                **{"from": "0xcccccccccccccccccccccccccccccccccccccccc"},
            )
        )

        with patch("blockchain.services.deployment.connect_to_rpc", return_value=client):
            with self.assertRaisesMessage(ValidationError, "outra carteira"):
                register_deployment_transaction_hash(
                    attempt_id=attempt.pk,
                    transaction_hash=tx_hash,
                )
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.PREPARED)

    def test_rpc_rejects_transaction_with_different_creation_data(self) -> None:
        """The on-chain init code must exactly match the prepared constructor data."""

        attempt = self.create_prepared_attempt()
        mark_deployment_submission_started(attempt.pk)
        tx_hash = "0x" + "2" * 64
        client = self.rpc_client(
            self.valid_rpc_transaction(attempt, tx_hash, input=b"different data")
        )

        with patch("blockchain.services.deployment.connect_to_rpc", return_value=client):
            with self.assertRaisesMessage(ValidationError, "não correspondem"):
                register_deployment_transaction_hash(
                    attempt_id=attempt.pk,
                    transaction_hash=tx_hash,
                )

    def test_rpc_rejects_wrong_chain_id_or_non_creation_transaction(self) -> None:
        """The verified transaction must be on the saved chain and create a contract."""

        attempt = self.create_prepared_attempt()
        mark_deployment_submission_started(attempt.pk)
        tx_hash = "0x" + "3" * 64

        for changes, message in (
            ({"chainId": 1}, "rede diferente"),
            ({"to": "0xdddddddddddddddddddddddddddddddddddddddd"}, "não cria um contrato"),
        ):
            with self.subTest(changes=changes):
                client = self.rpc_client(
                    self.valid_rpc_transaction(attempt, tx_hash, **changes)
                )
                with patch(
                    "blockchain.services.deployment.connect_to_rpc",
                    return_value=client,
                ):
                    with self.assertRaisesMessage(ValidationError, message):
                        register_deployment_transaction_hash(
                            attempt_id=attempt.pk,
                            transaction_hash=tx_hash,
                        )

    def test_admin_prepare_route_checks_permission_post_csrf_and_scope(self) -> None:
        """Only an authorized CSRF-protected POST can prepare its own process step."""

        prepare_url = reverse(
            "admin:blockchain_deploymentprocess_prepare_step",
            args=(self.process.pk, self.step.pk),
        )
        viewer = User.objects.create_user(
            username="wallet-flow-viewer",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(prepare_url).status_code, 403)

        operator = User.objects.create_superuser(
            username="wallet-flow-admin",
            email="wallet-flow-admin@example.test",
            password="test-password",
        )
        self.client.force_login(operator)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(operator)
        self.assertEqual(csrf_client.get(prepare_url).status_code, 405)
        self.assertEqual(
            csrf_client.post(prepare_url, {"wallet_chain_id": "31337"}).status_code,
            403,
        )

        response = self.client.post(
            prepare_url,
            {
                "operator_address": self.sender_address,
                "wallet_chain_id": "31337",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["chain_id"], self.network.chain_id)
        self.assertIn("transaction_data", response.json())

        other_company = Company.objects.create(
            legal_name="Another Wallet Company",
            cnpj="11444777000404",
        )
        other_process = start_deployment_process(
            company=other_company,
            network=self.network,
            owner_address=self.owner_address,
        )
        wrong_scope_url = reverse(
            "admin:blockchain_deploymentprocess_prepare_step",
            args=(self.process.pk, other_process.steps.first().pk),
        )
        self.assertEqual(self.client.post(wrong_scope_url).status_code, 404)

    def test_admin_process_page_shows_wallet_action_only_for_available_step(self) -> None:
        """The process page provides MetaMask controls for the next ordered step."""

        operator = User.objects.create_superuser(
            username="wallet-page-admin",
            email="wallet-page-admin@example.test",
            password="test-password",
        )
        self.client.force_login(operator)
        detail_url = reverse(
            "admin:blockchain_deploymentprocess_change",
            args=(self.process.pk,),
        )

        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Conectar carteira e implantar")
        self.assertContains(response, 'data-chain-id="31337"')
        self.assertContains(response, "blockchain/deployment.js")
        self.assertContains(response, "Aguardando etapa anterior")

    def test_admin_attempt_routes_require_attempt_step_and_process_to_match(self) -> None:
        """Attempt URLs cannot be moved across step or process boundaries."""

        operator = User.objects.create_superuser(
            username="wallet-attempt-admin",
            email="wallet-attempt-admin@example.test",
            password="test-password",
        )
        self.client.force_login(operator)
        attempt = self.create_prepared_attempt()
        started_url = reverse(
            "admin:blockchain_deploymentprocess_start_attempt_submission",
            args=(self.process.pk, self.step.pk, attempt.pk),
        )
        response = self.client.post(started_url)
        self.assertEqual(response.status_code, 200)

        other_company = Company.objects.create(
            legal_name="Third Wallet Company",
            cnpj="11444777000503",
        )
        other_process = start_deployment_process(
            company=other_company,
            network=self.network,
            owner_address=self.owner_address,
        )
        wrong_url = reverse(
            "admin:blockchain_deploymentprocess_start_attempt_submission",
            args=(other_process.pk, other_process.steps.first().pk, attempt.pk),
        )
        self.assertEqual(self.client.post(wrong_url).status_code, 404)

    def test_admin_prepare_rejects_process_outside_in_progress(self) -> None:
        """The Admin route cannot create a transaction for a canceled process."""

        cancel_deployment_process(self.process.pk)
        operator = User.objects.create_superuser(
            username="wallet-flow-canceled-admin",
            email="wallet-flow-canceled@example.test",
            password="test-password",
        )
        self.client.force_login(operator)
        prepare_url = reverse(
            "admin:blockchain_deploymentprocess_prepare_step",
            args=(self.process.pk, self.step.pk),
        )

        response = self.client.post(
            prepare_url,
            {
                "operator_address": self.sender_address,
                "wallet_chain_id": "31337",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("andamento", response.json()["error"])
        self.assertFalse(DeploymentAttempt.objects.exists())

    def test_rpc_registration_is_scoped_to_attempt_url_and_csrf_protected(self) -> None:
        """The hash endpoint checks URL scope, wallet permission and CSRF."""

        attempt = self.create_prepared_attempt()
        mark_deployment_submission_started(attempt.pk)
        register_url = reverse(
            "admin:blockchain_deploymentprocess_register_hash",
            args=(self.process.pk, self.step.pk, attempt.pk),
        )
        viewer = User.objects.create_user(
            username="wallet-hash-viewer",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(register_url).status_code, 403)

        operator = User.objects.create_superuser(
            username="wallet-hash-admin",
            email="wallet-hash-admin@example.test",
            password="test-password",
        )
        self.client.force_login(operator)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(operator)
        started_url = reverse(
            "admin:blockchain_deploymentprocess_start_attempt_submission",
            args=(self.process.pk, self.step.pk, attempt.pk),
        )
        rejected_url = reverse(
            "admin:blockchain_deploymentprocess_reject_attempt",
            args=(self.process.pk, self.step.pk, attempt.pk),
        )
        for action_url in (started_url, register_url, rejected_url):
            with self.subTest(action_url=action_url):
                self.assertEqual(csrf_client.get(action_url).status_code, 405)
                self.assertEqual(csrf_client.post(action_url).status_code, 403)

        other_step = self.process.steps.get(position=2)
        wrong_scope = reverse(
            "admin:blockchain_deploymentprocess_register_hash",
            args=(self.process.pk, other_step.pk, attempt.pk),
        )
        self.assertEqual(self.client.post(wrong_scope).status_code, 404)


class DeploymentReceiptConfirmationTests(TestCase):
    """Verify receipt reconciliation and atomic contract registration."""

    owner_address = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    sender_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    contract_address = "0xcccccccccccccccccccccccccccccccccccccccc"
    transaction_hash = "0x" + "1" * 64
    block_hash = "0x" + "2" * 64

    def setUp(self) -> None:
        self.company = Company.objects.create(
            legal_name="Receipt Confirmation Company",
            cnpj="22333444000109",
        )
        self.network = Network.objects.create(
            name="Receipt Confirmation Network",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )
        self.process = start_deployment_process(
            company=self.company,
            network=self.network,
            owner_address=self.owner_address,
        )
        self.step = self.process.steps.get(position=1)
        result = start_deployment_attempt(
            step_id=self.step.pk,
            sender_address=self.sender_address,
            wallet_chain_id=self.network.chain_id,
        )
        self.attempt = DeploymentAttempt.objects.select_related("transaction").get(
            pk=result.attempt_id
        )
        mark_deployment_submission_started(self.attempt.pk)
        submit_transaction(self.attempt.transaction, self.transaction_hash)

    def rpc(self, *, status: int = 1, code: bytes = b"\x60\x00") -> Mock:
        """Build an RPC client whose transaction matches the saved attempt."""

        client = Mock()
        client.eth.get_transaction.return_value = {
            "hash": bytes.fromhex(self.transaction_hash[2:]),
            "from": self.sender_address,
            "to": None,
            "chainId": self.network.chain_id,
            "input": bytes.fromhex(self.attempt.prepared_data[2:]),
        }
        client.eth.get_transaction_receipt.return_value = {
            "transactionHash": bytes.fromhex(self.transaction_hash[2:]),
            "status": status,
            "blockNumber": 42,
            "blockHash": bytes.fromhex(self.block_hash[2:]),
            "contractAddress": self.contract_address,
        }
        client.eth.get_code.return_value = code
        return client

    @patch("blockchain.services.deployment_confirmation.connect_to_rpc")
    def test_successful_receipt_saves_contract_and_links_step(
        self, connect: Mock
    ) -> None:
        """A valid receipt confirms the transaction and saves its contract."""

        connect.return_value = self.rpc()

        result = confirm_deployment_attempt(self.attempt.pk)

        self.step.refresh_from_db()
        self.attempt.refresh_from_db()
        self.attempt.transaction.refresh_from_db()
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.contract.address_normalized, self.contract_address)
        self.assertEqual(
            result.contract.deployment_transaction_hash,
            self.transaction_hash,
        )
        self.assertTrue(result.contract.version.startswith("sha256:"))
        self.assertEqual(len(result.contract.version), 71)
        self.assertEqual(self.step.contract_id, result.contract.pk)
        self.assertEqual(
            self.attempt.transaction.status,
            BlockchainTransaction.Status.CONFIRMED,
        )
        self.assertFalse(self.attempt.is_open)

    @patch("blockchain.services.deployment_confirmation.connect_to_rpc")
    def test_repeated_confirmation_returns_same_saved_contract(
        self, connect: Mock
    ) -> None:
        """A retry after local success never creates a duplicate contract."""

        connect.return_value = self.rpc()
        first = confirm_deployment_attempt(self.attempt.pk)
        second = confirm_deployment_attempt(self.attempt.pk)

        self.assertEqual(first.contract.pk, second.contract.pk)
        self.assertEqual(ContractInstance.objects.count(), 1)
        connect.assert_called_once()

    @patch("blockchain.services.deployment_confirmation.connect_to_rpc")
    def test_missing_receipt_leaves_attempt_and_step_unchanged(
        self, connect: Mock
    ) -> None:
        """A pending network transaction does not create local contract data."""

        client = self.rpc()
        client.eth.get_transaction_receipt.side_effect = TransactionNotFound(
            "receipt not indexed yet"
        )
        connect.return_value = client

        result = confirm_deployment_attempt(self.attempt.pk)

        self.assertEqual(result.status, "pending")
        self.assertEqual(ContractInstance.objects.count(), 0)
        self.step.refresh_from_db()
        self.assertIsNone(self.step.contract_id)
        self.attempt.transaction.refresh_from_db()
        self.assertEqual(
            self.attempt.transaction.status,
            BlockchainTransaction.Status.SUBMITTED,
        )

    @patch("blockchain.services.deployment_confirmation.connect_to_rpc")
    def test_reverted_receipt_marks_failure_without_contract(
        self, connect: Mock
    ) -> None:
        """A reverted creation is retryable and preserves its transaction record."""

        connect.return_value = self.rpc(status=0)

        result = confirm_deployment_attempt(self.attempt.pk)

        self.attempt.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.transaction.status,
            BlockchainTransaction.Status.FAILED,
        )
        self.assertFalse(self.attempt.is_open)
        self.assertEqual(ContractInstance.objects.count(), 0)

    @patch("blockchain.services.deployment_confirmation.connect_to_rpc")
    def test_rpc_transaction_with_wrong_sender_is_rejected(
        self, connect: Mock
    ) -> None:
        """The receipt is not trusted if its transaction sender differs."""

        client = self.rpc()
        client.eth.get_transaction.return_value["from"] = self.owner_address
        connect.return_value = client

        with self.assertRaisesMessage(ValidationError, "outra carteira"):
            confirm_deployment_attempt(self.attempt.pk)
        self.assertEqual(ContractInstance.objects.count(), 0)

    @patch("blockchain.services.deployment_confirmation.connect_to_rpc")
    def test_rpc_transaction_with_different_creation_data_is_rejected(
        self, connect: Mock
    ) -> None:
        """A successful receipt cannot register a different contract payload."""

        client = self.rpc()
        client.eth.get_transaction.return_value["input"] = b"different creation data"
        connect.return_value = client

        with self.assertRaisesMessage(ValidationError, "diferem"):
            confirm_deployment_attempt(self.attempt.pk)
        self.assertEqual(ContractInstance.objects.count(), 0)

    @patch("blockchain.services.deployment_confirmation.connect_to_rpc")
    def test_success_receipt_without_code_does_not_confirm_locally(
        self, connect: Mock
    ) -> None:
        """The created address must contain code at the receipt block."""

        connect.return_value = self.rpc(code=b"")

        with self.assertRaisesMessage(ValidationError, "não contém código"):
            confirm_deployment_attempt(self.attempt.pk)
        self.assertEqual(ContractInstance.objects.count(), 0)
        self.attempt.transaction.refresh_from_db()
        self.assertEqual(
            self.attempt.transaction.status,
            BlockchainTransaction.Status.SUBMITTED,
        )

    @patch("blockchain.services.deployment_confirmation.register_contract")
    @patch("blockchain.services.deployment_confirmation.connect_to_rpc")
    def test_database_error_rolls_back_transaction_confirmation(
        self, connect: Mock, register: Mock
    ) -> None:
        """A local contract-save failure leaves the attempt retryable."""

        connect.return_value = self.rpc()
        register.side_effect = ValidationError("contract write failed")

        with self.assertRaisesMessage(ValidationError, "contract write failed"):
            confirm_deployment_attempt(self.attempt.pk)

        self.attempt.transaction.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(
            self.attempt.transaction.status,
            BlockchainTransaction.Status.SUBMITTED,
        )
        self.assertTrue(self.attempt.is_open)
        self.assertIsNone(self.step.contract_id)

    @patch("blockchain.services.deployment_confirmation.connect_to_rpc")
    def test_all_nine_contracts_confirm_in_order_and_finish_process(
        self, connect: Mock
    ) -> None:
        """Each receipt unlocks only the next deployment and the ninth completes it."""

        for position, step in enumerate(
            self.process.steps.order_by("position"),
            start=1,
        ):
            # The public preparation service enforces dependencies from earlier
            # steps, so this loop exercises the same order as the Admin flow.
            if position == 1:
                attempt = self.attempt
                transaction_hash = self.transaction_hash
            else:
                prepared = start_deployment_attempt(
                    step_id=step.pk,
                    sender_address=self.sender_address,
                    wallet_chain_id=self.network.chain_id,
                )
                attempt = DeploymentAttempt.objects.select_related("transaction").get(
                    pk=prepared.attempt_id
                )
                mark_deployment_submission_started(attempt.pk)
                transaction_hash = f"0x{position + 10:064x}"
                submit_transaction(attempt.transaction, transaction_hash)

            contract_address = "0x" + f"{position + 100:040x}"
            client = Mock()
            client.eth.get_transaction.return_value = {
                "hash": bytes.fromhex(transaction_hash[2:]),
                "from": self.sender_address,
                "to": None,
                "chainId": self.network.chain_id,
                "input": bytes.fromhex(attempt.prepared_data[2:]),
            }
            client.eth.get_transaction_receipt.return_value = {
                "transactionHash": bytes.fromhex(transaction_hash[2:]),
                "status": 1,
                "blockNumber": position,
                "blockHash": bytes.fromhex(f"{position + 200:064x}"),
                "contractAddress": contract_address,
            }
            client.eth.get_code.return_value = b"\x60\x00"
            connect.return_value = client

            result = confirm_deployment_attempt(attempt.pk)

            self.assertEqual(result.status, "confirmed")
            self.assertEqual(result.contract.kind, step.kind)
            self.assertEqual(result.contract.address_normalized, contract_address)
            self.process.refresh_from_db()
            if position < len(DEPLOYMENT_ORDER):
                self.assertEqual(
                    self.process.status,
                    DeploymentProcess.Status.IN_PROGRESS,
                )

        self.process.refresh_from_db()
        self.assertEqual(self.process.status, DeploymentProcess.Status.DEPLOYED)
        self.assertEqual(self.process.confirmed_step_count, len(DEPLOYMENT_ORDER))
        self.assertEqual(
            ContractInstance.objects.filter(
                company=self.company,
                network=self.network,
                deployment_step__process=self.process,
            ).count(),
            len(DEPLOYMENT_ORDER),
        )
        for step in self.process.steps.select_related("contract").prefetch_related(
            "attempts__transaction"
        ):
            self.assertTrue(step.is_confirmed)
            attempt = step.linked_deployment_attempt
            self.assertIsNotNone(attempt)
            self.assertEqual(
                step.contract.deployment_transaction_hash,
                attempt.transaction.transaction_hash,
            )


class DeploymentConfirmationAdminTests(TestCase):
    """Protect the Admin receipt action and keep it read-only with respect to sending."""

    def setUp(self) -> None:
        self.company = Company.objects.create(
            legal_name="Confirmation Admin Company",
            cnpj="33444555000198",
        )
        self.network = Network.objects.create(
            name="Confirmation Admin Network",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )
        self.process = start_deployment_process(
            company=self.company,
            network=self.network,
            owner_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.step = self.process.steps.get(position=1)
        prepared = start_deployment_attempt(
            step_id=self.step.pk,
            sender_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            wallet_chain_id=self.network.chain_id,
        )
        self.attempt = DeploymentAttempt.objects.get(pk=prepared.attempt_id)
        self.operator = User.objects.create_superuser(
            username="deployment-confirmation-admin",
            email="deployment-confirmation-admin@example.test",
            password="test-password",
        )
        self.url = reverse(
            "admin:blockchain_deploymentprocess_confirm_attempt",
            args=(self.process.pk, self.step.pk, self.attempt.pk),
        )

    @patch("blockchain.admin.confirm_deployment_attempt")
    def test_confirmation_route_returns_pending_without_sending(
        self, confirm: Mock
    ) -> None:
        """A pending receipt response asks the operator to check the same attempt later."""

        confirm.return_value = DeploymentConfirmationResult(
            status="pending",
            transaction=self.attempt.transaction,
        )
        self.client.force_login(self.operator)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "pending")
        confirm.assert_called_once_with(self.attempt.pk)

    @patch("blockchain.admin.confirm_deployment_attempt")
    def test_rpc_error_preserves_attempt_for_the_same_confirmation_retry(
        self, confirm: Mock
    ) -> None:
        """An unavailable RPC leaves the local attempt unchanged and retryable."""

        confirm.side_effect = Web3Exception("local RPC is offline")
        self.client.force_login(self.operator)

        response = self.client.post(self.url)

        self.attempt.refresh_from_db()
        self.attempt.transaction.refresh_from_db()
        self.assertEqual(response.status_code, 503)
        self.assertTrue(self.attempt.is_open)
        self.assertIsNone(self.attempt.transaction.transaction_hash)
        self.assertEqual(
            self.attempt.transaction.status,
            BlockchainTransaction.Status.PREPARED,
        )
        confirm.assert_called_once_with(self.attempt.pk)

    def test_confirmation_route_requires_post_permission_and_csrf(self) -> None:
        """Only an authorized Admin POST with CSRF can consult the receipt."""

        viewer = User.objects.create_user(
            username="deployment-confirmation-viewer",
            email="deployment-confirmation-viewer@example.test",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.client.post(self.url).status_code, 403)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.operator)
        self.assertEqual(csrf_client.post(self.url).status_code, 403)

    def test_confirmation_route_hides_attempt_from_another_step(self) -> None:
        """URL identifiers must all refer to the same process and attempt."""

        other_step = self.process.steps.get(position=2)
        wrong_url = reverse(
            "admin:blockchain_deploymentprocess_confirm_attempt",
            args=(self.process.pk, other_step.pk, self.attempt.pk),
        )
        self.client.force_login(self.operator)

        self.assertEqual(self.client.post(wrong_url).status_code, 404)


class DeploymentConfigurationAttemptModelTests(TestCase):
    """Keep configuration calls tied to their process and transaction history."""

    owner_address = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    sender_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    sojacoin_address = "0xcccccccccccccccccccccccccccccccccccccccc"
    registry_address = "0xdddddddddddddddddddddddddddddddddddddddd"

    def setUp(self) -> None:
        self.company = Company.objects.create(
            legal_name="Configuration Attempt Company",
            cnpj="44555666000187",
        )
        self.network = Network.objects.create(
            name="Configuration Attempt Network",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )
        self.process = start_deployment_process(
            company=self.company,
            network=self.network,
            owner_address=self.owner_address,
        )
        self.soja_coin = ContractInstance.objects.create(
            company=self.company,
            network=self.network,
            kind=ContractInstance.Kind.SOJA_COIN,
            address=self.sojacoin_address,
            version="test-artifact",
        )

    def create_attempt(self, *, kind: str = DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY) -> DeploymentConfigurationAttempt:
        """Create a valid local record for one prepared configuration call."""

        transaction_record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            contract=self.soja_coin,
            operation_type=CONTRACT_CONFIGURATION_OPERATION_TYPE,
            sender_address=self.sender_address,
        )
        encoded_address = "0" * 24 + self.registry_address[2:]
        return DeploymentConfigurationAttempt.objects.create(
            process=self.process,
            kind=kind,
            transaction=transaction_record,
            expected_address=self.registry_address,
            prepared_data="0x12345678" + encoded_address,
        )

    def test_only_one_open_attempt_exists_per_process_and_configuration(self) -> None:
        """The model/database slot prevents parallel calls for the same link."""

        first = self.create_attempt()
        transaction_record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            contract=self.soja_coin,
            operation_type=CONTRACT_CONFIGURATION_OPERATION_TYPE,
            sender_address=self.sender_address,
        )
        duplicate = DeploymentConfigurationAttempt(
            process=self.process,
            kind=DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY,
            transaction=transaction_record,
            expected_address=self.registry_address,
            prepared_data="0x12345678" + "0" * 24 + self.registry_address[2:],
        )

        with self.assertRaises(ValidationError):
            duplicate.save()

        self.assertTrue(first.is_open)
        self.assertEqual(DeploymentConfigurationAttempt.objects.count(), 1)

    def test_closed_attempt_is_preserved_and_allows_a_later_attempt(self) -> None:
        """Closing a failed attempt keeps its history and frees the unique slot."""

        first = self.create_attempt()
        first.is_open = False
        first.save(update_fields=("is_open", "updated_at"))

        later = self.create_attempt()

        self.assertEqual(DeploymentConfigurationAttempt.objects.count(), 2)
        first.refresh_from_db()
        self.assertFalse(first.is_open)
        self.assertTrue(later.is_open)

    def test_prepared_call_cannot_be_changed_after_attempt_creation(self) -> None:
        """The transaction payload and expected address remain auditable."""

        attempt = self.create_attempt()
        attempt.prepared_data = "0x87654321" + "0" * 24 + self.registry_address[2:]

        with self.assertRaisesMessage(ValidationError, "não podem ser alterados"):
            attempt.save()

    def test_attempt_exposes_sender_and_lifecycle_state_from_its_transaction(self) -> None:
        """The linked lifecycle record is the source of sender and status data."""

        attempt = self.create_attempt()

        self.assertEqual(attempt.status, BlockchainTransaction.Status.PREPARED)
        self.assertEqual(attempt.transaction.sender_address, self.sender_address)
        self.assertIsNone(attempt.transaction.transaction_hash)


class DeploymentConfigurationServiceTests(TestCase):
    """Verify owner-only preparation, wallet hash checks and receipt outcomes."""

    owner_address = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    other_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    transaction_hash = "0x" + "a" * 64
    block_hash = "0x" + "b" * 64
    zero_address = "0x" + "0" * 40

    def setUp(self) -> None:
        self.company = Company.objects.create(
            legal_name="Configuration Service Company",
            cnpj="55666777000166",
        )
        self.network = Network.objects.create(
            name="Configuration Service Network",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )
        self.process = start_deployment_process(
            company=self.company,
            network=self.network,
            owner_address=self.owner_address,
        )
        self.contracts: dict[str, ContractInstance] = {}
        for index, step in enumerate(self.process.steps.order_by("position"), start=1):
            transaction_hash = f"0x{index + 500:064x}"
            contract = ContractInstance.objects.create(
                company=self.company,
                network=self.network,
                kind=step.kind,
                address="0x" + f"{index + 500:040x}",
                version="test-artifact",
                deployment_transaction_hash=transaction_hash,
            )
            record = BlockchainTransaction.objects.create(
                company=self.company,
                network=self.network,
                operation_type=DEPLOYMENT_OPERATION_TYPE,
                sender_address=self.other_address,
            )
            DeploymentAttempt.objects.create(step=step, transaction=record)
            submit_transaction(record, transaction_hash)
            confirm_transaction(record, index, "0x" + f"{index + 600:064x}")
            step.contract = contract
            step.save()
            self.contracts[step.kind] = contract

        self.process.refresh_from_db()
        self.process.status = DeploymentProcess.Status.DEPLOYED
        self.process.save()
        self.sojacoin = self.contracts[ContractInstance.Kind.SOJA_COIN]
        self.registry = self.contracts[ContractInstance.Kind.UNIT_REGISTRY]
        self.batches = self.contracts[ContractInstance.Kind.ISSUANCE_BATCHES]
        self.soja_web3 = Mock()
        self.soja_web3.address = self.sojacoin.address_normalized
        self.soja_web3.functions.owner.return_value.call.return_value = self.owner_address
        self.soja_web3.functions.unitRegistry.return_value.call.return_value = self.zero_address
        self.soja_web3.functions.issuanceBatchModule.return_value.call.return_value = (
            self.zero_address
        )
        self.soja_web3.functions.configureUnitRegistry.return_value._encode_transaction_data.return_value = (
            self._calldata(self.registry.address_normalized)
        )
        self.soja_web3.functions.configureIssuanceBatches.return_value._encode_transaction_data.return_value = (
            self._calldata(self.batches.address_normalized, selector="0x87654321")
        )
        self.helper_web3 = Mock()
        self.helper_web3.functions.sojaCoin.return_value.call.return_value = (
            self.sojacoin.address_normalized
        )
        self.web3_contracts = {
            ContractInstance.Kind.SOJA_COIN: self.soja_web3,
            ContractInstance.Kind.UNIT_REGISTRY: self.helper_web3,
            ContractInstance.Kind.ISSUANCE_BATCHES: self.helper_web3,
        }

    @staticmethod
    def _calldata(address: str, *, selector: str = "0x12345678") -> str:
        """Encode the single address argument shape used by both setup calls."""

        return selector + "0" * 24 + address[2:]

    def bind_contract(self, web3: Mock, deployment: ContractInstance) -> Mock:
        """Return a mock ABI binding for the saved deployment kind."""

        return self.web3_contracts[deployment.kind]

    def create_submitted_configuration(
        self,
        *,
        kind: str = DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY,
        transaction_hash: str | None = None,
    ) -> DeploymentConfigurationAttempt:
        """Create the persisted attempt and submit its known test transaction."""

        target = (
            self.registry
            if kind == DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY
            else self.batches
        )
        transaction_data = self._calldata(
            target.address_normalized,
            selector=(
                "0x12345678"
                if kind == DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY
                else "0x87654321"
            ),
        )
        record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            contract=self.sojacoin,
            operation_type=CONTRACT_CONFIGURATION_OPERATION_TYPE,
            sender_address=self.owner_address,
        )
        attempt = DeploymentConfigurationAttempt.objects.create(
            process=self.process,
            kind=kind,
            transaction=record,
            expected_address=target.address_normalized,
            prepared_data=transaction_data,
        )
        mark_configuration_submission_started(attempt.pk)
        submitted = submit_transaction(record, transaction_hash or self.transaction_hash)
        self.assertEqual(submitted.status, BlockchainTransaction.Status.SUBMITTED)
        attempt.refresh_from_db()
        submitted_record = BlockchainTransaction.objects.get(pk=attempt.transaction_id)
        self.assertEqual(submitted_record.transaction_hash, transaction_hash or self.transaction_hash)
        attempt.transaction = submitted_record
        return attempt

    def web3_client(
        self,
        attempt: DeploymentConfigurationAttempt,
        *,
        receipt_status: int = 1,
        transaction_changes: dict[str, object] | None = None,
        actual_address: str | None = None,
        event_found: bool = True,
    ) -> Mock:
        """Build a deterministic RPC mock for a matching configuration call."""

        record = BlockchainTransaction.objects.get(pk=attempt.transaction_id)
        rpc_hash = record.transaction_hash or self.transaction_hash
        chain_transaction: dict[str, object] = {
            "hash": bytes.fromhex(rpc_hash[2:]),
            "from": record.sender_address,
            "to": self.sojacoin.address_normalized,
            "chainId": self.network.chain_id,
            "value": 0,
            "input": bytes.fromhex(attempt.prepared_data[2:]),
        }
        chain_transaction.update(transaction_changes or {})
        client = Mock()
        client.eth.get_transaction.return_value = chain_transaction
        client.eth.get_transaction_receipt.return_value = {
            "transactionHash": bytes.fromhex(rpc_hash[2:]),
            "status": receipt_status,
            "blockNumber": 700,
            "blockHash": bytes.fromhex(self.block_hash[2:]),
        }
        self.soja_web3.functions.unitRegistry.return_value.call.return_value = (
            actual_address or attempt.expected_address
        )
        self.soja_web3.functions.issuanceBatchModule.return_value.call.return_value = (
            actual_address or attempt.expected_address
        )
        decoded_event = {
            "address": self.sojacoin.address_normalized,
            "args": {
                "unitRegistry": attempt.expected_address,
                "issuanceBatches": attempt.expected_address,
                "configuredBy": record.sender_address,
            }
        }
        self.soja_web3.events.UnitRegistryConfigured.return_value.process_receipt.return_value = (
            [decoded_event] if event_found else []
        )
        self.soja_web3.events.IssuanceBatchesConfigured.return_value.process_receipt.return_value = (
            [decoded_event] if event_found else []
        )
        return client

    def prepare(
        self,
        kind: str = DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY,
    ) -> PreparedConfiguration:
        """Call the preparation service with mocked chain reads and ABI binding."""

        client = Mock()
        client.eth.get_code.return_value = b"\x60\x00"
        with (
            patch(
                "blockchain.services.deployment_configuration.connect_to_rpc",
                return_value=client,
            ),
            patch(
                "blockchain.services.deployment_configuration.bind_contract_to_web3",
                side_effect=self.bind_contract,
            ),
        ):
            return prepare_deployment_configuration(
                process_id=self.process.pk,
                kind=kind,
                sender_address=self.owner_address,
                wallet_chain_id=self.network.chain_id,
            )

    def test_prepare_creates_server_encoded_attempt_for_each_correct_link(self) -> None:
        """The two ABI calls target SojaCoin with their same-process addresses."""

        for kind, target, selector in (
            (
                DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY,
                self.registry,
                "0x12345678",
            ),
            (
                DeploymentConfigurationAttempt.Kind.ISSUANCE_BATCHES,
                self.batches,
                "0x87654321",
            ),
        ):
            with self.subTest(kind=kind):
                result = self.prepare(kind)
                attempt = DeploymentConfigurationAttempt.objects.select_related(
                    "transaction"
                ).get(pk=result.attempt_id)
                self.assertEqual(result.status, "ready")
                self.assertEqual(result.transaction_to, self.sojacoin.address_normalized)
                self.assertEqual(result.expected_address, target.address_normalized)
                self.assertEqual(
                    attempt.prepared_data,
                    self._calldata(target.address_normalized, selector=selector),
                )
                self.assertEqual(attempt.transaction.company_id, self.company.pk)
                self.assertEqual(attempt.transaction.network_id, self.network.pk)
                self.assertEqual(
                    attempt.transaction.operation_type,
                    CONTRACT_CONFIGURATION_OPERATION_TYPE,
                )

    def test_configuration_calls_encode_the_real_abi_methods_and_addresses(self) -> None:
        """Both server payloads use their named SojaCoin ABI function and target."""

        abi = json.loads(
            Path(__file__).resolve().parent.joinpath(
                "abis",
                "sojacoin.json",
            ).read_text(encoding="utf-8")
        )
        web3 = Web3()
        contract = web3.eth.contract(
            address=Web3.to_checksum_address(self.sojacoin.address_normalized),
            abi=abi,
        )

        for function_name, target in (
            ("configureUnitRegistry", self.registry),
            ("configureIssuanceBatches", self.batches),
        ):
            with self.subTest(function_name=function_name):
                encoded = _configuration_function_data(
                    contract,
                    function_name,
                    target.address_normalized,
                )
                expected = getattr(contract.functions, function_name)(
                    Web3.to_checksum_address(target.address_normalized)
                )._encode_transaction_data()
                self.assertEqual(encoded, expected.lower())
                self.assertEqual(encoded[-40:], target.address_normalized[2:])

    def test_preparation_requires_the_current_on_chain_owner(self) -> None:
        """A non-owner wallet cannot create a configuration attempt."""

        self.soja_web3.functions.owner.return_value.call.return_value = self.other_address
        with self.assertRaisesMessage(ValidationError, "dona atual"):
            self.prepare()
        self.assertFalse(DeploymentConfigurationAttempt.objects.exists())

    def test_preparation_requires_all_nine_deploys_and_the_saved_network(self) -> None:
        """Configuration cannot start early or from a different wallet chain."""

        early_company = Company.objects.create(
            legal_name="Not Yet Deployed Company",
            cnpj="66777888000147",
        )
        early_network = Network.objects.create(
            name="Not Yet Deployed Network",
            chain_id=31338,
            rpc_url="http://127.0.0.1:8545",
        )
        early_process = start_deployment_process(
            company=early_company,
            network=early_network,
            owner_address=self.owner_address,
        )
        with self.assertRaisesMessage(ValidationError, "nove contratos"):
            prepare_deployment_configuration(
                process_id=early_process.pk,
                kind=DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY,
                sender_address=self.owner_address,
                wallet_chain_id=early_network.chain_id,
            )
        with self.assertRaisesMessage(ValidationError, "rede diferente"):
            prepare_deployment_configuration(
                process_id=self.process.pk,
                kind=DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY,
                sender_address=self.owner_address,
                wallet_chain_id=1,
            )
        self.assertFalse(DeploymentConfigurationAttempt.objects.exists())

    def test_preparation_requires_active_company_and_network(self) -> None:
        """Inactive scope records cannot start new contract configuration work."""

        self.company.active = False
        self.company.save()
        with self.assertRaisesMessage(ValidationError, "empresa e a rede"):
            self.prepare()

        self.company.active = True
        self.company.save()
        self.network.active = False
        self.network.save()
        with self.assertRaisesMessage(ValidationError, "empresa e a rede"):
            self.prepare()

        self.assertFalse(DeploymentConfigurationAttempt.objects.exists())

    def test_existing_expected_link_is_reported_without_a_fake_transaction(self) -> None:
        """A matching on-chain link is shown without inventing local history."""

        self.soja_web3.functions.unitRegistry.return_value.call.return_value = (
            self.registry.address_normalized
        )

        result = self.prepare()

        self.assertEqual(result.status, "already_configured")
        self.assertEqual(result.current_address, self.registry.address_normalized)
        self.assertFalse(DeploymentConfigurationAttempt.objects.exists())

    def test_different_existing_link_moves_process_to_review(self) -> None:
        """An unexpected one-time contract link blocks further setup calls."""

        self.soja_web3.functions.unitRegistry.return_value.call.return_value = (
            self.other_address
        )

        result = self.prepare()

        self.process.refresh_from_db()
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(self.process.status, DeploymentProcess.Status.NEEDS_REVIEW)
        self.assertFalse(DeploymentConfigurationAttempt.objects.exists())

    def test_open_attempt_is_reused_before_the_wallet_prompt_and_then_blocks_duplicate(self) -> None:
        """A repeated request reuses one untouched attempt, but never opens a second prompt."""

        first = self.prepare()
        second = self.prepare()
        self.assertEqual(first.attempt_id, second.attempt_id)
        self.assertEqual(DeploymentConfigurationAttempt.objects.count(), 1)

        mark_configuration_submission_started(first.attempt_id)
        with self.assertRaisesMessage(ValidationError, "tentativa para conferir"):
            self.prepare()
        self.assertEqual(DeploymentConfigurationAttempt.objects.count(), 1)

    def test_wrong_helper_reverse_link_requires_review(self) -> None:
        """A helper created for another SojaCoin cannot be connected."""

        self.helper_web3.functions.sojaCoin.return_value.call.return_value = self.other_address

        result = self.prepare()

        self.process.refresh_from_db()
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(self.process.status, DeploymentProcess.Status.NEEDS_REVIEW)

    def test_wallet_hash_must_match_sender_destination_value_and_calldata(self) -> None:
        """A wallet hash is submitted only when every protected field matches."""

        prepared = self.prepare()
        attempt = DeploymentConfigurationAttempt.objects.select_related(
            "transaction__contract",
            "process__network",
        ).get(pk=prepared.attempt_id)
        mark_configuration_submission_started(attempt.pk)

        for changes, message in (
            ({"from": self.other_address}, "outra carteira"),
            ({"to": self.registry.address_normalized}, "outro contrato"),
            ({"value": 1}, "transferir valor"),
            ({"chainId": 1}, "rede diferente"),
            ({"input": b"0xnot-the-prepared-call"}, "diferem"),
        ):
            with self.subTest(changes=changes):
                client = self.web3_client(attempt, transaction_changes=changes)
                with patch(
                    "blockchain.services.deployment_configuration.connect_to_rpc",
                    return_value=client,
                ):
                    with self.assertRaisesMessage(ValidationError, message):
                        register_configuration_transaction_hash(
                            attempt_id=attempt.pk,
                            transaction_hash=self.transaction_hash,
                        )

        attempt.transaction.refresh_from_db()
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.PREPARED)

    def test_same_wallet_hash_is_idempotently_registered(self) -> None:
        """Repeating the verified hash returns one submitted transaction."""

        prepared = self.prepare()
        attempt = DeploymentConfigurationAttempt.objects.select_related(
            "transaction__contract",
            "process__network",
        ).get(pk=prepared.attempt_id)
        mark_configuration_submission_started(attempt.pk)
        client = self.web3_client(attempt)

        with patch(
            "blockchain.services.deployment_configuration.connect_to_rpc",
            return_value=client,
        ):
            first = register_configuration_transaction_hash(
                attempt_id=attempt.pk,
                transaction_hash=self.transaction_hash,
            )
            repeated = register_configuration_transaction_hash(
                attempt_id=attempt.pk,
                transaction_hash=self.transaction_hash,
            )

        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(first.status, BlockchainTransaction.Status.SUBMITTED)
        self.assertEqual(DeploymentConfigurationAttempt.objects.count(), 1)

    def test_unknown_wallet_hash_stays_on_same_open_attempt(self) -> None:
        """An RPC that has not indexed the hash does not lose the retry path."""

        prepared = self.prepare()
        attempt = DeploymentConfigurationAttempt.objects.get(pk=prepared.attempt_id)
        mark_configuration_submission_started(attempt.pk)
        client = self.web3_client(attempt)
        client.eth.get_transaction.side_effect = TransactionNotFound(self.transaction_hash)

        with patch(
            "blockchain.services.deployment_configuration.connect_to_rpc",
            return_value=client,
        ):
            result = register_configuration_transaction_hash(
                attempt_id=attempt.pk,
                transaction_hash=self.transaction_hash,
            )

        attempt.refresh_from_db()
        self.assertIsNone(result)
        self.assertTrue(attempt.is_open)
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.PREPARED)

    def test_rpc_error_keeps_hash_recovery_on_the_same_attempt(self) -> None:
        """An RPC outage does not close or replace an uncertain wallet attempt."""

        prepared = self.prepare()
        attempt = DeploymentConfigurationAttempt.objects.get(pk=prepared.attempt_id)
        mark_configuration_submission_started(attempt.pk)

        with patch(
            "blockchain.services.deployment_configuration.connect_to_rpc",
            side_effect=ConnectionError("local RPC is offline"),
        ):
            with self.assertRaises(ConnectionError):
                register_configuration_transaction_hash(
                    attempt_id=attempt.pk,
                    transaction_hash=self.transaction_hash,
                )

        attempt.refresh_from_db()
        attempt.transaction.refresh_from_db()
        self.assertTrue(attempt.is_open)
        self.assertIsNotNone(attempt.submission_started_at)
        self.assertIsNone(attempt.transaction.transaction_hash)
        self.assertEqual(
            attempt.transaction.status,
            BlockchainTransaction.Status.PREPARED,
        )

    def test_explicit_rejection_cannot_fail_a_transaction_already_submitted(self) -> None:
        """A late rejection request cannot discard a transaction hash."""

        attempt = self.create_submitted_configuration()

        with self.assertRaisesMessage(ValidationError, "rejeição explícita"):
            reject_configuration_attempt(attempt.pk)

        attempt.refresh_from_db()
        attempt.transaction.refresh_from_db()
        self.assertTrue(attempt.is_open)
        self.assertEqual(
            attempt.transaction.status,
            BlockchainTransaction.Status.SUBMITTED,
        )
        self.assertEqual(attempt.transaction.transaction_hash, self.transaction_hash)

    def test_both_owner_configuration_calls_can_be_confirmed(self) -> None:
        """The owner confirms each helper link in its own transaction."""

        attempts = (
            self.create_submitted_configuration(),
            self.create_submitted_configuration(
                kind=DeploymentConfigurationAttempt.Kind.ISSUANCE_BATCHES,
                transaction_hash="0x" + "c" * 64,
            ),
        )
        expected_addresses = (self.registry.address_normalized, self.batches.address_normalized)

        for attempt, expected_address in zip(attempts, expected_addresses):
            with self.subTest(kind=attempt.kind):
                client = self.web3_client(attempt, actual_address=expected_address)
                with (
                    patch(
                        "blockchain.services.deployment_configuration.connect_to_rpc",
                        return_value=client,
                    ),
                    patch(
                        "blockchain.services.deployment_configuration.bind_contract_to_web3",
                        side_effect=self.bind_contract,
                    ),
                ):
                    result = confirm_deployment_configuration(attempt.pk)

                attempt.refresh_from_db()
                attempt.transaction.refresh_from_db()
                self.assertEqual(result.status, "confirmed")
                self.assertEqual(result.current_address, expected_address)
                self.assertEqual(
                    attempt.transaction.sender_address,
                    self.owner_address,
                )
                self.assertEqual(
                    attempt.transaction.status,
                    BlockchainTransaction.Status.CONFIRMED,
                )
                self.assertFalse(attempt.is_open)

    def test_rpc_error_keeps_submitted_configuration_available_for_confirmation_retry(self) -> None:
        """An unavailable RPC leaves the submitted history unchanged."""

        attempt = self.create_submitted_configuration()

        with patch(
            "blockchain.services.deployment_configuration.connect_to_rpc",
            side_effect=ConnectionError("local RPC is offline"),
        ):
            with self.assertRaises(ConnectionError):
                confirm_deployment_configuration(attempt.pk)

        attempt.refresh_from_db()
        attempt.transaction.refresh_from_db()
        self.assertTrue(attempt.is_open)
        self.assertEqual(
            attempt.transaction.status,
            BlockchainTransaction.Status.SUBMITTED,
        )
        self.assertEqual(attempt.transaction.transaction_hash, self.transaction_hash)

    def test_database_error_rolls_back_confirmation_and_attempt_closure(self) -> None:
        """A failed local write cannot leave a half-confirmed setup operation."""

        attempt = self.create_submitted_configuration()
        client = self.web3_client(attempt)
        with (
            patch(
                "blockchain.services.deployment_configuration.connect_to_rpc",
                return_value=client,
            ),
            patch(
                "blockchain.services.deployment_configuration.bind_contract_to_web3",
                side_effect=self.bind_contract,
            ),
            patch.object(
                DeploymentConfigurationAttempt,
                "save",
                side_effect=DatabaseError("local database write failed"),
            ),
        ):
            with self.assertRaises(DatabaseError):
                confirm_deployment_configuration(attempt.pk)

        attempt.refresh_from_db()
        attempt.transaction.refresh_from_db()
        self.process.refresh_from_db()
        self.assertTrue(attempt.is_open)
        self.assertEqual(
            attempt.transaction.status,
            BlockchainTransaction.Status.SUBMITTED,
        )
        self.assertEqual(self.process.status, DeploymentProcess.Status.DEPLOYED)

    def test_explicit_wallet_rejection_closes_attempt_and_keeps_history(self) -> None:
        """Only a definitive wallet rejection releases the configuration slot."""

        prepared = self.prepare()
        mark_configuration_submission_started(prepared.attempt_id)
        attempt = reject_configuration_attempt(prepared.attempt_id)

        self.assertFalse(attempt.is_open)
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.FAILED)
        self.assertEqual(DeploymentConfigurationAttempt.objects.count(), 1)
        self.assertEqual(BlockchainTransaction.objects.filter(
            operation_type=CONTRACT_CONFIGURATION_OPERATION_TYPE
        ).count(), 1)

    def test_confirmed_receipt_requires_matching_event_and_public_link(self) -> None:
        """Success is recorded only after receipt event and getter agree."""

        attempt = self.create_submitted_configuration()
        client = self.web3_client(attempt)
        with (
            patch(
                "blockchain.services.deployment_configuration.connect_to_rpc",
                return_value=client,
            ),
            patch(
                "blockchain.services.deployment_configuration.bind_contract_to_web3",
                side_effect=self.bind_contract,
            ),
        ):
            result = confirm_deployment_configuration(attempt.pk)
            repeated = confirm_deployment_configuration(attempt.pk)

        attempt.refresh_from_db()
        attempt.transaction.refresh_from_db()
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.current_address, self.registry.address_normalized)
        self.assertEqual(repeated.status, "confirmed")
        self.assertFalse(attempt.is_open)
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.CONFIRMED)
        self.assertEqual(DeploymentConfigurationAttempt.objects.count(), 1)

    def test_configuration_receipt_can_be_pending_or_reverted(self) -> None:
        """Pending receipts preserve the attempt; a reverted call releases it."""

        pending_attempt = self.create_submitted_configuration()
        pending_client = self.web3_client(pending_attempt)
        pending_client.eth.get_transaction_receipt.side_effect = TransactionNotFound(
            self.transaction_hash
        )
        with (
            patch(
                "blockchain.services.deployment_configuration.connect_to_rpc",
                return_value=pending_client,
            ),
            patch(
                "blockchain.services.deployment_configuration.bind_contract_to_web3",
                side_effect=self.bind_contract,
            ),
        ):
            pending = confirm_deployment_configuration(pending_attempt.pk)

        pending_attempt.refresh_from_db()
        self.assertEqual(pending.status, "pending")
        self.assertTrue(pending_attempt.is_open)
        self.assertEqual(
            pending_attempt.transaction.status,
            BlockchainTransaction.Status.SUBMITTED,
        )

        # The two configuration kinds have separate open slots, so the second
        # scenario can use the other link within this same process.
        failed_attempt = self.create_submitted_configuration(
            kind=DeploymentConfigurationAttempt.Kind.ISSUANCE_BATCHES,
            transaction_hash="0x" + "c" * 64,
        )
        failed_client = self.web3_client(failed_attempt, receipt_status=0)
        with patch(
            "blockchain.services.deployment_configuration.connect_to_rpc",
            return_value=failed_client,
        ):
            failed = confirm_deployment_configuration(failed_attempt.pk)

        failed_attempt.refresh_from_db()
        self.assertEqual(failed.status, "failed")
        self.assertFalse(failed_attempt.is_open)
        self.assertEqual(failed_attempt.transaction.status, BlockchainTransaction.Status.FAILED)

    def test_successful_receipt_with_wrong_event_or_link_moves_process_to_review(self) -> None:
        """A mined call is preserved but never shown complete when chain state differs."""

        attempt = self.create_submitted_configuration()
        client = self.web3_client(
            attempt,
            actual_address=self.other_address,
            event_found=False,
        )
        with (
            patch(
                "blockchain.services.deployment_configuration.connect_to_rpc",
                return_value=client,
            ),
            patch(
                "blockchain.services.deployment_configuration.bind_contract_to_web3",
                side_effect=self.bind_contract,
            ),
        ):
            result = confirm_deployment_configuration(attempt.pk)

        self.process.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(self.process.status, DeploymentProcess.Status.NEEDS_REVIEW)
        self.assertFalse(attempt.is_open)
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.CONFIRMED)

    def test_reorged_configuration_keeps_process_in_review(self) -> None:
        """A later chain reorganization invalidates setup completion locally."""

        attempt = self.create_submitted_configuration()
        client = self.web3_client(attempt)
        with (
            patch(
                "blockchain.services.deployment_configuration.connect_to_rpc",
                return_value=client,
            ),
            patch(
                "blockchain.services.deployment_configuration.bind_contract_to_web3",
                side_effect=self.bind_contract,
            ),
        ):
            confirm_deployment_configuration(attempt.pk)

        mark_transaction_reorged(attempt.transaction, "Configuration block was reorganized.")
        self.process.refresh_from_db()
        attempt.transaction.refresh_from_db()
        self.assertEqual(self.process.status, DeploymentProcess.Status.NEEDS_REVIEW)
        self.assertEqual(attempt.transaction.status, BlockchainTransaction.Status.REORGED)


    def test_contract_configuration_transaction_requires_linked_attempt(self) -> None:
        """The generic transaction lifecycle rejects an unscoped setup hash."""

        record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            contract=self.sojacoin,
            operation_type=CONTRACT_CONFIGURATION_OPERATION_TYPE,
            sender_address=self.owner_address,
        )

        with self.assertRaisesMessage(ValidationError, "vinculada a uma tentativa"):
            submit_transaction(record, "0x" + "d" * 64)


class DeploymentConfigurationAdminTests(TestCase):
    """Protect setup actions and scope every request to its deployment process."""

    owner_address = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    registry_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def setUp(self) -> None:
        self.company = Company.objects.create(
            legal_name="Configuration Admin Company",
            cnpj="77888999000120",
        )
        self.network = Network.objects.create(
            name="Configuration Admin Network",
            chain_id=31337,
            rpc_url="http://127.0.0.1:8545",
        )
        self.process = start_deployment_process(
            company=self.company,
            network=self.network,
            owner_address=self.owner_address,
        )
        soja_coin = ContractInstance.objects.create(
            company=self.company,
            network=self.network,
            kind=ContractInstance.Kind.SOJA_COIN,
            address="0x" + "c" * 40,
            version="admin-test",
        )
        record = BlockchainTransaction.objects.create(
            company=self.company,
            network=self.network,
            contract=soja_coin,
            operation_type=CONTRACT_CONFIGURATION_OPERATION_TYPE,
            sender_address=self.owner_address,
        )
        self.attempt = DeploymentConfigurationAttempt.objects.create(
            process=self.process,
            kind=DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY,
            transaction=record,
            expected_address=self.registry_address,
            prepared_data="0x12345678" + "0" * 24 + self.registry_address[2:],
        )
        self.operator = User.objects.create_superuser(
            username="configuration-admin-operator",
            email="configuration-admin@example.test",
            password="test-password",
        )
        self.prepare_url = reverse(
            "admin:blockchain_deploymentprocess_prepare_configuration",
            args=(self.process.pk, self.attempt.kind),
        )
        self.confirm_url = reverse(
            "admin:blockchain_deploymentprocess_confirm_configuration",
            args=(self.process.pk, self.attempt.pk),
        )

    @patch("blockchain.admin.prepare_deployment_configuration")
    def test_prepare_route_uses_process_from_url_and_returns_wallet_data(
        self,
        prepare: Mock,
    ) -> None:
        """Posted company identifiers cannot replace the process URL scope."""

        prepare.return_value = PreparedConfiguration(
            status="ready",
            attempt_id=self.attempt.pk,
            chain_id=self.network.chain_id,
            owner_address=self.owner_address,
            transaction_to=self.attempt.transaction.contract.address_normalized,
            transaction_data=self.attempt.prepared_data,
            expected_address=self.registry_address,
            current_address="0x" + "0" * 40,
        )
        self.client.force_login(self.operator)

        response = self.client.post(
            self.prepare_url,
            {
                "operator_address": self.owner_address,
                "wallet_chain_id": str(self.network.chain_id),
                "company_id": "999999",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transaction_to"], "0x" + "c" * 40)
        self.assertTrue(response.json()["started_url"].endswith("/started/"))
        prepare.assert_called_once_with(
            process_id=self.process.pk,
            kind=self.attempt.kind,
            sender_address=self.owner_address,
            wallet_chain_id=self.network.chain_id,
        )

    @patch("blockchain.admin.confirm_deployment_configuration")
    def test_confirm_route_consults_only_the_scoped_attempt(self, confirm: Mock) -> None:
        """The confirmation button only checks the saved transaction by POST."""

        confirm.return_value = ConfigurationConfirmation(
            status="pending",
            attempt=self.attempt,
            transaction=self.attempt.transaction,
            expected_address=self.registry_address,
        )
        self.client.force_login(self.operator)

        response = self.client.post(self.confirm_url)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "pending")
        confirm.assert_called_once_with(self.attempt.pk)

    @patch("blockchain.admin.confirm_deployment_configuration")
    def test_confirm_route_preserves_attempt_when_rpc_is_unavailable(
        self,
        confirm: Mock,
    ) -> None:
        """An RPC error returns a retryable response without changing history."""

        confirm.side_effect = ConnectionError("local RPC is offline")
        self.client.force_login(self.operator)

        response = self.client.post(self.confirm_url)

        self.attempt.refresh_from_db()
        self.attempt.transaction.refresh_from_db()
        self.assertEqual(response.status_code, 503)
        self.assertIn("nova consulta", response.json()["error"])
        self.assertTrue(self.attempt.is_open)
        self.assertEqual(
            self.attempt.transaction.status,
            BlockchainTransaction.Status.PREPARED,
        )

    @patch("blockchain.admin.confirm_deployment_configuration")
    def test_confirm_route_returns_clear_results_for_failed_review_and_confirmed(
        self,
        confirm: Mock,
    ) -> None:
        """The operator receives a distinct message for every final result."""

        self.client.force_login(self.operator)
        cases = (
            (
                "failed",
                "A rede reverteu a chamada",
                "0x" + "0" * 40,
            ),
            (
                "needs_review",
                "precisa de revisão",
                "0x" + "c" * 40,
            ),
            (
                "confirmed",
                "Vínculo confirmado",
                self.registry_address,
            ),
        )

        for status, expected_message, current_address in cases:
            with self.subTest(status=status):
                confirm.return_value = ConfigurationConfirmation(
                    status=status,
                    attempt=self.attempt,
                    transaction=self.attempt.transaction,
                    expected_address=self.registry_address,
                    current_address=current_address,
                )
                response = self.client.post(self.confirm_url)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], status)
                self.assertIn(expected_message, response.json()["message"])

    def test_setup_routes_require_post_permission_and_csrf(self) -> None:
        """A read request or staff viewer cannot start an owner transaction."""

        viewer = User.objects.create_user(
            username="configuration-admin-viewer",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(self.confirm_url).status_code, 405)

        self.client.force_login(viewer)
        self.assertEqual(self.client.post(self.confirm_url).status_code, 403)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.operator)
        self.assertEqual(csrf_client.post(self.confirm_url).status_code, 403)

    def test_setup_route_returns_404_for_attempt_from_another_process(self) -> None:
        """An attempt ID from another company cannot be substituted in the URL."""

        other_company = Company.objects.create(
            legal_name="Other Configuration Admin Company",
            cnpj="88999000000110",
        )
        other_network = Network.objects.create(
            name="Other Configuration Admin Network",
            chain_id=31338,
            rpc_url="http://127.0.0.1:8545",
        )
        other_process = start_deployment_process(
            company=other_company,
            network=other_network,
            owner_address=self.owner_address,
        )
        other_soja = ContractInstance.objects.create(
            company=other_company,
            network=other_network,
            kind=ContractInstance.Kind.SOJA_COIN,
            address="0x" + "d" * 40,
            version="admin-test",
        )
        other_transaction = BlockchainTransaction.objects.create(
            company=other_company,
            network=other_network,
            contract=other_soja,
            operation_type=CONTRACT_CONFIGURATION_OPERATION_TYPE,
            sender_address=self.owner_address,
        )
        other_attempt = DeploymentConfigurationAttempt.objects.create(
            process=other_process,
            kind=DeploymentConfigurationAttempt.Kind.UNIT_REGISTRY,
            transaction=other_transaction,
            expected_address=self.registry_address,
            prepared_data="0x12345678" + "0" * 24 + self.registry_address[2:],
        )
        wrong_scope_url = reverse(
            "admin:blockchain_deploymentprocess_confirm_configuration",
            args=(self.process.pk, other_attempt.pk),
        )
        self.client.force_login(self.operator)

        self.assertEqual(self.client.post(wrong_scope_url).status_code, 404)

    def test_admin_shows_two_distinct_setup_rows_only_after_nine_deploys(self) -> None:
        """The process page separates contract deployment from link setup."""

        before_deployments = reverse(
            "admin:blockchain_deploymentprocess_change",
            args=(self.process.pk,),
        )
        self.client.force_login(self.operator)
        early_response = self.client.get(before_deployments)
        self.assertNotContains(early_response, "Configuração dos vínculos")

        completed_company = Company.objects.create(
            legal_name="Completed Configuration Admin Company",
            cnpj="99000111000109",
        )
        completed_network = Network.objects.create(
            name="Completed Configuration Admin Network",
            chain_id=31339,
            rpc_url="http://127.0.0.1:8545",
        )
        process = start_deployment_process(
            company=completed_company,
            network=completed_network,
            owner_address=self.owner_address,
        )
        for position, step in enumerate(process.steps.order_by("position"), start=1):
            transaction_hash = f"0x{position + 800:064x}"
            contract = ContractInstance.objects.create(
                company=completed_company,
                network=completed_network,
                kind=step.kind,
                address="0x" + f"{position + 800:040x}",
                version="admin-template-test",
                deployment_transaction_hash=transaction_hash,
            )
            record = BlockchainTransaction.objects.create(
                company=completed_company,
                network=completed_network,
                operation_type=DEPLOYMENT_OPERATION_TYPE,
                sender_address=self.owner_address,
            )
            DeploymentAttempt.objects.create(step=step, transaction=record)
            submit_transaction(record, transaction_hash)
            confirm_transaction(
                record,
                position,
                "0x" + f"{position + 900:064x}",
            )
            step.contract = contract
            step.save()
        process.refresh_from_db()
        process.status = DeploymentProcess.Status.DEPLOYED
        process.save()

        completed_url = reverse(
            "admin:blockchain_deploymentprocess_change",
            args=(process.pk,),
        )
        response = self.client.get(completed_url)

        self.assertContains(response, "Nove contratos:")
        self.assertContains(response, "Configuração dos vínculos")
        self.assertContains(response, "Registro de unidades")
        self.assertContains(response, "Lotes de emissão")
        self.assertContains(response, "Configurar vínculo")
        self.assertContains(response, "9 de 9 confirmados")

        first_step = process.steps.order_by("position").first()
        deployment_attempt = first_step.linked_deployment_attempt
        mark_transaction_reorged(
            deployment_attempt.transaction,
            "The local test block was reorganized.",
        )
        reorged_response = self.client.get(completed_url)

        self.assertNotContains(reorged_response, "Configuração dos vínculos")
        self.assertContains(reorged_response, "8 de 9 contratos confirmados")
