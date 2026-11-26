// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import {SojaCoin} from "../src/contracts/SojaCoin.sol";
import {SojaCoinStorage} from "../src/contracts/storage/SojaCoinStorage.sol";
import {
    SojaCoinAdministration,
    SojaCoinDeposits,
    SojaCoinCPR,
    SojaCoinServices,
    SojaCoinLocks,
    SojaCoinRedemptions
} from "../src/contracts/SojaCoinModules.sol";
import {IUnitRegistry} from "../src/contracts/interfaces/IUnitRegistry.sol";
import {ITokenIssuanceBatches} from "../src/contracts/interfaces/ITokenIssuanceBatches.sol";

interface VmCPR {
    function warp(uint256 timestamp) external;
    function addr(uint256 privateKey) external returns (address);
    function sign(uint256 privateKey, bytes32 digest) external returns (uint8 v, bytes32 r, bytes32 s);
    function prank(address caller) external;
    function expectRevert(bytes calldata revertData) external;
}

contract MockUnitRegistryForCPR is IUnitRegistry {
    address public immutable override sojaCoin;
    uint256 public override registeredInventoryMg;
    uint256 public override inventoryInTransitMg;
    uint256 public override lastAuditAt;

    mapping(uint256 => bool) private _active;
    mapping(uint256 => uint256) private _unitByDeposit;
    mapping(uint256 => bool) private _depositActive;

    constructor(address token) {
        sojaCoin = token;
        _active[1] = true;
        _active[2] = true;
    }

    function setInventoryInitial(uint256 inventoryMG, uint256 audit) external {
        registeredInventoryMg = inventoryMG;
        lastAuditAt = audit;
    }

    function PERMISSION_MANAGE_SERVICES() external pure override returns (uint256) { return 16; }
    function PERMISSION_RECEIVE_SOYBEANS() external pure override returns (uint256) { return 1; }

    function units(uint256 unitId)
        external
        view
        override
        returns (bytes32 code, bytes32 registrationHash, bytes32 lastReportHash, uint256 inventoryMG, uint256 audit, bool active)
    {
        return (bytes32(unitId), bytes32(0), bytes32(0), 0, 0, _active[unitId]);
    }

    function hasPermissionForUnit(uint256, address, uint256) external pure override returns (bool) { return true; }

    function registerPendingDeposit(uint256 depositId, uint256 unitId, address) external override {
        require(msg.sender == sojaCoin && _active[unitId], "mock: unit nao permitida");
        require(!_depositActive[depositId], "mock: deposito repetido");
        _depositActive[depositId] = true;
        _unitByDeposit[depositId] = unitId;
    }

    function cancelPendingDeposit(uint256 depositId) external override {
        require(msg.sender == sojaCoin, "mock: chamador invalido");
        _depositActive[depositId] = false;
    }

    function confirmDepositReceipt(uint256 depositId, uint256 weightMG, uint256, address) external override {
        require(msg.sender == sojaCoin && _depositActive[depositId], "mock: deposito nao aberto");
        registeredInventoryMg += weightMG;
    }

    function validateDepositIssuance(uint256 depositId, address) external view override {
        require(msg.sender == sojaCoin && _depositActive[depositId], "mock: deposito nao valido");
    }

    function recordDepositReturn(uint256 depositId, uint256 weightMG, address) external override {
        require(msg.sender == sojaCoin && _depositActive[depositId], "mock: deposito nao valido");
        registeredInventoryMg -= weightMG;
    }

    function closeDeposit(uint256 depositId) external override {
        require(msg.sender == sojaCoin, "mock: chamador invalido");
        _depositActive[depositId] = false;
    }

    function availableInventoryMg(uint256) external pure override returns (uint256) { return 0; }
    function recordRedemptionRequest(uint256, uint256, uint256, address) external pure override {}
    function authorizeRedemption(uint256, address) external pure override {}
    function completeRedemption(uint256, uint256, address, bool) external pure override {}
    function cancelRedemption(uint256, address, bool) external pure override {}
}

contract MockIssuanceBatchesForCPR is ITokenIssuanceBatches {
    address public immutable override sojaCoin;
    mapping(uint256 => bool) private _approved;

    constructor(address token) { sojaCoin = token; }
    function approve(uint256 depositId) external { _approved[depositId] = true; }
    function isDepositInApprovedBatch(uint256 depositId) external view override returns (bool) { return _approved[depositId]; }
    function isDepositLocked(uint256) external pure override returns (bool) { return false; }
    function batchCreatorForDeposit(uint256) external pure override returns (address) { return address(0); }
    function recordIssuance(uint256) external view override { require(msg.sender == sojaCoin, "mock: token invalido"); }
}

contract SojaCoinCPRTest {
    VmCPR private constant vm = VmCPR(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 private constant PRODUCER_PK = 0xA11CE;
    uint256 private constant CREDITOR_A_PK = 0xB0B;
    uint256 private constant CREDITOR_B_PK = 0xB0C;
    uint256 private constant CREDITOR_C_PK = 0xB0D;
    uint256 private constant EMPLOYEE_PK = 0xB0E;
    uint256 private constant UNITS_PER_SACK = 1_000_000;
    bytes32 private constant CROP_YEAR = keccak256("Safra 2026/2027");

    SojaCoin private coin;
    MockUnitRegistryForCPR private registry;
    MockIssuanceBatchesForCPR private batches;
    address private producer;
    address private creditorA;
    address private creditorB;
    address private creditorC;
    address private employee;

    function setUp() public {
        vm.warp(1_800_000_000);
        producer = vm.addr(PRODUCER_PK);
        creditorA = vm.addr(CREDITOR_A_PK);
        creditorB = vm.addr(CREDITOR_B_PK);
        creditorC = vm.addr(CREDITOR_C_PK);
        employee = vm.addr(EMPLOYEE_PK);

        SojaCoinAdministration adminModule = new SojaCoinAdministration();
        SojaCoinDeposits depositsModule = new SojaCoinDeposits();
        SojaCoinCPR cprModule = new SojaCoinCPR();
        SojaCoinServices servicesModule = new SojaCoinServices();
        SojaCoinLocks locksModule = new SojaCoinLocks();
        SojaCoinRedemptions redemptionsModule = new SojaCoinRedemptions();

        coin = new SojaCoin(
            address(this),
            address(adminModule),
            address(depositsModule),
            address(cprModule),
            address(servicesModule),
            address(locksModule),
            address(redemptionsModule)
        );
        registry = new MockUnitRegistryForCPR(address(coin));
        batches = new MockIssuanceBatchesForCPR(address(coin));
        coin.configureUnitRegistry(address(registry));
        coin.configureIssuanceBatches(address(batches));
        registry.setInventoryInitial(1_000_000_000_000_000, block.timestamp);

        _register(producer, PRODUCER_PK);
        _register(creditorA, CREDITOR_A_PK);
        _register(creditorB, CREDITOR_B_PK);
        _register(creditorC, CREDITOR_C_PK);
        _register(employee, EMPLOYEE_PK);
    }

    function test_paidOffCPRAndExcessGoToProducer() public {
        uint256 cprId = _recordCPR(7_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);

        uint256 depositId = _depositAndIssue(producer, 1, CROP_YEAR, 8_000 * 60, bytes32(uint256(1)));
        SojaCoin.CPRDebt memory cpr = coin.cprDetails(cprId);

        _eq(coin.balanceOf(creditorA), 7_000 * UNITS_PER_SACK, "credor recebe 7.000");
        _eq(coin.balanceOf(producer), 1_000 * UNITS_PER_SACK, "producer recebe excedente");
        _eq(coin.balanceOf(address(this)), 0, "dono nao recebe por administrar");
        _eq(cpr.outstandingBalanceSJC, 0, "saldo quitado");
        _eq(uint256(cpr.status), 3, "estado PaidOff");
        _eq(coin.repaymentCPRByDeposit(depositId, cprId), 7_000 * UNITS_PER_SACK, "abatimento gravado");
    }

    function test_partialDeliveryReducesCPRBalance() public {
        uint256 cprId = _recordCPR(10_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);

        uint256 depositId = _depositAndIssue(producer, 1, CROP_YEAR, 3_000 * 60, bytes32(uint256(2)));
        SojaCoin.CPRDebt memory cpr = coin.cprDetails(cprId);

        _eq(cpr.outstandingBalanceSJC, 7_000 * UNITS_PER_SACK, "saldo restante");
        _eq(coin.balanceOf(creditorA), 3_000 * UNITS_PER_SACK, "credor recebe 3.000");
        _eq(coin.balanceOf(producer), 0, "sem excedente");
        _eq(coin.repaymentCPRByDeposit(depositId, cprId), 3_000 * UNITS_PER_SACK, "abatimento gravado");
    }

    function test_compatibleCPRsAreRepaidInQueueOrder() public {
        uint256 cprA = _recordCPR(7_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        uint256 cprB = _recordCPR(5_000, 1, CROP_YEAR, 2, creditorB, block.timestamp + 30 days);

        uint256 depositId = _depositAndIssue(producer, 1, CROP_YEAR, 8_000 * 60, bytes32(uint256(3)));
        SojaCoin.CPRDebt memory cprAData = coin.cprDetails(cprA);
        SojaCoin.CPRDebt memory cprBData = coin.cprDetails(cprB);

        _eq(cprAData.outstandingBalanceSJC, 0, "CPR A quitada");
        _eq(cprBData.outstandingBalanceSJC, 4_000 * UNITS_PER_SACK, "CPR B com saldo 4.000");
        _eq(coin.balanceOf(creditorA), 7_000 * UNITS_PER_SACK, "credor A recebe 7.000");
        _eq(coin.balanceOf(creditorB), 1_000 * UNITS_PER_SACK, "credor B recebe 1.000");
        _eq(coin.balanceOf(producer), 0, "producer sem excedente");
        _eq(coin.repaymentCPRByDeposit(depositId, cprB), 1_000 * UNITS_PER_SACK, "abatimento B gravado");
    }

    function test_depositFromAnotherUnitDoesNotRepayIncompatibleCPR() public {
        uint256 cprA = _recordCPR(7_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        uint256 cprB = _recordCPR(5_000, 2, CROP_YEAR, 2, creditorB, block.timestamp + 30 days);

        _depositAndIssue(producer, 1, CROP_YEAR, 8_000 * 60, bytes32(uint256(4)));
        SojaCoin.CPRDebt memory cprAData = coin.cprDetails(cprA);
        SojaCoin.CPRDebt memory cprBData = coin.cprDetails(cprB);

        _eq(cprAData.outstandingBalanceSJC, 0, "CPR A quitada");
        _eq(cprBData.outstandingBalanceSJC, 5_000 * UNITS_PER_SACK, "CPR B sem alteracao");
        _eq(coin.balanceOf(creditorA), 7_000 * UNITS_PER_SACK, "credor recebe parte compativel");
        _eq(coin.balanceOf(producer), 1_000 * UNITS_PER_SACK, "excedente vai ao producer");
    }

    function test_differentCropYearDoesNotRepayIncompatibleCPR() public {
        uint256 cprId = _recordCPR(7_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);

        uint256 depositId = _depositAndIssue(
            producer,
            1,
            keccak256("Safra 2027/2028"),
            8_000 * 60,
            bytes32(uint256(9))
        );

        _eq(coin.cprDetails(cprId).outstandingBalanceSJC, 7_000 * UNITS_PER_SACK, "CPR de outra cropYear intacta");
        _eq(coin.balanceOf(creditorA), 0, "credor nao recebe nesta cropYear");
        _eq(coin.balanceOf(producer), 8_000 * UNITS_PER_SACK, "toda a carga vai ao producer");
        _eq(coin.repaymentCPRByDeposit(depositId, cprId), 0, "sem abatimento entre safras");
    }

    function test_unitAlternativeRequiresAcceptanceOfTheProducerAndCreditor() public {
        uint256 cprId = _recordCPR(1_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        uint256 depositId = _openAndConfirm(producer, 2, CROP_YEAR, 1_000 * 60, bytes32(uint256(10)));
        uint256 deadline = block.timestamp + 1 days;
        bytes32 digest = coin.alternativeUnitCPRAuthorizationHash(depositId, cprId, deadline);

        coin.authorizeAlternativeCPRUnit(
            depositId,
            cprId,
            deadline,
            _sign(PRODUCER_PK, digest),
            _sign(CREDITOR_A_PK, digest)
        );
        batches.approve(depositId);
        vm.prank(employee);
        coin.issueTokens(depositId);

        _eq(coin.balanceOf(creditorA), 1_000 * UNITS_PER_SACK, "credor recebe com autorizacao bilateral");
        _eq(coin.cprDetails(cprId).outstandingBalanceSJC, 0, "CPR abatida na unit alternativa");
    }

    function test_employeeCannotChangePriorityWhenRecordingLoad() public {
        uint256 cprId = _recordCPR(2_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        uint256 depositId = _openAndConfirm(producer, 1, CROP_YEAR, 60_000, bytes32(uint256(5)));

        vm.prank(employee);
        (bool callAccepted,) = address(coin).call(
            abi.encodeWithSignature("changePriorityInTheLoad(uint256,uint256)", depositId, 99)
        );
        require(!callAccepted, "nao existe alteracao de fila por carga");

        batches.approve(depositId);
        vm.prank(employee);
        coin.issueTokens(depositId);
        _eq(coin.cprDetails(cprId).priority, 1, "priority preservada");
    }

    function test_reorderingWithoutCreditorAcceptanceIsRejected() public {
        uint256 cprA = _recordCPR(2_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        _recordCPR(2_000, 1, CROP_YEAR, 2, creditorB, block.timestamp + 30 days);
        uint256 deadline = block.timestamp + 1 days;
        bytes32 documentHash = keccak256("pedido de mudanca de fila");
        bytes32 digest = coin.cprReorderingAuthorizationHash(producer, cprA, 2, documentHash, deadline);
        bytes memory producerAcceptance = _sign(PRODUCER_PK, digest);
        bytes[] memory withoutCreditorAcceptances = new bytes[](0);

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Missing creditor acceptances"));
        coin.reorderCPR(producer, cprA, 2, documentHash, deadline, producerAcceptance, withoutCreditorAcceptances);
    }

    function test_loadAtWrongUnitDoesNotRepayWithoutAuthorization() public {
        uint256 cprId = _recordCPR(7_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        uint256 depositId = _depositAndIssue(producer, 2, CROP_YEAR, 7_000 * 60, bytes32(uint256(6)));

        _eq(coin.cprDetails(cprId).outstandingBalanceSJC, 7_000 * UNITS_PER_SACK, "CPR nao abatida");
        _eq(coin.balanceOf(creditorA), 0, "credor nao recebe");
        _eq(coin.balanceOf(producer), 7_000 * UNITS_PER_SACK, "producer recebe carga");
        _eq(coin.repaymentCPRByDeposit(depositId, cprId), 0, "nenhum abatimento registrado");
    }

    function test_assignmentWithoutReferenceOrHashIsRejected() public {
        uint256 cprId = _recordCPR(1_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        uint256 deadline = block.timestamp + 1 days;

        vm.prank(creditorA);
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Invalid reference"));
        coin.assignCPR(cprId, creditorC, "", bytes32(0), deadline, bytes(""));
    }

    function test_assignmentWithProofChangesCreditorForFutureDeliveries() public {
        uint256 cprId = _recordCPR(1_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        vm.prank(creditorA);
        coin.assignCPR(
            cprId,
            creditorC,
            "Registro CPR protocolo 98431",
            keccak256("documento de cessao"),
            block.timestamp + 1 days,
            bytes("")
        );

        _depositAndIssue(producer, 1, CROP_YEAR, 1_000 * 60, bytes32(uint256(7)));
        _eq(coin.balanceOf(creditorA), 0, "credor anterior nao recebe");
        _eq(coin.balanceOf(creditorC), 1_000 * UNITS_PER_SACK, "novo credor recebe");
        _eq(uint160(coin.cprDetails(cprId).creditor), uint160(creditorC), "credor atual alterado");
        require(
            keccak256(bytes(coin.cprAssignmentRegistryReference(cprId)))
                == keccak256(bytes("Registro CPR protocolo 98431")),
            "referencia da cessao guardada"
        );
    }

    function test_assignmentByNonCreditorIsRejected() public {
        uint256 cprId = _recordCPR(1_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        vm.prank(creditorB);
        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Invalid creditor authorization"));
        coin.assignCPR(
            cprId,
            creditorC,
            "Protocolo 98432",
            keccak256("documento de cessao"),
            block.timestamp + 1 days,
            bytes("")
        );
    }

    function test_sameWeighingTicketCannotBeConfirmedTwice() public {
        bytes32 duplicateWeighingTicketHash = keccak256("weighingTicket repetido");
        _openAndConfirm(producer, 1, CROP_YEAR, 60_000, duplicateWeighingTicketHash);
        uint256 secondDepositId = coin.openLoadReceipt(producer, 1, CROP_YEAR);

        vm.expectRevert(abi.encodeWithSignature("Error(string)", "Weighing ticket has already been used"));
        coin.confirmLoadReceipt(secondDepositId, _weighingTicket(60_000, duplicateWeighingTicketHash, block.timestamp));
    }

    function test_deliveryAfterDueDateRepaysAndRecordsDelay() public {
        uint256 cprId = _recordCPR(1_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 1 days);
        vm.warp(block.timestamp + 2 days);

        uint256 depositId = _depositAndIssue(producer, 1, CROP_YEAR, 1_000 * 60, bytes32(uint256(8)));
        SojaCoin.CPRDebt memory cpr = coin.cprDetails(cprId);
        SojaCoin.CPRRepayment[] memory repayments = coin.cprRepaymentsForDeposit(depositId);

        _eq(cpr.outstandingBalanceSJC, 0, "saldo abatido mesmo atrasado");
        _eq(uint256(cpr.status), 3, "CPR quitada");
        require(repayments.length == 1 && repayments[0].overdue, "atraso fica registrado");
        _eq(repayments[0].amountSJC, 1_000 * UNITS_PER_SACK, "valor abatido");
    }

    function test_overdueWithBalanceContinuesActiveForRepayment() public {
        uint256 cprId = _recordCPR(1_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 1 days);
        vm.warp(block.timestamp + 2 days);

        _depositAndIssue(producer, 1, CROP_YEAR, 400 * 60, bytes32(uint256(11)));
        SojaCoin.CPRDebt memory cpr = coin.cprDetails(cprId);

        _eq(uint256(cpr.status), 2, "CPR permanece vencida com saldo");
        _eq(cpr.outstandingBalanceSJC, 600 * UNITS_PER_SACK, "abatimento reduz saldo vencido");
    }

    function test_cancelCPRRequiresBothPartiesAcceptance() public {
        uint256 cprId = _recordCPR(1_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        uint256 deadline = block.timestamp + 1 days;
        bytes32 documentHash = keccak256("cancelamento acordado");
        bytes32 digest = coin.cprClosureAuthorizationHash(cprId, true, documentHash, deadline);

        coin.cancelCPR(
            cprId,
            documentHash,
            deadline,
            _sign(PRODUCER_PK, digest),
            _sign(CREDITOR_A_PK, digest)
        );

        _eq(uint256(coin.cprDetails(cprId).status), 5, "CPR cancelada por acordo");
        _eq(coin.activeCPRsForProducer(producer).length, 0, "CPR removida da fila ativa");
    }

    function test_closeByAgreementRecordsBalanceWithoutMintingSJC() public {
        uint256 cprId = _recordCPR(1_000, 1, CROP_YEAR, 1, creditorA, block.timestamp + 30 days);
        _depositAndIssue(producer, 1, CROP_YEAR, 400 * 60, bytes32(uint256(12)));
        uint256 supplyBefore = coin.totalSupply();
        uint256 deadline = block.timestamp + 1 days;
        bytes32 documentHash = keccak256("encerramento por acordo");
        bytes32 digest = coin.cprClosureAuthorizationHash(cprId, false, documentHash, deadline);

        coin.closeCPRByAgreement(
            cprId,
            documentHash,
            deadline,
            _sign(PRODUCER_PK, digest),
            _sign(CREDITOR_A_PK, digest)
        );
        SojaCoin.CPRDebt memory cpr = coin.cprDetails(cprId);

        _eq(uint256(cpr.status), 4, "CPR encerrada por acordo");
        _eq(cpr.outstandingBalanceSJC, 0, "saldo ativo zerado");
        _eq(cpr.closedBalanceSJC, 600 * UNITS_PER_SACK, "saldo remanescente registrado");
        _eq(coin.totalSupply(), supplyBefore, "encerramento nao cria tokens");
    }

    function _recordCPR(
        uint256 amountSacks,
        uint256 unitId,
        bytes32 cropYear,
        uint256 priority,
        address creditor,
        uint256 dueDate
    ) private returns (uint256 cprId) {
        uint256 amountSJC = amountSacks * UNITS_PER_SACK;
        bytes32 documentHash = keccak256(abi.encode("CPR", priority, creditor));
        uint256 deadline = block.timestamp + 1 days;
        bytes32 digest = coin.cprAuthorizationHash(
            producer,
            creditor,
            amountSJC,
            unitId,
            cropYear,
            priority,
            dueDate,
            documentHash,
            deadline
        );
        cprId = coin.registerCPR(
            producer,
            creditor,
            amountSJC,
            unitId,
            cropYear,
            priority,
            dueDate,
            documentHash,
            deadline,
            _sign(PRODUCER_PK, digest)
        );
    }

    function _depositAndIssue(
        address owner,
        uint256 unitId,
        bytes32 cropYear,
        uint256 weightKG,
        bytes32 identifierWeighingTicket
    ) private returns (uint256 depositId) {
        depositId = _openAndConfirm(owner, unitId, cropYear, weightKG, identifierWeighingTicket);
        batches.approve(depositId);
        vm.prank(employee);
        coin.issueTokens(depositId);
    }

    function _openAndConfirm(
        address owner,
        uint256 unitId,
        bytes32 cropYear,
        uint256 weightKG,
        bytes32 identifierWeighingTicket
    ) private returns (uint256 depositId) {
        depositId = coin.openLoadReceipt(owner, unitId, cropYear);
        coin.linkTaxInvoice(depositId, keccak256(abi.encode("NF", depositId)));
        coin.confirmLoadReceipt(depositId, _weighingTicket(weightKG, identifierWeighingTicket, block.timestamp));
    }

    function _weighingTicket(uint256 weightKG, bytes32 weighingTicketHash, uint256 timestamp)
        private
        pure
        returns (SojaCoinStorage.WeighingTicket memory)
    {
        return SojaCoinStorage.WeighingTicket({
            grossWeightKg: weightKG,
            tareKg: 0,
            moistureBps: 0,
            impuritiesBps: 0,
            damagedGrainBps: 0,
            moistureDeductionKg: 0,
            impuritiesDeductionKg: 0,
            damagedGrainDeductionKg: 0,
            otherDeductionsKg: 0,
            weighingTicketHash: weighingTicketHash,
            reportHash: keccak256(abi.encode("laudo", weighingTicketHash)),
            timestamp: timestamp
        });
    }

    function _register(address participant, uint256 privateKey) private {
        coin.proposeParticipantRegistration(participant, keccak256(abi.encode("termo", participant)));
        uint256 deadline = block.timestamp + 1 days;
        bytes32 digest = coin.participationTermsAcceptanceHash(participant, deadline);
        coin.acceptParticipationTerms(participant, deadline, _sign(privateKey, digest));
    }

    function _sign(uint256 privateKey, bytes32 digest) private returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey, digest);
        return abi.encodePacked(r, s, v);
    }

    function _eq(uint256 obtido, uint256 expected, string memory message) private pure {
        require(obtido == expected, message);
    }
}
