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


def record_path(revision_tag: str = "v15") -> Path:
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


# ── Which revision a NEW pool is built on, per network ─────────────────────────────────────
# Mirrors FORGE_CREATE_PROTOCOL_BY_NETWORK / FORGE_CREATE_FALLBACK_BY_NETWORK and createProtocol()
# in api/_forgeVersion.js (the router declares the protocol on every creation; this is the default
# for a forge_stdin request that names no revision).
# Both networks target V20 (protocol 21; owner, 2026-11-18) once that network's V20 registry
# record (v20-<net>.json) is held; until then each falls back down its ladder, the first revision
# whose record is held winning and the last taken unconditionally -- what it created before V20:
#   testnet  21, unconditionally (V18 and V19 are no longer served on testnet).
#   mainnet  21 with v20-mainnet.json held, else 19 with v18-mainnet.json held, else 17 (V16;
#            owner, 2026-11-14: no V17 on mainnet). Mainnet never gets V19: it goes from V18
#            straight to V20, so 20 is no rung of its ladder.
# The gate is the record, never a setting: on a router it reaches the volume only through the
# admin upload, which checks it against the chain.
# To change a table: edit BOTH (here and api/_forgeVersion.js) and the pinned expectations in
# contracts/_test_v20_create_default.py, contracts/_test_v19_create_default.py,
# contracts/_test_v18_create_default.py and api/__checks__/createProtocolByNetwork.check.mjs.
# There is deliberately no environment override.
CREATE_PROTOCOL_BY_NETWORK = {"mainnet": 21, "testnet11": 21}
# What a network creates on while its target's record is not held, in order (the last unconditionally).
CREATE_FALLBACK_BY_NETWORK = {"mainnet": [], "testnet11": []}


def revision_tag_of(protocol: int) -> str:
    """"v18" for protocol 19: the protocol number and the revision name are off by one."""
    return f"v{int(protocol) - 1}"


def create_protocol(held=None) -> int:
    """The protocol a creation that names none is built on, on this network, now.

    `held(revision_tag)` says whether that revision's registry record is held; by default
    record_path() names an existing file (the state volume first, else .awizard/)."""
    network = network_id()
    ladder = [CREATE_PROTOCOL_BY_NETWORK[network], *CREATE_FALLBACK_BY_NETWORK.get(network, [])]
    is_held = held if held is not None else (lambda tag: record_path(tag).is_file())
    for protocol in ladder[:-1]:
        if is_held(revision_tag_of(protocol)):
            return protocol
    return ladder[-1]
