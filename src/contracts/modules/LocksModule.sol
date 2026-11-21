// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {TokenModule} from "./TokenModule.sol";

// Guarantees backed by already-issued SJC held by the contract.
abstract contract LocksModule is TokenModule {
    /// @notice Locks SJC held by the holder to secure an agreement with a creditor.
    /// @dev The caller's wallet is the holder. Both parties must be registered; the tokens
    /// leave the holder's balance and remain in the contract's custody until released or executed.
    /// @param amountSJC Amount of SJC to lock, in the token's internal units.
    /// @param creditor Registered wallet allowed to release or execute the lock.
    /// @param hashAgreement Hash of the agreement stored off-chain.
    /// @param dueDate Unix timestamp from which the creditor may execute the lock.
    /// @return lockId Identifier assigned to this guarantee.
    function lockSJC(
        uint256 amountSJC,
        address creditor,
        bytes32 hashAgreement,
        uint256 dueDate
    ) external returns (uint256 lockId) {
        require(
            participantRegistered[msg.sender],
            "Holder is not registered"
        );
        require(participantRegistered[creditor], "Creditor is not registered");
        require(creditor != msg.sender, "Creditor and holder must differ");
        require(amountSJC > 0, "Invalid amount");
        require(hashAgreement != bytes32(0), "Invalid agreement hash");
        require(dueDate > block.timestamp, "Invalid due date");

        lockId = ++nextLockId;
        locks[lockId] = TokenLock(
            msg.sender,
            creditor,
            amountSJC,
            hashAgreement,
            dueDate,
            true
        );

        _transferTokensFromLock(msg.sender, address(this), amountSJC);

        activeLocksByParticipant[msg.sender] += 1;
        activeLocksByParticipant[creditor] += 1;

        emit TokensLocked(
            lockId,
            msg.sender,
            creditor,
            amountSJC,
            hashAgreement
        );
    }

    /// @notice Closes a lock and returns the locked SJC to the holder.
    /// @dev Only the lock's creditor can call this. The return does not depend on the due date.
    /// @param lockId Identifier of the lock to release.
    function releaseTokenLock(uint256 lockId) external {
        TokenLock storage lock = locks[lockId];

        require(lock.active, "Token lock is inactive");
        require(msg.sender == lock.creditor, "Creditor only");

        lock.active = false;
        activeLocksByParticipant[lock.holder] -= 1;
        activeLocksByParticipant[lock.creditor] -= 1;

        _transferTokensFromLock(address(this), lock.holder, lock.amountSJC);
        emit TokenLockReleased(lockId);
    }

    /// @notice Executes an expired lock and transfers the locked SJC to the creditor.
    /// @dev Only the lock's creditor can call this, and only after the recorded due date.
    /// @param lockId Identifier of the lock to execute.
    function executeTokenLock(uint256 lockId) external {
        TokenLock storage lock = locks[lockId];

        require(lock.active, "Token lock is inactive");
        require(msg.sender == lock.creditor, "Creditor only");
        require(block.timestamp >= lock.dueDate, "Not yet due");

        lock.active = false;
        activeLocksByParticipant[lock.holder] -= 1;
        activeLocksByParticipant[lock.creditor] -= 1;

        _transferTokensFromLock(address(this), lock.creditor, lock.amountSJC);
        emit TokenLockExecuted(lockId);
    }
}
