// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {UnitRegistry} from "../src/contracts/UnitRegistry.sol";
import {SojaCoinTestBase} from "./helpers/SojaCoinTestBase.sol";

contract UnitRegistryTest is SojaCoinTestBase {
    address private employee = address(0x1111);
    address private otherEmployee = address(0x2222);

    function test_unitIdentifiersAreSequentialAndCodesCannotRepeat() public {
        _eq(registry.nextUnitId(), 2, "two fixture units exist");
        _eq(registry.unitIdByCode(bytes32("UNIT-ONE")), 1, "first code maps to first unit");
        vm.expectRevert(UnitRegistry.UnitCodeAlreadyUsed.selector);
        registry.registerUnit(bytes32("UNIT-ONE"), keccak256("different registration"));
        vm.expectRevert(UnitRegistry.InvalidUnitCode.selector);
        registry.registerUnit(bytes32(0), keccak256("registration"));
        vm.expectRevert(UnitRegistry.InvalidHash.selector);
        registry.registerUnit(bytes32("UNIT-THREE"), bytes32(0));
    }

    function test_auditorCannotReceiveInventoryPermissionsEvenWhenUnitIsInactive() public {
        vm.expectRevert(UnitRegistry.AuditPermissionConflict.selector);
        registry.setPermissions(1, auditor, 1);

        registry.updateUnit(2, keccak256("updated unit two"), false);
        vm.expectRevert(UnitRegistry.AuditPermissionConflict.selector);
        registry.setPermissions(0, auditor, 32);

        registry.removeUnitAuditor(2);
        registry.setUnitAuditor(1, address(0xA0D1));
        registry.setPermissions(0, auditor, registry.PERMISSION_TRANSFER_INVENTORY());
        require(
            registry.hasPermissionForUnit(1, auditor, registry.PERMISSION_TRANSFER_INVENTORY()),
            "general permission applies after auditor removal"
        );
    }

    function test_roleSeparationBlocksReceiveAndIssueOrAuthorizeAndComplete() public {
        vm.expectRevert(UnitRegistry.RoleSeparationConflict.selector);
        registry.setPermissions(1, receiver, 1 | 2);

        vm.expectRevert(UnitRegistry.RoleSeparationConflict.selector);
        registry.setPermissions(
            1,
            employee,
            4 | 8
        );

        registry.setPermissions(0, employee, registry.PERMISSION_AUTHORIZE_WITHDRAWAL());
        vm.expectRevert(UnitRegistry.RoleSeparationConflict.selector);
        registry.setPermissions(1, employee, 8);
    }

    function test_auditPermissionAndAggregateInventoryAreEnforced() public {
        vm.expectRevert(UnitRegistry.UnitAuditorNotAuthorized.selector);
        vm.prank(employee);
        registry.recordUnitAudit(1, 900, keccak256("unauthorized audit"));

        vm.expectRevert(UnitRegistry.InvalidHash.selector);
        vm.prank(auditor);
        registry.recordUnitAudit(1, 900, bytes32(0));

        vm.prank(auditor);
        registry.recordUnitAudit(1, 1_000, keccak256("valid audit one"));
        _eq(_unitInventory(1), 1_000 * 1_000_000, "unit inventory updated");
        _eq(registry.registeredInventoryMg(), 1_000 * 1_000_000, "aggregate inventory updated");
        _eq(coin.registeredInventoryMg(), 1_000 * 1_000_000, "SojaCoin backing synchronized");

        vm.prank(auditor);
        registry.recordUnitAudit(1, 700, keccak256("lower audit count"));
        _eq(registry.registeredInventoryMg(), 700 * 1_000_000, "aggregate changes by the physical difference");
    }

    function test_unitCannotBeDeactivatedWithInventoryOrPendingTransfer() public {
        vm.prank(auditor);
        registry.recordUnitAudit(1, 100, keccak256("unit inventory"));
        vm.expectRevert(UnitRegistry.UnitHasInventory.selector);
        registry.updateUnit(1, keccak256("deactivate"), false);

        vm.prank(auditor);
        registry.recordUnitAudit(1, 100, keccak256("stock for pending transfer"));
        registry.setPermissions(1, employee, registry.PERMISSION_TRANSFER_INVENTORY());
        vm.prank(employee);
        uint256 transferId = registry.requestInventoryTransfer(1, 2, 100, keccak256("transfer order"));
        vm.prank(employee);
        registry.recordTransferDispatch(transferId, 100, keccak256("transfer dispatched"));
        vm.expectRevert(UnitRegistry.UnitHasPendingTransfer.selector);
        registry.updateUnit(1, keccak256("deactivate with transfer"), false);
    }

    function test_transferDispatchAndReceiptRecordExactLossAndReleaseTransit() public {
        vm.prank(auditor);
        registry.recordUnitAudit(1, 1_000, keccak256("inventory before transfer"));
        address sourceOperator = address(0x3333);
        address destinationOperator = address(0x4444);
        registry.setPermissions(1, sourceOperator, registry.PERMISSION_TRANSFER_INVENTORY());
        registry.setPermissions(2, destinationOperator, registry.PERMISSION_TRANSFER_INVENTORY());

        vm.prank(sourceOperator);
        uint256 transferId = registry.requestInventoryTransfer(1, 2, 100, keccak256("order-100kg"));
        _eq(registry.reservedInventoryMgByUnit(1), 100 * 1_000_000, "source stock reserved");

        vm.prank(sourceOperator);
        registry.recordTransferDispatch(transferId, 100, keccak256("dispatch-doc"));
        _eq(registry.inventoryInTransitMg(), 100 * 1_000_000, "dispatched amount is in transit");
        _eq(_unitInventory(1), 900 * 1_000_000, "source inventory decreases at dispatch");

        vm.prank(destinationOperator);
        registry.confirmTransferReceipt(transferId, 98, keccak256("receipt-proof"));
        _eq(registry.inventoryInTransitMg(), 0, "receipt clears transit amount");
        _eq(_unitInventory(2), 98 * 1_000_000, "destination receives measured amount");
        _eq(registry.registeredInventoryMg(), 998 * 1_000_000, "global stock reduces by exact 2 kg discrepancy");
        _eq(coin.registeredInventoryMg(), 998 * 1_000_000, "SojaCoin backing reflects the same discrepancy");
        _eq(registry.transfersPendingByUnit(1), 0, "source pending count clears");
        _eq(registry.transfersPendingByUnit(2), 0, "destination pending count clears");
    }

    function test_transferRequiresDifferentDispatchAndReceiptWallets() public {
        vm.prank(auditor);
        registry.recordUnitAudit(1, 100, keccak256("inventory for separation test"));
        address sharedOperator = address(0x5555);
        registry.setPermissions(1, sharedOperator, registry.PERMISSION_TRANSFER_INVENTORY());
        registry.setPermissions(2, sharedOperator, registry.PERMISSION_TRANSFER_INVENTORY());

        vm.prank(sharedOperator);
        uint256 transferId = registry.requestInventoryTransfer(1, 2, 10, keccak256("separation order"));
        vm.prank(sharedOperator);
        registry.recordTransferDispatch(transferId, 10, keccak256("separation dispatch"));

        vm.expectRevert(UnitRegistry.TransferOperatorsMustDiffer.selector);
        vm.prank(sharedOperator);
        registry.confirmTransferReceipt(transferId, 10, keccak256("same operator receipt"));
    }

    function test_cancelBeforeDispatchReleasesReservation() public {
        vm.prank(auditor);
        registry.recordUnitAudit(1, 50, keccak256("inventory for cancel"));
        registry.setPermissions(1, employee, registry.PERMISSION_TRANSFER_INVENTORY());

        vm.prank(employee);
        uint256 transferId = registry.requestInventoryTransfer(1, 2, 20, keccak256("cancel order"));
        vm.prank(employee);
        registry.cancelTransfer(transferId);

        _eq(registry.reservedInventoryMgByUnit(1), 0, "reservation released");
        _eq(registry.transfersPendingByUnit(1), 0, "source pending counter released");
        _eq(registry.transfersPendingByUnit(2), 0, "destination pending counter released");
        _eq(registry.inventoryInTransitMg(), 0, "canceled load was not dispatched");
    }

    function test_invalidPermissionsAndInactiveUnitAreRejected() public {
        vm.expectRevert(UnitRegistry.InvalidPermission.selector);
        registry.setPermissions(1, employee, 1 << 20);

        registry.updateUnit(2, keccak256("inactive unit"), false);
        require(!registry.hasPermissionForUnit(2, receiver, registry.PERMISSION_RECEIVE_SOYBEANS()), "inactive unit has no operational permission");
        vm.expectRevert(UnitRegistry.UnitIsInactive.selector);
        vm.prank(auditor);
        registry.recordUnitAudit(2, 0, keccak256("audit inactive"));
    }

    function _unitInventory(uint256 unitId) private view returns (uint256 inventoryMg) {
        (,,, inventoryMg,,) = registry.units(unitId);
    }
}
