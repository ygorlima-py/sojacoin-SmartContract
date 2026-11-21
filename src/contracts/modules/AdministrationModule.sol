// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {TokenModule} from "./TokenModule.sol";
import {IUnitRegistry} from "../interfaces/IUnitRegistry.sol";
import {ITokenIssuanceBatches} from "../interfaces/ITokenIssuanceBatches.sol";

// Contract administration: pausing, participants, and backing.
abstract contract AdministrationModule is TokenModule {
    /// @notice Pauses SJC transfers.
    /// @dev Only the owner can call this. The first pause records the timestamp
    /// later used to require a newer audit before reactivating the contract.
    function pause() external onlyOwner {
        _pauseContract();
    }

    /// @notice Reactivates the contract after a recent audit confirms sufficient backing.
    /// @dev Only the owner can call this. Requires an audit within the validity period,
    /// after the latest pause, and enough recorded inventory to cover all issued SJC.
    function unpause() external onlyOwner {
        _requireCurrentAudit();
        require(
            _currentAuditDate() >= lastPausedAt,
            "Audit predates the pause"
        );
        require(totalSupply() * MG_PER_SJC_UNIT <= registeredInventoryMg, "Insufficient inventory backing");
        _unpause();
    }

    /// @notice Links the contract that records units, permissions, and per-unit inventory.
    /// @dev Only the owner can call this. After setup, backing audits must be
    /// submitted by the registry, which accepts only a complete audit of active units.
    /// @param newUnitRegistry Address of the company's UnitRegistry contract.
    function configureUnitRegistry(address newUnitRegistry) external onlyOwner {
        require(newUnitRegistry != address(0), "Invalid unit registry address");
        require(unitRegistry == address(0), "Unit registry already configured");
        require(newUnitRegistry.code.length > 0, "Unit registry address has no code");
        require(IUnitRegistry(newUnitRegistry).sojaCoin() == address(this), "Unit registry is linked to another SojaCoin");

        unitRegistry = newUnitRegistry;
        emit UnitRegistryConfigured(newUnitRegistry, msg.sender);
    }

    /// @notice Connects the separate module that records batches approved by the producer.
    /// @dev This can be configured only once. The module must point to this SojaCoin deployment.
    /// @param newModule Address of the TokenIssuanceBatches contract.
    function configureIssuanceBatches(address newModule) external onlyOwner {
        require(issuanceBatchModule == address(0), "Module already configured");
        require(newModule != address(0), "Invalid module address");
        require(newModule.code.length > 0, "Module address has no code");
        require(ITokenIssuanceBatches(newModule).sojaCoin() == address(this), "Module is linked to another SojaCoin");

        issuanceBatchModule = newModule;
        emit IssuanceBatchesConfigured(newModule, msg.sender);
    }

    /// @notice Receives the total inventory after a unit audit.
    /// @dev Only the configured UnitRegistry can call this. The received timestamp is
    /// the oldest audit among active units and determines global backing validity.
    /// @param physicalInventoryMg Total audited inventory across units, in milligrams.
    /// @param reportHash Hash of the report for the unit audited in this update.
    /// @param auditorOfTheUnit Auditor responsible for the audited unit.
    /// @param lastAuditAt Oldest audit timestamp among all active units.
    function syncInventoryFromUnits(
        uint256 physicalInventoryMg,
        bytes32 reportHash,
        address auditorOfTheUnit,
        uint256 lastAuditAt
    ) external {
        require(msg.sender == unitRegistry, "Unit registry only");
        require(reportHash != bytes32(0), "Invalid report hash");
        require(auditorOfTheUnit != address(0), "Invalid unit auditor");

        registeredInventoryMg = physicalInventoryMg;
        _updateIssuanceCap();
        lastAuditAt = lastAuditAt;

        emit InventoryAuditedByUnit(
            msg.sender, auditorOfTheUnit, physicalInventoryMg / MG_PER_KG, reportHash, block.timestamp
        );

        _pauseIfUnderBacked(msg.sender);
    }

    /// @notice Updates global audit validity when a unit is activated or deactivated.
    /// @dev This does not change inventory; it only prevents using an audit that no
    /// longer covers all active units.
    function updateAuditValidityFromUnits(
        uint256 lastAuditAt
    ) external {
        require(msg.sender == unitRegistry, "Unit registry only");
        lastAuditAt = lastAuditAt;
    }

    /// @notice Updates the issuance cap after a dispatch or receipt between units.
    /// @dev UnitRegistry calls this after updating in-transit inventory.
    /// In-transit goods cannot back new issuance until their arrival is recorded.
    function updateIssuanceCapAfterTransfer() external {
        require(msg.sender == unitRegistry, "Unit registry only");
        _updateIssuanceCap();
    }

    /// @notice Records an inventory reduction caused by a transfer discrepancy.
    /// @dev Only the configured UnitRegistry can call this. Updates backing and the cap,
    /// but does not change the latest audit timestamp. If remaining inventory cannot cover
    /// issued SJC, pauses the token until an audit resolves the discrepancy.
    /// @param discrepancyMg Physical quantity missing from the transfer, in milligrams.
    /// @param receiptProofHash Hash of the destination weighing receipt.
    /// @param responsibleParty Wallet that confirmed receipt with a discrepancy.
    function recordTransferDiscrepancy(
        uint256 discrepancyMg,
        bytes32 receiptProofHash,
        address responsibleParty
    ) external {
        require(msg.sender == unitRegistry, "Unit registry only");
        require(discrepancyMg > 0, "Invalid discrepancy");
        require(receiptProofHash != bytes32(0), "Invalid receipt hash");
        require(responsibleParty != address(0), "Invalid responsible party");
        uint256 previousInventoryMg = registeredInventoryMg;
        if (discrepancyMg >= registeredInventoryMg) {
            registeredInventoryMg = 0;
        } else {
            registeredInventoryMg -= discrepancyMg;
        }
        _updateIssuanceCap();

        emit InventoryAdjustedByTransfer(
            msg.sender, responsibleParty, previousInventoryMg, registeredInventoryMg, receiptProofHash, block.timestamp
        );

        _pauseIfUnderBacked(responsibleParty);
    }

    /// @notice Proposes a wallet for registration as a participant.
    /// @dev Only company administration can make a proposal. The wallet cannot receive
    /// SJC or make deposits until it signs the terms through {acceptParticipationTerms}.
    /// @param walletAddress Wallet of the producer or creditor being invited.
    /// @param participationTermsHash Hash of the terms stored off-chain.
    function proposeParticipantRegistration(address walletAddress, bytes32 participationTermsHash) external onlyOwner {
        require(walletAddress != address(0), "Invalid address");
        require(participationTermsHash != bytes32(0), "Invalid terms hash");
        require(!participantRegistered[walletAddress], "Participant is already registered");
        require(!participantRegistrationPending[walletAddress], "Registration is already pending");

        participationTermsHashByParticipant[walletAddress] = participationTermsHash;
        participantRegistrationPending[walletAddress] = true;
        emit ParticipantRegistrationProposed(walletAddress, participationTermsHash, msg.sender);
    }

    /// @notice Activates a participant after validating acceptance signed by their wallet.
    /// @dev Any wallet can submit a valid signature. This lets the SaaS pay for the
    /// transaction without holding the producer's private key.
    /// @param participant Wallet that accepted the pending terms.
    /// @param deadline Unix deadline included in the signature.
    /// @param signature EIP-712 acceptance signed by the participant's wallet.
    function acceptParticipationTerms(
        address participant,
        uint256 deadline,
        bytes calldata signature
    ) external {
        require(participantRegistrationPending[participant], "Registration is not pending");
        require(block.timestamp <= deadline, "Acceptance has expired");

        uint256 nonce = participationTermsNonces[participant];
        bytes32 digest = _hashTypedDataV4(
            keccak256(
                abi.encode(
                    PARTICIPATION_TERMS_ACCEPTANCE_TYPEHASH,
                    participant,
                    participationTermsHashByParticipant[participant],
                    nonce,
                    deadline
                )
            )
        );
        require(
            SignatureChecker.isValidSignatureNow(participant, digest, signature),
            "Invalid participant acceptance"
        );

        participationTermsNonces[participant] = nonce + 1;
        participantRegistrationPending[participant] = false;
        participantRegistered[participant] = true;
        depositAuthorizationActive[participant] = true;

        emit ParticipantRegistered(participant, participationTermsHashByParticipant[participant], participant);
        emit DepositAuthorizationChanged(
            participant, true, participationTermsHashByParticipant[participant], msg.sender
        );
    }

    /// @notice Cancels a registration proposal that has not yet been accepted.
    /// @dev Only administration can cancel it. The wallet does not become a participant.
    /// @param participant Wallet whose pending invitation will be canceled.
    function cancelPendingParticipantRegistration(address participant) external onlyOwner {
        require(participantRegistrationPending[participant], "Registration is not pending");
        participantRegistrationPending[participant] = false;
        delete participationTermsHashByParticipant[participant];
        emit PendingParticipantRegistrationCanceled(participant, msg.sender);
    }

    /// @notice Revokes authorization to register new deposits for the participant.
    /// @dev Revocation does not cancel deposits already registered or prevent the
    /// participant from transferring or redeeming SJC they already hold.
    function revokeDepositAuthorization() external {
        require(participantRegistered[msg.sender], "Participant is not registered");
        require(depositAuthorizationActive[msg.sender], "Authorization has already been revoked");

        depositAuthorizationActive[msg.sender] = false;
        emit DepositAuthorizationChanged(msg.sender, false, participationTermsHashByParticipant[msg.sender], msg.sender);
    }

    /// @notice Re-enables future deposits using a new authorization agreement.
    /// @dev The call must come from the participant's wallet, and the hash must point
    /// to the updated terms stored off-chain.
    /// @param newTermsHash Hash of the updated authorization terms.
    function authorizeFutureDeposits(bytes32 newTermsHash) external {
        require(participantRegistered[msg.sender], "Participant is not registered");
        require(newTermsHash != bytes32(0), "Invalid terms hash");

        participationTermsHashByParticipant[msg.sender] = newTermsHash;
        depositAuthorizationActive[msg.sender] = true;
        emit DepositAuthorizationChanged(msg.sender, true, newTermsHash, msg.sender);
    }

    /// @notice Removes a participant registration with no pending operations or balances.
    /// @dev Only the owner can call this. Requires a zero SJC balance and no pending deposits,
    /// redemptions, locks, or active CPRs for the wallet.
    /// @param walletAddress Wallet of the participant to remove.
    function removeParticipant(address walletAddress) external onlyOwner {
        require(participantRegistered[walletAddress], "Participant is not registered");
        require(pendingDeposits[walletAddress] == 0, "Participant has pending deposits");
        require(balanceOf(walletAddress) == 0, "Participant has an SJC balance");
        require(pendingRedemptionsByHolder[walletAddress] == 0, "Participant has pending redemptions");
        require(activeLocksByParticipant[walletAddress] == 0, "Participant has active token locks");
        require(activeCPRsByParticipant[walletAddress] == 0, "Participant has active CPRs");

        participantRegistered[walletAddress] = false;
        if (depositAuthorizationActive[walletAddress]) {
            depositAuthorizationActive[walletAddress] = false;
            emit DepositAuthorizationChanged(
                walletAddress, false, participationTermsHashByParticipant[walletAddress], msg.sender
            );
        }
        emit ParticipantRemoved(walletAddress);
    }
}
