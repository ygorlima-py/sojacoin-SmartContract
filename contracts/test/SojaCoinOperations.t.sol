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
import {SojaCoinTestBase} from "./helpers/SojaCoinTestBase.sol";

contract SojaCoinOperationsTest is SojaCoinTestBase {
    function test_receiptUsesNetWeightAndUpdatesUnitInventory() public {
        vm.prank(receiver);
        uint256 depositId = coin.openLoadReceipt(producer, 1, CROP_YEAR);
        vm.prank(receiver);
        coin.linkTaxInvoice(depositId, keccak256("invoice-net-weight"));
        vm.prank(receiver);
        coin.confirmLoadReceipt(depositId, _ticket(1_000, 100, 50, keccak256("ticket-net-weight")));

        _eq(registry.registeredInventoryMg(), 850 * 1_000_000, "unit registry receives net inventory");
        _eq(coin.registeredInventoryMg(), 850 * 1_000_000, "SojaCoin backing matches registry");
        (
            address depositor,
            bytes32 invoiceHash,
            uint256 unitId,
            bytes32 cropYear,
            bytes32 termsHash,
            uint256 amountSJC,
            bool receiptConfirmed,
            bool tokensIssued,
            bool canceled,
            bool closedWithoutIssuance,
            uint256 depositDate,
            uint256 weightKg,
            uint256 returnedWeightKg,
            bytes32 weighingTicketHash,
            uint256 documentTimestamp,
            uint256 receivedAt
        ) = coin.deposits(depositId);
        depositor;
        invoiceHash;
        unitId;
        cropYear;
        termsHash;
        amountSJC;
        receiptConfirmed;
        tokensIssued;
        canceled;
        closedWithoutIssuance;
        depositDate;
        returnedWeightKg;
        weighingTicketHash;
        documentTimestamp;
        receivedAt;
        _eq(weightKg, 850, "net weight subtracts tare and deductions");
    }

    function test_receiptRejectsDuplicateWeighingTicket() public {
        bytes32 ticketHash = keccak256("same-ticket");
        vm.prank(receiver);
        uint256 first = coin.openLoadReceipt(producer, 1, CROP_YEAR);
        vm.prank(receiver);
        coin.confirmLoadReceipt(first, _ticket(100, 0, 0, ticketHash));

        vm.prank(receiver);
        uint256 second = coin.openLoadReceipt(producer, 1, CROP_YEAR);
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Weighing ticket has already been used"));
        vm.prank(receiver);
        coin.confirmLoadReceipt(second, _ticket(100, 0, 0, ticketHash));
    }

    function test_receiptRejectsFutureTimestampAndInvalidWeight() public {
        vm.prank(receiver);
        uint256 depositId = coin.openLoadReceipt(producer, 1, CROP_YEAR);
        SojaCoinStorage.WeighingTicket memory futureTicket = _ticket(100, 0, 0, keccak256("future-ticket"));
        futureTicket.timestamp = block.timestamp + 1;
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Invalid future date"));
        vm.prank(receiver);
        coin.confirmLoadReceipt(depositId, futureTicket);

        SojaCoinStorage.WeighingTicket memory invalidTicket = _ticket(100, 100, 0, keccak256("zero-net"));
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Tare exceeds gross weight"));
        vm.prank(receiver);
        coin.confirmLoadReceipt(depositId, invalidTicket);
    }

    function test_batchRequiresProducerAcceptanceAndRecordsIssuance() public {
        vm.prank(receiver);
        uint256 depositId = coin.openLoadReceipt(producer, 1, CROP_YEAR);
        vm.prank(receiver);
        coin.linkTaxInvoice(depositId, keccak256("batch-invoice"));
        vm.prank(receiver);
        coin.confirmLoadReceipt(depositId, _ticket(6_000, 0, 0, keccak256("batch-ticket")));

        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        vm.prank(issuer);
        uint256 batchId = batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);
        bytes32 digest = batches.issuanceBatchAuthorizationHash(batchId);

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Invalid producer authorization"));
        batches.approveIssuanceBatch(batchId, _sign(CREDITOR_PK, digest));

        batches.approveIssuanceBatch(batchId, _sign(PRODUCER_PK, digest));
        require(batches.isDepositInApprovedBatch(depositId), "deposit waits for issue");
        vm.prank(issuer);
        coin.issueTokens(depositId);

        _eq(coin.balanceOf(producer), 100 * UNITS_PER_SACK, "6000 kg issues 100 SJC");
        require(batches.depositIssued(depositId), "batch records the issued deposit");
        require(!batches.isDepositInApprovedBatch(depositId), "completed batch no longer approves a deposit");
        _eq(registry.pendingDepositsByUnit(1), 0, "issued deposit closes unit tracking");
    }

    function test_participantApprovesAndIssuesInOneTransaction() public {
        uint256 depositId = _receivedLoad(6_000, "approve-and-issue");
        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        vm.prank(issuer);
        uint256 batchId = batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);

        vm.prank(producer);
        batches.approveAndIssueBatch(batchId);

        _eq(coin.balanceOf(producer), 100 * UNITS_PER_SACK, "participant receives issued SJC");
        _eq(_batchStatus(batchId), 3, "batch completes atomically");
        require(batches.depositIssued(depositId), "batch records the issued receipt");
        _eq(registry.pendingDepositsByUnit(1), 0, "issued receipt closes unit tracking");
    }

    function test_automaticIssuanceRechecksCreatorPermissionAndRollsBackApproval() public {
        uint256 depositId = _receivedLoad(6_000, "approve-and-issue-revoked");
        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        vm.prank(issuer);
        uint256 batchId = batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);

        registry.setPermissions(1, issuer, 0);
        vm.expectRevert(UnitRegistry.InsufficientOperationalPermission.selector);
        vm.prank(producer);
        batches.approveAndIssueBatch(batchId);

        _eq(_batchStatus(batchId), 1, "failed issue rolls approval back");
        require(!batches.depositIssued(depositId), "failed issue remains unrecorded");
        _eq(coin.balanceOf(producer), 0, "failed issue mints no tokens");
        _eq(registry.pendingDepositsByUnit(1), 1, "failed issue keeps receipt open");
    }

    function test_automaticIssuanceRejectsWrongParticipantAndExpiredBatch() public {
        uint256 depositId = _receivedLoad(6_000, "approve-and-issue-guards");
        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        vm.prank(issuer);
        uint256 batchId = batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Producer only"));
        vm.prank(creditor);
        batches.approveAndIssueBatch(batchId);

        vm.warp(block.timestamp + 1 days + 1);
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Approval has expired"));
        vm.prank(producer);
        batches.approveAndIssueBatch(batchId);
        _eq(_batchStatus(batchId), 1, "rejected calls keep batch pending");
    }

    function test_automaticIssuanceRejectsMultiReceiptBatchWithoutApprovingIt() public {
        uint256 firstDeposit = _receivedLoad(6_000, "approve-and-issue-multi-one");
        uint256 secondDeposit = _receivedLoad(6_000, "approve-and-issue-multi-two");
        uint256[] memory depositIds = new uint256[](2);
        depositIds[0] = firstDeposit;
        depositIds[1] = secondDeposit;
        vm.prank(issuer);
        uint256 batchId = batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Automatic issuance supports one deposit"));
        vm.prank(producer);
        batches.approveAndIssueBatch(batchId);

        _eq(_batchStatus(batchId), 1, "multi-receipt batch remains pending");
        require(!batches.depositIssued(firstDeposit) && !batches.depositIssued(secondDeposit), "no receipt is issued");
    }

    function test_automaticIssuanceRevertsAfterAuditExpires() public {
        uint256 depositId = _receivedLoad(6_000, "approve-and-issue-expired-audit");
        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        vm.prank(issuer);
        uint256 batchId = batches.createIssuanceBatch(depositIds, block.timestamp + 90 days);

        vm.warp(block.timestamp + registry.AUDIT_PERIOD() + 1);
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Audit has expired"));
        vm.prank(producer);
        batches.approveAndIssueBatch(batchId);

        _eq(_batchStatus(batchId), 1, "expired audit rolls approval back");
        _eq(coin.balanceOf(producer), 0, "expired audit mints no SJC");
    }

    function test_batchCannotBeCreatedWithoutTaxInvoice() public {
        vm.prank(receiver);
        uint256 depositId = coin.openLoadReceipt(producer, 1, CROP_YEAR);
        vm.prank(receiver);
        coin.confirmLoadReceipt(depositId, _ticket(600, 0, 0, keccak256("no-invoice-ticket")));

        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Tax invoice is not linked"));
        vm.prank(issuer);
        batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);
    }

    function test_batchCancellationReleasesDepositForANewBatch() public {
        uint256 depositId = _receivedLoad(6_000, "cancel-batch");
        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        vm.prank(issuer);
        uint256 firstBatch = batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);

        vm.prank(producer);
        batches.cancelIssuanceBatch(firstBatch);
        require(!batches.isDepositLocked(depositId), "canceled batch releases deposit");

        vm.prank(issuer);
        uint256 secondBatch = batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);
        _eq(secondBatch, firstBatch + 1, "batch IDs are sequential");
    }

    function test_issueRequiresUnitIssuancePermissionAndCannotRepeat() public {
        uint256 depositId = _receivedLoad(6_000, "permission-issue");
        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        vm.prank(issuer);
        uint256 batchId = batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);
        batches.approveIssuanceBatch(batchId, _sign(PRODUCER_PK, batches.issuanceBatchAuthorizationHash(batchId)));

        vm.expectRevert(UnitRegistry.InsufficientOperationalPermission.selector);
        vm.prank(receiver);
        coin.issueTokens(depositId);

        vm.prank(issuer);
        coin.issueTokens(depositId);
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Tokens have already been issued"));
        vm.prank(issuer);
        coin.issueTokens(depositId);
    }

    function test_revokeAndRenewDepositAuthorizationAndBlockRemovalWhilePending() public {
        vm.prank(producer);
        coin.revokeDepositAuthorization();
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Producer has not authorized deposits"));
        vm.prank(receiver);
        coin.openLoadReceipt(producer, 1, CROP_YEAR);

        vm.prank(producer);
        coin.authorizeFutureDeposits(keccak256("renewed terms"));
        vm.prank(receiver);
        uint256 depositId = coin.openLoadReceipt(producer, 1, CROP_YEAR);

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Participant has pending deposits"));
        coin.removeParticipant(producer);

        vm.prank(producer);
        coin.cancelLoadReceipt(depositId);
        coin.removeParticipant(producer);
        require(!coin.participantRegistered(producer), "participant removed after pending work ends");
    }

    function test_transferRequiresRegisteredRecipientAndPauseRequiresNewAudit() public {
        _issueLoad(1, 6_000, keccak256("transfer-pause"));
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Recipient is not registered"));
        vm.prank(producer);
        coin.transfer(address(0xD00D), UNITS_PER_SACK);

        vm.prank(producer);
        coin.transfer(creditor, UNITS_PER_SACK);
        _eq(coin.balanceOf(creditor), UNITS_PER_SACK, "registered recipient receives SJC");

        coin.pause();
        vm.expectRevert(bytes4(keccak256("EnforcedPause()")));
        vm.prank(producer);
        coin.transfer(creditor, 1);

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Audit predates the pause"));
        coin.unpause();

        vm.warp(block.timestamp + 1);
        vm.prank(auditor);
        registry.recordUnitAudit(1, 6_000, keccak256("post-pause audit"));
        vm.prank(auditor);
        registry.recordUnitAudit(2, 0, keccak256("post-pause audit unit two"));
        coin.unpause();
        require(!coin.paused(), "post-pause audit allows reactivation");
    }

    function test_auditExpirationBlocksCreatingIssuanceBatch() public {
        uint256 depositId = _receivedLoad(600, "expired-audit");
        vm.warp(block.timestamp + registry.AUDIT_PERIOD() + 1);
        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Audit has expired"));
        vm.prank(issuer);
        batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);
    }

    function test_rejectsMismatchedAndRepeatedConfiguration() public {
        SojaCoinAdministration administration = new SojaCoinAdministration();
        SojaCoinDeposits deposits = new SojaCoinDeposits();
        SojaCoinCPR cpr = new SojaCoinCPR();
        SojaCoinServices services = new SojaCoinServices();
        SojaCoinLocks locks = new SojaCoinLocks();
        SojaCoinRedemptions redemptions = new SojaCoinRedemptions();
        SojaCoin freshCoin = new SojaCoin(
            address(this),
            address(administration),
            address(deposits),
            address(cpr),
            address(services),
            address(locks),
            address(redemptions)
        );
        UnitRegistry privateRegistry = new UnitRegistry(address(this), address(0x1234));
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Unit registry is linked to another SojaCoin"));
        freshCoin.configureUnitRegistry(address(privateRegistry));

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Unit registry already configured"));
        coin.configureUnitRegistry(address(registry));

        TokenIssuanceBatches wrongBatches = new TokenIssuanceBatches(address(coin));
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Module is linked to another SojaCoin"));
        freshCoin.configureIssuanceBatches(address(wrongBatches));
    }

    function _receivedLoad(uint256 netWeightKg, string memory label) private returns (uint256 depositId) {
        vm.prank(receiver);
        depositId = coin.openLoadReceipt(producer, 1, CROP_YEAR);
        vm.prank(receiver);
        coin.linkTaxInvoice(depositId, keccak256(abi.encode("invoice", label)));
        vm.prank(receiver);
        coin.confirmLoadReceipt(
            depositId,
            _ticket(netWeightKg, 0, 0, keccak256(abi.encode("ticket", label)))
        );
    }

    function _batchStatus(uint256 batchId) private view returns (uint256 status) {
        (,,,,,,, TokenIssuanceBatches.IssuanceBatchStatus chainStatus) = batches.batches(batchId);
        return uint256(chainStatus);
    }
}
