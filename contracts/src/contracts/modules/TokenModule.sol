// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import {ERC20Pausable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Pausable.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {SojaCoinStorage} from "../storage/SojaCoinStorage.sol";
import {IUnitRegistry} from "../interfaces/IUnitRegistry.sol";

// SJC transfer rules. Deposits, guarantees, and redemptions use the base's
// internal routines to move tokens held by the contract.
abstract contract TokenModule is
    SojaCoinStorage,
    ERC20,
    ERC20Burnable,
    ERC20Pausable,
    Ownable2Step,
    ERC20Permit
{
    /// @notice Initializes the token, its initial owner, and SJC's EIP-2612 permit support.
    /// @param initialOwner Wallet that receives initial ownership of the contract.
    constructor(address initialOwner)
        ERC20("SojaCoin", "SJC")
        Ownable(initialOwner)
        ERC20Permit("SojaCoin")
    {}

    /// @notice Applies the shared rules for SJC transfers, issuance, and burns.
    /// @dev Recipients of ordinary transfers must be registered. SJC entering or leaving
    /// the contract requires an authorized internal operation. During controlled redemption
    /// settlement, this routine uses the ERC-20 implementation without the pause.
    /// @param from Source wallet; zero indicates SJC issuance.
    /// @param to Destination wallet; zero indicates an SJC burn.
    /// @param value Amount moved in the token's internal units.
    function _update(
        address from,
        address to,
        uint256 value
    ) internal virtual override(ERC20, ERC20Pausable) {
        if (to != address(0)) {
            if (to == address(this) || from == address(this)) {
                require(
                    _movementOfLock,
                    "Unauthorized token lock movement"
                );
            } else {
                require(
                    participantRegistered[to],
                    "Recipient is not registered"
                );
            }
        }

        if (_settlementRedemption) {
            require(from == address(this), "Invalid redemption settlement");
            ERC20._update(from, to, value);
        } else {
            super._update(from, to, value);
        }
    }

    /// @notice Returns the number of decimal places used by SJC.
    /// @return Number of decimal places displayed for the token.
    function decimals() public pure virtual override returns (uint8) {
        return 6;
    }

    /// @notice Disables renunciation of contract ownership.
    /// @dev The owner can transfer ownership, but this call always reverts
    /// to keep an administrative account configured.
    function renounceOwnership() public view virtual override onlyOwner {
        revert("Owner renunciation is disabled");
    }

    /// @notice Transfers SJC during an internal lock, CPR, or redemption operation.
    /// @dev Temporarily enables the contract to receive or return tokens; the ERC-20
    /// operation still honors the global pause.
    /// @param from Wallet from which the tokens are sent.
    /// @param to Wallet that receives the tokens.
    /// @param amountSJC Amount transferred in SJC's internal units.
    function _transferTokensFromLock(
        address from,
        address to,
        uint256 amountSJC
    ) internal {
        _movementOfLock = true;
        _transfer(from, to, amountSJC);
        _movementOfLock = false;
    }

    /// @notice Returns to the holder SJC held for a redemption request.
    /// @dev Temporarily bypasses the global pause only to transfer from the contract
    /// to the holder. This function cannot move tokens between participants.
    /// @param to Holder's wallet that receives the returned tokens.
    /// @param amountSJC Amount returned in SJC's internal units.
    function _transferTokensFromRedemption(address to, uint256 amountSJC) internal {
        _movementOfLock = true;
        _settlementRedemption = true;
        _transfer(address(this), to, amountSJC);
        _settlementRedemption = false;
        _movementOfLock = false;
    }

    /// @notice Burns SJC held by the contract after a redemption is completed.
    /// @dev The temporary pause bypass only allows burns from the contract's own balance.
    /// @param amountSJC Amount to burn, in SJC's internal units.
    function _burnRedemptionTokens(uint256 amountSJC) internal {
        _settlementRedemption = true;
        _burn(address(this), amountSJC);
        _settlementRedemption = false;
    }

    /// @notice Recalculates the issuance cap using inventory available across units.
    /// @dev In-transit inventory remains part of global backing but is excluded from the cap
    /// for new issuance until receipt is confirmed. One SJC represents 60 mg.
    function _updateIssuanceCap() internal {
        uint256 eligibleInventoryMg = registeredInventoryMg;
        if (unitRegistry != address(0)) {
            uint256 inTransitMg = IUnitRegistry(unitRegistry)
                .inventoryInTransitMg();
            require(inTransitMg <= eligibleInventoryMg, "In-transit amount exceeds inventory");
            eligibleInventoryMg -= inTransitMg;
        }
        issuanceCapSJC = eligibleInventoryMg / MG_PER_SJC_UNIT;
    }

    /// @notice Requires a valid audit for the current inventory model.
    /// @dev When units are configured, the valid timestamp is the oldest audit among
    /// all active units. Auditing one unit therefore does not renew the others.
    function _requireCurrentAudit() internal view {
        uint256 dateAudit = lastAuditAt;
        if (unitRegistry != address(0)) {
            dateAudit = IUnitRegistry(unitRegistry)
                .lastAuditAt();
        }

        require(dateAudit != 0, "Audit does not exist");
        require(
            block.timestamp <= dateAudit + AUDIT_PERIOD,
            "Audit has expired"
        );
    }

    /// @notice Returns the audit timestamp used by the current inventory model.
    function _currentAuditDate() internal view returns (uint256) {
        if (unitRegistry != address(0)) {
            return IUnitRegistry(unitRegistry).lastAuditAt();
        }
        return lastAuditAt;
    }

    /// @notice Reduces global inventory by the physical weight that left custody.
    /// @dev If global inventory is lower than the reported outbound amount, sets the balance
    /// to zero, emits a discrepancy alert, and pauses if remaining backing cannot cover SJC.
    /// @param massMg Outbound weight, in milligrams.
    /// @param responsibleParty Wallet that recorded the outbound movement.
    function _reducePhysicalInventory(
        uint256 massMg,
        address responsibleParty
    ) internal {
        if (registeredInventoryMg >= massMg) {
            registeredInventoryMg -= massMg;
        } else {
            emit DiscrepancyDetected(
                responsibleParty,
                owner(),
                registeredInventoryMg,
                totalSupply(),
                block.timestamp
            );
            registeredInventoryMg = 0;
        }

        _updateIssuanceCap();
        _pauseIfUnderBacked(responsibleParty);
    }

    /// @notice Pauses the contract and records the pause timestamp if it is not already paused.
    /// @dev Internal function used by administration and backing controls.
    function _pauseContract() internal {
        if (!paused()) {
            lastPausedAt = block.timestamp;
            _pause();
        }
    }

    /// @notice Emits an alert and pauses the contract if issued SJC exceeds recorded inventory.
    /// @dev Compares the total SJC converted to milligrams against accounted inventory.
    /// @param detectedBy Wallet or operation that detected the discrepancy.
    function _pauseIfUnderBacked(address detectedBy) internal {
        if (totalSupply() * MG_PER_SJC_UNIT > registeredInventoryMg) {
            emit DiscrepancyDetected(
                detectedBy,
                owner(),
                registeredInventoryMg,
                totalSupply(),
                block.timestamp
            );
            _pauseContract();
        }
    }
}
