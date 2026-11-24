// SPDX-License-Identifier: MIT
// Compatible with OpenZeppelin Contracts ^5.7.0
pragma solidity ^0.8.27;

import {TokenModule} from "./modules/TokenModule.sol";
import {IUnitRegistry} from "./interfaces/IUnitRegistry.sol";
import {ISojaCoinModule} from "./interfaces/ISojaCoinModule.sol";

/// @title SojaCoin
/// @notice SJC token and single entry point for the operational modules.
/// @dev Business rules are executed by fixed modules through delegatecall.
/// The modules share TokenModule's storage layout, so any state change must
/// preserve the same base layout in every module.
contract SojaCoin is TokenModule {
    address public immutable administrationModule;
    address public immutable depositsModule;
    address public immutable cprModule;
    address public immutable servicesModule;
    address public immutable locksModule;
    address public immutable redemptionsModule;

    /// @notice Creates the token and connects its immutable operational modules.
    /// @param initialOwner The company's administrative wallet; it may be a Safe.
    /// @param _administrationModule Implementation of administrative functions.
    /// @param _depositsModule Implementation of deposit receipts and token issuance.
    /// @param _cprModule Implementation of CPR debts and delivery allocations.
    /// @param _servicesModule Implementation of service charges.
    /// @param _locksModule Implementation of guarantees backed by issued SJC.
    /// @param _redemptionsModule Implementation of soybean withdrawal requests.
    constructor(
        address initialOwner,
        address _administrationModule,
        address _depositsModule,
        address _cprModule,
        address _servicesModule,
        address _locksModule,
        address _redemptionsModule
    ) TokenModule(initialOwner) {
        _validateModule(_administrationModule, keccak256("SojaCoin.Administration.v2"));
        _validateModule(_depositsModule, keccak256("SojaCoin.Deposits.v2"));
        _validateModule(_cprModule, keccak256("SojaCoin.CPR.v2"));
        _validateModule(_servicesModule, keccak256("SojaCoin.Services.v2"));
        _validateModule(_locksModule, keccak256("SojaCoin.Locks.v2"));
        _validateModule(_redemptionsModule, keccak256("SojaCoin.Redemptions.v2"));

        administrationModule = _administrationModule;
        depositsModule = _depositsModule;
        cprModule = _cprModule;
        servicesModule = _servicesModule;
        locksModule = _locksModule;
        redemptionsModule = _redemptionsModule;
    }

    // Administration

    /// @notice Pauses transfers and operations that honor the global pause.
    function pause() external {
        _delegate(administrationModule);
    }

    /// @notice Reactivates the contract after backing and audit checks.
    function unpause() external {
        _delegate(administrationModule);
    }

    /// @notice Connects the registry of units, permissions, and local inventories.
    function configureUnitRegistry(address newUnitRegistry) external {
        _delegate(administrationModule);
    }

    /// @notice Connects the helper contract that groups deposits into batches.
    function configureIssuanceBatches(address newModule) external {
        _delegate(administrationModule);
    }

    /// @notice Updates the backing after a physical unit has been audited.
    function syncInventoryFromUnits(
        uint256 physicalInventoryMg,
        bytes32 reportHash,
        address auditorOfTheUnit,
        uint256 lastAuditAt
    ) external {
        _delegate(administrationModule);
    }

    /// @notice Updates audit validity when the set of active units changes.
    function updateAuditValidityFromUnits(
        uint256 lastAuditAt
    ) external {
        _delegate(administrationModule);
    }

    /// @notice Updates the issuance cap after an in-transit inventory change.
    function updateIssuanceCapAfterTransfer() external {
        _delegate(administrationModule);
    }

    /// @notice Records a physical loss identified during a transfer between units.
    function recordTransferDiscrepancy(
        uint256 discrepancyMg,
        bytes32 receiptProofHash,
        address responsibleParty
    ) external {
        _delegate(administrationModule);
    }

    /// @notice Proposes a producer or creditor, pending acceptance by their wallet.
    function proposeParticipantRegistration(address walletAddress, bytes32 participationTermsHash) external {
        _delegate(administrationModule);
    }

    /// @notice Calculates the EIP-712 acceptance digest for the proposed wallet.
    function participationTermsAcceptanceHash(address participant, uint256 deadline) external view returns (bytes32) {
        require(participantRegistrationPending[participant], "Registration is not pending");
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    PARTICIPATION_TERMS_ACCEPTANCE_TYPEHASH,
                    participant,
                    participationTermsHashByParticipant[participant],
                    participationTermsNonces[participant],
                    deadline
                )
            )
        );
    }

    /// @notice Activates the participant after acceptance signed by their own wallet.
    function acceptParticipationTerms(address participant, uint256 deadline, bytes calldata signature) external {
        _delegate(administrationModule);
    }

    /// @notice Cancels a registration that has not yet been accepted by the proposed wallet.
    function cancelPendingParticipantRegistration(address participant) external {
        _delegate(administrationModule);
    }

    /// @notice Revokes the producer's authorization to register new deposits.
    function revokeDepositAuthorization() external {
        _delegate(administrationModule);
    }

    /// @notice Re-enables future deposits with a new authorization agreement.
    function authorizeFutureDeposits(bytes32 newTermsHash) external {
        _delegate(administrationModule);
    }

    /// @notice Removes a participant with no balance or pending operations.
    function removeParticipant(address walletAddress) external {
        _delegate(administrationModule);
    }

    // Deposits and CPRs

    /// @notice Opens a load receipt identified by the producer, unit, and crop year.
    /// @return depositId Sequential identifier assigned to the receipt.
    function openLoadReceipt(
        address producer,
        uint256 unitId,
        bytes32 cropYear
    ) external returns (uint256 depositId) {
        depositId = abi.decode(_delegate(depositsModule), (uint256));
    }

    /// @notice Links the tax invoice hash to the received load.
    function linkTaxInvoice(uint256 depositId, bytes32 taxInvoiceKeyHash) external {
        _delegate(depositsModule);
    }

    /// @notice Cancels a load before physical receipt is confirmed.
    function cancelLoadReceipt(uint256 depositId) external {
        _delegate(depositsModule);
    }

    /// @notice Confirms the weighing ticket and records the net received weight.
    function confirmLoadReceipt(uint256 depositId, WeighingTicket calldata weighingTicket) external {
        _delegate(depositsModule);
    }

    /// @notice Issues SJC for a deposit approved within a batch.
    function issueTokens(uint256 depositId) external {
        _delegate(depositsModule);
    }

    /// @dev Internal call from the deposits module; external wallets cannot initiate it.
    function distributeIssuanceByCPR(uint256 depositId, uint256 amountSJC) external returns (uint256 unencumberedAmountSJC) {
        require(msg.sender == address(this), "Internal operation only");
        return abi.decode(_delegate(cprModule), (uint256));
    }

    /// @notice Records the physical return of part of a deposit before issuance.
    function recordDepositReturn(uint256 depositId, uint256 returnedAmountKg, bytes32 documentHash)
        external
    {
        _delegate(depositsModule);
    }

    /// @notice Validates a deposit and returns the data used by the batches module.
    /// @dev This read stays in the main contract because a view call cannot use delegatecall.
    function validateDepositForBatch(uint256 depositId, address responsibleParty)
        external
        view
        returns (
            address producer,
            bytes32 taxInvoiceKeyHash,
            uint256 unitId,
            uint256 netWeightKg,
            bytes32 weighingTicketHash,
            bytes32 cropYear
        )
    {
        require(msg.sender == issuanceBatchModule && issuanceBatchModule != address(0), "Issuance batch module only");
        require(unitRegistry != address(0), "Unit registry is not configured");
        _requireCurrentAudit();

        Deposit storage deposit = deposits[depositId];
        require(deposit.depositor != address(0), "Deposit does not exist");
        require(deposit.receiptConfirmed, "Receipt has not been confirmed");
        require(!deposit.canceled, "Deposit is canceled");
        require(!deposit.closedWithoutIssuance, "Deposit is closed");
        require(!deposit.tokensIssued, "Tokens have already been issued");
        require(deposit.taxInvoiceKeyHash != bytes32(0), "Tax invoice is not linked");
        require(deposit.returnedWeightKg == 0, "Deposit has a return recorded");
        require(deposit.netWeightKg > 0, "Deposit has no soybeans to issue");

        IUnitRegistry(unitRegistry).validateDepositIssuance(depositId, responsibleParty);
        return (
            deposit.depositor,
            deposit.taxInvoiceKeyHash,
            deposit.unitId,
            deposit.netWeightKg,
            deposit.weighingTicketHash,
            deposit.cropYear
        );
    }

    /// @notice Returns the producer's active CPRs in priority order.
    function activeCPRsForProducer(address producer) external view returns (uint256[] memory) {
        return _cprsActiveOfTheProducer[producer];
    }

    /// @notice Returns all current fields of a CPR in a single query.
    function cprDetails(uint256 cprId) external view returns (CPRDebt memory) {
        require(cprDebts[cprId].status != CPRStatus.Nonexistent, "CPR does not exist");
        return cprDebts[cprId];
    }

    /// @notice Calculates the EIP-712 digest the producer signs to register a CPR debt.
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
        bytes32 structHash = keccak256(
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
                cprNonces[producer],
                deadline
            )
        );
        return _hashTypedDataV4(structHash);
    }

    /// @notice Registers a CPR as a debt in bags after the producer accepts it.
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
        return abi.decode(_delegate(cprModule), (uint256));
    }

    /// @notice Returns the producer's acceptance signature stored in the CPR record.
    function cprAcceptanceSignature(uint256 cprId) external view returns (bytes memory) {
        require(cprDebts[cprId].status != CPRStatus.Nonexistent, "CPR does not exist");
        return _signaturesAcceptanceCPR[cprId];
    }

    /// @notice Calculates the CPR state from the current time, even before it is stored.
    function currentCPRStatus(uint256 cprId) external view returns (CPRStatus) {
        CPRDebt storage cpr = cprDebts[cprId];
        require(cpr.status != CPRStatus.Nonexistent, "CPR does not exist");
        if (cpr.status == CPRStatus.Open && cpr.outstandingBalanceSJC > 0 && block.timestamp >= cpr.dueDate) {
            return CPRStatus.OverdueWithBalance;
        }
        return cpr.status;
    }

    /// @notice Stores the overdue state when a CPR has a balance after its due date.
    function updateCPRStatus(uint256 cprId) external {
        _delegate(cprModule);
    }

    /// @notice Calculates producer and creditor acceptance to use another unit for this load.
    function alternativeUnitCPRAuthorizationHash(uint256 depositId, uint256 cprId, uint256 deadline)
        external
        view
        returns (bytes32)
    {
        Deposit storage deposit = deposits[depositId];
        CPRDebt storage cpr = cprDebts[cprId];
        require(cpr.status != CPRStatus.Nonexistent, "CPR does not exist");
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
                    noncesUnitAlternative[depositId][cprId],
                    deadline
                )
            )
        );
    }

    /// @notice Records signed authorization to apply this load to a CPR at another unit.
    function authorizeAlternativeCPRUnit(
        uint256 depositId,
        uint256 cprId,
        uint256 deadline,
        bytes calldata signatureProducer,
        bytes calldata signatureCreditor
    ) external {
        _delegate(cprModule);
    }

    /// @notice Calculates the current creditor's authorization to assign a CPR.
    function cprAssignmentAuthorizationHash(
        uint256 cprId,
        address newCreditor,
        string calldata registryReference,
        bytes32 documentHash,
        uint256 deadline
    ) external view returns (bytes32) {
        CPRDebt storage cpr = cprDebts[cprId];
        require(cpr.status != CPRStatus.Nonexistent, "CPR does not exist");
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    CPR_ASSIGNMENT_AUTHORIZATION_TYPEHASH,
                    cprId,
                    cpr.creditor,
                    newCreditor,
                    keccak256(bytes(registryReference)),
                    documentHash,
                    noncesAssignmentCPR[cprId],
                    deadline
                )
            )
        );
    }

    /// @notice Changes the creditor and stores the submitted transfer evidence.
    function assignCPR(
        uint256 cprId,
        address newCreditor,
        string calldata registryReference,
        bytes32 documentHash,
        uint256 deadline,
        bytes calldata signatureCreditorCurrent
    ) external {
        _delegate(cprModule);
    }

    /// @notice Returns the registration reference stored for the latest assignment.
    function cprAssignmentRegistryReference(uint256 cprId) external view returns (string memory) {
        return _referencesAssignmentCPR[cprId];
    }

    /// @notice Calculates producer and creditor acceptance to move a CPR in the queue.
    function cprReorderingAuthorizationHash(
        address producer,
        uint256 cprId,
        uint256 newPosition,
        bytes32 documentHash,
        uint256 deadline
    ) external view returns (bytes32) {
        uint256[] storage queue = _cprsActiveOfTheProducer[producer];
        require(newPosition > 0 && newPosition <= queue.length, "Invalid position");
        bool found;
        for (uint256 i = 0; i < queue.length; i++) {
            if (queue[i] == cprId) found = true;
        }
        require(found, "CPR is not in the repayment queue");
        uint256[] memory copiaQueue = queue;
        uint256[] memory priorities = new uint256[](queue.length);
        address[] memory creditors = new address[](queue.length);
        for (uint256 i = 0; i < queue.length; i++) {
            CPRDebt storage cpr = cprDebts[queue[i]];
            priorities[i] = cpr.priority;
            creditors[i] = cpr.creditor;
        }
        bytes32 queueHash = keccak256(abi.encode(copiaQueue, priorities, creditors));
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

    /// @notice Changes the priority with acceptance from the producer and affected creditors.
    function reorderCPR(
        address producer,
        uint256 cprId,
        uint256 newPosition,
        bytes32 documentHash,
        uint256 deadline,
        bytes calldata signatureProducer,
        bytes[] calldata signaturesCreditors
    ) external {
        _delegate(cprModule);
    }

    /// @notice Returns the parties that accepted a priority change.
    function cprReorderingAcceptingParties(uint256 reorderingId) external view returns (address[] memory) {
        return _cprReorderingAcceptingParties[reorderingId];
    }

    /// @notice Calculates the message signed by the producer and creditor to close a CPR.
    function cprClosureAuthorizationHash(
        uint256 cprId,
        bool cancellation,
        bytes32 documentHash,
        uint256 deadline
    ) external view returns (bytes32) {
        CPRDebt storage cpr = cprDebts[cprId];
        require(cpr.status != CPRStatus.Nonexistent, "CPR does not exist");
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    CPR_CLOSURE_AUTHORIZATION_TYPEHASH,
                    cprId,
                    cpr.producer,
                    cpr.creditor,
                    cancellation,
                    documentHash,
                    noncesClosureCPR[cprId],
                    deadline
                )
            )
        );
    }

    /// @notice Cancels by agreement a CPR that has not received any repayments.
    function cancelCPR(
        uint256 cprId,
        bytes32 documentHash,
        uint256 deadline,
        bytes calldata signatureProducer,
        bytes calldata signatureCreditor
    ) external {
        _delegate(cprModule);
    }

    /// @notice Closes a CPR's outstanding balance by agreement without creating tokens.
    function closeCPRByAgreement(
        uint256 cprId,
        bytes32 documentHash,
        uint256 deadline,
        bytes calldata signatureProducer,
        bytes calldata signatureCreditor
    ) external {
        _delegate(cprModule);
    }

    /// @notice Returns CPR repayments recorded for a deposit.
    function cprRepaymentsForDeposit(uint256 depositId) external view returns (CPRRepayment[] memory) {
        return _repaymentsCPRByDeposit[depositId];
    }

    // Services and fees

    /// @notice Returns the IDs of service charges linked to a deposit.
    function servicesAndFeesForDeposit(uint256 depositId) external view returns (uint256[] memory) {
        return _servicesAndFeesOfTheDeposit[depositId];
    }

    /// @notice Records a service charge linked to an issued deposit.
    function recordServiceAndFee(
        uint256 depositId,
        address recipient,
        ServiceType serviceType,
        FeeUnit unit,
        uint256 amount,
        bytes32 hashCharge
    ) external returns (uint256 serviceAndFeeId) {
        return abi.decode(_delegate(servicesModule), (uint256));
    }

    /// @notice Pays an open charge in SJC.
    function payServiceInSJC(uint256 serviceAndFeeId) external {
        _delegate(servicesModule);
    }

    /// @notice Records payment in Brazilian reais and the hash of its proof.
    function confirmServicePaymentInBRL(uint256 serviceAndFeeId, bytes32 paymentProofHash) external {
        _delegate(servicesModule);
    }

    /// @notice Cancels a service charge that is still open.
    function cancelServiceAndFee(uint256 serviceAndFeeId) external {
        _delegate(servicesModule);
    }

    // Locks on already-issued SJC

    /// @notice Locks SJC already held by the holder to secure an agreement.
    function lockSJC(uint256 amountSJC, address creditor, bytes32 hashAgreement, uint256 dueDate)
        external
        returns (uint256 lockId)
    {
        return abi.decode(_delegate(locksModule), (uint256));
    }

    /// @notice Releases locked SJC back to the holder.
    function releaseTokenLock(uint256 lockId) external {
        _delegate(locksModule);
    }

    /// @notice Executes an expired lock and transfers SJC to the creditor.
    function executeTokenLock(uint256 lockId) external {
        _delegate(locksModule);
    }

    // Redemptions and withdrawals

    /// @notice Requests a soybean withdrawal from a unit and holds the SJC in custody.
    function requestRedemption(uint256 amountSJC, uint256 unitId) external returns (uint256 redemptionId) {
        return abi.decode(_delegate(redemptionsModule), (uint256));
    }

    /// @notice Cancels a redemption that has not yet been authorized.
    function cancelRequest(uint256 redemptionId) external {
        _delegate(redemptionsModule);
    }

    /// @notice Authorizes the withdrawal and reserves the matching weight at the selected unit.
    function authorizeWithdrawal(uint256 redemptionId) external {
        _delegate(redemptionsModule);
    }

    /// @notice Completes the withdrawal using the weight actually delivered.
    function completeWithdrawal(uint256 redemptionId, uint256 deliveredWeightKg, bytes32 returnTaxInvoiceHash) external {
        _delegate(redemptionsModule);
    }

    /// @notice Cancels an authorized withdrawal when no physical goods left the unit.
    function cancelAuthorizedWithdrawal(uint256 redemptionId) external {
        _delegate(redemptionsModule);
    }

    /// @notice Resolves an authorized redemption that was not completed within the deadline.
    function resolveAsArbitrator(uint256 redemptionId, bool soybeansWasDelivered, uint256 deliveredWeightKg, bytes32 returnTaxInvoiceHash)
        external
    {
        _delegate(redemptionsModule);
    }

    /// @dev Executes the selected module while preserving the caller and SojaCoin storage.
    function _delegate(address module) private returns (bytes memory result) {
        (bool success, bytes memory returnData) = module.delegatecall(msg.data);
        if (!success) {
            assembly ("memory-safe") {
                revert(add(returnData, 0x20), mload(returnData))
            }
        }
        return returnData;
    }

    /// @dev Rejects empty modules and addresses without bytecode.
    function _validateModule(address module, bytes32 identifierExpected) private view {
        require(module != address(0) && module.code.length > 0, "Invalid module address");
        require(ISojaCoinModule(module).moduleIdentifier() == identifierExpected, "Invalid module type");
    }
}
