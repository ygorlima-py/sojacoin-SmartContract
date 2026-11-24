// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {SojaCoinStorage} from "../src/contracts/storage/SojaCoinStorage.sol";
import {UnitRegistry} from "../src/contracts/UnitRegistry.sol";
import {SojaCoinTestBase} from "./helpers/SojaCoinTestBase.sol";

contract SojaCoinFinancialFlowsTest is SojaCoinTestBase {
    function test_lockCanBeReleasedByCreditorBeforeDueDate() public {
        _issueLoad(1, 6_000, keccak256("lock-release-load"));
        uint256 initialBalance = coin.balanceOf(producer);
        uint256 locked = 10 * UNITS_PER_SACK;

        vm.prank(producer);
        uint256 lockId = coin.lockSJC(locked, creditor, keccak256("lock agreement"), block.timestamp + 30 days);
        _eq(coin.balanceOf(producer), initialBalance - locked, "holder balance decreases by locked amount");
        _eq(coin.balanceOf(address(coin)), locked, "contract holds the locked SJC");
        _eq(coin.activeLocksByParticipant(producer), 1, "holder has an active lock");

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Creditor only"));
        vm.prank(address(0xBAD));
        coin.releaseTokenLock(lockId);

        vm.prank(creditor);
        coin.releaseTokenLock(lockId);
        _eq(coin.balanceOf(producer), initialBalance, "released amount returns to holder");
        _eq(coin.balanceOf(address(coin)), 0, "contract no longer holds lock amount");
        _eq(coin.activeLocksByParticipant(producer), 0, "lock is closed");
    }

    function test_lockExecutionRequiresCreditorAndDueDate() public {
        _issueLoad(1, 6_000, keccak256("lock-execution-load"));
        uint256 amount = 5 * UNITS_PER_SACK;
        uint256 dueDate = block.timestamp + 2 days;
        vm.prank(producer);
        uint256 lockId = coin.lockSJC(amount, creditor, keccak256("execution agreement"), dueDate);

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Not yet due"));
        vm.prank(creditor);
        coin.executeTokenLock(lockId);

        vm.warp(dueDate);
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Creditor only"));
        coin.executeTokenLock(lockId);

        vm.prank(creditor);
        coin.executeTokenLock(lockId);
        _eq(coin.balanceOf(creditor), amount, "creditor receives executed amount");
        _eq(coin.balanceOf(address(coin)), 0, "executed amount leaves contract custody");
    }

    function test_serviceChargeCanBePaidInSJCOnlyByProducer() public {
        uint256 depositId = _issueLoad(1, 6_000, keccak256("service-sjc-load"));
        uint256 charge = 2 * UNITS_PER_SACK;
        vm.prank(serviceManager);
        uint256 feeId = coin.recordServiceAndFee(
            depositId,
            serviceRecipient,
            SojaCoinStorage.ServiceType.Storage,
            SojaCoinStorage.FeeUnit.SJC,
            charge,
            keccak256("storage charge")
        );

        vm.expectRevert(bytes4(keccak256("OnlyServiceProducer()")));
        vm.prank(creditor);
        coin.payServiceInSJC(feeId);

        uint256 producerBalanceBefore = coin.balanceOf(producer);
        vm.prank(producer);
        coin.payServiceInSJC(feeId);
        _eq(coin.balanceOf(producer), producerBalanceBefore - charge, "producer pays service amount");
        _eq(coin.balanceOf(serviceRecipient), charge, "recipient receives service payment");

        vm.expectRevert(bytes4(keccak256("ServiceNotOpen()")));
        vm.prank(producer);
        coin.payServiceInSJC(feeId);
    }

    function test_serviceChargeCanBeConfirmedPaidInBRLWithProofHash() public {
        uint256 depositId = _issueLoad(1, 6_000, keccak256("service-brl-load"));
        vm.prank(serviceManager);
        uint256 feeId = coin.recordServiceAndFee(
            depositId,
            serviceRecipient,
            SojaCoinStorage.ServiceType.Cleaning,
            SojaCoinStorage.FeeUnit.BRLCents,
            12_500,
            keccak256("cleaning charge")
        );

        bytes32 proofHash = keccak256("bank proof hash");
        vm.prank(serviceManager);
        coin.confirmServicePaymentInBRL(feeId, proofHash);
        (,,,,,,, bytes32 storedProofHash, SojaCoinStorage.ServiceAndFeeStatus status) = coin.servicesAndFees(feeId);
        require(storedProofHash == proofHash, "proof hash is stored");
        require(status == SojaCoinStorage.ServiceAndFeeStatus.PaidInBRL, "charge is marked paid in BRL");
    }

    function test_openServiceChargeCanBeCanceledButPaidChargeCannot() public {
        uint256 depositId = _issueLoad(1, 6_000, keccak256("service-cancel-load"));
        vm.prank(serviceManager);
        uint256 canceledFee = coin.recordServiceAndFee(
            depositId,
            serviceRecipient,
            SojaCoinStorage.ServiceType.Drying,
            SojaCoinStorage.FeeUnit.BRLCents,
            4_000,
            keccak256("drying charge")
        );
        vm.prank(serviceManager);
        coin.cancelServiceAndFee(canceledFee);
        (,,,,,,,, SojaCoinStorage.ServiceAndFeeStatus canceledStatus) = coin.servicesAndFees(canceledFee);
        require(canceledStatus == SojaCoinStorage.ServiceAndFeeStatus.Canceled, "open service is canceled");

        vm.prank(serviceManager);
        uint256 paidFee = coin.recordServiceAndFee(
            depositId,
            serviceRecipient,
            SojaCoinStorage.ServiceType.Weighing,
            SojaCoinStorage.FeeUnit.BRLCents,
            2_000,
            keccak256("weighing charge")
        );
        vm.prank(serviceManager);
        coin.confirmServicePaymentInBRL(paidFee, keccak256("payment proof"));
        vm.expectRevert(bytes4(keccak256("ServiceNotOpen()")));
        vm.prank(serviceManager);
        coin.cancelServiceAndFee(paidFee);
    }

    function test_unapprovedServiceManagerCannotCreateCharge() public {
        uint256 depositId = _issueLoad(1, 6_000, keccak256("service-auth-load"));
        vm.expectRevert(bytes4(keccak256("InsufficientServicePermission()")));
        vm.prank(receiver);
        coin.recordServiceAndFee(
            depositId,
            serviceRecipient,
            SojaCoinStorage.ServiceType.Storage,
            SojaCoinStorage.FeeUnit.SJC,
            UNITS_PER_SACK,
            keccak256("unauthorized charge")
        );
    }

    function test_redemptionRequestCanBeCanceledBeforeAuthorization() public {
        _issueLoad(1, 6_000, keccak256("redemption-cancel-load"));
        uint256 amount = 10 * UNITS_PER_SACK;
        uint256 balanceBefore = coin.balanceOf(producer);
        vm.prank(producer);
        uint256 redemptionId = coin.requestRedemption(amount, 1);

        _eq(coin.balanceOf(address(coin)), amount, "requested SJC held by contract");
        _eq(coin.balanceOf(producer), balanceBefore - amount, "holder balance lowered while pending");
        _eq(registry.redemptionsPendingByUnit(1), 1, "unit has pending redemption");
        _eq(registry.reservedInventoryMgByUnit(1), 0, "request alone does not reserve inventory");

        vm.prank(producer);
        coin.cancelRequest(redemptionId);
        _eq(coin.balanceOf(producer), balanceBefore, "cancel returns all SJC");
        _eq(registry.redemptionsPendingByUnit(1), 0, "pending request is cleared");
    }

    function test_redemptionAuthorizationReservesInventoryAndPartialCompletionBurnsOnlyDeliveredAmount() public {
        _issueLoad(1, 6_000, keccak256("redemption-settlement-load"));
        uint256 requested = 10 * UNITS_PER_SACK;
        uint256 balanceBefore = coin.balanceOf(producer);
        vm.prank(producer);
        uint256 redemptionId = coin.requestRedemption(requested, 1);

        vm.prank(authorizer);
        coin.authorizeWithdrawal(redemptionId);
        _eq(registry.reservedInventoryMgByUnit(1), 600 * 1_000_000, "600kg is reserved on authorization");

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "The authorizer cannot complete the withdrawal"));
        vm.prank(authorizer);
        coin.completeWithdrawal(redemptionId, 590, keccak256("blocked same-operator completion"));

        vm.prank(completer);
        coin.completeWithdrawal(redemptionId, 590, keccak256("outbound invoice 590kg"));

        uint256 burned = 9_833_334;
        uint256 returned = requested - burned;
        _eq(coin.totalSupply(), balanceBefore - burned, "only the delivered soybean amount is burned");
        _eq(coin.balanceOf(producer), balanceBefore - requested + returned, "unused authorized amount returns to holder");
        _eq(registry.registeredInventoryMg(), 5_410 * 1_000_000, "physical inventory falls by delivered 590kg");
        _eq(registry.reservedInventoryMgByUnit(1), 0, "reservation is released at settlement");
        _eq(registry.redemptionsPendingByUnit(1), 0, "redemption is closed");
        _eq(coin.sacksInWithdrawal(), 0, "global authorized withdrawal counter clears");
    }

    function test_authorizedRedemptionCanBeCanceledAndReservationReturned() public {
        _issueLoad(1, 6_000, keccak256("redemption-authorized-cancel-load"));
        uint256 amount = 10 * UNITS_PER_SACK;
        uint256 balanceBefore = coin.balanceOf(producer);
        vm.prank(producer);
        uint256 redemptionId = coin.requestRedemption(amount, 1);
        vm.prank(authorizer);
        coin.authorizeWithdrawal(redemptionId);

        vm.prank(authorizer);
        coin.cancelAuthorizedWithdrawal(redemptionId);
        _eq(coin.balanceOf(producer), balanceBefore, "cancellation restores all SJC");
        _eq(registry.reservedInventoryMgByUnit(1), 0, "cancellation releases physical reservation");
        _eq(registry.registeredInventoryMg(), 6_000 * 1_000_000, "cancellation does not reduce physical inventory");
        _eq(coin.sacksInWithdrawal(), 0, "authorized amount is released");
    }

    function test_ownerArbitrationAfterDeadlineCanReturnUnsettledRedemption() public {
        _issueLoad(1, 6_000, keccak256("redemption-arbitration-load"));
        uint256 amount = 5 * UNITS_PER_SACK;
        uint256 balanceBefore = coin.balanceOf(producer);
        vm.prank(producer);
        uint256 redemptionId = coin.requestRedemption(amount, 1);
        vm.prank(authorizer);
        coin.authorizeWithdrawal(redemptionId);

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Deadline has not passed"));
        coin.resolveAsArbitrator(redemptionId, false, 0, bytes32(0));

        vm.warp(block.timestamp + coin.WITHDRAWAL_PERIOD());
        coin.resolveAsArbitrator(redemptionId, false, 0, bytes32(0));
        _eq(coin.balanceOf(producer), balanceBefore, "arbitration returns all locked SJC");
        _eq(registry.reservedInventoryMgByUnit(1), 0, "arbitration releases unit reservation");
        _eq(registry.registeredInventoryMg(), 6_000 * 1_000_000, "no soybeans removed in cancellation arbitration");
    }

    function test_redemptionCannotExceedInventoryAndCannotDeliverAboveAuthorization() public {
        _issueLoad(1, 6_000, keccak256("redemption-limits-load"));
        vm.expectRevert(UnitRegistry.InsufficientAvailableInventory.selector);
        vm.prank(producer);
        coin.requestRedemption(101 * UNITS_PER_SACK, 1);

        vm.prank(producer);
        uint256 redemptionId = coin.requestRedemption(10 * UNITS_PER_SACK, 1);
        vm.prank(authorizer);
        coin.authorizeWithdrawal(redemptionId);
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Exceeds authorized weight"));
        vm.prank(completer);
        coin.completeWithdrawal(redemptionId, 601, keccak256("too much delivered"));
    }
}
