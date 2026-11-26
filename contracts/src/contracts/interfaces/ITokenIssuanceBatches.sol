// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

/// @notice Integration between SojaCoin and the separate batch approval contract.
interface ITokenIssuanceBatches {
    function sojaCoin() external view returns (address);

    function isDepositInApprovedBatch(uint256 depositId) external view returns (bool);

    function isDepositLocked(uint256 depositId) external view returns (bool);

    function batchCreatorForDeposit(uint256 depositId) external view returns (address);

    function recordIssuance(uint256 depositId) external;
}
