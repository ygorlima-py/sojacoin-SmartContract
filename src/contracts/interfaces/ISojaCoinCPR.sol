// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

/// @notice Internal operation between the deposits module and the CPR module.
interface ISojaCoinCPR {
    function distributeIssuanceByCPR(uint256 depositId, uint256 amountSJC) external returns (uint256 unencumberedAmountSJC);
}
