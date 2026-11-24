"""Check a company's registered contracts and their on-chain connections."""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import BadFunctionCallOutput, ContractLogicError

from companies.models import Company
from utils.identifiers import is_valid_wallet_address, normalize_wallet_address

from ..models import ContractInstance, Network
from .contracts import bind_contract_to_web3
from .rpc import connect_to_rpc


@dataclass(frozen=True)
class ContractCheck:
    """One comparison between a registered value and the blockchain."""

    name: str
    passed: bool
    expected: str
    actual: str


@dataclass(frozen=True)
class ContractVerification:
    """Results from a single block, without modifying local or chain state."""

    block_number: int
    checks: tuple[ContractCheck, ...]

    @property
    def passed(self) -> bool:
        """Return whether every required check succeeded."""

        return all(check.passed for check in self.checks)


def _read_address(
    contract: Contract,
    function_name: str,
    block_number: int,
) -> str:
    """Read and normalize one address returned by a contract function."""

    address = getattr(contract.functions, function_name)().call(
        block_identifier=block_number
    )
    if not isinstance(address, str):
        raise ValueError("Contract did not return an address.")
    normalized = normalize_wallet_address(address)
    if not is_valid_wallet_address(normalized):
        raise ValueError("Contract returned an invalid address.")
    return normalized


def verify_company_contracts(
    *,
    company: Company,
    network: Network,
    expected_sojacoin_owner: str,
    expected_unit_registry_owner: str,
) -> ContractVerification:
    """Compare registered deployments, connections and owners on one chain.

    The expected owner addresses must come from an authorized server-side
    onboarding flow. RPC errors propagate to the caller, so an unavailable
    network is never reported as a verified configuration.
    """

    if company.pk is None or network.pk is None:
        raise ValidationError("A empresa e a rede devem estar cadastradas.")
    if not Company.objects.filter(pk=company.pk, active=True).exists():
        raise ValidationError("Empresa inexistente ou inativa.")
    saved_network = Network.objects.filter(pk=network.pk, active=True).first()
    if saved_network is None:
        raise ValidationError("Rede inexistente ou inativa.")

    soja_owner = normalize_wallet_address(expected_sojacoin_owner)
    registry_owner = normalize_wallet_address(expected_unit_registry_owner)
    if not is_valid_wallet_address(soja_owner) or not is_valid_wallet_address(registry_owner):
        raise ValidationError("Informe endereços válidos para os dois proprietários.")

    deployments = {
        item.kind: item
        for item in ContractInstance.objects.filter(
            company_id=company.pk, network_id=network.pk, active=True
        )
    }
    web3 = connect_to_rpc(saved_network)
    # Read every contract at the same block to avoid comparing different states.
    block_number = web3.eth.block_number
    checks: list[ContractCheck] = []
    available: dict[str, Contract] = {}

    for kind, label in ContractInstance.Kind.choices:
        deployment = deployments.get(kind)
        if deployment is None:
            checks.append(ContractCheck(f"Contrato {label}", False, "Cadastrado", "Não cadastrado"))
            continue
        code = web3.eth.get_code(
            Web3.to_checksum_address(deployment.address_normalized),
            block_identifier=block_number,
        )
        has_code = bool(code)
        checks.append(ContractCheck(
            f"Contrato {label}", has_code, "Código implantado",
            "Código encontrado" if has_code else "Sem código no endereço",
        ))
        if has_code:
            available[kind] = bind_contract_to_web3(web3, deployment)

    def compare(name: str, source: str, function_name: str, expected: str | None) -> None:
        """Record a readable failure when a required contract is missing."""

        contract = available.get(source)
        if contract is None or expected is None:
            checks.append(ContractCheck(name, False, expected or "Contrato cadastrado", "Contrato indisponível"))
            return
        try:
            actual = _read_address(contract, function_name, block_number)
        except (BadFunctionCallOutput, ContractLogicError, ValueError):
            checks.append(ContractCheck(name, False, expected, "Não foi possível ler o contrato"))
            return
        checks.append(ContractCheck(name, actual == expected, expected, actual))

    def address_for(kind: str) -> str | None:
        """Return the registered address when the contract has code."""

        return deployments[kind].address_normalized if kind in available else None

    kinds = ContractInstance.Kind
    module_functions = (
        (kinds.SOJA_COIN_ADMINISTRATION, "administrationModule"),
        (kinds.SOJA_COIN_DEPOSITS, "depositsModule"),
        (kinds.SOJA_COIN_CPR, "cprModule"),
        (kinds.SOJA_COIN_SERVICES, "servicesModule"),
        (kinds.SOJA_COIN_LOCKS, "locksModule"),
        (kinds.SOJA_COIN_REDEMPTIONS, "redemptionsModule"),
    )
    for kind, function_name in module_functions:
        compare(function_name, kinds.SOJA_COIN, function_name, address_for(kind))

    compare("SojaCoin → UnitRegistry", kinds.SOJA_COIN, "unitRegistry", address_for(kinds.UNIT_REGISTRY))
    compare("SojaCoin → lotes", kinds.SOJA_COIN, "issuanceBatchModule", address_for(kinds.ISSUANCE_BATCHES))
    compare("UnitRegistry → SojaCoin", kinds.UNIT_REGISTRY, "sojaCoin", address_for(kinds.SOJA_COIN))
    compare("Lotes → SojaCoin", kinds.ISSUANCE_BATCHES, "sojaCoin", address_for(kinds.SOJA_COIN))
    compare("Proprietário do SojaCoin", kinds.SOJA_COIN, "owner", soja_owner)
    compare("Proprietário do UnitRegistry", kinds.UNIT_REGISTRY, "owner", registry_owner)

    return ContractVerification(block_number=block_number, checks=tuple(checks))
