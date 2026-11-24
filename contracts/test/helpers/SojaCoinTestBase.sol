// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {SojaCoin} from "../../src/contracts/SojaCoin.sol";
import {SojaCoinStorage} from "../../src/contracts/storage/SojaCoinStorage.sol";
import {
    SojaCoinAdministration,
    SojaCoinDeposits,
    SojaCoinCPR,
    SojaCoinServices,
    SojaCoinLocks,
    SojaCoinRedemptions
} from "../../src/contracts/SojaCoinModules.sol";
import {UnitRegistry} from "../../src/contracts/UnitRegistry.sol";
import {TokenIssuanceBatches} from "../../src/contracts/modules/TokenIssuanceBatches.sol";

interface VmSojaCoinBase {
    function addr(uint256 privateKey) external returns (address);
    function sign(uint256 privateKey, bytes32 digest) external returns (uint8 v, bytes32 r, bytes32 s);
    function prank(address caller) external;
    function warp(uint256 timestamp) external;
    function expectRevert(bytes calldata revertData) external;
    function expectRevert(bytes4 revertData) external;
}

abstract contract SojaCoinTestBase {
    VmSojaCoinBase internal constant vm = VmSojaCoinBase(address(uint160(uint256(keccak256("hevm cheat code")))));

    uint256 internal constant PRODUCER_PK = 0xA11CE;
    uint256 internal constant CREDITOR_PK = 0xB0B;
    uint256 internal constant AUDITOR_PK = 0xA0D17;
    uint256 internal constant RECEIVER_PK = 0xCECE1;
    uint256 internal constant ISSUER_PK = 0x155E3;
    uint256 internal constant AUTHORIZER_PK = 0xA07041;
    uint256 internal constant COMPLETER_PK = 0xC0A1E7E;
    uint256 internal constant SERVICE_MANAGER_PK = 0x5E271CE;
    uint256 internal constant SERVICE_RECIPIENT_PK = 0x5E271CE2;
    uint256 internal constant UNITS_PER_SACK = 1_000_000;

    bytes32 internal constant CROP_YEAR = keccak256("2026/2027");

    SojaCoin internal coin;
    UnitRegistry internal registry;
    TokenIssuanceBatches internal batches;
    address internal producer;
    address internal creditor;
    address internal auditor;
    address internal receiver;
    address internal issuer;
    address internal authorizer;
    address internal completer;
    address internal serviceManager;
    address internal serviceRecipient;

    function setUp() public virtual {
        vm.warp(1_800_000_000);
        producer = vm.addr(PRODUCER_PK);
        creditor = vm.addr(CREDITOR_PK);
        auditor = vm.addr(AUDITOR_PK);
        receiver = vm.addr(RECEIVER_PK);
        issuer = vm.addr(ISSUER_PK);
        authorizer = vm.addr(AUTHORIZER_PK);
        completer = vm.addr(COMPLETER_PK);
        serviceManager = vm.addr(SERVICE_MANAGER_PK);
        serviceRecipient = vm.addr(SERVICE_RECIPIENT_PK);

        SojaCoinAdministration administration = new SojaCoinAdministration();
        SojaCoinDeposits deposits = new SojaCoinDeposits();
        SojaCoinCPR cpr = new SojaCoinCPR();
        SojaCoinServices services = new SojaCoinServices();
        SojaCoinLocks locks = new SojaCoinLocks();
        SojaCoinRedemptions redemptions = new SojaCoinRedemptions();

        coin = new SojaCoin(
            address(this),
            address(administration),
            address(deposits),
            address(cpr),
            address(services),
            address(locks),
            address(redemptions)
        );
        registry = new UnitRegistry(address(this), address(coin));
        batches = new TokenIssuanceBatches(address(coin));
        coin.configureUnitRegistry(address(registry));
        coin.configureIssuanceBatches(address(batches));

        registry.registerUnit(bytes32("UNIT-ONE"), keccak256("registration unit one"));
        registry.registerUnit(bytes32("UNIT-TWO"), keccak256("registration unit two"));
        registry.setUnitAuditor(1, auditor);
        registry.setUnitAuditor(2, auditor);
        _setUnitPermission(1, receiver, registry.PERMISSION_RECEIVE_SOYBEANS());
        _setUnitPermission(1, issuer, registry.PERMISSION_ISSUE_SJC());
        _setUnitPermission(1, authorizer, registry.PERMISSION_AUTHORIZE_WITHDRAWAL());
        _setUnitPermission(1, completer, registry.PERMISSION_COMPLETE_WITHDRAWAL());
        _setUnitPermission(1, serviceManager, registry.PERMISSION_MANAGE_SERVICES());
        _setUnitPermission(2, receiver, registry.PERMISSION_RECEIVE_SOYBEANS());
        _setUnitPermission(2, issuer, registry.PERMISSION_ISSUE_SJC());

        vm.prank(auditor);
        registry.recordUnitAudit(1, 0, keccak256("initial audit unit one"));
        vm.prank(auditor);
        registry.recordUnitAudit(2, 0, keccak256("initial audit unit two"));
        vm.warp(block.timestamp + 1);

        _registerParticipant(producer, PRODUCER_PK);
        _registerParticipant(creditor, CREDITOR_PK);
        _registerParticipant(serviceRecipient, SERVICE_RECIPIENT_PK);
    }

    function _setUnitPermission(uint256 unitId, address wallet, uint256 permission) internal {
        registry.setPermissions(unitId, wallet, permission);
    }

    function _registerParticipant(address participant, uint256 privateKey) internal {
        coin.proposeParticipantRegistration(participant, keccak256(abi.encode("terms", participant)));
        uint256 deadline = block.timestamp + 1 days;
        bytes32 digest = coin.participationTermsAcceptanceHash(participant, deadline);
        coin.acceptParticipationTerms(participant, deadline, _sign(privateKey, digest));
    }

    function _sign(uint256 privateKey, bytes32 digest) internal returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey, digest);
        return abi.encodePacked(r, s, v);
    }

    function _ticket(uint256 grossKg, uint256 tareKg, uint256 deductionKg, bytes32 ticketHash)
        internal
        view
        returns (SojaCoinStorage.WeighingTicket memory)
    {
        return SojaCoinStorage.WeighingTicket({
            grossWeightKg: grossKg,
            tareKg: tareKg,
            moistureBps: 0,
            impuritiesBps: 0,
            damagedGrainBps: 0,
            moistureDeductionKg: deductionKg,
            impuritiesDeductionKg: 0,
            damagedGrainDeductionKg: 0,
            otherDeductionsKg: 0,
            weighingTicketHash: ticketHash,
            reportHash: keccak256(abi.encode("report", ticketHash)),
            timestamp: block.timestamp
        });
    }

    function _issueLoad(uint256 unitId, uint256 netWeightKg, bytes32 ticketHash)
        internal
        returns (uint256 depositId)
    {
        vm.prank(receiver);
        depositId = coin.openLoadReceipt(producer, unitId, CROP_YEAR);
        vm.prank(receiver);
        coin.linkTaxInvoice(depositId, keccak256(abi.encode("invoice", depositId)));
        vm.prank(receiver);
        coin.confirmLoadReceipt(depositId, _ticket(netWeightKg, 0, 0, ticketHash));

        uint256[] memory depositIds = new uint256[](1);
        depositIds[0] = depositId;
        vm.prank(issuer);
        uint256 batchId = batches.createIssuanceBatch(depositIds, block.timestamp + 1 days);
        bytes32 digest = batches.issuanceBatchAuthorizationHash(batchId);
        batches.approveIssuanceBatch(batchId, _sign(PRODUCER_PK, digest));

        vm.prank(issuer);
        coin.issueTokens(depositId);
    }

    function _eq(uint256 actual, uint256 expected, string memory message) internal pure {
        require(actual == expected, message);
    }
}
