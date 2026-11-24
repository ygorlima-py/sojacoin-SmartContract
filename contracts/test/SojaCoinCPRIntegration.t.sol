// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {SojaCoin} from "../src/contracts/SojaCoin.sol";
import {SojaCoinStorage} from "../src/contracts/storage/SojaCoinStorage.sol";
import {
    SojaCoinAdministration,
    SojaCoinDeposits,
    SojaCoinCPR,
    SojaCoinServices,
    SojaCoinLocks,
    SojaCoinRedemptions
} from "../src/contracts/SojaCoinModules.sol";
import {UnitRegistry} from "../src/contracts/UnitRegistry.sol";
import {TokenIssuanceBatches} from "../src/contracts/modules/TokenIssuanceBatches.sol";

interface VmCPRIntegration {
    function addr(uint256 privateKey) external returns (address);
    function sign(uint256 privateKey, bytes32 digest) external returns (uint8 v, bytes32 r, bytes32 s);
    function prank(address caller) external;
    function warp(uint256 timestamp) external;
}

/// @notice End-to-end tests for the actual CPR, unit registry, and issuance-batch contracts.
/// @dev These tests deliberately do not use the CPR mocks from SojaCoinCPR.t.sol.
contract SojaCoinCPRIntegrationTest {
    VmCPRIntegration private constant vm = VmCPRIntegration(address(uint160(uint256(keccak256("hevm cheat code")))));

    uint256 private constant PRODUCER_PK = 0xA11CE;
    uint256 private constant CREDITOR_A_PK = 0xB0B;
    uint256 private constant CREDITOR_B_PK = 0xB0C;
    uint256 private constant CREDITOR_C_PK = 0xB0D;
    uint256 private constant AUDITOR_PK = 0xA0D17;
    uint256 private constant RECEIVER_PK = 0xCECE1;
    uint256 private constant ISSUER_PK = 0x155E3;

    uint256 private constant UNITS_PER_SACK = 1_000_000;
    bytes32 private constant CROP_2026 = keccak256("2026/2027");
    bytes32 private constant CROP_2027 = keccak256("2027/2028");

    SojaCoin private coin;
    UnitRegistry private registry;
    TokenIssuanceBatches private batches;
    address private producer;
    address private creditorA;
    address private creditorB;
    address private creditorC;
    address private auditor;
    address private receiver;
    address private issuer;

    function setUp() public {
        vm.warp(1_800_000_000);
        producer = vm.addr(PRODUCER_PK);
        creditorA = vm.addr(CREDITOR_A_PK);
        creditorB = vm.addr(CREDITOR_B_PK);
        creditorC = vm.addr(CREDITOR_C_PK);
        auditor = vm.addr(AUDITOR_PK);
        receiver = vm.addr(RECEIVER_PK);
        issuer = vm.addr(ISSUER_PK);

        SojaCoinAdministration administration = new SojaCoinAdministration();
        SojaCoinDeposits deposits = new SojaCoinDeposits();
        SojaCoinCPR cpr = new SojaCoinCPR();
        SojaCoinServices services = new SojaCoinServices();
        SojaCoinLocks locks = new SojaCoinLocks();
        SojaCoinRedemptions redemptions = new SojaCoinRedemptions();

        coin = new SojaCoin(
            address(this),
            address(administration),
            address(deposits),
            address(cpr),
            address(services),
            address(locks),
            address(redemptions)
        );
        registry = new UnitRegistry(address(this), address(coin));
        batches = new TokenIssuanceBatches(address(coin));
        coin.configureUnitRegistry(address(registry));
        coin.configureIssuanceBatches(address(batches));

        registry.registerUnit(bytes32("UNIT-ONE"), keccak256("registration unit one"));
        registry.registerUnit(bytes32("UNIT-TWO"), keccak256("registration unit two"));
        registry.setUnitAuditor(1, auditor);
        registry.setUnitAuditor(2, auditor);
        registry.setPermissions(1, receiver, registry.PERMISSION_RECEIVE_SOYBEANS());
        registry.setPermissions(1, issuer, registry.PERMISSION_ISSUE_SJC());
        registry.setPermissions(2, receiver, registry.PERMISSION_RECEIVE_SOYBEANS());
        registry.setPermissions(2, issuer, registry.PERMISSION_ISSUE_SJC());

        vm.prank(auditor);
        registry.recordUnitAudit(1, 0, keccak256("audit unit one"));
        vm.prank(auditor);
        registry.recordUnitAudit(2, 0, keccak256("audit unit two"));
        vm.warp(block.timestamp + 1);

        _register(producer, PRODUCER_PK);
        _register(creditorA, CREDITOR_A_PK);
        _register(creditorB, CREDITOR_B_PK);
        _register(creditorC, CREDITOR_C_PK);
    }

    function test_realWorkflowAllocatesCPRFirstAndExcessToProducer() public {
        uint256 cprId = _registerCPR(creditorA, 7_000, 1, CROP_2026, 1, block.timestamp + 30 days);

        uint256 depositId = _openApproveAndIssue(1, CROP_2026, 8_000 * 60, bytes32("ticket-1"));
        SojaCoin.CPRDebt memory cpr = coin.cprDetails(cprId);

        _eq(coin.balanceOf(creditorA), 7_000 * UNITS_PER_SACK, "creditor receives the CPR balance");
        _eq(coin.balanceOf(producer), 1_000 * UNITS_PER_SACK, "producer receives only the excess");
        _eq(cpr.outstandingBalanceSJC, 0, "CPR is paid off");
        _eq(uint256(cpr.status), uint256(SojaCoinStorage.CPRStatus.PaidOff), "CPR status is paid off");
        _eq(coin.repaymentCPRByDeposit(depositId, cprId), 7_000 * UNITS_PER_SACK, "repayment is linked to deposit");
        _eq(registry.pendingDepositsByUnit(1), 0, "unit closes issued deposit");
        _eq(batches.depositIssued(depositId) ? 1 : 0, 1, "batch records issuance");
    }

    function test_realWorkflowUsesCompatibleQueueAndLeavesIncompatibleCPRUntouched() public {
        uint256 firstCPR = _registerCPR(creditorA, 7_000, 1, CROP_2026, 1, block.timestamp + 30 days);
        uint256 secondCPR = _registerCPR(creditorB, 5_000, 1, CROP_2026, 2, block.timestamp + 30 days);
        uint256 incompatibleUnitCPR = _registerCPR(creditorC, 2_000, 2, CROP_2026, 3, block.timestamp + 30 days);
        uint256 incompatibleCropCPR = _registerCPR(creditorC, 2_000, 1, CROP_2027, 4, block.timestamp + 30 days);

        uint256 depositId = _openApproveAndIssue(1, CROP_2026, 8_000 * 60, bytes32("ticket-2"));

        _eq(coin.cprDetails(firstCPR).outstandingBalanceSJC, 0, "first CPR is fully paid");
        _eq(coin.cprDetails(secondCPR).outstandingBalanceSJC, 4_000 * UNITS_PER_SACK, "second CPR receives the remainder");
        _eq(coin.cprDetails(incompatibleUnitCPR).outstandingBalanceSJC, 2_000 * UNITS_PER_SACK, "other unit CPR is unchanged");
        _eq(coin.cprDetails(incompatibleCropCPR).outstandingBalanceSJC, 2_000 * UNITS_PER_SACK, "other crop CPR is unchanged");
        _eq(coin.balanceOf(creditorA), 7_000 * UNITS_PER_SACK, "first creditor balance");
        _eq(coin.balanceOf(creditorB), 1_000 * UNITS_PER_SACK, "second creditor balance");
        _eq(coin.balanceOf(producer), 0, "there is no excess for producer");
        _eq(coin.repaymentCPRByDeposit(depositId, secondCPR), 1_000 * UNITS_PER_SACK, "second repayment recorded");
    }

    function test_realWorkflowRoutesFutureIssuanceToAssignedCreditor() public {
        uint256 cprId = _registerCPR(creditorA, 1_000, 1, CROP_2026, 1, block.timestamp + 30 days);
        vm.prank(creditorA);
        coin.assignCPR(
            cprId,
            creditorC,
            "registry protocol 2026-001",
            keccak256("assignment evidence"),
            block.timestamp + 1 days,
            bytes("")
        );

        _openApproveAndIssue(1, CROP_2026, 1_000 * 60, bytes32("ticket-3"));

        _eq(coin.balanceOf(creditorA), 0, "former creditor does not receive issuance");
        _eq(coin.balanceOf(creditorC), 1_000 * UNITS_PER_SACK, "assignee receives future issuance");
        _eq(uint160(coin.cprDetails(cprId).creditor), uint160(creditorC), "current creditor is the assignee");
    }

    function _registerCPR(
        address creditor,
        uint256 sacks,
        uint256 unitId,
        bytes32 cropYear,
        uint256 priority,
        uint256 dueDate
    ) private returns (uint256) {
        uint256 amountSJC = sacks * UNITS_PER_SACK;
        bytes32 documentHash = keccak256(abi.encode("CPR", creditor, priority));
        uint256 deadline = block.timestamp + 1 days;
        bytes32 digest = coin.cprAuthorizationHash(
            producer,
            creditor,
            amountSJC,
            unitId,
            cropYear,
            priority,
            dueDate,
            documentHash,
            deadline
        );
        return coin.registerCPR(
            producer,
            creditor,
            amountSJC,
            unitId,
            cropYear,
            priority,
            dueDate,
            documentHash,
            deadline,
            _sign(PRODUCER_PK, digest)
        );
    }

    function _openApproveAndIssue(uint256 unitId, bytes32 cropYear, uint256 weightKg, bytes32 ticketHash)
        private
        returns (uint256 depositId)
    {
        vm.prank(receiver);
        depositId = coin.openLoadReceipt(producer, unitId, cropYear);
        vm.prank(receiver);
        coin.linkTaxInvoice(depositId, keccak256(abi.encode("invoice", depositId)));
        vm.prank(receiver);
        coin.confirmLoadReceipt(depositId, _ticket(weightKg, ticketHash));

        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        uint256 deadline = block.timestamp + 1 days;
        vm.prank(issuer);
        uint256 batchId = batches.createIssuanceBatch(depositIds, deadline);
        bytes32 digest = batches.issuanceBatchAuthorizationHash(batchId);
        batches.approveIssuanceBatch(batchId, _sign(PRODUCER_PK, digest));

        vm.prank(issuer);
        coin.issueTokens(depositId);
    }

    function _ticket(uint256 weightKg, bytes32 ticketHash)
        private
        view
        returns (SojaCoinStorage.WeighingTicket memory)
    {
        return SojaCoinStorage.WeighingTicket({
            grossWeightKg: weightKg,
            tareKg: 0,
            moistureBps: 0,
            impuritiesBps: 0,
            damagedGrainBps: 0,
            moistureDeductionKg: 0,
            impuritiesDeductionKg: 0,
            damagedGrainDeductionKg: 0,
            otherDeductionsKg: 0,
            weighingTicketHash: ticketHash,
            reportHash: keccak256(abi.encode("report", ticketHash)),
            timestamp: block.timestamp
        });
    }

    function _register(address participant, uint256 privateKey) private {
        coin.proposeParticipantRegistration(participant, keccak256(abi.encode("terms", participant)));
        uint256 deadline = block.timestamp + 1 days;
        bytes32 digest = coin.participationTermsAcceptanceHash(participant, deadline);
        coin.acceptParticipationTerms(participant, deadline, _sign(privateKey, digest));
    }

    function _sign(uint256 privateKey, bytes32 digest) private returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey, digest);
        return abi.encodePacked(r, s, v);
    }

    function _eq(uint256 actual, uint256 expected, string memory message) private pure {
        require(actual == expected, message);
    }
}
