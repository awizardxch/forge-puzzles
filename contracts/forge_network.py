"""The network the responder and the deploy tooling run against.

One source for the Python side, mirroring api/_network.js: FORGE_NETWORK names
it (testnet11 or mainnet; unset is testnet11), FORGE_NODE_URL overrides the node.
The registry record, the public node, the native coin's ticker and the address
prefix follow from it. Modules that used to hardcode testnet11 read from here, so
a mainnet responder built from the same tree cannot quietly consult a testnet
node or file its pools in a testnet record.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

NETWORKS = {
    "testnet11": {"node": "https://testnet11.api.coinset.org", "hrp": "txch", "ticker": "TXCH", "record_suffix": "testnet"},
    "mainnet": {"node": "https://api.coinset.org", "hrp": "xch", "ticker": "XCH", "record_suffix": "mainnet"},
}


def network_id() -> str:
    value = os.environ.get("FORGE_NETWORK", "").strip().lower()
    return "mainnet" if value == "mainnet" else "testnet11"


def facts() -> dict:
    return NETWORKS[network_id()]


def node_url() -> str:
    return (os.environ.get("FORGE_NODE_URL", "").strip() or facts()["node"]).rstrip("/")


def native_ticker() -> str:
    return facts()["ticker"]


def address_prefix() -> str:
    return facts()["hrp"]


def record_path(revision_tag: str = "v14") -> Path:
    """`.awizard/v14-testnet.json` on testnet (unchanged), `v14-mainnet.json` on mainnet.
    A state directory (AWIZARD_STATE_DIR / the platform volume) wins when it holds one."""
    name = f"{revision_tag}-{facts()['record_suffix']}.json"
    state = os.environ.get("AWIZARD_STATE_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if state and (Path(state) / name).is_file():
        return Path(state) / name
    return ROOT / ".awizard" / name


def check_address_network(text: str, what: str = "address") -> None:
    """Refuse an address of the other network: xch1 on testnet or txch1 on mainnet."""
    t = str(text or "").strip().lower()
    prefix = address_prefix()
    other = "xch1" if prefix == "txch" else "txch1"
    if t.startswith(other):
        raise ValueError(f"{what} {t[:12]}... is a {other[:-1]} address; this responder serves {network_id()}")
