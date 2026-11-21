// SPDX-License-Identifier: MIT
// Compatible with OpenZeppelin Contracts ^5.7.0
pragma solidity ^0.8.27;

// Shared module base. Stores data and internal routines used by the contracts;
// it does not expose business operations on its own.
abstract contract SojaCoinStorage {
    // 1 SJC represents one 60 kg bag. The token uses six decimal places.
    // 1 SJC equals 60 kg. With six decimals, one minimum unit equals 60 mg.
    // Amounts passed to ERC-20 functions use these internal units.
    uint256 public constant UNITS_PER_SACK = 1_000_000;
    uint256 internal constant MG_PER_KG = 1_000_000;
    uint256 internal constant MG_PER_SJC_UNIT = 60;

    // Deposit fields.
    struct Deposit {
        address depositor;
        bytes32 taxInvoiceKeyHash;
        uint256 unitId;
        // Crop year provided at receipt; only CPRs from the same crop year can be repaid.
        bytes32 cropYear;
        bytes32 participationTermsHash;
        // SJC amounts in internal units (1 SJC = 1,000,000 units).
        uint256 amountSJC;
        bool receiptConfirmed;
        bool tokensIssued;
        bool canceled;
        bool closedWithoutIssuance;
        uint256 depositDate;
        uint256 netWeightKg;
        uint256 returnedWeightKg;
        bytes32 weighingTicketHash;
        uint256 documentTimestamp;
        // Timestamp when the warehouse confirmed the load on-chain.
        uint256 receivedAt;
    }

    struct WeighingTicket {
        // Weighing.
        uint256 grossWeightKg;
        uint256 tareKg;

        // Classification values in hundredths of a percent.
        uint16 moistureBps;
        uint16 impuritiesBps;
        uint16 damagedGrainBps;

        // Deductions actually applied.
        uint256 moistureDeductionKg;
        uint256 impuritiesDeductionKg;
        uint256 damagedGrainDeductionKg;
        uint256 otherDeductionsKg;

        // Identification fields.
        bytes32 weighingTicketHash;
        bytes32 reportHash;
        // Receipt timestamp provided by the warehouse, in Unix seconds.
        uint256 timestamp;
    }

    // Service types that may be charged separately from the weighing ticket.
    // "Other" uses the linked document to describe the service.
    enum ServiceType {
        Reception,
        Cleaning,
        Drying,
        Storage,
        Dispatch,
        Transshipment,
        Weighing,
        DocumentIssuance,
        Other
    }

    // A charge may be informational in Brazilian reais or payable in SJC.
    // Amounts in reais use cents to avoid decimals in the contract.
    enum FeeUnit {
        BRLCents,
        SJC
    }

    enum ServiceAndFeeStatus {
        Nonexistent,
        Open,
        PaidInSJC,
        PaidInBRL,
        Canceled
    }

    // A service line is linked to a deposit but does not change the kilograms
    // accepted on the weighing ticket or the SJC issued for it.
    struct ServiceAndFee {
        uint256 depositId;
        address producer;
        address recipient;
        ServiceType serviceType;
        FeeUnit unit;
        uint256 amount;
        bytes32 hashCharge;
        bytes32 paymentProofHash;
        ServiceAndFeeStatus status;
    }

    // Guarantee backed by SJC already held by the holder.
    struct TokenLock {
        address holder;
        address creditor;
        uint256 amountSJC;
        bytes32 hashAgreement;
        uint256 dueDate;
        bool active;
    }

    // States of the bag-denominated debt recorded by the CPR.
    enum CPRStatus {
        Nonexistent,
        Open,
        OverdueWithBalance,
        PaidOff,
        ClosedByAgreement,
        Canceled
    }

    // The CPR represents a debt in bags. Full documents remain off-chain.
    struct CPRDebt {
        address producer;
        address creditor;
        uint256 initialAmountSJC;
        uint256 outstandingBalanceSJC;
        uint256 amountPaidSJC;
        uint256 closedBalanceSJC;
        uint256 deliveryUnitId;
        bytes32 cropYear;
        uint256 priority;
        uint256 dueDate;
        bytes32 documentHash;
        bytes32 closureDocumentHash;
        bool producerAcceptance;
        uint256 acceptedAt;
        CPRStatus status;
    }

    struct CPRRepayment {
        uint256 cprId;
        uint256 amountSJC;
        bool overdue;
    }

    struct CPRReordering {
        address producer;
        address requestedBy;
        uint256 cprId;
        uint256 previousPriority;
        uint256 newPriority;
        bytes32 documentHash;
        uint256 timestamp;
    }

    // Possible redemption states.
    // Nonexistent comes first so unregistered IDs do not appear valid.
    enum RedemptionStatus {
        Nonexistent,
        Requested,
        Authorized,
        Completed,
        Canceled
    }

    // Data stored for each withdrawal request.
    struct Redemption {
        address holder;
        address authorizedBy;
        uint256 amountSJC;
        uint256 unitId;
        RedemptionStatus status;
        uint256 authorizedIn;
    }

    // Stores the latest pause time so a later audit is required before unpausing.
    uint256 public lastPausedAt;

    // Prevents an address from being registered twice.
    mapping(address => bool) public participantRegistered;

    // Hash of the terms authorizing tokenization of accepted deposits.
    mapping(address => bytes32) public participationTermsHashByParticipant;

    // Allows new deposits to be registered on behalf of the participant under the terms.
    mapping(address => bool) public depositAuthorizationActive;

    // A registration proposed by an administrator becomes active only after acceptance
    // signed by the specified wallet.
    mapping(address => bool) public participantRegistrationPending;
    mapping(address => uint256) public participationTermsNonces;
    bytes32 public constant PARTICIPATION_TERMS_ACCEPTANCE_TYPEHASH = keccak256(
        "ParticipationTermsAcceptance(address participant,bytes32 termsHash,uint256 nonce,uint256 deadline)"
    );

    // External registry that stores units, permissions, and per-unit inventory.
    address public unitRegistry;

    // Helper contract that groups loads and stores producer approvals.
    address public issuanceBatchModule;

    // Oldest timestamp among the latest audits of active units.
    uint256 public lastAuditAt;

    // Issuance cap calculated from unit inventory, excluding loads in transit.
    uint256 public issuanceCapSJC;

    // Total accounted inventory in milligrams. Includes loads in transit;
    // audits replace the balance, receipts add to it, and withdrawals/losses subtract from it.
    uint256 public registeredInventoryMg;

    // Remainder from converting kilograms to fractional SJC units, per producer.
    // The value is between 0 and 59 mg and carries over to the next issued deposit.
    mapping(address => uint8) public conversionRemainderMgByProducer;

    // Maximum time between audits.
    uint256 public constant AUDIT_PERIOD = 60 days;

    // Limits the active queue to keep issuance executable and predictable.
    uint256 public constant MAX_ACTIVE_CPRS_PER_PRODUCER = 16;

    // Stores the next lock ID.
    uint256 public nextLockId;

    // Stores the next ID for a service charge or fee.
    uint256 public nextServiceAndFeeId;

    // True only during a transfer performed by lock functions.
    bool internal _movementOfLock;

    // Allows only redemption settlements to bypass the global pause.
    bool internal _settlementRedemption;

    // A weighing ticket identifies a physical receipt and can be used only once.
    mapping(bytes32 => bool) public weighingTicketUsed;

    // A tax invoice may be linked to multiple weighing tickets; this count helps query them.
    mapping(bytes32 => uint256) public depositCountByTaxInvoice;

    // Maps each ID to the data for a guarantee backed by issued SJC.
    mapping(uint256 => TokenLock) public locks;

    // Services and fees do not affect a deposit's classification or issuance.
    mapping(uint256 => ServiceAndFee) public servicesAndFees;
    mapping(uint256 => uint256[]) internal _servicesAndFeesOfTheDeposit;

    // Deposits mapped by ID.
    mapping(uint256 => Deposit) public deposits;

    // Counts each participant's deposits that have not been issued or canceled.
    mapping(address => uint256) public pendingDeposits;

    // Prevents removal of a participant with an ongoing withdrawal or guarantee.
    mapping(address => uint256) public pendingRedemptionsByHolder;
    mapping(address => uint256) public activeLocksByParticipant;
    mapping(address => uint256) public activeCPRsByParticipant;

    // Nonce used to prevent reuse of the same CPR authorization.
    mapping(address => uint256) public cprNonces;
    bytes32 public constant CPR_AUTHORIZATION_TYPEHASH = keccak256(
        "CPRDebtAuthorization(address producer,address creditor,uint256 initialAmountSJC,uint256 deliveryUnitId,bytes32 cropYear,uint256 priority,uint256 dueDate,bytes32 documentHash,uint256 nonce,uint256 deadline)"
    );

    bytes32 public constant CPR_ASSIGNMENT_AUTHORIZATION_TYPEHASH = keccak256(
        "CPRAssignment(uint256 cprId,address currentCreditor,address newCreditor,bytes32 referenceHash,bytes32 documentHash,uint256 nonce,uint256 deadline)"
    );
    bytes32 public constant ALTERNATIVE_CPR_UNIT_AUTHORIZATION_TYPEHASH = keccak256(
        "AlternativeCPRUnit(uint256 depositId,uint256 cprId,address producer,address creditor,uint256 actualUnitId,bytes32 cropYear,uint256 nonce,uint256 deadline)"
    );
    bytes32 public constant CPR_REORDERING_AUTHORIZATION_TYPEHASH = keccak256(
        "CPRReordering(address producer,uint256 cprId,uint256 newPosition,bytes32 documentHash,bytes32 currentQueueHash,uint256 nonce,uint256 deadline)"
    );
    bytes32 public constant CPR_CLOSURE_AUTHORIZATION_TYPEHASH = keccak256(
        "CPRClosure(uint256 cprId,address producer,address creditor,bool cancellation,bytes32 documentHash,uint256 nonce,uint256 deadline)"
    );

    mapping(uint256 => CPRDebt) public cprDebts;
    mapping(address => uint256[]) internal _cprsActiveOfTheProducer;
    mapping(address => uint256) public lastCPROrderByProducer;
    uint256 public nextCPRId;
    mapping(uint256 => bytes) internal _signaturesAcceptanceCPR;
    mapping(uint256 => CPRRepayment[]) internal _repaymentsCPRByDeposit;
    mapping(uint256 => mapping(uint256 => uint256)) public repaymentCPRByDeposit;
    mapping(uint256 => mapping(uint256 => bool)) public alternativeUnitAuthorized;
    mapping(uint256 => mapping(uint256 => address)) public creditorWhoAuthorizedAlternativeUnit;
    mapping(uint256 => mapping(uint256 => uint256)) public noncesUnitAlternative;
    mapping(uint256 => uint256) public noncesAssignmentCPR;
    mapping(address => uint256) public noncesReorderingCPR;
    mapping(uint256 => uint256) public noncesClosureCPR;
    uint256 public nextCPRReorderingId;
    mapping(uint256 => CPRReordering) public reorderingsCPR;
    mapping(uint256 => address[]) internal _cprReorderingAcceptingParties;
    mapping(uint256 => string) internal _referencesAssignmentCPR;

    // Stores each redemption by ID.
    mapping(uint256 => Redemption) public redemptions;

    // Stores the next redemption ID.
    uint256 public nextRedemptionId;

    // Counts authorized SJC units that have not yet been completed or canceled.
    uint256 public sacksInWithdrawal;

    // Stores the next load receipt ID; kept at the end of storage to preserve
    // slots already used by modules sharing this layout.
    uint256 public nextDepositId;

    // Deadline for the owner to arbitrate an authorized redemption that was not resolved.
    uint256 public constant WITHDRAWAL_PERIOD = 7 days;

    // Event emitted when tokens are locked.
    event TokensLocked(
        uint256 indexed lockId, address indexed holder, address indexed creditor, uint256 amountSJC, bytes32 hashAgreement
    );

    event TokenLockReleased(uint256 indexed lockId);
    event TokenLockExecuted(uint256 indexed lockId);

    event ServiceAndFeeRecorded(
        uint256 indexed serviceAndFeeId,
        uint256 indexed depositId,
        address indexed producer,
        address recipient,
        ServiceType serviceType,
        FeeUnit unit,
        uint256 amount,
        bytes32 hashCharge,
        address recordedBy
    );

    event ServicePaidInSJC(
        uint256 indexed serviceAndFeeId, address indexed producer, address indexed recipient, uint256 amountSJC
    );

    event ServicePaidInBRL(
        uint256 indexed serviceAndFeeId, address indexed confirmedBy, bytes32 paymentProofHash
    );

    event ServiceAndFeeCanceled(uint256 indexed serviceAndFeeId, address indexed canceledBy);

    event DiscrepancyDetected(
        address indexed detectedBy,
        address indexed administrator,
        uint256 registeredInventoryMg,
        uint256 totalTokenUnitsSJC,
        uint256 timestamp
    );

    event ParticipantRegistered(
        address indexed walletAddress, bytes32 participationTermsHash, address indexed acceptedBy
    );

    event ParticipantRegistrationProposed(
        address indexed walletAddress, bytes32 participationTermsHash, address indexed proposedBy
    );

    event PendingParticipantRegistrationCanceled(address indexed walletAddress, address indexed canceledBy);

    event DepositAuthorizationChanged(
        address indexed producer, bool authorized, bytes32 participationTermsHash, address indexed changedBy
    );

    event ParticipantRemoved(address indexed walletAddress);

    /// @notice Records the opening of a receipt before weighing, a weighing ticket, or a tax invoice.
    event LoadReceiptOpened(
        uint256 indexed depositId,
        address indexed depositor,
        uint256 indexed unitId,
        uint256 timestamp,
        address openBy,
        bytes32 participationTermsHash
    );

    /// @notice Links the tax document to the already-open receipt.
    /// @dev The invoice may be linked before or after the weighing ticket is confirmed.
    event TaxInvoiceLinked(
        uint256 indexed depositId,
        uint256 indexed unitId,
        bytes32 taxInvoiceKeyHash,
        address indexed linkedBy,
        uint256 timestamp
    );

    event LoadReceiptCanceled(
        uint256 indexed depositId,
        address indexed depositor,
        uint256 indexed unitId,
        bytes32 taxInvoiceKeyHash,
        bytes32 weighingTicketHash,
        address canceledBy
    );

    event DepositReturnRecorded(
        uint256 indexed depositId,
        uint256 indexed unitId,
        uint256 returnedAmountKg,
        uint256 totalReturnedWeightKg,
        bytes32 documentHash,
        address indexed recordedBy,
        uint256 timestamp
    );

    event LoadReceiptConfirmed(
        uint256 indexed depositId,
        uint256 indexed unitId,
        address responsibleParty,
        address indexed depositor,
        uint256 grossWeightKg,
        uint256 tareKg,
        uint256 netWeightKg,
        bytes32 weighingTicketHash,
        bytes32 reportHash,
        uint256 documentTimestamp,
        uint256 timestamp
    );

    event ReceiptClassified(
        uint256 indexed depositId,
        uint16 moistureBps,
        uint16 impuritiesBps,
        uint16 damagedGrainBps,
        uint256 moistureDeductionKg,
        uint256 impuritiesDeductionKg,
        uint256 damagedGrainDeductionKg,
        uint256 otherDeductionsKg
    );

    event TokensIssued(
        uint256 indexed depositId,
        address indexed wallet,
        address indexed issuedBy,
        uint256 amount,
        uint8 conversionRemainderMg,
        uint256 timestamp
    );

    event CPRRegistered(
        uint256 indexed cprId,
        address indexed producer,
        address indexed creditor,
        uint256 initialAmountSJC,
        uint256 deliveryUnitId,
        bytes32 cropYear,
        uint256 priority,
        bytes32 documentHash,
        uint256 dueDate
    );

    event ProducerCPRAcceptanceRecorded(
        uint256 indexed cprId, address indexed producer, bytes32 hashSignature, uint256 timestamp
    );

    event CPRRepaidByDelivery(
        uint256 indexed depositId,
        uint256 indexed cprId,
        address indexed creditor,
        uint256 amountSJC,
        uint256 remainingBalanceSJC,
        bool overdue,
        uint256 timestamp
    );

    event CPRStatusUpdated(uint256 indexed cprId, CPRStatus previousStatus, CPRStatus newStatus);

    event CPRAssigned(
        uint256 indexed cprId,
        address indexed previousCreditor,
        address indexed newCreditor,
        address producer,
        address assignedBy,
        string registryReference,
        bytes32 documentHash,
        uint256 timestamp
    );

    event AlternativeCPRUnitAuthorized(
        uint256 indexed depositId,
        uint256 indexed cprId,
        address indexed producer,
        address creditor,
        uint256 actualUnitId,
        bytes32 cropYear
    );

    event CPRPriorityChanged(
        uint256 indexed reorderingId,
        uint256 indexed cprId,
        address indexed producer,
        address requestedBy,
        uint256 previousPriority,
        uint256 newPriority,
        bytes32 documentHash,
        address[] acceptingParties,
        uint256 timestamp
    );

    event CPRClosedByAgreement(
        uint256 indexed cprId,
        address indexed producer,
        address indexed creditor,
        uint256 closedBalanceSJC,
        bytes32 documentHash,
        uint256 timestamp
    );

    event CPRCanceled(
        uint256 indexed cprId,
        address indexed producer,
        address indexed creditor,
        uint256 canceledBalanceSJC,
        bytes32 documentHash,
        uint256 timestamp
    );

    event UnitRegistryConfigured(address indexed unitRegistry, address indexed configuredBy);

    event IssuanceBatchesConfigured(address indexed issuanceBatches, address indexed configuredBy);

    event InventoryAuditedByUnit(
        address indexed unitRegistry,
        address indexed auditor,
        uint256 physicalInventoryKg,
        bytes32 auditRoundHash,
        uint256 timestamp
    );

    event InventoryAdjustedByTransfer(
        address indexed unitRegistry,
        address indexed responsibleParty,
        uint256 previousInventoryMg,
        uint256 newInventoryMg,
        bytes32 receiptProofHash,
        uint256 timestamp
    );

    event RedemptionRequested(
        uint256 indexed redemptionId, address indexed holder, uint256 indexed unitId, uint256 amountSJC
    );

    event WithdrawalAuthorized(
        uint256 indexed redemptionId, uint256 indexed unitId, address indexed authorizedBy, uint256 amountSJC
    );

    event WithdrawalCompleted(
        uint256 indexed redemptionId,
        uint256 indexed unitId,
        uint256 deliveredWeightKg,
        uint256 burnedAmountSJC,
        uint256 returnedAmountSJC,
        bytes32 returnTaxInvoiceHash
    );

    event RedemptionCanceled(uint256 indexed redemptionId, uint256 indexed unitId, address holder);
}
