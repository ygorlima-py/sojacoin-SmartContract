// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ISojaCoinUnits} from "./interfaces/ISojaCoinUnits.sol";

/// @notice Maintains a company's physical units and the audited inventory of each one.
/// @dev This contract is separate from the token to stay below Ethereum's contract size
/// limit. Each instance serves one company and points to a specific SojaCoin.
contract UnitRegistry is Ownable2Step {
    uint256 public constant MG_PER_KG = 1_000_000;
    /// @notice Fixed threshold for discrepancy alerts; it does not tolerate or erase any loss.
    uint256 public constant DISCREPANCY_ALERT_LIMIT_BPS = 25;
    uint256 private constant BPS_DENOMINATOR = 10_000;

    /// @notice Possible states of a physical transfer between units.
    enum TransferStatus {
        Nonexistent,
        Requested,
        InTransit,
        Received,
        Canceled
    }

    uint256 public constant PERMISSION_RECEIVE_SOYBEANS = 1 << 0;
    uint256 public constant PERMISSION_ISSUE_SJC = 1 << 1;
    uint256 public constant PERMISSION_AUTHORIZE_WITHDRAWAL = 1 << 2;
    uint256 public constant PERMISSION_COMPLETE_WITHDRAWAL = 1 << 3;
    uint256 public constant PERMISSION_MANAGE_SERVICES = 1 << 4;
    uint256 public constant PERMISSION_TRANSFER_INVENTORY = 1 << 5;
    uint256 public constant AUDIT_PERIOD = 60 days;
    uint256 private constant VALID_PERMISSIONS_MASK =
        PERMISSION_RECEIVE_SOYBEANS |
        PERMISSION_ISSUE_SJC |
        PERMISSION_AUTHORIZE_WITHDRAWAL |
        PERMISSION_COMPLETE_WITHDRAWAL |
        PERMISSION_MANAGE_SERVICES |
        PERMISSION_TRANSFER_INVENTORY;
    uint256 private constant AUDIT_CONFLICTING_PERMISSIONS_MASK =
        PERMISSION_RECEIVE_SOYBEANS |
        PERMISSION_ISSUE_SJC |
        PERMISSION_AUTHORIZE_WITHDRAWAL |
        PERMISSION_COMPLETE_WITHDRAWAL |
        PERMISSION_TRANSFER_INVENTORY;

    struct Unit {
        bytes32 code;
        bytes32 registrationHash;
        bytes32 lastReportHash;
        uint256 registeredInventoryMg;
        uint256 lastAuditAt;
        bool active;
    }

    /// @notice Data and evidence for a company's physical transfer.
    /// @dev Records a movement of aggregated inventory without claiming that the grain
    /// corresponds to a single producer deposit.
    struct Transfer {
        uint256 sourceUnitId;
        uint256 destinationUnitId;
        uint256 requestedAmountKg;
        uint256 dispatchedAmountKg;
        uint256 receivedAmountKg;
        uint256 discrepancyMg;
        address requestedBy;
        address dispatchedBy;
        address receivedBy;
        uint256 requestedIn;
        uint256 dispatchedIn;
        uint256 receivedIn;
        bytes32 hashOrder;
        bytes32 outboundDocumentHash;
        bytes32 receiptProofHash;
        TransferStatus status;
    }

    /// @notice Soybean reservation associated with a SojaCoin withdrawal request.
    /// @dev The reservation is created only when the warehouse authorizes the request;
    /// until then, the producer can cancel without tying up the unit's physical inventory.
    struct UnitRedemption {
        uint256 unitId;
        uint256 requestedMassMg;
        uint256 reservedMassMg;
        address holder;
        bool active;
    }

    ISojaCoinUnits public immutable sojaCoin;
    uint256 public nextUnitId;
    uint256 public activeUnitCount;
    uint256 public nextTransferId;
    uint256 public registeredInventoryMg;
    uint256 public inventoryInTransitMg;
    uint256 public lastAuditAt;

    mapping(uint256 => Unit) public units;
    mapping(bytes32 => uint256) public unitIdByCode;
    mapping(address => uint256) public permissionsGeneral;
    mapping(uint256 => mapping(address => uint256)) public permissionsByUnit;
    mapping(address => uint256) private _unitsWithReceiptSpecific;
    mapping(address => uint256) private _unitsWithIssuanceSpecific;
    mapping(address => uint256) private _unitsWithAuthorizationWithdrawalSpecific;
    mapping(address => uint256) private _unitsWithCompletionWithdrawalSpecific;
    mapping(uint256 => address) public auditorByUnit;
    mapping(address => uint256) public unitsAuditadasByWallet;
    mapping(uint256 => Transfer) public transfers;
    mapping(uint256 => uint256) public reservedInventoryMgByUnit;
    mapping(uint256 => uint256) public transfersPendingByUnit;
    mapping(uint256 => uint256) public pendingDepositsByUnit;
    mapping(uint256 => uint256) public redemptionsPendingByUnit;
    mapping(uint256 => uint256) public unitByDeposit;
    mapping(uint256 => bool) private _depositPending;
    mapping(uint256 => bool) private _depositReceived;
    mapping(uint256 => uint256) public unitByRedemption;
    mapping(uint256 => UnitRedemption) public redemptionsByUnit;

    error InvalidUnitCode();
    error UnitCodeAlreadyUsed();
    error InvalidHash();
    error UnitDoesNotExist();
    error UnitIsInactive();
    error UnitHasInventory();
    error InvalidWallet();
    error InvalidPermission();
    error UnitAuditorNotSet();
    error UnitAuditorNotAuthorized();
    error AuditorRemovalRequiresInactiveUnit();
    error InsufficientTransferPermission();
    error UnitsMustDiffer();
    error AmountInvalid();
    error InsufficientAvailableInventory();
    error TransferDoesNotExist();
    error InvalidTransferStatus();
    error ReceivedAmountExceedsDispatchedAmount();
    error TransferOperatorsMustDiffer();
    error UnitHasPendingTransfer();
    error ReservationsExceedInventory();
    error SojaCoinOnly();
    error InvalidUnitDeposit();
    error UnitDepositAlreadyRecorded();
    error UnitDepositNotPending();
    error UnitDepositAlreadyReceived();
    error InvalidUnitRedemption();
    error UnitRedemptionAlreadyRecorded();
    error UnitRedemptionNotPending();
    error InsufficientOperationalPermission();
    error AuditPermissionConflict();
    error RoleSeparationConflict();

    event UnitRegistered(
        uint256 indexed unitId,
        bytes32 indexed code,
        bytes32 registrationHash,
        address indexed registeredBy
    );
    event UnitUpdated(
        uint256 indexed unitId,
        bytes32 registrationHash,
        bool active,
        address indexed updatedBy
    );
    event PermissionsSet(
        uint256 indexed unitId,
        address indexed wallet,
        uint256 permissions,
        address definidasBy
    );
    event UnitAuditorSet(
        uint256 indexed unitId,
        address indexed auditorPrevious,
        address indexed newAuditor,
        address setBy
    );
    event UnitAudited(
        uint256 indexed unitId,
        address indexed auditor,
        uint256 physicalInventoryKg,
        bytes32 reportHash,
        uint256 timestamp
    );
    event TransferRequested(
        uint256 indexed transferId,
        uint256 indexed sourceUnitId,
        uint256 indexed destinationUnitId,
        uint256 requestedAmountKg,
        bytes32 hashOrder,
        address requestedBy,
        uint256 timestamp
    );
    event TransferDispatched(
        uint256 indexed transferId,
        uint256 dispatchedAmountKg,
        bytes32 outboundDocumentHash,
        address indexed dispatchedBy,
        uint256 timestamp
    );
    event TransferReceived(
        uint256 indexed transferId,
        uint256 dispatchedAmountKg,
        uint256 receivedAmountKg,
        bytes32 receiptProofHash,
        address indexed receivedBy,
        uint256 timestamp
    );
    event TransferDiscrepancy(
        uint256 indexed transferId,
        uint256 dispatchedAmountKg,
        uint256 receivedAmountKg,
        uint256 discrepancyKg,
        bytes32 receiptProofHash,
        address indexed detectedBy,
        uint256 timestamp
    );
    event TransferDiscrepancyAlert(
        uint256 indexed transferId,
        uint256 dispatchedAmountKg,
        uint256 receivedAmountKg,
        uint256 discrepancyKg,
        uint256 alertLimitMg,
        bytes32 receiptProofHash,
        address indexed detectedBy,
        uint256 timestamp
    );
    event TransferCanceled(
        uint256 indexed transferId,
        address indexed canceledBy,
        uint256 timestamp
    );
    event DepositLinkedToUnit(
        uint256 indexed depositId,
        uint256 indexed unitId,
        address indexed recordedBy
    );
    event UnitDepositEntryRecorded(
        uint256 indexed depositId,
        uint256 indexed unitId,
        uint256 amountKg,
        address indexed responsibleParty,
        uint256 timestamp
    );
    event UnitWithdrawalReserved(
        uint256 indexed redemptionId,
        uint256 indexed unitId,
        uint256 amountMg,
        address indexed authorizedBy
    );
    event UnitWithdrawalSettled(
        uint256 indexed redemptionId,
        uint256 indexed unitId,
        uint256 deliveredAmountMg,
        address indexed responsibleParty,
        uint256 timestamp
    );
    event UnitWithdrawalCanceled(
        uint256 indexed redemptionId,
        uint256 indexed unitId,
        address indexed canceledBy
    );

    /// @notice Creates a company's registry and links it to the corresponding SojaCoin.
    /// @param initialOwner Wallet that will administer the company's units and permissions.
    /// @param sojaCoinAddress SojaCoin contract that will receive the audited total.
    constructor(address initialOwner, address sojaCoinAddress)
        Ownable(initialOwner)
    {
        if (sojaCoinAddress == address(0)) revert InvalidWallet();
        sojaCoin = ISojaCoinUnits(sojaCoinAddress);
    }

    /// @notice Disables renunciation of registry ownership.
    /// @dev Prevents units, permissions, and audits from being left without an administrator.
    function renounceOwnership() public view virtual override onlyOwner {
        revert("Owner renunciation is disabled");
    }

    /// @notice Registers a company's physical unit.
    /// @dev The name, address, CNPJ, and state registration number are stored externally;
    /// the hash can later prove which version of those details was used.
    /// @param code Unique unit code converted to bytes32 by the external system.
    /// @param registrationHash Hash of the unit's complete registration record.
    /// @return unitId Identifier assigned to the unit.
    function registerUnit(
        bytes32 code,
        bytes32 registrationHash
    ) external onlyOwner returns (uint256 unitId) {
        if (code == bytes32(0)) revert InvalidUnitCode();
        if (registrationHash == bytes32(0)) revert InvalidHash();
        if (unitIdByCode[code] != 0) revert UnitCodeAlreadyUsed();

        unitId = ++nextUnitId;
        units[unitId] = Unit({
            code: code,
            registrationHash: registrationHash,
            lastReportHash: bytes32(0),
            registeredInventoryMg: 0,
            lastAuditAt: 0,
            active: true
        });
        unitIdByCode[code] = unitId;
        activeUnitCount += 1;

        // A new unit has no audit report yet, so the global audit timestamp
        // remains invalid until its assigned auditor completes an audit.
        lastAuditAt = 0;
        _syncAuditValidityWithSojaCoin();

        emit UnitRegistered(unitId, code, registrationHash, msg.sender);
    }

    /// @notice Updates a unit's registration and status without erasing its history.
    /// @dev A unit can be deactivated only when its latest audited inventory is zero.
    /// @param unitId Identifier of the unit being changed.
    /// @param registrationHash New hash of the complete registration record.
    /// @param active Whether the unit can be included in new audits and operations.
    function updateUnit(
        uint256 unitId,
        bytes32 registrationHash,
        bool active
    ) external onlyOwner {
        Unit storage unit = _existingUnit(unitId);
        bool statusChanged = unit.active != active;
        if (registrationHash == bytes32(0)) revert InvalidHash();

        if (unit.active && !active) {
            if (unit.registeredInventoryMg != 0) revert UnitHasInventory();
            if (
                transfersPendingByUnit[unitId] != 0 ||
                reservedInventoryMgByUnit[unitId] != 0 ||
                pendingDepositsByUnit[unitId] != 0 ||
                redemptionsPendingByUnit[unitId] != 0
            ) {
                revert UnitHasPendingTransfer();
            }
            activeUnitCount -= 1;
        } else if (!unit.active && active) {
            activeUnitCount += 1;
        }

        unit.registrationHash = registrationHash;
        unit.active = active;
        if (statusChanged) {
            _recalculateLastAudit();
            _syncAuditValidityWithSojaCoin();
        }
        emit UnitUpdated(unitId, registrationHash, active, msg.sender);
    }

    /// @notice Sets company-wide or unit-specific permissions for a wallet.
    /// @dev A unitId of zero sets company-wide permissions; any other ID sets permissions
    /// for that unit. Unit-specific permissions are added to company-wide permissions. An
    /// auditor wallet cannot receive permissions that move or affect inventory in the same
    /// unit. Such company-wide permissions are also rejected while that wallet remains the
    /// auditor of any unit, whether active or inactive.
    /// The same wallet also cannot both receive and issue SJC, or authorize and complete
    /// a withdrawal, within the same unit.
    /// @param unitId Unit ID, or zero for the entire company.
    /// @param wallet Employee wallet receiving the permissions.
    /// @param permissions Bitwise sum of the desired PERMISSION_* constants.
    function setPermissions(
        uint256 unitId,
        address wallet,
        uint256 permissions
    ) external onlyOwner {
        if (wallet == address(0)) revert InvalidWallet();
        if ((permissions & ~VALID_PERMISSIONS_MASK) != 0) {
            revert InvalidPermission();
        }

        if (unitId == 0) {
            if (
                _hasAuditConflictingPermission(permissions) &&
                unitsAuditadasByWallet[wallet] != 0
            ) {
                revert AuditPermissionConflict();
            }
            _validateGeneralPermissionsWithoutConflict(wallet, permissions);
            permissionsGeneral[wallet] = permissions;
        } else {
            _existingUnit(unitId);
            if (
                _hasAuditConflictingPermission(permissions) &&
                auditorByUnit[unitId] == wallet
            ) {
                revert AuditPermissionConflict();
            }

            _validateRoleSeparation(permissionsGeneral[wallet] | permissions);
            _updateSpecificPermissionCounters(
                wallet,
                permissionsByUnit[unitId][wallet],
                permissions
            );
            permissionsByUnit[unitId][wallet] = permissions;
        }

        emit PermissionsSet(unitId, wallet, permissions, msg.sender);
    }

    /// @notice Sets who is responsible for recording an audit of a unit.
    /// @dev Each unit has one auditor wallet. The owner can replace it when the company
    /// changes the responsible professional or audit firm. The same wallet cannot have
    /// permissions that move or affect inventory in this unit or company-wide.
    /// @param unitId Physical unit whose auditor is being set.
    /// @param newAuditor Wallet allowed to record audits for that unit.
    function setUnitAuditor(
        uint256 unitId,
        address newAuditor
    ) external onlyOwner {
        Unit storage unit = _existingUnit(unitId);
        if (!unit.active) revert UnitIsInactive();
        if (newAuditor == address(0)) revert InvalidWallet();
        if (
            _hasAuditConflictingPermission(permissionsGeneral[newAuditor]) ||
            _hasAuditConflictingPermission(permissionsByUnit[unitId][newAuditor])
        ) {
            revert AuditPermissionConflict();
        }

        address auditorPrevious = auditorByUnit[unitId];
        if (auditorPrevious != newAuditor) {
            if (auditorPrevious != address(0)) {
                unitsAuditadasByWallet[auditorPrevious] -= 1;
            }
            unitsAuditadasByWallet[newAuditor] += 1;
        }
        auditorByUnit[unitId] = newAuditor;
        emit UnitAuditorSet(
            unitId,
            auditorPrevious,
            newAuditor,
            msg.sender
        );
    }

    /// @notice Removes the auditor from an inactive unit that will no longer be audited.
    /// @dev Removal is allowed only after the unit is deactivated. For an active unit,
    /// the owner must replace the auditor directly so its inventory always has an auditor.
    /// @param unitId Inactive unit whose auditor will be removed.
    function removeUnitAuditor(uint256 unitId) external onlyOwner {
        Unit storage unit = _existingUnit(unitId);
        if (unit.active) revert AuditorRemovalRequiresInactiveUnit();

        address auditorPrevious = auditorByUnit[unitId];
        if (auditorPrevious == address(0)) revert UnitAuditorNotSet();

        auditorByUnit[unitId] = address(0);
        unitsAuditadasByWallet[auditorPrevious] -= 1;
        emit UnitAuditorSet(
            unitId,
            auditorPrevious,
            address(0),
            msg.sender
        );
    }

    /// @notice Checks whether a wallet has a permission for an active unit.
    /// @dev Combines company-wide and unit-specific permissions. This function does not change contract state.
    /// @param unitId Unit being queried.
    /// @param wallet Employee wallet.
    /// @param permission PERMISSION_* constant to check.
    /// @return hasPermission True if the wallet has the requested permission.
    function hasPermissionForUnit(
        uint256 unitId,
        address wallet,
        uint256 permission
    ) external view returns (bool hasPermission) {
        return _hasPermissionForUnit(unitId, wallet, permission);
    }

    /// @notice Opens a link between a deposit and the unit that received the load.
    /// @dev Only SojaCoin can call this after validating the producer's terms. The responsible
    /// wallet needs receipt permission for that unit. The link remains reserved until the deposit
    /// is canceled before receipt or closed after issuance or a full return.
    /// @param depositId Permanent deposit identifier in SojaCoin.
    /// @param unitId Unit that recorded the incoming load.
    /// @param responsibleParty Employee recording the incoming load.
    function registerPendingDeposit(
        uint256 depositId,
        uint256 unitId,
        address responsibleParty
    ) external {
        _requireSojaCoinOnly();
        if (depositId == 0 || unitId == 0) revert InvalidUnitDeposit();
        if (unitByDeposit[depositId] != 0) {
            revert UnitDepositAlreadyRecorded();
        }

        Unit storage unit = _existingUnit(unitId);
        if (!unit.active) revert UnitIsInactive();
        _requireOperationalPermission(
            unitId,
            responsibleParty,
            PERMISSION_RECEIVE_SOYBEANS
        );

        unitByDeposit[depositId] = unitId;
        _depositPending[depositId] = true;
        pendingDepositsByUnit[unitId] += 1;
        emit DepositLinkedToUnit(depositId, unitId, responsibleParty);
    }

    /// @notice Cancels a registered deposit whose receipt has not been confirmed.
    /// @dev Does not change inventory; keeps the link so the ID cannot be reused.
    /// @param depositId Identifier of the canceled deposit.
    function cancelPendingDeposit(uint256 depositId) external {
        _requireSojaCoinOnly();
        if (!_depositPending[depositId]) revert UnitDepositNotPending();
        if (_depositReceived[depositId]) revert UnitDepositAlreadyReceived();
        _closePendingDeposit(depositId);
    }

    /// @notice Records the net weight accepted at the unit specified by the deposit.
    /// @dev The incoming amount is added only if its timestamp is later than the unit's last
    /// audit; a later audit would already have counted the physical goods.
    /// SojaCoin still updates the global balance, exactly once.
    /// @param depositId Deposit whose receipt was confirmed.
    /// @param receivedWeightMg Accepted net weight, in milligrams.
    /// @param timestamp Receipt timestamp from the weighing ticket.
    /// @param responsibleParty Employee who confirmed receipt.
    function confirmDepositReceipt(
        uint256 depositId,
        uint256 receivedWeightMg,
        uint256 timestamp,
        address responsibleParty
    ) external {
        _requireSojaCoinOnly();
        if (!_depositPending[depositId]) revert UnitDepositNotPending();
        if (_depositReceived[depositId]) revert UnitDepositAlreadyReceived();
        if (receivedWeightMg == 0 || timestamp == 0) {
            revert AmountInvalid();
        }

        uint256 unitId = unitByDeposit[depositId];
        Unit storage unit = _existingUnit(unitId);
        if (!unit.active) revert UnitIsInactive();
        _requireOperationalPermission(
            unitId,
            responsibleParty,
            PERMISSION_RECEIVE_SOYBEANS
        );

        _depositReceived[depositId] = true;
        if (timestamp > unit.lastAuditAt) {
            unit.registeredInventoryMg += receivedWeightMg;
            registeredInventoryMg += receivedWeightMg;
        }

        emit UnitDepositEntryRecorded(
            depositId,
            unitId,
            receivedWeightMg / MG_PER_KG,
            responsibleParty,
            block.timestamp
        );
    }

    /// @notice Checks whether the employee can issue tokens for the deposit at that unit.
    /// @dev Read-only check; issuance and the global balance are handled by SojaCoin.
    /// @param depositId Confirmed deposit for which tokens will be issued.
    /// @param responsibleParty Employee calling the issuance function.
    function validateDepositIssuance(
        uint256 depositId,
        address responsibleParty
    ) external view {
        if (msg.sender != address(sojaCoin)) revert SojaCoinOnly();
        if (!_depositPending[depositId] || !_depositReceived[depositId]) {
            revert UnitDepositNotPending();
        }
        _requireOperationalPermission(
            unitByDeposit[depositId],
            responsibleParty,
            PERMISSION_ISSUE_SJC
        );
    }

    /// @notice Records a physical return of part of a deposit that has not yet been issued.
    /// @dev The outbound movement reduces the unit balance and registry aggregate. SojaCoin
    /// adjusts its global backing in the same transaction.
    /// @param depositId Deposit from which the returned soybeans came.
    /// @param returnedWeightMg Returned weight, in milligrams.
    /// @param responsibleParty Employee who recorded the outbound movement.
    function recordDepositReturn(
        uint256 depositId,
        uint256 returnedWeightMg,
        address responsibleParty
    ) external {
        _requireSojaCoinOnly();
        if (!_depositPending[depositId] || !_depositReceived[depositId]) {
            revert UnitDepositNotPending();
        }
        if (returnedWeightMg == 0) revert AmountInvalid();

        uint256 unitId = unitByDeposit[depositId];
        _requireOperationalPermission(
            unitId,
            responsibleParty,
            PERMISSION_COMPLETE_WITHDRAWAL
        );
        if (availableInventoryMg(unitId) < returnedWeightMg) {
            revert InsufficientAvailableInventory();
        }

        Unit storage unit = units[unitId];
        unit.registeredInventoryMg -= returnedWeightMg;
        registeredInventoryMg -= returnedWeightMg;
    }

    /// @notice Closes local tracking for a deposit without deleting its history.
    /// @dev SojaCoin calls this after issuance, cancellation before receipt, or a full return.
    /// @param depositId Identifier of the closed deposit.
    function closeDeposit(uint256 depositId) external {
        _requireSojaCoinOnly();
        if (!_depositPending[depositId]) revert UnitDepositNotPending();
        _closePendingDeposit(depositId);
    }

    /// @notice Returns the unit's inventory that is not reserved for another operation.
    /// @param unitId Unit being queried.
    /// @return amountMg Available inventory, in milligrams.
    function availableInventoryMg(
        uint256 unitId
    ) public view returns (uint256 amountMg) {
        Unit storage unit = _existingUnit(unitId);
        if (!unit.active) revert UnitIsInactive();

        uint256 reservedMg = reservedInventoryMgByUnit[unitId];
        if (reservedMg > unit.registeredInventoryMg) {
            revert ReservationsExceedInventory();
        }
        return unit.registeredInventoryMg - reservedMg;
    }

    /// @notice Records a withdrawal request and checks available inventory at the selected unit.
    /// @dev Does not reserve physical inventory until the warehouse authorizes the request.
    /// A pending request prevents deactivation of the unit while awaiting a decision.
    /// @param redemptionId Request identifier in SojaCoin.
    /// @param unitId Withdrawal unit selected by the holder.
    /// @param requestedMassMg Mass equivalent to the SJC held for the request.
    /// @param holder Wallet that receives any returned SJC.
    function recordRedemptionRequest(
        uint256 redemptionId,
        uint256 unitId,
        uint256 requestedMassMg,
        address holder
    ) external {
        _requireSojaCoinOnly();
        if (
            redemptionId == 0 ||
            unitId == 0 ||
            requestedMassMg == 0 ||
            holder == address(0)
        ) {
            revert InvalidUnitRedemption();
        }
        if (unitByRedemption[redemptionId] != 0) {
            revert UnitRedemptionAlreadyRecorded();
        }
        if (availableInventoryMg(unitId) < requestedMassMg) {
            revert InsufficientAvailableInventory();
        }

        unitByRedemption[redemptionId] = unitId;
        redemptionsByUnit[redemptionId] = UnitRedemption({
            unitId: unitId,
            requestedMassMg: requestedMassMg,
            reservedMassMg: 0,
            holder: holder,
            active: true
        });
        redemptionsPendingByUnit[unitId] += 1;
    }

    /// @notice Reserves the withdrawal weight when a unit employee authorizes the request.
    /// @dev Rechecks available inventory to prevent concurrent reservations from exceeding it.
    /// @param redemptionId Request being authorized.
    /// @param responsibleParty Employee who authorized the withdrawal.
    function authorizeRedemption(
        uint256 redemptionId,
        address responsibleParty
    ) external {
        _requireSojaCoinOnly();
        UnitRedemption storage redemption = redemptionsByUnit[redemptionId];
        if (!redemption.active || redemption.reservedMassMg != 0) {
            revert UnitRedemptionNotPending();
        }
        _requireOperationalPermission(
            redemption.unitId,
            responsibleParty,
            PERMISSION_AUTHORIZE_WITHDRAWAL
        );
        if (availableInventoryMg(redemption.unitId) < redemption.requestedMassMg) {
            revert InsufficientAvailableInventory();
        }

        redemption.reservedMassMg = redemption.requestedMassMg;
        reservedInventoryMgByUnit[redemption.unitId] +=
            redemption.reservedMassMg;
        emit UnitWithdrawalReserved(
            redemptionId,
            redemption.unitId,
            redemption.reservedMassMg,
            responsibleParty
        );
    }

    /// @notice Completes the withdrawal using the physical weight released and closes the unit reservation.
    /// @dev Owner arbitration may bypass operational permission, but not the reserved limit
    /// or the unit's physical availability.
    /// @param redemptionId Request being settled.
    /// @param deliveredMassMg Actual released weight, in milligrams.
    /// @param responsibleParty Employee or owner responsible for the record.
    /// @param arbitration True when the call comes from owner arbitration.
    function completeRedemption(
        uint256 redemptionId,
        uint256 deliveredMassMg,
        address responsibleParty,
        bool arbitration
    ) external {
        _requireSojaCoinOnly();
        UnitRedemption storage redemption = redemptionsByUnit[redemptionId];
        if (!redemption.active || redemption.reservedMassMg == 0) {
            revert UnitRedemptionNotPending();
        }
        if (deliveredMassMg == 0 || deliveredMassMg > redemption.reservedMassMg) {
            revert AmountInvalid();
        }
        if (!arbitration) {
            _requireOperationalPermission(
                redemption.unitId,
                responsibleParty,
                PERMISSION_COMPLETE_WITHDRAWAL
            );
        }

        Unit storage unit = units[redemption.unitId];
        if (unit.registeredInventoryMg < deliveredMassMg) {
            revert InsufficientAvailableInventory();
        }

        reservedInventoryMgByUnit[redemption.unitId] -=
            redemption.reservedMassMg;
        unit.registeredInventoryMg -= deliveredMassMg;
        registeredInventoryMg -= deliveredMassMg;
        redemption.active = false;
        redemptionsPendingByUnit[redemption.unitId] -= 1;

        emit UnitWithdrawalSettled(
            redemptionId,
            redemption.unitId,
            deliveredMassMg,
            responsibleParty,
            block.timestamp
        );
    }

    /// @notice Cancels a request and releases any physical reservation without reducing unit inventory.
    /// @dev The holder may cancel directly; employees need authorization permission.
    /// @param redemptionId Canceled request.
    /// @param responsibleParty Holder, employee, or owner who initiated the cancellation.
    /// @param arbitration True when the owner cancels during arbitration.
    function cancelRedemption(
        uint256 redemptionId,
        address responsibleParty,
        bool arbitration
    ) external {
        _requireSojaCoinOnly();
        UnitRedemption storage redemption = redemptionsByUnit[redemptionId];
        if (!redemption.active) revert UnitRedemptionNotPending();
        if (!arbitration && responsibleParty != redemption.holder) {
            _requireOperationalPermission(
                redemption.unitId,
                responsibleParty,
                PERMISSION_AUTHORIZE_WITHDRAWAL
            );
        }

        if (redemption.reservedMassMg != 0) {
            reservedInventoryMgByUnit[redemption.unitId] -=
                redemption.reservedMassMg;
        }
        redemption.active = false;
        redemptionsPendingByUnit[redemption.unitId] -= 1;

        emit UnitWithdrawalCanceled(
            redemptionId,
            redemption.unitId,
            responsibleParty
        );
    }

    /// @notice Records a physical load transfer between units of the same company.
    /// @dev Each ID represents one load with a dispatch and a receipt. Use separate IDs
    /// when a movement includes multiple loads. Checks company-wide and source-unit permissions
    /// and reserves the weight until dispatch or cancellation.
    /// @param sourceUnitId Unit that will send the load.
    /// @param destinationUnitId Unit that will receive the load.
    /// @param requestedAmountKg Planned weight, in whole kilograms.
    /// @param hashOrder Hash of the transfer order or document bundle.
    /// @return transferId Identifier assigned to the transfer.
    function requestInventoryTransfer(
        uint256 sourceUnitId,
        uint256 destinationUnitId,
        uint256 requestedAmountKg,
        bytes32 hashOrder
    ) external returns (uint256 transferId) {
        if (sourceUnitId == destinationUnitId) revert UnitsMustDiffer();
        if (requestedAmountKg == 0) revert AmountInvalid();
        if (hashOrder == bytes32(0)) revert InvalidHash();

        _requireTransferPermission(sourceUnitId, msg.sender);
        _existingUnit(destinationUnitId);
        if (!units[destinationUnitId].active) revert UnitIsInactive();

        uint256 amountMg = requestedAmountKg * MG_PER_KG;
        uint256 reservedInventoryMg = reservedInventoryMgByUnit[
            sourceUnitId
        ];
        Unit storage source = units[sourceUnitId];
        if (
            source.registeredInventoryMg < reservedInventoryMg ||
            source.registeredInventoryMg - reservedInventoryMg < amountMg
        ) {
            revert InsufficientAvailableInventory();
        }

        transferId = ++nextTransferId;
        transfers[transferId] = Transfer({
            sourceUnitId: sourceUnitId,
            destinationUnitId: destinationUnitId,
            requestedAmountKg: requestedAmountKg,
            dispatchedAmountKg: 0,
            receivedAmountKg: 0,
            discrepancyMg: 0,
            requestedBy: msg.sender,
            dispatchedBy: address(0),
            receivedBy: address(0),
            requestedIn: block.timestamp,
            dispatchedIn: 0,
            receivedIn: 0,
            hashOrder: hashOrder,
            outboundDocumentHash: bytes32(0),
            receiptProofHash: bytes32(0),
            status: TransferStatus.Requested
        });

        reservedInventoryMgByUnit[sourceUnitId] += amountMg;
        transfersPendingByUnit[sourceUnitId] += 1;
        transfersPendingByUnit[destinationUnitId] += 1;

        emit TransferRequested(
            transferId,
            sourceUnitId,
            destinationUnitId,
            requestedAmountKg,
            hashOrder,
            msg.sender,
            block.timestamp
        );
    }

    /// @notice Records the physical dispatch and moves the weight from the source into transit.
    /// @dev The dispatched amount may be less than requested; the unused reservation is released.
    /// Dispatched weight is in transit and cannot support new issuance until receipt is confirmed.
    /// Dispatching more than requested or reserving more than available inventory is not allowed.
    /// @param transferId Identifier of the requested transfer.
    /// @param dispatchedAmountKg Weight that actually left the source, in kilograms.
    /// @param outboundDocumentHash Hash of the off-chain dispatch documents and weighing record.
    function recordTransferDispatch(
        uint256 transferId,
        uint256 dispatchedAmountKg,
        bytes32 outboundDocumentHash
    ) external {
        Transfer storage transfer = _existingTransfer(
            transferId
        );
        if (transfer.status != TransferStatus.Requested) {
            revert InvalidTransferStatus();
        }
        _requireTransferPermission(
            transfer.sourceUnitId,
            msg.sender
        );
        if (
            dispatchedAmountKg == 0 ||
            dispatchedAmountKg > transfer.requestedAmountKg
        ) {
            revert AmountInvalid();
        }
        if (outboundDocumentHash == bytes32(0)) revert InvalidHash();

        uint256 requestedAmountMg =
            transfer.requestedAmountKg * MG_PER_KG;
        uint256 dispatchedAmountMg = dispatchedAmountKg * MG_PER_KG;
        uint256 reservationMg = reservedInventoryMgByUnit[
            transfer.sourceUnitId
        ];
        Unit storage source = units[transfer.sourceUnitId];

        if (
            reservationMg < requestedAmountMg ||
            source.registeredInventoryMg < dispatchedAmountMg
        ) {
            revert InsufficientAvailableInventory();
        }
        uint256 remainingReservationMg = reservationMg - requestedAmountMg;
        if (
            source.registeredInventoryMg - dispatchedAmountMg <
            remainingReservationMg
        ) {
            revert ReservationsExceedInventory();
        }

        reservedInventoryMgByUnit[
            transfer.sourceUnitId
        ] = remainingReservationMg;
        source.registeredInventoryMg -= dispatchedAmountMg;
        inventoryInTransitMg += dispatchedAmountMg;

        transfer.dispatchedAmountKg = dispatchedAmountKg;
        transfer.dispatchedBy = msg.sender;
        transfer.dispatchedIn = block.timestamp;
        transfer.outboundDocumentHash = outboundDocumentHash;
        transfer.status = TransferStatus.InTransit;

        sojaCoin.updateIssuanceCapAfterTransfer();

        emit TransferDispatched(
            transferId,
            dispatchedAmountKg,
            outboundDocumentHash,
            msg.sender,
            block.timestamp
        );
    }

    /// @notice Records the weight checked on arrival and closes the transfer.
    /// @dev Requires a wallet different from the one used for dispatch. Any discrepancy reduces
    /// global inventory by its exact amount; a discrepancy above 0.25% also emits an alert event.
    /// Received weight returns to the issuance cap. SojaCoin pauses if total backing becomes insufficient.
    /// @param transferId Identifier of the transfer in transit.
    /// @param receivedAmountKg Weight checked at the destination, in kilograms.
    /// @param receiptProofHash Hash of the receipt proof stored off-chain.
    function confirmTransferReceipt(
        uint256 transferId,
        uint256 receivedAmountKg,
        bytes32 receiptProofHash
    ) external {
        Transfer storage transfer = _existingTransfer(
            transferId
        );
        if (transfer.status != TransferStatus.InTransit) {
            revert InvalidTransferStatus();
        }
        _requireTransferPermission(
            transfer.destinationUnitId,
            msg.sender
        );
        if (msg.sender == transfer.dispatchedBy) {
            revert TransferOperatorsMustDiffer();
        }
        if (receivedAmountKg > transfer.dispatchedAmountKg) {
            revert ReceivedAmountExceedsDispatchedAmount();
        }
        if (receiptProofHash == bytes32(0)) revert InvalidHash();

        uint256 dispatchedAmountMg =
            transfer.dispatchedAmountKg * MG_PER_KG;
        uint256 receivedAmountMg = receivedAmountKg * MG_PER_KG;
        uint256 discrepancyMg = dispatchedAmountMg - receivedAmountMg;
        Unit storage destination = units[transfer.destinationUnitId];

        inventoryInTransitMg -= dispatchedAmountMg;
        destination.registeredInventoryMg += receivedAmountMg;
        transfer.receivedAmountKg = receivedAmountKg;
        transfer.discrepancyMg = discrepancyMg;
        transfer.receivedBy = msg.sender;
        transfer.receivedIn = block.timestamp;
        transfer.receiptProofHash = receiptProofHash;
        transfer.status = TransferStatus.Received;

        transfersPendingByUnit[
            transfer.sourceUnitId
        ] -= 1;
        transfersPendingByUnit[
            transfer.destinationUnitId
        ] -= 1;

        if (discrepancyMg > 0) {
            if (registeredInventoryMg < discrepancyMg) {
                revert InsufficientAvailableInventory();
            }
            registeredInventoryMg -= discrepancyMg;
            sojaCoin.recordTransferDiscrepancy(
                discrepancyMg,
                receiptProofHash,
                msg.sender
            );
            emit TransferDiscrepancy(
                transferId,
                transfer.dispatchedAmountKg,
                receivedAmountKg,
                discrepancyMg / MG_PER_KG,
                receiptProofHash,
                msg.sender,
                block.timestamp
            );

            uint256 alertLimitMg = _discrepancyLimitMg(
                dispatchedAmountMg
            );
            if (discrepancyMg > alertLimitMg) {
                emit TransferDiscrepancyAlert(
                    transferId,
                    transfer.dispatchedAmountKg,
                    receivedAmountKg,
                    discrepancyMg / MG_PER_KG,
                    alertLimitMg,
                    receiptProofHash,
                    msg.sender,
                    block.timestamp
                );
            }
        } else {
            sojaCoin.updateIssuanceCapAfterTransfer();
        }

        emit TransferReceived(
            transferId,
            transfer.dispatchedAmountKg,
            receivedAmountKg,
            receiptProofHash,
            msg.sender,
            block.timestamp
        );
    }

    /// @notice Cancels a requested transfer before the load is dispatched.
    /// @dev Releases the source reservation and unit counters. Dispatched transfers
    /// must be received; they cannot be canceled as if the load had never left.
    /// @param transferId Identifier of the request to cancel.
    function cancelTransfer(uint256 transferId) external {
        Transfer storage transfer = _existingTransfer(
            transferId
        );
        if (transfer.status != TransferStatus.Requested) {
            revert InvalidTransferStatus();
        }
        _requireTransferPermission(
            transfer.sourceUnitId,
            msg.sender
        );

        uint256 reservedAmountMg =
            transfer.requestedAmountKg * MG_PER_KG;
        reservedInventoryMgByUnit[
            transfer.sourceUnitId
        ] -= reservedAmountMg;
        transfersPendingByUnit[
            transfer.sourceUnitId
        ] -= 1;
        transfersPendingByUnit[
            transfer.destinationUnitId
        ] -= 1;
        transfer.status = TransferStatus.Canceled;

        emit TransferCanceled(
            transferId,
            msg.sender,
            block.timestamp
        );
    }

    /// @notice Records the physical inventory counted at one unit.
    /// @dev Only the wallet assigned as that unit's auditor can call this. The global balance
    /// is adjusted only by the difference between the previous and new counts.
    /// @param unitId Physical unit that was audited.
    /// @param physicalInventoryKg Counted physical amount, in whole kilograms.
    /// @param reportHash Hash of the audit report stored off-chain.
    function recordUnitAudit(
        uint256 unitId,
        uint256 physicalInventoryKg,
        bytes32 reportHash
    ) external {
        Unit storage unit = _existingUnit(unitId);
        if (!unit.active) revert UnitIsInactive();
        if (reportHash == bytes32(0)) revert InvalidHash();

        address auditorOfTheUnit = auditorByUnit[unitId];
        if (auditorOfTheUnit == address(0)) revert UnitAuditorNotSet();
        if (msg.sender != auditorOfTheUnit) {
            revert UnitAuditorNotAuthorized();
        }

        uint256 previousInventoryMg = unit.registeredInventoryMg;
        uint256 newInventoryMg = physicalInventoryKg * MG_PER_KG;
        unit.registeredInventoryMg = newInventoryMg;
        unit.lastReportHash = reportHash;
        unit.lastAuditAt = block.timestamp;

        if (newInventoryMg >= previousInventoryMg) {
            registeredInventoryMg += newInventoryMg - previousInventoryMg;
        } else {
            registeredInventoryMg -= previousInventoryMg - newInventoryMg;
        }

        _recalculateLastAudit();
        sojaCoin.syncInventoryFromUnits(
            registeredInventoryMg,
            reportHash,
            msg.sender,
            lastAuditAt
        );

        emit UnitAudited(
            unitId,
            msg.sender,
            physicalInventoryKg,
            reportHash,
            block.timestamp
        );
    }

    /// @notice Returns whether every active unit has a still-valid audit.
    /// @dev The oldest audit timestamp is stored in lastAuditAt.
    function allUnitAuditsCurrent() external view returns (bool) {
        return
            lastAuditAt != 0 &&
            block.timestamp <= lastAuditAt + AUDIT_PERIOD;
    }

    /// @notice Gets an existing unit for internal reads or updates.
    /// @param unitId Expected identifier of a registered unit.
    /// @return unit Reference to the unit structure.
    function _existingUnit(
        uint256 unitId
    ) private view returns (Unit storage unit) {
        unit = units[unitId];
        if (unit.code == bytes32(0)) revert UnitDoesNotExist();
    }

    /// @notice Recalculates the oldest audit timestamp among active units.
    /// @dev If any unit has no audit report, the global timestamp remains zero and
    /// issuance stays blocked until every unit has been audited.
    function _recalculateLastAudit() private {
        uint256 dateMoreOld = type(uint256).max;

        for (uint256 unitId = 1; unitId <= nextUnitId; unitId++) {
            Unit storage unit = units[unitId];
            if (!unit.active) continue;
            if (unit.lastAuditAt == 0) {
                lastAuditAt = 0;
                return;
            }
            if (unit.lastAuditAt < dateMoreOld) {
                dateMoreOld = unit.lastAuditAt;
            }
        }

        lastAuditAt =
            dateMoreOld == type(uint256).max ? 0 : dateMoreOld;
    }

    /// @notice Updates SojaCoin's validity after a unit is activated or deactivated.
    /// @dev The registry may be created before it is connected to SojaCoin; in that case,
    /// no update occurs until the owner configures the link.
    function _syncAuditValidityWithSojaCoin() private {
        if (sojaCoin.unitRegistry() == address(this)) {
            sojaCoin.updateAuditValidityFromUnits(lastAuditAt);
        }
    }

    /// @notice Ensures a balance change comes from the linked SojaCoin.
    function _requireSojaCoinOnly() private view {
        if (msg.sender != address(sojaCoin)) revert SojaCoinOnly();
    }

    /// @notice Requires a specific operational permission for the active unit.
    function _requireOperationalPermission(
        uint256 unitId,
        address wallet,
        uint256 permission
    ) private view {
        if (
            !_hasPermissionForUnit(unitId, wallet, permission)
        ) {
            revert InsufficientOperationalPermission();
        }
    }

    /// @notice Closes a pending deposit and allows the unit to be closed later.
    function _closePendingDeposit(uint256 depositId) private {
        uint256 unitId = unitByDeposit[depositId];
        _depositPending[depositId] = false;
        pendingDepositsByUnit[unitId] -= 1;
    }

    /// @notice Gets an existing transfer for internal validation.
    /// @param transferId Identifier of the queried transfer.
    /// @return transfer Transfer record.
    function _existingTransfer(
        uint256 transferId
    ) private view returns (Transfer storage transfer) {
        transfer = transfers[transferId];
        if (transfer.status == TransferStatus.Nonexistent) {
            revert TransferDoesNotExist();
        }
    }

    /// @notice Requires the wallet to have transfer permission for the active unit.
    /// @param unitId Unit where the operation will take place.
    /// @param wallet Wallet calling the function.
    function _requireTransferPermission(
        uint256 unitId,
        address wallet
    ) private view {
        if (
            !_hasPermissionForUnit(
                unitId,
                wallet,
                PERMISSION_TRANSFER_INVENTORY
            )
        ) {
            revert InsufficientTransferPermission();
        }
    }

    /// @notice Calculates the fixed transfer alert threshold in milligrams.
    /// @dev Divides before multiplying to avoid overflow. The alert is triggered
    /// only when the discrepancy exceeds 0.25% of the dispatched weight.
    /// @param dispatchedAmountMg Dispatched weight, in milligrams.
    /// @return limitMg Maximum discrepancy before an alert, in milligrams.
    function _discrepancyLimitMg(
        uint256 dispatchedAmountMg
    ) private pure returns (uint256 limitMg) {
        uint256 wholePart = dispatchedAmountMg / BPS_DENOMINATOR;
        uint256 remainder = dispatchedAmountMg % BPS_DENOMINATOR;
        limitMg =
            wholePart * DISCREPANCY_ALERT_LIMIT_BPS +
            (remainder * DISCREPANCY_ALERT_LIMIT_BPS) / BPS_DENOMINATOR;
    }

    /// @notice Checks whether the effective permission mask separates critical operation steps.
    /// @dev Receiving and issuing SJC, or authorizing and completing a withdrawal, require
    /// different wallets within the same unit.
    /// @param permissions Company-wide mask combined with the unit-specific mask.
    function _validateRoleSeparation(uint256 permissions) private pure {
        bool receivesSoybeans = (permissions & PERMISSION_RECEIVE_SOYBEANS) != 0;
        bool issuesSJC = (permissions & PERMISSION_ISSUE_SJC) != 0;
        bool authorizesWithdrawal = (permissions & PERMISSION_AUTHORIZE_WITHDRAWAL) != 0;
        bool completesWithdrawal = (permissions & PERMISSION_COMPLETE_WITHDRAWAL) != 0;

        if ((receivesSoybeans && issuesSJC) || (authorizesWithdrawal && completesWithdrawal)) {
            revert RoleSeparationConflict();
        }
    }

    /// @notice Checks company-wide permissions against existing unit-specific roles.
    /// @dev Because a company-wide permission applies to every unit, it cannot complete
    /// a conflicting role pair that already exists for any specific unit.
    /// @param wallet Wallet whose company-wide permissions will be updated.
    /// @param permissions Proposed company-wide permission mask.
    function _validateGeneralPermissionsWithoutConflict(address wallet, uint256 permissions) private view {
        _validateRoleSeparation(permissions);

        if (
            ((permissions & PERMISSION_RECEIVE_SOYBEANS) != 0 && _unitsWithIssuanceSpecific[wallet] != 0) ||
            ((permissions & PERMISSION_ISSUE_SJC) != 0 && _unitsWithReceiptSpecific[wallet] != 0) ||
            (
                (permissions & PERMISSION_AUTHORIZE_WITHDRAWAL) != 0 &&
                _unitsWithCompletionWithdrawalSpecific[wallet] != 0
            ) ||
            (
                (permissions & PERMISSION_COMPLETE_WITHDRAWAL) != 0 &&
                _unitsWithAuthorizationWithdrawalSpecific[wallet] != 0
            )
        ) {
            revert RoleSeparationConflict();
        }
    }

    /// @notice Maintains counters used to validate company-wide permissions without iterating over units.
    /// @param wallet Wallet whose unit-specific permissions changed.
    /// @param permissionsPrevious Unit-specific mask before the change.
    /// @param newPermissions Unit-specific mask after the change.
    function _updateSpecificPermissionCounters(
        address wallet,
        uint256 permissionsPrevious,
        uint256 newPermissions
    ) private {
        _unitsWithReceiptSpecific[wallet] = _updatedCounter(
            _unitsWithReceiptSpecific[wallet],
            permissionsPrevious,
            newPermissions,
            PERMISSION_RECEIVE_SOYBEANS
        );
        _unitsWithIssuanceSpecific[wallet] = _updatedCounter(
            _unitsWithIssuanceSpecific[wallet],
            permissionsPrevious,
            newPermissions,
            PERMISSION_ISSUE_SJC
        );
        _unitsWithAuthorizationWithdrawalSpecific[wallet] = _updatedCounter(
            _unitsWithAuthorizationWithdrawalSpecific[wallet],
            permissionsPrevious,
            newPermissions,
            PERMISSION_AUTHORIZE_WITHDRAWAL
        );
        _unitsWithCompletionWithdrawalSpecific[wallet] = _updatedCounter(
            _unitsWithCompletionWithdrawalSpecific[wallet],
            permissionsPrevious,
            newPermissions,
            PERMISSION_COMPLETE_WITHDRAWAL
        );
    }

    /// @notice Adjusts a unit-specific permission counter when a bit changes.
    /// @dev Solidity does not allow a storage reference to be passed to a simple internal
    /// function, so this function returns the updated counter.
    function _updatedCounter(
        uint256 count,
        uint256 permissionsPrevious,
        uint256 newPermissions,
        uint256 permission
    ) private pure returns (uint256) {
        bool hadPermission = (permissionsPrevious & permission) != 0;
        bool hasPermissionAfter = (newPermissions & permission) != 0;
        if (!hadPermission && hasPermissionAfter) return count + 1;
        if (hadPermission && !hasPermissionAfter) return count - 1;
        return count;
    }

    /// @notice Returns whether a permission mask contains a role incompatible with auditing.
    /// @dev Managing services does not affect inventory or issuance and may coexist with auditing.
    /// Other permissions move soybeans, affect backing, or authorize their release.
    /// @param permissions Permission mask to check.
    /// @return hasPermission True if any incompatible permission is present.
    function _hasAuditConflictingPermission(
        uint256 permissions
    ) private pure returns (bool hasPermission) {
        return (permissions & AUDIT_CONFLICTING_PERMISSIONS_MASK) != 0;
    }

    /// @notice Returns company-wide and unit-specific permissions for an active unit.
    /// @param unitId Unit being queried.
    /// @param wallet Wallet being queried.
    /// @param permission Specific permission to check.
    /// @return hasPermission True if the wallet has the requested permission.
    function _hasPermissionForUnit(
        uint256 unitId,
        address wallet,
        uint256 permission
    ) private view returns (bool hasPermission) {
        Unit storage unit = units[unitId];
        if (
            unit.code == bytes32(0) ||
            !unit.active ||
            permission == 0 ||
            (permission & ~VALID_PERMISSIONS_MASK) != 0
        ) {
            return false;
        }

        uint256 permissions =
            permissionsGeneral[wallet] |
            permissionsByUnit[unitId][wallet];
        return (permissions & permission) == permission;
    }
}
