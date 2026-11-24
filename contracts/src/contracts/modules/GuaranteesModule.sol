// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {TokenModule} from "./TokenModule.sol";
import {IUnitRegistry} from "../interfaces/IUnitRegistry.sol";

/// @notice Registers CPRs as debts in bags and distributes each issuance according to the queue.
/// @dev Full documents remain off-chain; hashes and workflow records are stored here.
abstract contract GuaranteesModule is TokenModule {
    /// @notice Returns a producer's open CPRs, ordered by their current priority.
    function activeCPRsForProducer(address producer) external view returns (uint256[] memory) {
        return _cprsActiveOfTheProducer[producer];
    }

    /// @notice Returns the signature used by the producer to accept a new CPR.
    function cprAuthorizationHash(
        address producer,
        address creditor,
        uint256 initialAmountSJC,
        uint256 deliveryUnitId,
        bytes32 cropYear,
        uint256 priority,
        uint256 dueDate,
        bytes32 documentHash,
        uint256 deadline
    ) external view returns (bytes32) {
        return _digestAuthorizationCPR(
            producer,
            creditor,
            initialAmountSJC,
            deliveryUnitId,
            cropYear,
            priority,
            dueDate,
            documentHash,
            cprNonces[producer],
            deadline
        );
    }

    /// @notice Registers the debt after the producer signs its terms.
    /// @dev The initial priority must be greater than the last priority assigned to the producer.
    function registerCPR(
        address producer,
        address creditor,
        uint256 initialAmountSJC,
        uint256 deliveryUnitId,
        bytes32 cropYear,
        uint256 priority,
        uint256 dueDate,
        bytes32 documentHash,
        uint256 deadline,
        bytes calldata signatureProducer
    ) external returns (uint256 cprId) {
        require(participantRegistered[producer], "Producer is not registered");
        require(participantRegistered[creditor], "Creditor is not registered");
        require(producer != creditor, "Producer and creditor must differ");
        require(initialAmountSJC > 0, "Invalid CPR amount");
        require(deliveryUnitId > 0, "Invalid delivery unit");
        require(cropYear != bytes32(0), "Invalid crop year");
        require(priority > lastCPROrderByProducer[producer], "Invalid priority");
        require(dueDate > block.timestamp, "Invalid due date");
        require(documentHash != bytes32(0), "Invalid CPR document hash");
        require(block.timestamp <= deadline, "Acceptance has expired");
        require(
            _cprsActiveOfTheProducer[producer].length < MAX_ACTIVE_CPRS_PER_PRODUCER,
            "Active CPR limit reached"
        );
        _requireActiveUnit(deliveryUnitId);

        uint256 nonce = cprNonces[producer];
        bytes32 digest = _digestAuthorizationCPR(
            producer,
            creditor,
            initialAmountSJC,
            deliveryUnitId,
            cropYear,
            priority,
            dueDate,
            documentHash,
            nonce,
            deadline
        );
        require(
            SignatureChecker.isValidSignatureNow(producer, digest, signatureProducer),
            "Invalid producer acceptance"
        );

        cprNonces[producer] = nonce + 1;
        lastCPROrderByProducer[producer] = priority;
        cprId = ++nextCPRId;

        cprDebts[cprId] = CPRDebt({
            producer: producer,
            creditor: creditor,
            initialAmountSJC: initialAmountSJC,
            outstandingBalanceSJC: initialAmountSJC,
            amountPaidSJC: 0,
            closedBalanceSJC: 0,
            deliveryUnitId: deliveryUnitId,
            cropYear: cropYear,
            priority: priority,
            dueDate: dueDate,
            documentHash: documentHash,
            closureDocumentHash: bytes32(0),
            producerAcceptance: true,
            acceptedAt: block.timestamp,
            status: CPRStatus.Open
        });
        _signaturesAcceptanceCPR[cprId] = signatureProducer;
        _insertCPRIntoQueue(producer, cprId);
        activeCPRsByParticipant[producer] += 1;
        activeCPRsByParticipant[creditor] += 1;

        emit CPRRegistered(
            cprId,
            producer,
            creditor,
            initialAmountSJC,
            deliveryUnitId,
            cropYear,
            priority,
            documentHash,
            dueDate
        );
        emit ProducerCPRAcceptanceRecorded(cprId, producer, keccak256(signatureProducer), block.timestamp);
    }

    /// @notice Returns the producer's acceptance signature stored in the CPR record.
    function cprAcceptanceSignature(uint256 cprId) external view returns (bytes memory) {
        require(cprDebts[cprId].status != CPRStatus.Nonexistent, "CPR does not exist");
        return _signaturesAcceptanceCPR[cprId];
    }

    /// @notice Returns the state based on the current time, even before it is stored.
    function currentCPRStatus(uint256 cprId) external view returns (CPRStatus) {
        CPRDebt storage cpr = cprDebts[cprId];
        require(cpr.status != CPRStatus.Nonexistent, "CPR does not exist");
        if (cpr.status == CPRStatus.Open && cpr.outstandingBalanceSJC > 0 && block.timestamp >= cpr.dueDate) {
            return CPRStatus.OverdueWithBalance;
        }
        return cpr.status;
    }

    /// @notice Records that an open CPR has expired and still has an outstanding balance.
    /// @dev Anyone can call this; it does not change the balance or issue SJC.
    function updateCPRStatus(uint256 cprId) external {
        require(cprDebts[cprId].status != CPRStatus.Nonexistent, "CPR does not exist");
        _updateCPRStatus(cprId);
    }

    /// @notice Calculates producer and creditor acceptance to use another unit for this load.
    function alternativeUnitCPRAuthorizationHash(uint256 depositId, uint256 cprId, uint256 deadline)
        external
        view
        returns (bytes32)
    {
        return _digestUnitAlternative(depositId, cprId, noncesUnitAlternative[depositId][cprId], deadline);
    }

    /// @notice Allows a load received at another unit to repay a CPR.
    /// @dev Requires signatures from the producer and current creditor. Authorization applies only to this load and CPR.
    function authorizeAlternativeCPRUnit(
        uint256 depositId,
        uint256 cprId,
        uint256 deadline,
        bytes calldata signatureProducer,
        bytes calldata signatureCreditor
    ) external {
        Deposit storage deposit = deposits[depositId];
        CPRDebt storage cpr = cprDebts[cprId];
        require(deposit.depositor == cpr.producer && deposit.depositor != address(0), "Load and CPR do not match");
        require(deposit.cropYear == cpr.cropYear, "Crop year does not match");
        require(deposit.unitId != cpr.deliveryUnitId, "Unit is already set");
        require(!deposit.canceled && !deposit.tokensIssued, "Load is unavailable");
        require(_cprCanReceivePayment(cpr), "CPR has no active balance");
        require(!alternativeUnitAuthorized[depositId][cprId], "Authorization has already been recorded");
        require(block.timestamp <= deadline, "Authorization has expired");

        bytes32 digest = _digestUnitAlternative(
            depositId,
            cprId,
            noncesUnitAlternative[depositId][cprId],
            deadline
        );
        require(
            SignatureChecker.isValidSignatureNow(cpr.producer, digest, signatureProducer),
            "Invalid producer acceptance"
        );
        require(
            SignatureChecker.isValidSignatureNow(cpr.creditor, digest, signatureCreditor),
            "Invalid creditor acceptance"
        );

        noncesUnitAlternative[depositId][cprId] += 1;
        alternativeUnitAuthorized[depositId][cprId] = true;
        creditorWhoAuthorizedAlternativeUnit[depositId][cprId] = cpr.creditor;
        emit AlternativeCPRUnitAuthorized(
            depositId,
            cprId,
            cpr.producer,
            cpr.creditor,
            deposit.unitId,
            deposit.cropYear
        );
    }

    /// @notice Calculates the current creditor's acceptance to assign a CPR to another creditor.
    function cprAssignmentAuthorizationHash(
        uint256 cprId,
        address newCreditor,
        string calldata registryReference,
        bytes32 documentHash,
        uint256 deadline
    ) external view returns (bytes32) {
        CPRDebt storage cpr = cprDebts[cprId];
        require(cpr.status != CPRStatus.Nonexistent, "CPR does not exist");
        return _digestAssignmentCPR(
            cprId,
            cpr.creditor,
            newCreditor,
            keccak256(bytes(registryReference)),
            documentHash,
            noncesAssignmentCPR[cprId],
            deadline
        );
    }

    /// @notice Changes the current creditor and records the assignment evidence reference and hash.
    /// @dev The contract stores the submitted evidence but does not query an official registry.
    /// The current creditor may submit the transaction or sign a single-use authorization.
    function assignCPR(
        uint256 cprId,
        address newCreditor,
        string calldata registryReference,
        bytes32 documentHash,
        uint256 deadline,
        bytes calldata signatureCreditorCurrent
    ) external {
        CPRDebt storage cpr = cprDebts[cprId];
        require(_cprCanReceivePayment(cpr), "CPR has no active balance");
        require(participantRegistered[newCreditor], "New creditor is not registered");
        require(newCreditor != cpr.producer && newCreditor != cpr.creditor, "Invalid new creditor");
        require(bytes(registryReference).length > 0 && bytes(registryReference).length <= 128, "Invalid reference");
        require(documentHash != bytes32(0), "Invalid proof hash");
        require(block.timestamp <= deadline, "Authorization has expired");

        address previousCreditor = cpr.creditor;
        if (msg.sender != previousCreditor) {
            bytes32 digest = _digestAssignmentCPR(
                cprId,
                previousCreditor,
                newCreditor,
                keccak256(bytes(registryReference)),
                documentHash,
                noncesAssignmentCPR[cprId],
                deadline
            );
            require(
                SignatureChecker.isValidSignatureNow(previousCreditor, digest, signatureCreditorCurrent),
                "Invalid creditor authorization"
            );
        }

        noncesAssignmentCPR[cprId] += 1;
        cpr.creditor = newCreditor;
        activeCPRsByParticipant[previousCreditor] -= 1;
        activeCPRsByParticipant[newCreditor] += 1;
        _referencesAssignmentCPR[cprId] = registryReference;

        emit CPRAssigned(
            cprId,
            previousCreditor,
            newCreditor,
            cpr.producer,
            msg.sender,
            registryReference,
            documentHash,
            block.timestamp
        );
    }

    /// @notice Returns the reference recorded for the CPR's latest assignment.
    function cprAssignmentRegistryReference(uint256 cprId) external view returns (string memory) {
        return _referencesAssignmentCPR[cprId];
    }

    /// @notice Calculates the signature required to move a CPR in the producer's queue.
    /// @dev The digest is bound to the current queue, proposed position, and document hash.
    function cprReorderingAuthorizationHash(
        address producer,
        uint256 cprId,
        uint256 newPosition,
        bytes32 documentHash,
        uint256 deadline
    ) external view returns (bytes32) {
        (,,,, bytes32 queueHash) = _dataReordering(producer, cprId, newPosition);
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    CPR_REORDERING_AUTHORIZATION_TYPEHASH,
                    producer,
                    cprId,
                    newPosition,
                    documentHash,
                    queueHash,
                    noncesReorderingCPR[producer],
                    deadline
                )
            )
        );
    }

    /// @notice Changes a CPR's position with acceptance from the producer and affected creditors.
    /// @dev The new position is 1-based. Priorities of crossed CPRs are rearranged
    /// without changing the relative order of the others. Each creditor wallet signs once.
    function reorderCPR(
        address producer,
        uint256 cprId,
        uint256 newPosition,
        bytes32 documentHash,
        uint256 deadline,
        bytes calldata signatureProducer,
        bytes[] calldata signaturesCreditors
    ) external {
        require(documentHash != bytes32(0), "Invalid document hash");
        require(block.timestamp <= deadline, "Authorization has expired");
        (uint256 indexCurrent, uint256 indexNew, uint256 startIndex, uint256 endIndex, bytes32 queueHash) =
            _dataReordering(producer, cprId, newPosition);
        require(indexCurrent != indexNew, "Priority has not changed");

        bytes32 digest = _hashTypedDataV4(
            keccak256(
                abi.encode(
                    CPR_REORDERING_AUTHORIZATION_TYPEHASH,
                    producer,
                    cprId,
                    newPosition,
                    documentHash,
                    queueHash,
                    noncesReorderingCPR[producer],
                    deadline
                )
            )
        );
        require(
            SignatureChecker.isValidSignatureNow(producer, digest, signatureProducer),
            "Invalid producer acceptance"
        );

        address[] memory creditors = _affectedCreditors(producer, startIndex, endIndex);
        require(signaturesCreditors.length == creditors.length, "Missing creditor acceptances");
        for (uint256 i = 0; i < creditors.length; i++) {
            require(
                SignatureChecker.isValidSignatureNow(creditors[i], digest, signaturesCreditors[i]),
                "Invalid creditor acceptance"
            );
        }

        uint256[] storage queue = _cprsActiveOfTheProducer[producer];
        uint256[] memory priorities = new uint256[](endIndex - startIndex + 1);
        for (uint256 i = startIndex; i <= endIndex; i++) {
            priorities[i - startIndex] = cprDebts[queue[i]].priority;
        }
        uint256 previousPriority = cprDebts[cprId].priority;

        if (indexNew < indexCurrent) {
            for (uint256 i = indexCurrent; i > indexNew; i--) queue[i] = queue[i - 1];
        } else {
            for (uint256 i = indexCurrent; i < indexNew; i++) queue[i] = queue[i + 1];
        }
        queue[indexNew] = cprId;
        for (uint256 i = startIndex; i <= endIndex; i++) {
            cprDebts[queue[i]].priority = priorities[i - startIndex];
        }

        noncesReorderingCPR[producer] += 1;
        uint256 newPriority = cprDebts[cprId].priority;
        uint256 reorderingId = ++nextCPRReorderingId;
        reorderingsCPR[reorderingId] = CPRReordering({
            producer: producer,
            requestedBy: msg.sender,
            cprId: cprId,
            previousPriority: previousPriority,
            newPriority: newPriority,
            documentHash: documentHash,
            timestamp: block.timestamp
        });
        _cprReorderingAcceptingParties[reorderingId].push(producer);
        for (uint256 i = 0; i < creditors.length; i++) {
            _cprReorderingAcceptingParties[reorderingId].push(creditors[i]);
        }
        emit CPRPriorityChanged(
            reorderingId,
            cprId,
            producer,
            msg.sender,
            previousPriority,
            newPriority,
            documentHash,
            _cprReorderingAcceptingParties[reorderingId],
            block.timestamp
        );
    }

    /// @notice Returns the parties that accepted a priority change.
    function cprReorderingAcceptingParties(uint256 reorderingId) external view returns (address[] memory) {
        return _cprReorderingAcceptingParties[reorderingId];
    }

    /// @notice Calculates the message the producer and creditor must sign to close a CPR.
    function cprClosureAuthorizationHash(
        uint256 cprId,
        bool cancellation,
        bytes32 documentHash,
        uint256 deadline
    ) external view returns (bytes32) {
        CPRDebt storage cpr = cprDebts[cprId];
        require(cpr.status != CPRStatus.Nonexistent, "CPR does not exist");
        return _digestClosureCPR(
            cprId,
            cpr.producer,
            cpr.creditor,
            cancellation,
            documentHash,
            noncesClosureCPR[cprId],
            deadline
        );
    }

    /// @notice Records an agreed cancellation before any repayment has been made.
    function cancelCPR(
        uint256 cprId,
        bytes32 documentHash,
        uint256 deadline,
        bytes calldata signatureProducer,
        bytes calldata signatureCreditor
    ) external {
        CPRDebt storage cpr = cprDebts[cprId];
        require(_cprCanReceivePayment(cpr), "CPR has no active balance");
        require(cpr.amountPaidSJC == 0, "CPR has already received a repayment");
        _validateClosureAcceptances(cprId, true, documentHash, deadline, signatureProducer, signatureCreditor);

        uint256 balanceCanceled = cpr.outstandingBalanceSJC;
        cpr.outstandingBalanceSJC = 0;
        cpr.closedBalanceSJC = balanceCanceled;
        cpr.closureDocumentHash = documentHash;
        _changeCPRStatus(cprId, CPRStatus.Canceled);
        _closeCPRInQueue(cprId, cpr.producer, cpr.creditor);
        emit CPRCanceled(cprId, cpr.producer, cpr.creditor, balanceCanceled, documentHash, block.timestamp);
    }

    /// @notice Closes all or part of an outstanding debt by agreement.
    /// @dev The closed balance is stored in closedBalanceSJC and does not create SJC.
    function closeCPRByAgreement(
        uint256 cprId,
        bytes32 documentHash,
        uint256 deadline,
        bytes calldata signatureProducer,
        bytes calldata signatureCreditor
    ) external {
        CPRDebt storage cpr = cprDebts[cprId];
        require(_cprCanReceivePayment(cpr), "CPR has no active balance");
        _validateClosureAcceptances(cprId, false, documentHash, deadline, signatureProducer, signatureCreditor);

        uint256 balanceClosed = cpr.outstandingBalanceSJC;
        cpr.outstandingBalanceSJC = 0;
        cpr.closedBalanceSJC = balanceClosed;
        cpr.closureDocumentHash = documentHash;
        _changeCPRStatus(cprId, CPRStatus.ClosedByAgreement);
        _closeCPRInQueue(cprId, cpr.producer, cpr.creditor);
        emit CPRClosedByAgreement(cprId, cpr.producer, cpr.creditor, balanceClosed, documentHash, block.timestamp);
    }

    /// @notice Distributes issuance to compatible creditors and sends any remainder to the producer.
    /// @dev Only CPRs with the same crop year and unit enter the queue; another unit requires per-load acceptance.
    /// Each repayment and its late indicator are recorded by deposit and CPR.
    function distributeIssuanceByCPR(uint256 depositId, uint256 amountSJC) external returns (uint256 unencumberedAmountSJC) {
        require(msg.sender == address(this), "Internal operation only");
        Deposit storage deposit = deposits[depositId];
        require(deposit.receiptConfirmed && !deposit.canceled && !deposit.tokensIssued, "Load is unavailable");
        return _distributeIssuanceByCPR(deposit, depositId, amountSJC);
    }

    function _distributeIssuanceByCPR(Deposit storage deposit, uint256 depositId, uint256 amountSJC)
        private
        returns (uint256 unencumberedAmountSJC)
    {
        uint256 remaining = amountSJC;
        uint256[] memory queue = _cprsActiveOfTheProducer[deposit.depositor];

        for (uint256 i = 0; i < queue.length && remaining > 0; i++) {
            uint256 cprId = queue[i];
            CPRDebt storage cpr = cprDebts[cprId];
            if (!_cprCanReceivePayment(cpr) || cpr.cropYear != deposit.cropYear) continue;
            if (
                cpr.deliveryUnitId != deposit.unitId
                    && (!alternativeUnitAuthorized[depositId][cprId]
                        || creditorWhoAuthorizedAlternativeUnit[depositId][cprId] != cpr.creditor)
            ) {
                continue;
            }

            _updateCPRStatus(cprId);
            uint256 amountSJC = remaining < cpr.outstandingBalanceSJC ? remaining : cpr.outstandingBalanceSJC;
            bool overdue = deposit.receivedAt >= cpr.dueDate;
            cpr.outstandingBalanceSJC -= amountSJC;
            cpr.amountPaidSJC += amountSJC;
            remaining -= amountSJC;

            _mint(cpr.creditor, amountSJC);
            _repaymentsCPRByDeposit[depositId].push(CPRRepayment(cprId, amountSJC, overdue));
            repaymentCPRByDeposit[depositId][cprId] += amountSJC;
            emit CPRRepaidByDelivery(
                depositId,
                cprId,
                cpr.creditor,
                amountSJC,
                cpr.outstandingBalanceSJC,
                overdue,
                block.timestamp
            );

            if (cpr.outstandingBalanceSJC == 0) {
                _changeCPRStatus(cprId, CPRStatus.PaidOff);
                _closeCPRInQueue(cprId, cpr.producer, cpr.creditor);
            }
        }

        return remaining;
    }

    /// @notice Returns how much each CPR received from a deposit and whether repayment was late.
    function cprRepaymentsForDeposit(uint256 depositId) external view returns (CPRRepayment[] memory) {
        return _repaymentsCPRByDeposit[depositId];
    }

    function _digestAuthorizationCPR(
        address producer,
        address creditor,
        uint256 initialAmountSJC,
        uint256 deliveryUnitId,
        bytes32 cropYear,
        uint256 priority,
        uint256 dueDate,
        bytes32 documentHash,
        uint256 nonce,
        uint256 deadline
    ) private view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    CPR_AUTHORIZATION_TYPEHASH,
                    producer,
                    creditor,
                    initialAmountSJC,
                    deliveryUnitId,
                    cropYear,
                    priority,
                    dueDate,
                    documentHash,
                    nonce,
                    deadline
                )
            )
        );
    }

    function _digestUnitAlternative(uint256 depositId, uint256 cprId, uint256 nonce, uint256 deadline)
        private
        view
        returns (bytes32)
    {
        Deposit storage deposit = deposits[depositId];
        CPRDebt storage cpr = cprDebts[cprId];
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    ALTERNATIVE_CPR_UNIT_AUTHORIZATION_TYPEHASH,
                    depositId,
                    cprId,
                    cpr.producer,
                    cpr.creditor,
                    deposit.unitId,
                    deposit.cropYear,
                    nonce,
                    deadline
                )
            )
        );
    }

    function _digestAssignmentCPR(
        uint256 cprId,
        address currentCreditor,
        address newCreditor,
        bytes32 referenceHash,
        bytes32 documentHash,
        uint256 nonce,
        uint256 deadline
    ) private view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    CPR_ASSIGNMENT_AUTHORIZATION_TYPEHASH,
                    cprId,
                    currentCreditor,
                    newCreditor,
                    referenceHash,
                    documentHash,
                    nonce,
                    deadline
                )
            )
        );
    }

    function _digestClosureCPR(
        uint256 cprId,
        address producer,
        address creditor,
        bool cancellation,
        bytes32 documentHash,
        uint256 nonce,
        uint256 deadline
    ) private view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    CPR_CLOSURE_AUTHORIZATION_TYPEHASH,
                    cprId,
                    producer,
                    creditor,
                    cancellation,
                    documentHash,
                    nonce,
                    deadline
                )
            )
        );
    }

    function _validateClosureAcceptances(
        uint256 cprId,
        bool cancellation,
        bytes32 documentHash,
        uint256 deadline,
        bytes calldata signatureProducer,
        bytes calldata signatureCreditor
    ) private {
        require(documentHash != bytes32(0), "Invalid document hash");
        require(block.timestamp <= deadline, "Authorization has expired");
        CPRDebt storage cpr = cprDebts[cprId];
        bytes32 digest = _digestClosureCPR(
            cprId,
            cpr.producer,
            cpr.creditor,
            cancellation,
            documentHash,
            noncesClosureCPR[cprId],
            deadline
        );
        require(
            SignatureChecker.isValidSignatureNow(cpr.producer, digest, signatureProducer),
            "Invalid producer acceptance"
        );
        require(
            SignatureChecker.isValidSignatureNow(cpr.creditor, digest, signatureCreditor),
            "Invalid creditor acceptance"
        );
        noncesClosureCPR[cprId] += 1;
    }

    function _dataReordering(address producer, uint256 cprId, uint256 newPosition)
        private
        view
        returns (uint256 indexCurrent, uint256 indexNew, uint256 startIndex, uint256 endIndex, bytes32 queueHash)
    {
        uint256[] storage queue = _cprsActiveOfTheProducer[producer];
        require(queue.length > 1, "Not enough CPRs in the queue");
        require(newPosition > 0 && newPosition <= queue.length, "Invalid position");
        require(cprDebts[cprId].producer == producer && _cprCanReceivePayment(cprDebts[cprId]), "CPR is not in the repayment queue");

        bool found;
        for (uint256 i = 0; i < queue.length; i++) {
            if (queue[i] == cprId) {
                indexCurrent = i;
                found = true;
                break;
            }
        }
        require(found, "CPR is not in the repayment queue");
        indexNew = newPosition - 1;
        startIndex = indexCurrent < indexNew ? indexCurrent : indexNew;
        endIndex = indexCurrent > indexNew ? indexCurrent : indexNew;
        queueHash = _cprQueueHash(producer);
    }

    function _cprQueueHash(address producer) private view returns (bytes32) {
        uint256[] storage queue = _cprsActiveOfTheProducer[producer];
        uint256[] memory ids = queue;
        uint256[] memory priorities = new uint256[](queue.length);
        address[] memory creditors = new address[](queue.length);
        for (uint256 i = 0; i < queue.length; i++) {
            CPRDebt storage cpr = cprDebts[queue[i]];
            priorities[i] = cpr.priority;
            creditors[i] = cpr.creditor;
        }
        return keccak256(abi.encode(ids, priorities, creditors));
    }

    function _affectedCreditors(address producer, uint256 startIndex, uint256 endIndex)
        private
        view
        returns (address[] memory creditors)
    {
        uint256[] storage queue = _cprsActiveOfTheProducer[producer];
        address[] memory temporaryAddresses = new address[](endIndex - startIndex + 1);
        uint256 amount;
        for (uint256 i = startIndex; i <= endIndex; i++) {
            address creditor = cprDebts[queue[i]].creditor;
            bool visto;
            for (uint256 j = 0; j < amount; j++) {
                if (temporaryAddresses[j] == creditor) {
                    visto = true;
                    break;
                }
            }
            if (!visto) temporaryAddresses[amount++] = creditor;
        }
        creditors = new address[](amount);
        for (uint256 i = 0; i < amount; i++) creditors[i] = temporaryAddresses[i];
    }

    function _insertCPRIntoQueue(address producer, uint256 cprId) private {
        uint256[] storage queue = _cprsActiveOfTheProducer[producer];
        uint256 priority = cprDebts[cprId].priority;
        queue.push(cprId);
        uint256 i = queue.length - 1;
        while (i > 0 && cprDebts[queue[i - 1]].priority > priority) {
            queue[i] = queue[i - 1];
            i--;
        }
        queue[i] = cprId;
    }

    function _requireActiveUnit(uint256 unitId) private view {
        require(unitRegistry != address(0), "Unit registry is not configured");
        (,,,,, bool active) = IUnitRegistry(unitRegistry).units(unitId);
        require(active, "Delivery unit is inactive");
    }

    function _cprCanReceivePayment(CPRDebt storage cpr) private view returns (bool) {
        return cpr.outstandingBalanceSJC > 0
            && (cpr.status == CPRStatus.Open || cpr.status == CPRStatus.OverdueWithBalance);
    }

    function _updateCPRStatus(uint256 cprId) private {
        CPRDebt storage cpr = cprDebts[cprId];
        if (cpr.status == CPRStatus.Open && cpr.outstandingBalanceSJC > 0 && block.timestamp >= cpr.dueDate) {
            _changeCPRStatus(cprId, CPRStatus.OverdueWithBalance);
        }
    }

    function _changeCPRStatus(uint256 cprId, CPRStatus newStatus) private {
        CPRDebt storage cpr = cprDebts[cprId];
        CPRStatus previous = cpr.status;
        if (previous != newStatus) {
            cpr.status = newStatus;
            emit CPRStatusUpdated(cprId, previous, newStatus);
        }
    }

    function _closeCPRInQueue(uint256 cprId, address producer, address creditor) private {
        uint256[] storage queue = _cprsActiveOfTheProducer[producer];
        for (uint256 i = 0; i < queue.length; i++) {
            if (queue[i] == cprId) {
                for (uint256 j = i; j + 1 < queue.length; j++) queue[j] = queue[j + 1];
                queue.pop();
                activeCPRsByParticipant[producer] -= 1;
                activeCPRsByParticipant[creditor] -= 1;
                return;
            }
        }
        revert("CPR was not found in the queue");
    }
}
