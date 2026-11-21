// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {TokenModule} from "./TokenModule.sol";
import {IUnitRegistry} from "../interfaces/IUnitRegistry.sol";

// Per-unit withdrawal requests, authorization, settlement, and arbitration.
abstract contract RedemptionsModule is TokenModule {
    /// @notice Requests a soybean withdrawal from a unit and places the SJC in custody.
    /// @dev The holder chooses the unit. Inventory is checked when requested and checked
    /// again when a unit employee authorizes the operation and reserves it.
    /// @param amountSJC Amount of SJC requested, in the token's internal units.
    /// @param unitId Physical unit from which the holder wants to withdraw soybeans.
    /// @return redemptionId Identifier assigned to the request.
    function requestRedemption(
        uint256 amountSJC,
        uint256 unitId
    ) external returns (uint256 redemptionId) {
        require(unitRegistry != address(0), "Unit registry is not configured");
        require(
            participantRegistered[msg.sender],
            "Requester is not registered"
        );
        require(amountSJC > 0, "Invalid amount");

        redemptionId = ++nextRedemptionId;
        redemptions[redemptionId] = Redemption({
            holder: msg.sender,
            authorizedBy: address(0),
            amountSJC: amountSJC,
            unitId: unitId,
            status: RedemptionStatus.Requested,
            authorizedIn: 0
        });
        IUnitRegistry(unitRegistry).recordRedemptionRequest(
            redemptionId,
            unitId,
            amountSJC * MG_PER_SJC_UNIT,
            msg.sender
        );
        pendingRedemptionsByHolder[msg.sender] += 1;

        _transferTokensFromLock(msg.sender, address(this), amountSJC);
        emit RedemptionRequested(redemptionId, msg.sender, unitId, amountSJC);
    }

    /// @notice Cancels a request that has not been authorized and returns all SJC to the holder.
    /// @dev The holder may cancel it; an employee needs authorization permission for
    /// the request's unit. Any local reservation is released by the registry.
    /// @param redemptionId Identifier of the requested redemption.
    function cancelRequest(uint256 redemptionId) external {
        Redemption storage r = redemptions[redemptionId];
        require(
            r.status == RedemptionStatus.Requested,
            "Redemption has not been requested"
        );

        IUnitRegistry(unitRegistry).cancelRedemption(
            redemptionId,
            msg.sender,
            false
        );
        r.status = RedemptionStatus.Canceled;
        pendingRedemptionsByHolder[r.holder] -= 1;

        _transferTokensFromRedemption(r.holder, r.amountSJC);
        emit RedemptionCanceled(redemptionId, r.unitId, r.holder);
    }

    /// @notice Authorizes the withdrawal and reserves the matching weight at the selected unit.
    /// @dev The wallet needs authorization permission for that unit. The global limit
    /// and recent-audit requirement still apply.
    /// @param redemptionId Identifier of the request made by the holder.
    function authorizeWithdrawal(uint256 redemptionId) external {
        require(!paused(), "Contract is paused");
        _requireCurrentAudit();

        Redemption storage r = redemptions[redemptionId];
        require(
            r.status == RedemptionStatus.Requested,
            "Redemption has not been requested"
        );
        require(
            sacksInWithdrawal + r.amountSJC <= issuanceCapSJC,
            "Insufficient inventory for withdrawal"
        );

        IUnitRegistry(unitRegistry).authorizeRedemption(
            redemptionId,
            msg.sender
        );
        r.status = RedemptionStatus.Authorized;
        r.authorizedBy = msg.sender;
        r.authorizedIn = block.timestamp;
        sacksInWithdrawal += r.amountSJC;

        emit WithdrawalAuthorized(redemptionId, r.unitId, msg.sender, r.amountSJC);
    }

    /// @notice Confirms the actual released weight, burns the matching SJC, and returns any remainder.
    /// @dev The employee needs completion permission for the unit. The physical amount
    /// cannot exceed the reserved SJC. The conversion rounds the debit up.
    /// @param redemptionId Identifier of the authorized withdrawal.
    /// @param deliveredWeightKg Actual released weight, in whole kilograms.
    /// @param returnTaxInvoiceHash Hash of the outbound tax document.
    function completeWithdrawal(
        uint256 redemptionId,
        uint256 deliveredWeightKg,
        bytes32 returnTaxInvoiceHash
    ) external {
        _complete(redemptionId, deliveredWeightKg, returnTaxInvoiceHash, false);
    }

    /// @notice Cancels an authorized withdrawal when no goods left the unit and returns all SJC.
    /// @dev The employee needs authorization permission for the unit.
    /// @param redemptionId Identifier of the authorized withdrawal to cancel.
    function cancelAuthorizedWithdrawal(uint256 redemptionId) external {
        _cancelAuthorized(redemptionId, false);
    }

    /// @notice Resolves a withdrawal that received no response within seven days.
    /// @dev If soybeans left the unit, provide the actual weight and document hash. If they did not,
    /// provide zero for both to release the reservation and return all SJC.
    /// @param redemptionId Identifier of the withdrawal being arbitrated.
    /// @param soybeansWasDelivered Owner's decision on whether the goods were delivered.
    /// @param deliveredWeightKg Actual weight, used only when soybeansWasDelivered is true.
    /// @param returnTaxInvoiceHash Hash of the outbound document, used only for settlement.
    function resolveAsArbitrator(
        uint256 redemptionId,
        bool soybeansWasDelivered,
        uint256 deliveredWeightKg,
        bytes32 returnTaxInvoiceHash
    ) external onlyOwner {
        Redemption storage r = redemptions[redemptionId];
        require(
            r.status == RedemptionStatus.Authorized,
            "Redemption is not authorized"
        );
        require(
            block.timestamp >= r.authorizedIn + WITHDRAWAL_PERIOD,
            "Deadline has not passed"
        );

        if (soybeansWasDelivered) {
            _complete(redemptionId, deliveredWeightKg, returnTaxInvoiceHash, true);
        } else {
            require(
                deliveredWeightKg == 0 && returnTaxInvoiceHash == bytes32(0),
                "Unexpected delivery data"
            );
            _cancelAuthorized(redemptionId, true);
        }
    }

    /// @notice Settles the withdrawal for the amount actually delivered and returns any remainder.
    /// @dev SJC is debited in minimum units, each equal to 60 mg. Rounding up prevents
    /// the released soybeans from exceeding the mass covered by the burned tokens.
    /// @param redemptionId Identifier of the authorized withdrawal.
    /// @param deliveredWeightKg Physical weight released, in kilograms.
    /// @param returnTaxInvoiceHash Hash of the outbound tax document.
    /// @param arbitration True when the owner determined the settlement.
    function _complete(
        uint256 redemptionId,
        uint256 deliveredWeightKg,
        bytes32 returnTaxInvoiceHash,
        bool arbitration
    ) private {
        require(returnTaxInvoiceHash != bytes32(0), "Invalid return tax invoice hash");
        require(deliveredWeightKg > 0, "Invalid delivered weight");

        Redemption storage r = redemptions[redemptionId];
        require(
            r.status == RedemptionStatus.Authorized,
            "Redemption is not authorized"
        );
        if (!arbitration) {
            require(msg.sender != r.authorizedBy, "The authorizer cannot complete the withdrawal");
        }

        uint256 deliveredMassMg = deliveredWeightKg * MG_PER_KG;
        uint256 authorizedMassMg = r.amountSJC * MG_PER_SJC_UNIT;
        require(deliveredMassMg <= authorizedMassMg, "Exceeds authorized weight");

        uint256 burnedAmountSJC = deliveredMassMg / MG_PER_SJC_UNIT;
        if (deliveredMassMg % MG_PER_SJC_UNIT != 0) {
            burnedAmountSJC += 1;
        }
        require(burnedAmountSJC <= r.amountSJC, "Insufficient authorized SJC");

        IUnitRegistry(unitRegistry).completeRedemption(
            redemptionId,
            deliveredMassMg,
            msg.sender,
            arbitration
        );

        r.status = RedemptionStatus.Completed;
        sacksInWithdrawal -= r.amountSJC;
        pendingRedemptionsByHolder[r.holder] -= 1;

        uint256 returnedAmountSJC = r.amountSJC - burnedAmountSJC;
        _burnRedemptionTokens(burnedAmountSJC);
        if (returnedAmountSJC > 0) {
            _transferTokensFromRedemption(r.holder, returnedAmountSJC);
        }
        _reducePhysicalInventory(deliveredMassMg, msg.sender);

        emit WithdrawalCompleted(
            redemptionId,
            r.unitId,
            deliveredWeightKg,
            burnedAmountSJC,
            returnedAmountSJC,
            returnTaxInvoiceHash
        );
    }

    /// @notice Cancels the authorized withdrawal and releases the unit's physical reservation.
    /// @dev Normal settlement requires permission for the unit; owner arbitration can
    /// bypass that permission. In either case, no physical inventory is removed.
    /// @param redemptionId Identifier of the authorized withdrawal.
    /// @param arbitration True when the cancellation comes from owner arbitration.
    function _cancelAuthorized(
        uint256 redemptionId,
        bool arbitration
    ) private {
        Redemption storage r = redemptions[redemptionId];
        require(
            r.status == RedemptionStatus.Authorized,
            "Redemption is not authorized"
        );

        IUnitRegistry(unitRegistry).cancelRedemption(
            redemptionId,
            msg.sender,
            arbitration
        );
        r.status = RedemptionStatus.Canceled;
        sacksInWithdrawal -= r.amountSJC;
        pendingRedemptionsByHolder[r.holder] -= 1;

        _transferTokensFromRedemption(r.holder, r.amountSJC);
        emit RedemptionCanceled(redemptionId, r.unitId, r.holder);
    }
}
