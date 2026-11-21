// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

/// @notice Interface used by UnitRegistry to synchronize inventory,
/// audits, and physical discrepancies with SojaCoin.
interface ISojaCoinUnits {
    function unitRegistry() external view returns (address);

    function syncInventoryFromUnits(
        uint256 physicalInventoryMg,
        bytes32 reportHash,
        address auditorOfTheUnit,
        uint256 lastAuditAt
    ) external;

    function updateAuditValidityFromUnits(
        uint256 lastAuditAt
    ) external;

    function recordTransferDiscrepancy(
        uint256 discrepancyMg,
        bytes32 receiptProofHash,
        address responsibleParty
    ) external;

    function updateIssuanceCapAfterTransfer() external;
}
