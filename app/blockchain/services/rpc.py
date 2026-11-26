"""Create a Web3 connection using the selected database network."""

from django.core.exceptions import ImproperlyConfigured
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from blockchain.models import Network


def connect_to_rpc(network: Network) -> Web3:
    """Connect to a saved network's RPC endpoint and verify its chain ID.

    The endpoint and expected chain ID both come from the same ``Network``
    record. This prevents one global environment URL from being reused for
    records that represent different networks.

    Args:
        network: Saved network whose RPC endpoint should be queried.

    Returns:
        A connected Web3 client for the selected network.

    Raises:
        ImproperlyConfigured: If the saved network has no valid RPC URL.
        ConnectionError: If the endpoint is unavailable or reports another
            chain ID.
    """

    rpc_url = (network.rpc_url or "").strip()
    if not rpc_url:
        raise ImproperlyConfigured(
            f"Configure an RPC URL for the '{network.name}' network."
        )

    web3 = Web3(
        Web3.HTTPProvider(
            rpc_url,
            request_kwargs={"timeout": 10},
        )
    )
    # BNB Smart Chain uses PoA-style block extraData, which exceeds Web3.py's
    # standard 32-byte limit. Keep this chain-specific so other RPCs retain
    # their normal block response format.
    if network.chain_id in {56, 97}:
        web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not web3.is_connected():
        raise ConnectionError("Não foi possível conectar ao RPC.")

    actual_chain_id = web3.eth.chain_id
    if actual_chain_id != network.chain_id:
        raise ConnectionError(
            f"RPC connected to chain {actual_chain_id}; "
            f"expected chain {network.chain_id} for '{network.name}'."
        )

    return web3
