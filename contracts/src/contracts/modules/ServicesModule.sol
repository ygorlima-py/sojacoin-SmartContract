// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {TokenModule} from "./TokenModule.sol";
import {IUnitRegistry} from "../interfaces/IUnitRegistry.sol";

// Services charged by the warehouse without changing the weight accepted on the weighing ticket.
abstract contract ServicesModule is TokenModule {
    error InsufficientServicePermission();
    error ServiceDepositDoesNotExist();
    error DepositTokensNotIssued();
    error RecipientNotRegistered();
    error RecipientIsProducer();
    error InvalidServiceAmount();
    error InvalidChargeHash();
    error ServiceNotOpen();
    error OnlyServiceProducer();
    error InvalidChargeUnit();
    error InvalidPaymentHash();

    /// @notice Returns the IDs of service charges linked to a deposit.
    /// @dev This query only reads the list recorded for the deposit.
    /// @param depositId Identifier of the queried deposit.
    /// @return idsServiceAndFee IDs of the charges associated with the deposit.
    function servicesAndFeesForDeposit(
        uint256 depositId
    ) external view returns (uint256[] memory) {
        return _servicesAndFeesOfTheDeposit[depositId];
    }

    /// @notice Records a service charge linked to an already-issued deposit.
    /// @dev Requires permission to manage services for the deposit's unit. The recipient
    /// must be another registered participant. The charge remains open for payment in SJC
    /// or for confirmation of payment in Brazilian reais.
    /// The hash identifies the detailed charge document stored off-chain.
    /// @param depositId Deposit that generated the service charge.
    /// @param recipient Participant who receives payment in SJC, if that unit is selected.
    /// @param serviceType Category of the service provided.
    /// @param unit Indicates whether the amount is in SJC or Brazilian cents.
    /// @param amount Charge amount in the selected unit.
    /// @param hashCharge Hash of the document detailing the charge.
    /// @return serviceAndFeeId Identifier of the recorded charge.
    function recordServiceAndFee(
        uint256 depositId,
        address recipient,
        ServiceType serviceType,
        FeeUnit unit,
        uint256 amount,
        bytes32 hashCharge
    ) external returns (uint256 serviceAndFeeId) {
        Deposit storage deposit = deposits[depositId];
        if (deposit.depositor == address(0)) revert ServiceDepositDoesNotExist();
        if (!deposit.tokensIssued) revert DepositTokensNotIssued();
        _requirePermissionService(deposit.unitId);
        if (!participantRegistered[recipient]) revert RecipientNotRegistered();
        if (recipient == deposit.depositor) revert RecipientIsProducer();
        if (amount == 0) revert InvalidServiceAmount();
        if (hashCharge == bytes32(0)) revert InvalidChargeHash();

        serviceAndFeeId = ++nextServiceAndFeeId;
        servicesAndFees[serviceAndFeeId] = ServiceAndFee(
            depositId,
            deposit.depositor,
            recipient,
            serviceType,
            unit,
            amount,
            hashCharge,
            bytes32(0),
            ServiceAndFeeStatus.Open
        );
        _servicesAndFeesOfTheDeposit[depositId].push(serviceAndFeeId);

        emit ServiceAndFeeRecorded(
            serviceAndFeeId,
            depositId,
            deposit.depositor,
            recipient,
            serviceType,
            unit,
            amount,
            hashCharge,
            msg.sender
        );
    }

    /// @notice Pays an open charge in SJC when its unit is SJC.
    /// @dev Only the producer linked to the charge can call this. Transfers the amount to
    /// the registered recipient and changes the charge state to PaidInSJC.
    /// @param serviceAndFeeId Identifier of the charge to pay.
    function payServiceInSJC(uint256 serviceAndFeeId) external {
        ServiceAndFee storage service = servicesAndFees[serviceAndFeeId];

        if (service.status != ServiceAndFeeStatus.Open) revert ServiceNotOpen();
        if (msg.sender != service.producer) revert OnlyServiceProducer();
        if (service.unit != FeeUnit.SJC) revert InvalidChargeUnit();

        service.status = ServiceAndFeeStatus.PaidInSJC;
        _transfer(msg.sender, service.recipient, service.amount);

        emit ServicePaidInSJC(
            serviceAndFeeId,
            msg.sender,
            service.recipient,
            service.amount
        );
    }

    /// @notice Records that a charge in Brazilian reais was paid off-chain.
    /// @dev Requires permission to manage services for the deposit's unit. The contract stores
    /// the submitted proof hash; it does not query or directly verify a bank account.
    /// @param serviceAndFeeId Identifier of the charge in Brazilian reais.
    /// @param paymentProofHash Hash of the proof stored off-chain.
    function confirmServicePaymentInBRL(
        uint256 serviceAndFeeId,
        bytes32 paymentProofHash
    ) external {
        ServiceAndFee storage service = servicesAndFees[serviceAndFeeId];
        if (service.status != ServiceAndFeeStatus.Open) revert ServiceNotOpen();
        if (service.unit != FeeUnit.BRLCents) {
            revert InvalidChargeUnit();
        }
        _requirePermissionService(deposits[service.depositId].unitId);
        if (paymentProofHash == bytes32(0)) {
            revert InvalidPaymentHash();
        }

        service.paymentProofHash = paymentProofHash;
        service.status = ServiceAndFeeStatus.PaidInBRL;

        emit ServicePaidInBRL(
            serviceAndFeeId,
            msg.sender,
            paymentProofHash
        );
    }

    /// @notice Cancels a service charge that is still open.
    /// @dev Requires permission to manage services for the deposit's unit. A paid charge
    /// cannot be canceled using this method.
    /// @param serviceAndFeeId Identifier of the charge to cancel.
    function cancelServiceAndFee(uint256 serviceAndFeeId) external {
        ServiceAndFee storage service = servicesAndFees[serviceAndFeeId];
        if (service.status != ServiceAndFeeStatus.Open) revert ServiceNotOpen();
        _requirePermissionService(deposits[service.depositId].unitId);

        service.status = ServiceAndFeeStatus.Canceled;
        emit ServiceAndFeeCanceled(serviceAndFeeId, msg.sender);
    }

    /// @notice Checks local permission to manage a charge linked to a deposit.
    /// @param unitId Unit where the charge was created.
    function _requirePermissionService(uint256 unitId) private view {
        if (unitRegistry == address(0)) {
            revert InsufficientServicePermission();
        }

        IUnitRegistry registry = IUnitRegistry(unitRegistry);
        if (
            !registry.hasPermissionForUnit(
                unitId,
                msg.sender,
                registry.PERMISSION_MANAGE_SERVICES()
            )
        ) {
            revert InsufficientServicePermission();
        }
    }
}
