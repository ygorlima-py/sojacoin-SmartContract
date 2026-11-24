"""Bind saved contract deployments to their packaged Web3 ABIs."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.core.exceptions import ImproperlyConfigured, ValidationError
from web3 import Web3
from web3.contract import Contract

from ..models import ContractInstance
from .rpc import connect_to_rpc


ABI_DIRECTORY = Path(__file__).resolve().parent.parent / "abis"
ABI_FILES = {
    ContractInstance.Kind.SOJA_COIN: "sojacoin.json",
    ContractInstance.Kind.UNIT_REGISTRY: "unit_registry.json",
    ContractInstance.Kind.ISSUANCE_BATCHES: "issuance_batches.json",
    ContractInstance.Kind.SOJA_COIN_ADMINISTRATION: "sojacoin_administration.json",
    ContractInstance.Kind.SOJA_COIN_DEPOSITS: "sojacoin_deposits.json",
    ContractInstance.Kind.SOJA_COIN_CPR: "sojacoin_cpr.json",
    ContractInstance.Kind.SOJA_COIN_SERVICES: "sojacoin_services.json",
    ContractInstance.Kind.SOJA_COIN_LOCKS: "sojacoin_locks.json",
    ContractInstance.Kind.SOJA_COIN_REDEMPTIONS: "sojacoin_redemptions.json",
}
# Reject malformed ABI entries before Web3 tries to build a contract object.
ABI_ENTRY_TYPES = {"constructor", "error", "event", "fallback", "function", "receive"}


@lru_cache(maxsize=9)
def load_contract_abi(kind: str) -> list[dict[str, Any]]:
    """Load the packaged ABI for one supported contract kind."""

    abi_file = ABI_FILES.get(kind)
    if abi_file is None:
        raise ImproperlyConfigured(f"No ABI is configured for contract kind '{kind}'.")

    abi_path = ABI_DIRECTORY / abi_file
    try:
        abi = json.loads(abi_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ImproperlyConfigured(f"Could not load contract ABI: {abi_path.name}.") from error

    if not isinstance(abi, list) or any(
        not isinstance(entry, dict) or entry.get("type") not in ABI_ENTRY_TYPES
        for entry in abi
    ):
        raise ImproperlyConfigured(f"Contract ABI contains invalid entries: {abi_path.name}.")

    return abi


def bind_contract_to_web3(
    web3: Web3,
    deployment: ContractInstance,
) -> Contract:
    """Bind a saved contract address and ABI to an existing Web3 connection.

    This low-level helper does not check whether the deployment is active.
    Callers decide whether inactive deployments are allowed for their operation.
    """

    abi = load_contract_abi(deployment.kind)
    return web3.eth.contract(
        address=Web3.to_checksum_address(deployment.address_normalized),
        abi=abi,
    )


def get_web3_contract(instance: ContractInstance) -> Contract:
    """Return a Web3 contract object for an active saved deployment.

    The address comes from ``ContractInstance`` and the ABI comes from the
    matching file under ``blockchain/abis``. The RPC chain must match the
    contract instance's saved network.
    """

    if instance.pk is None:
        raise ValidationError("Save the contract instance before connecting to it.")
    if not instance.active:
        raise ValidationError("The contract instance is inactive.")
    if not instance.network.active:
        raise ValidationError("The contract network is inactive.")

    # Use the network attached to this deployment to prevent cross-network calls.
    web3 = connect_to_rpc(instance.network)
    return bind_contract_to_web3(web3, instance)
