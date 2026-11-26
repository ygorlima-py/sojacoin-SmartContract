// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {ITokenIssuanceBatches} from "../interfaces/ITokenIssuanceBatches.sol";

interface ISojaCoinIssuanceBatches {
    function owner() external view returns (address);

    function issueTokensFromBatch(uint256 depositId) external;

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
        );
}

/// @notice Groups received loads and records a single producer approval.
/// @dev Kept separate from the token so SojaCoin stays below the deployment bytecode
/// size limit. SojaCoin checks the approval before issuing tokens.
contract TokenIssuanceBatches is EIP712, ITokenIssuanceBatches {
    uint256 public constant MAX_DEPOSITS_PER_BATCH = 64;

    bytes32 public constant ISSUANCE_BATCH_AUTHORIZATION_TYPEHASH = keccak256(
        "IssuanceBatchAuthorization(uint256 batchId,address producer,bytes32 summaryHash,uint256 totalNetWeightKg,uint256 nonce,uint256 deadline)"
    );

    enum IssuanceBatchStatus {
        Nonexistent,
        PendingApproval,
        Approved,
        Completed,
        Canceled
    }

    struct IssuanceBatch {
        address producer;
        address createdBy;
        uint256 totalNetWeightKg;
        uint256 amountDeposits;
        uint256 amountIssued;
        uint256 deadline;
        bytes32 summaryHash;
        IssuanceBatchStatus status;
    }

    address public immutable override sojaCoin;
    ISojaCoinIssuanceBatches private immutable _sojaCoin;

    mapping(uint256 => IssuanceBatch) public batches;
    mapping(uint256 => uint256[]) private _depositsByBatch;
    mapping(uint256 => uint256) public batchByDeposit;
    mapping(uint256 => bool) public depositIssued;
    mapping(address => uint256) public nonces;
    // Stores the latest batch ID; the next batch uses the following value.
    uint256 public nextBatchId;

    event BatchCreated(
        uint256 indexed batchId,
        address indexed producer,
        address indexed createdBy,
        uint256 amountDeposits,
        uint256 totalNetWeightKg,
        bytes32 summaryHash,
        uint256 deadline
    );

    event BatchApproved(
        uint256 indexed batchId,
        address indexed producer,
        address indexed recordedBy,
        bytes32 summaryHash,
        uint256 timestamp
    );

    event BatchCanceled(uint256 indexed batchId, address indexed canceledBy);

    event BatchIssuanceRecorded(
        uint256 indexed batchId, uint256 indexed depositId, uint256 amountIssued, uint256 amountDeposits
    );

    event BatchCompleted(
        uint256 indexed batchId, address indexed producer, uint256 amountDeposits, uint256 timestamp
    );

    /// @notice Creates the module for an existing SojaCoin deployment.
    /// @param sojaCoinAddress Address of the token that will issue SJC.
    constructor(address sojaCoinAddress) EIP712("SojaCoin Token Issuance Batches", "1") {
        require(sojaCoinAddress != address(0), "Invalid SojaCoin address");
        require(sojaCoinAddress.code.length > 0, "SojaCoin address has no code");
        sojaCoin = sojaCoinAddress;
        _sojaCoin = ISojaCoinIssuanceBatches(sojaCoinAddress);
    }

    /// @notice Returns the load IDs stored in a batch, in the order signed by the producer.
    function getBatchDepositIds(uint256 batchId) external view returns (uint256[] memory) {
        return _depositsByBatch[batchId];
    }

    /// @notice Returns whether a load belongs to an approved batch awaiting issuance.
    function isDepositInApprovedBatch(uint256 depositId) external view override returns (bool) {
        uint256 batchId = batchByDeposit[depositId];
        return batchId != 0 && batches[batchId].status == IssuanceBatchStatus.Approved;
    }

    /// @notice Returns the issuance operator saved when the receipt entered its batch.
    /// @dev SojaCoin uses this address to recheck current unit permission when the
    /// participant approves and issues in one transaction. The caller cannot choose it.
    function batchCreatorForDeposit(uint256 depositId) external view override returns (address) {
        uint256 batchId = batchByDeposit[depositId];
        require(batchId != 0, "Deposit is not in a batch");
        return batches[batchId].createdBy;
    }

    /// @notice Returns whether a load is locked against issuance or changes by a batch.
    function isDepositLocked(uint256 depositId) external view override returns (bool) {
        uint256 batchId = batchByDeposit[depositId];
        if (batchId == 0) return false;
        IssuanceBatchStatus status = batches[batchId].status;
        return status == IssuanceBatchStatus.PendingApproval || status == IssuanceBatchStatus.Approved;
    }

    /// @notice Creates a batch containing received loads from the same producer.
    /// @dev The unit registry verifies that the creator has issuance permission for each load.
    /// The list, weights, and hashes are fixed when the batch is created. The contract
    /// generates a sequential ID and returns it to the caller.
    /// @param depositIds Loads to be presented together to the producer.
    /// @param deadline Unix deadline for the producer to approve the batch.
    /// @return batchId Sequential identifier assigned to the batch.
    function createIssuanceBatch(
        uint256[] calldata depositIds,
        uint256 deadline
    ) external returns (uint256 batchId) {
        require(
            depositIds.length != 0 && depositIds.length <= MAX_DEPOSITS_PER_BATCH,
            "Invalid number of deposits"
        );
        require(deadline > block.timestamp, "Invalid batch deadline");

        batchId = ++nextBatchId;

        address producer;
        uint256 totalWeightKg;
        bytes32 resumo = keccak256(abi.encode(depositIds.length));

        for (uint256 i; i < depositIds.length; ++i) {
            uint256 depositId = depositIds[i];
            require(batchByDeposit[depositId] == 0, "Deposit is already in a batch");

            (
                address producerLoad,
                bytes32 taxInvoiceKeyHash,
                uint256 unitId,
                uint256 netWeightKg,
                bytes32 weighingTicketHash,
                bytes32 cropYear
            ) = _sojaCoin.validateDepositForBatch(depositId, msg.sender);

            if (i == 0) {
                producer = producerLoad;
            } else {
                require(producerLoad == producer, "Producers do not match");
            }

            batchByDeposit[depositId] = batchId;
            _depositsByBatch[batchId].push(depositId);
            totalWeightKg += netWeightKg;
            resumo = keccak256(
                abi.encode(resumo, depositId, unitId, cropYear, taxInvoiceKeyHash, weighingTicketHash, netWeightKg)
            );
        }

        batches[batchId] = IssuanceBatch({
            producer: producer,
            createdBy: msg.sender,
            totalNetWeightKg: totalWeightKg,
            amountDeposits: depositIds.length,
            amountIssued: 0,
            deadline: deadline,
            summaryHash: resumo,
            status: IssuanceBatchStatus.PendingApproval
        });

        emit BatchCreated(batchId, producer, msg.sender, depositIds.length, totalWeightKg, resumo, deadline);
    }

    /// @notice Calculates the EIP-712 digest the producer signs to approve the batch.
    function issuanceBatchAuthorizationHash(uint256 batchId) external view returns (bytes32) {
        IssuanceBatch storage batch = batches[batchId];
        require(batch.status == IssuanceBatchStatus.PendingApproval, "Batch is not awaiting approval");
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    ISSUANCE_BATCH_AUTHORIZATION_TYPEHASH,
                    batchId,
                    batch.producer,
                    batch.summaryHash,
                    batch.totalNetWeightKg,
                    nonces[batch.producer],
                    batch.deadline
                )
            )
        );
    }

    /// @notice Records the producer's single signature for the loads in the batch.
    /// @dev Any wallet may submit the signature; before enabling issuance, the contract
    /// checks the producer, fixed batch contents, deadline, and nonce.
    function approveIssuanceBatch(uint256 batchId, bytes calldata signatureProducer) external {
        IssuanceBatch storage batch = batches[batchId];
        require(batch.status == IssuanceBatchStatus.PendingApproval, "Batch is not awaiting approval");
        require(block.timestamp <= batch.deadline, "Approval has expired");

        uint256 nonce = nonces[batch.producer];
        bytes32 digest = _hashTypedDataV4(
            keccak256(
                abi.encode(
                    ISSUANCE_BATCH_AUTHORIZATION_TYPEHASH,
                    batchId,
                    batch.producer,
                    batch.summaryHash,
                    batch.totalNetWeightKg,
                    nonce,
                    batch.deadline
                )
            )
        );
        require(
            SignatureChecker.isValidSignatureNow(batch.producer, digest, signatureProducer),
            "Invalid producer authorization"
        );

        nonces[batch.producer] = nonce + 1;
        batch.status = IssuanceBatchStatus.Approved;
        emit BatchApproved(batchId, batch.producer, msg.sender, batch.summaryHash, block.timestamp);
    }

    /// @notice Approves a one-receipt batch and issues its SJC in one transaction.
    /// @dev The participant's wallet pays for this call. Solidity reverts the approval,
    /// event, and issuance together if any SojaCoin, audit, stock, CPR, or unit check fails.
    /// The original batch creator remains the responsible party for the unit permission.
    /// @param batchId Sequential batch identifier assigned by this contract.
    function approveAndIssueBatch(uint256 batchId) external {
        IssuanceBatch storage batch = batches[batchId];
        require(batch.status == IssuanceBatchStatus.PendingApproval, "Batch is not awaiting approval");
        require(block.timestamp <= batch.deadline, "Approval has expired");
        require(msg.sender == batch.producer, "Producer only");
        require(batch.amountDeposits == 1, "Automatic issuance supports one deposit");

        // SojaCoin checks that the receipt belongs to an approved batch. If issuance
        // fails, transaction atomicity restores this status and removes this event.
        batch.status = IssuanceBatchStatus.Approved;
        emit BatchApproved(batchId, batch.producer, msg.sender, batch.summaryHash, block.timestamp);
        _sojaCoin.issueTokensFromBatch(_depositsByBatch[batchId][0]);
    }

    /// @notice Cancels an unapproved batch and releases its loads for a new batch.
    /// @dev The list cannot be changed after approval.
    function cancelIssuanceBatch(uint256 batchId) external {
        IssuanceBatch storage batch = batches[batchId];
        require(batch.status == IssuanceBatchStatus.PendingApproval, "Batch cannot be canceled");
        require(
            msg.sender == batch.producer || msg.sender == batch.createdBy || msg.sender == _sojaCoin.owner(),
            "Not authorized"
        );

        uint256[] storage depositIds = _depositsByBatch[batchId];
        for (uint256 i; i < depositIds.length; ++i) {
            batchByDeposit[depositIds[i]] = 0;
        }
        batch.status = IssuanceBatchStatus.Canceled;
        emit BatchCanceled(batchId, msg.sender);
    }

    /// @notice Records in the batch an issuance already completed by the SojaCoin contract.
    /// @dev Only the linked SojaCoin can call this; issuance reverts if this record fails.
    function recordIssuance(uint256 depositId) external override {
        require(msg.sender == sojaCoin, "SojaCoin only");
        uint256 batchId = batchByDeposit[depositId];
        IssuanceBatch storage batch = batches[batchId];
        require(batchId != 0 && batch.status == IssuanceBatchStatus.Approved, "Batch is not approved");
        require(!depositIssued[depositId], "Issuance has already been recorded");

        depositIssued[depositId] = true;
        batch.amountIssued += 1;
        emit BatchIssuanceRecorded(batchId, depositId, batch.amountIssued, batch.amountDeposits);

        if (batch.amountIssued == batch.amountDeposits) {
            batch.status = IssuanceBatchStatus.Completed;
            emit BatchCompleted(batchId, batch.producer, batch.amountDeposits, block.timestamp);
        }
    }
}
