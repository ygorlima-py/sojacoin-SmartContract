// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {TokenModule} from "./TokenModule.sol";
import {IUnitRegistry} from "../interfaces/IUnitRegistry.sol";
import {ITokenIssuanceBatches} from "../interfaces/ITokenIssuanceBatches.sol";
import {ISojaCoinCPR} from "../interfaces/ISojaCoinCPR.sol";

// Deposits, weighing tickets, and SJC issuance. Issuance repays compatible CPRs
// in queue order and sends the producer only the amount exceeding those debts.
abstract contract DepositsModule is TokenModule {
    /// @notice Opens a receipt for a load that arrived at the warehouse.
    /// @dev Called by an employee with receipt permission for the unit.
    /// The terms hash recorded in the producer's registration authorizes per-load entries,
    /// so the producer does not need to submit a transaction for every delivery. The contract
    /// generates a sequential ID and returns it to the caller.
    /// @param producer Wallet of the producer identified in the load documents.
    /// @param unitId Physical unit where the truck arrived.
    /// @param cropYear Crop year identifier to match against CPRs.
    /// @return depositId Sequential identifier assigned to the receipt.
    function openLoadReceipt(
        address producer,
        uint256 unitId,
        bytes32 cropYear
    ) external returns (uint256 depositId) {
        require(participantRegistered[producer], "Producer is not registered");
        require(
            depositAuthorizationActive[producer] && participationTermsHashByParticipant[producer] != bytes32(0),
            "Producer has not authorized deposits"
        );
        require(unitRegistry != address(0), "Unit registry is not configured");
        require(unitId > 0, "Invalid unit");
        require(cropYear != bytes32(0), "Invalid crop year");

        depositId = ++nextDepositId;

        IUnitRegistry(unitRegistry).registerPendingDeposit(depositId, unitId, msg.sender);

        deposits[depositId].depositor = producer;
        deposits[depositId].unitId = unitId;
        deposits[depositId].cropYear = cropYear;
        deposits[depositId].participationTermsHash = participationTermsHashByParticipant[producer];
        deposits[depositId].depositDate = block.timestamp;
        pendingDeposits[producer] += 1;

        emit LoadReceiptOpened(
            depositId,
            producer,
            unitId,
            block.timestamp,
            msg.sender,
            deposits[depositId].participationTermsHash
        );
    }

    /// @notice Links the tax invoice to the open receipt.
    /// @dev Allows linking it before or after the weighing ticket to support both
    /// tax workflows used by companies. The load cannot enter a batch or issue SJC without this invoice.
    /// @param depositId Unique receipt identifier.
    /// @param taxInvoiceKeyHash Hash of the tax document linked to the load.
    function linkTaxInvoice(uint256 depositId, bytes32 taxInvoiceKeyHash) external {
        Deposit storage deposit = deposits[depositId];
        require(deposit.depositor != address(0), "Deposit does not exist");
        require(!deposit.canceled, "Deposit is canceled");
        require(!deposit.tokensIssued, "Tokens have already been issued");
        require(unitRegistry != address(0), "Unit registry is not configured");
        require(taxInvoiceKeyHash != bytes32(0), "Invalid tax invoice hash");
        require(deposit.taxInvoiceKeyHash == bytes32(0), "Tax invoice is already linked");

        IUnitRegistry registry = IUnitRegistry(unitRegistry);
        require(
            registry.hasPermissionForUnit(deposit.unitId, msg.sender, registry.PERMISSION_RECEIVE_SOYBEANS()),
            "Missing receipt permission"
        );

        deposit.taxInvoiceKeyHash = taxInvoiceKeyHash;
        depositCountByTaxInvoice[taxInvoiceKeyHash] += 1;

        emit TaxInvoiceLinked(depositId, deposit.unitId, taxInvoiceKeyHash, msg.sender, block.timestamp);
    }

    /// @notice Cancels a receipt before physical arrival of the load is confirmed.
    /// @dev Keeps the deposit data and closes its pending link; IDs cannot be reused.
    /// After receipt, any return must be recorded separately.
    /// @param depositId Identifier of the receipt to cancel.
    function cancelLoadReceipt(uint256 depositId) external {
        Deposit storage deposit = deposits[depositId];
        require(deposit.depositor != address(0), "Deposit does not exist");
        require(!deposit.tokensIssued, "Tokens have already been issued");
        require(!deposit.canceled, "Deposit is already canceled");
        require(!deposit.receiptConfirmed, "Use the deposit return registry");

        bool authorized = msg.sender == deposit.depositor;
        if (!authorized && unitRegistry != address(0)) {
            IUnitRegistry registry = IUnitRegistry(unitRegistry);
            authorized =
                registry.hasPermissionForUnit(deposit.unitId, msg.sender, registry.PERMISSION_RECEIVE_SOYBEANS());
        }
        require(authorized, "Not authorized");

        address depositor = deposit.depositor;
        uint256 unitId = deposit.unitId;
        bytes32 taxInvoiceKeyHash = deposit.taxInvoiceKeyHash;
        bytes32 weighingTicketHash = deposit.weighingTicketHash;
        deposit.canceled = true;
        if (taxInvoiceKeyHash != bytes32(0)) {
            depositCountByTaxInvoice[taxInvoiceKeyHash] -= 1;
        }
        pendingDeposits[depositor] -= 1;
        IUnitRegistry(unitRegistry).cancelPendingDeposit(depositId);

        emit LoadReceiptCanceled(depositId, depositor, unitId, taxInvoiceKeyHash, weighingTicketHash, msg.sender);
    }

    /// @notice Validates a load and returns the data the batches module will lock at approval.
    /// @dev Only the configured module can call this. It also confirms that the wallet
    /// creating the batch has issuance permission for that unit.
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

    /// @notice Confirms receipt of the load and records the net weight from the weighing ticket.
    /// @dev Called by an employee with receipt permission for the authorized unit.
    /// Calculates net weight by subtracting tare and deductions in kilograms; updates the unit and,
    /// if the weighing ticket is later than the last audit, adds the incoming amount to global inventory.
    /// @param depositId Identifier of the previously registered deposit.
    /// @param weighingTicket Weights, deductions, document hashes, and receipt timestamp.
    function confirmLoadReceipt(uint256 depositId, WeighingTicket calldata weighingTicket) external {
        Deposit storage deposit = deposits[depositId];
        require(deposit.depositor != address(0), "Deposit does not exist");
        require(!deposit.canceled, "Deposit is canceled");
        require(!deposit.receiptConfirmed, "Deposit is already confirmed");
        require(weighingTicket.weighingTicketHash != bytes32(0), "Invalid weighing ticket hash");
        require(!weighingTicketUsed[weighingTicket.weighingTicketHash], "Weighing ticket has already been used");
        require(weighingTicket.timestamp != 0, "Invalid weighing ticket date");
        require(weighingTicket.timestamp <= block.timestamp, "Invalid future date");

        require(weighingTicket.grossWeightKg > weighingTicket.tareKg, "Tare exceeds gross weight");
        uint256 loadWeightKg = weighingTicket.grossWeightKg - weighingTicket.tareKg;

        uint256 totalDeductions = weighingTicket.moistureDeductionKg + weighingTicket.impuritiesDeductionKg
            + weighingTicket.damagedGrainDeductionKg + weighingTicket.otherDeductionsKg;
        require(loadWeightKg > totalDeductions, "Deductions exceed gross weight");

        uint256 netWeightKg = loadWeightKg - totalDeductions;
        IUnitRegistry(unitRegistry)
            .confirmDepositReceipt(depositId, netWeightKg * MG_PER_KG, weighingTicket.timestamp, msg.sender);
        deposit.netWeightKg = netWeightKg;
        deposit.weighingTicketHash = weighingTicket.weighingTicketHash;
        deposit.documentTimestamp = weighingTicket.timestamp;
        deposit.receivedAt = block.timestamp;
        deposit.receiptConfirmed = true;
        weighingTicketUsed[weighingTicket.weighingTicketHash] = true;

        // The unit registry determines whether this receipt was already included
        // in that unit's audit and returns the company's correct total.
        registeredInventoryMg = IUnitRegistry(unitRegistry)
            .registeredInventoryMg();
        _updateIssuanceCap();

        emit LoadReceiptConfirmed(
            depositId,
            deposit.unitId,
            msg.sender,
            deposit.depositor,
            weighingTicket.grossWeightKg,
            weighingTicket.tareKg,
            netWeightKg,
            weighingTicket.weighingTicketHash,
            weighingTicket.reportHash,
            weighingTicket.timestamp,
            block.timestamp
        );

        emit ReceiptClassified(
            depositId,
            weighingTicket.moistureBps,
            weighingTicket.impuritiesBps,
            weighingTicket.damagedGrainBps,
            weighingTicket.moistureDeductionKg,
            weighingTicket.impuritiesDeductionKg,
            weighingTicket.damagedGrainDeductionKg,
            weighingTicket.otherDeductionsKg
        );
    }

    /// @notice Issues SJC based on the net weight of a confirmed deposit.
    /// @dev Called by an employee with issuance permission for the unit, while the audit
    /// is valid and the backing cap is sufficient.
    /// Repays compatible CPRs in priority order and issues any remainder to the producer.
    /// A remainder smaller than one token unit is carried forward to the next deposit.
    /// @param depositId Identifier of the deposit whose receipt was confirmed.
    function issueTokens(uint256 depositId) external {
        Deposit storage deposit = deposits[depositId];

        require(unitRegistry != address(0), "Unit registry is not configured");
        require(issuanceBatchModule != address(0), "Issuance batch module is not configured");
        require(!deposit.canceled, "Deposit is canceled");
        require(!deposit.closedWithoutIssuance, "Deposit has been fully returned");
        require(deposit.receiptConfirmed, "Receipt has not been confirmed");
        require(!deposit.tokensIssued, "Tokens have already been issued");
        require(ITokenIssuanceBatches(issuanceBatchModule).isDepositInApprovedBatch(depositId), "Deposit is not in an approved batch");
        _requireCurrentAudit();

        IUnitRegistry(unitRegistry).validateDepositIssuance(depositId, msg.sender);

        uint256 issuableWeightKg = deposit.netWeightKg - deposit.returnedWeightKg;
        require(issuableWeightKg > 0, "Deposit has no soybeans to issue");

        uint256 massWithRemainderMg = issuableWeightKg * MG_PER_KG + conversionRemainderMgByProducer[deposit.depositor];
        uint256 amountSJC = massWithRemainderMg / MG_PER_SJC_UNIT;
        uint8 newRemainderMg = uint8(massWithRemainderMg % MG_PER_SJC_UNIT);
        require(amountSJC > 0, "Amount is below the smallest SJC unit");

        require(totalSupply() + amountSJC <= issuanceCapSJC, "Exceeds inventory backing");

        conversionRemainderMgByProducer[deposit.depositor] = newRemainderMg;
        deposit.amountSJC = amountSJC;

        // The CPR module reduces compatible debts and mints SJC directly to
        // each creditor. Only the remaining amount is minted to the producer.
        uint256 unencumberedAmountSJC = ISojaCoinCPR(address(this)).distributeIssuanceByCPR(depositId, amountSJC);
        if (unencumberedAmountSJC > 0) {
            _mint(deposit.depositor, unencumberedAmountSJC);
        }

        deposit.tokensIssued = true;
        pendingDeposits[deposit.depositor] -= 1;
        IUnitRegistry(unitRegistry).closeDeposit(depositId);
        ITokenIssuanceBatches(issuanceBatchModule).recordIssuance(depositId);

        emit TokensIssued(depositId, deposit.depositor, msg.sender, amountSJC, newRemainderMg, block.timestamp);
    }

    /// @notice Records a partial soybean return accepted before SJC issuance.
    /// @dev Updates the weight eligible for SJC issuance and reduces unit and global inventory.
    /// The return requires its own document and does not erase the original weighing ticket.
    /// @param depositId Deposit for which soybeans are being partially returned.
    /// @param returnedAmountKg Actual returned weight, in whole kilograms.
    /// @param documentHash Hash of the document proving the return or dispatch.
    function recordDepositReturn(uint256 depositId, uint256 returnedAmountKg, bytes32 documentHash)
        external
    {
        Deposit storage deposit = deposits[depositId];
        require(unitRegistry != address(0), "Unit registry is not configured");
        require(deposit.depositor != address(0), "Deposit does not exist");
        require(deposit.receiptConfirmed, "Receipt has not been confirmed");
        require(!deposit.canceled, "Deposit is canceled");
        require(!deposit.tokensIssued, "Tokens have already been issued");
        require(!deposit.closedWithoutIssuance, "Deposit is closed");
        require(issuanceBatchModule != address(0), "Issuance batch module is not configured");
        require(!ITokenIssuanceBatches(issuanceBatchModule).isDepositLocked(depositId), "Cancel the batch before recording a return");
        require(returnedAmountKg > 0, "Invalid amount");
        require(documentHash != bytes32(0), "Invalid document hash");

        uint256 remainingWeightKg = deposit.netWeightKg - deposit.returnedWeightKg;
        require(returnedAmountKg <= remainingWeightKg, "Return amount exceeds remaining balance");

        IUnitRegistry(unitRegistry)
            .recordDepositReturn(depositId, returnedAmountKg * MG_PER_KG, msg.sender);
        deposit.returnedWeightKg += returnedAmountKg;
        _reducePhysicalInventory(returnedAmountKg * MG_PER_KG, msg.sender);

        emit DepositReturnRecorded(
            depositId,
            deposit.unitId,
            returnedAmountKg,
            deposit.returnedWeightKg,
            documentHash,
            msg.sender,
            block.timestamp
        );

        if (deposit.returnedWeightKg == deposit.netWeightKg) {
            deposit.closedWithoutIssuance = true;
            pendingDeposits[deposit.depositor] -= 1;
            IUnitRegistry(unitRegistry).closeDeposit(depositId);
        }
    }
}
