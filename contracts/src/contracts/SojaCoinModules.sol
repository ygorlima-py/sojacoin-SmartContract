// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {TokenModule} from "./modules/TokenModule.sol";
import {AdministrationModule} from "./modules/AdministrationModule.sol";
import {DepositsModule} from "./modules/DepositsModule.sol";
import {GuaranteesModule} from "./modules/GuaranteesModule.sol";
import {LocksModule} from "./modules/LocksModule.sol";
import {RedemptionsModule} from "./modules/RedemptionsModule.sol";
import {ServicesModule} from "./modules/ServicesModule.sol";
import {ISojaCoinModule} from "./interfaces/ISojaCoinModule.sol";

/// @dev Logic implementations called only by SojaCoin through delegatecall.
/// The initial address is used only to satisfy ERC-20 module initialization;
/// implementation storage is not used during delegated calls.
contract SojaCoinAdministration is AdministrationModule, ISojaCoinModule {
    constructor() TokenModule(address(1)) {}

    function moduleIdentifier() external pure returns (bytes32) {
        return keccak256("SojaCoin.Administration.v2");
    }
}

/// @dev Handles deposit receipts and token issuance.
contract SojaCoinDeposits is DepositsModule, ISojaCoinModule {
    constructor() TokenModule(address(1)) {}

    function moduleIdentifier() external pure returns (bytes32) {
        return keccak256("SojaCoin.Deposits.v2");
    }
}

/// @dev Registers CPRs as debts in bags and distributes issued tokens to creditors.
contract SojaCoinCPR is GuaranteesModule, ISojaCoinModule {
    constructor() TokenModule(address(1)) {}

    function moduleIdentifier() external pure returns (bytes32) {
        return keccak256("SojaCoin.CPR.v2");
    }
}

contract SojaCoinServices is ServicesModule, ISojaCoinModule {
    constructor() TokenModule(address(1)) {}

    function moduleIdentifier() external pure returns (bytes32) {
        return keccak256("SojaCoin.Services.v2");
    }
}

contract SojaCoinLocks is LocksModule, ISojaCoinModule {
    constructor() TokenModule(address(1)) {}

    function moduleIdentifier() external pure returns (bytes32) {
        return keccak256("SojaCoin.Locks.v2");
    }
}

contract SojaCoinRedemptions is RedemptionsModule, ISojaCoinModule {
    constructor() TokenModule(address(1)) {}

    function moduleIdentifier() external pure returns (bytes32) {
        return keccak256("SojaCoin.Redemptions.v2");
    }
}
