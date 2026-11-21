// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

/// @notice Identifies the role and version of a SojaCoin logic implementation.
interface ISojaCoinModule {
    function moduleIdentifier() external pure returns (bytes32);
}

