// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

/// @notice Integration interface between SojaCoin and the unit registry.
/// @dev SojaCoin calls these functions to keep local balances and per-unit reserves
/// synchronized with deposits and withdrawals.
interface IUnitRegistry {
    function sojaCoin() external view returns (address);

    function registeredInventoryMg() external view returns (uint256);

    function inventoryInTransitMg() external view returns (uint256);

    function lastAuditAt() external view returns (uint256);

    function units(uint256 unitId)
        external
        view
        returns (bytes32 code, bytes32 registrationHash, bytes32 lastReportHash, uint256 registeredInventoryMg, uint256 lastAuditAt, bool active);

    function PERMISSION_MANAGE_SERVICES() external view returns (uint256);

    function PERMISSION_RECEIVE_SOYBEANS() external view returns (uint256);

    function hasPermissionForUnit(
        uint256 unitId,
        address wallet,
        uint256 permission
    ) external view returns (bool);

    function registerPendingDeposit(
        uint256 depositId,
        uint256 unitId,
        address responsibleParty
    ) external;

    function cancelPendingDeposit(uint256 depositId) external;

    function confirmDepositReceipt(
        uint256 depositId,
        uint256 receivedWeightMg,
        uint256 timestamp,
        address responsibleParty
    ) external;

    function validateDepositIssuance(
        uint256 depositId,
        address responsibleParty
    ) external view;

    function recordDepositReturn(
        uint256 depositId,
        uint256 returnedWeightMg,
        address responsibleParty
    ) external;

    function closeDeposit(uint256 depositId) external;

    function availableInventoryMg(
        uint256 unitId
    ) external view returns (uint256);

    function recordRedemptionRequest(
        uint256 redemptionId,
        uint256 unitId,
        uint256 requestedMassMg,
        address holder
    ) external;

    function authorizeRedemption(
        uint256 redemptionId,
        address responsibleParty
    ) external;

    function completeRedemption(
        uint256 redemptionId,
        uint256 deliveredMassMg,
        address responsibleParty,
        bool arbitration
    ) external;

    function cancelRedemption(
        uint256 redemptionId,
        address responsibleParty,
        bool arbitration
    ) external;
}
