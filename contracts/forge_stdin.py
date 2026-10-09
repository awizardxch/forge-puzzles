#!/usr/bin/env python3
"""V3-only stdin bundle builder for future Node API integration.

This module performs no RPC calls, signing, submission, or production routing.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

from chia.types.blockchain_format.coin import Coin
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, LineageProof, construct_cat_puzzle
from chia.util.bech32m import decode_puzzle_hash
from chia.wallet.trading.offer import Offer
from untrusted_clvm import offer_from_bech32
from chia_rs import G1Element, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from forge_offer import (
    MODE_ADD,
    MODE_REMOVE,
    MODE_SWAP,
    LaunchConfig,
    LaunchIntent,
    PoolCoin,
    ReserveCoin,
    V3Pool,
    build_create_v3,
    build_transition_v3,
    reserve_inner_puzzle,
    compiled_program,
    finalize_create_offer,
    prepare_create_v3,
    singleton_struct,
)
from forge_multihop_swap import build_multihop_swap
from forge_vault_route import build_swap_then_redeem
from forge_split_swap import SplitBranchSpec, build_split_swap
from forge_flow_balance import FlowLegSpec, build_flow_balance
from forge_routed_deposit import DepositSale, build_routed_deposit
from forge_transition import build_transition
import forge_v16_offer as off16
import forge_v16_route as rt16
import forge_v16_create as cre16
import forge_v16_driver as drv16
import forge_v15_offer as off15
import forge_v15_route as rt15
import forge_v15_create as cre15
import forge_v15_driver as drv15
import forge_v14_offer as off14
import forge_v14_route as rt14
import forge_v14_create as cre14
import forge_v14_driver as drv14

# One lane per protocol revision: (offer, route, create, driver). A request picks its lane by
# its own protocol_version or its pool snapshots'; a bundle never mixes revisions, except a
# route (below). V16 (protocol 17) is the live lane and the one a creation takes. V15 (16)
# and V14 (15) are NOT retired: the owner kept each live beside its successor (2026-10-09,
# 2026-10-07), their pools stay on chain undrained and their lanes ship everywhere, so they
# are imported unconditionally. V13 (14) is kept only while its record is read back;
# V11.1 (12) and V12 (13) were drained and retired.
_LANES = {17: (off16, rt16, cre16, drv16), 16: (off15, rt15, cre15, drv15), 15: (off14, rt14, cre14, drv14)}
# The retired lane is OPTIONAL. It is only needed while V13's record is still read back,
# and it is absent wherever the retired sources are not shipped -- the public repository
# prunes every retired revision, so importing it unconditionally made this dispatcher fail
# to import there at all. A missing retired lane is a pool this build cannot serve, which
# `_lane` already reports; it is not a reason for the live lane to be unreachable.
try:
    import forge_v13_offer as off13
    import forge_v13_route as rt13
    import forge_v13_create as cre13
    import forge_v13_driver as drv13
except ImportError:
    pass
else:
    _LANES[14] = (off13, rt13, cre13, drv13)


# The route actions a composer builds across revisions (forge_v16_route._drv; a route that
# never touches a V16 pool is built by forge_v15_route, which spends V14 and V15 pools).
_CROSS_REVISION_ACTIONS = frozenset({"multihop-swap", "split-swap", "flow-balance", "routed-deposit", "vault-route"})


def _lane_version(payload: dict) -> int:
    declared = payload.get("protocol_version")
    versions = {int(sn["protocol_version"]) for sn in _pool_snapshots(payload) if sn.get("protocol_version") is not None}
    if declared is not None:
        versions.add(int(declared))
    if len(versions) > 1:
        # V14 and V15 are live beside V16 (owner, 2026-10-07, 2026-10-09): a ROUTE may cross
        # revisions, and the newest lane on it builds it, spending each pool with its own driver. Anything
        # else -- a single-pool action, a creation -- is one revision by construction.
        if payload.get("action") in _CROSS_REVISION_ACTIONS and versions <= set(_LANES):
            return max(versions)
        raise ValueError(f"a bundle cannot mix Forge revisions: {sorted(versions)}")
    # Nothing declared and no snapshot to read it from (a creation): the newest lane
    # this build carries. This said 13 long after V14 shipped, so a creation through
    # api/forge-create was built by the retired V13 lane where its sources exist and
    # refused outright where they are pruned (forge-ui) -- found 2026-10-01.
    return versions.pop() if versions else max(_LANES)


def _lane(payload: dict):
    version = _lane_version(payload)
    if version not in _LANES:
        raise ValueError(f"unsupported Forge protocol version for this lane: {version}")
    return _LANES[version]
from chia.types.coin_spend import CoinSpend

ZERO_32 = bytes32(b"\x00" * 32)


def _bytes32(value: str) -> bytes32:
    normalized = value.removeprefix("0x")
    if len(normalized) != 64:
        raise ValueError(f"expected 32-byte hex value, got {value!r}")
    return bytes32.from_hexstr(normalized)


def _coin(value: dict[str, Any]) -> Coin:
    return Coin(
        _bytes32(str(value["parent_coin_info"])),
        _bytes32(str(value["puzzle_hash"])),
        uint64(int(value["amount"])),
    )


def _lineage(value: dict[str, Any]) -> LineageProof:
    inner = value.get("inner_puzzle_hash")
    return LineageProof(
        _bytes32(str(value["parent_name"])),
        None if inner is None else _bytes32(str(inner)),
        uint64(int(value["amount"])),
    )


def _launch_intent(value: dict[str, Any]) -> LaunchIntent:
    assets = tuple(_bytes32(item) for item in value["asset_ids"])
    settlements = tuple(_bytes32(item) for item in value["cat_settlement_coin_ids"])
    if len(assets) != 2 or len(settlements) != 2:
        raise ValueError("V3 launch requires exactly two assets and CAT settlements")
    return LaunchIntent(
        _bytes32(value["xch_settlement_coin_id"]),
        (settlements[0], settlements[1]),
        (assets[0], assets[1]),
        (int(value["weights"][0]), int(value["weights"][1])),
        (int(value["bootstrap_amounts"][0]), int(value["bootstrap_amounts"][1])),
        int(value["fee_bps"]),
        _bytes32(value["lp_recipient"]),
        int(value["min_initial_lp"]),
        int(value["max_initial_lp"]),
        int(value["expiry_height"]),
        _bytes32(value["salt"]),
    )


def _launch_config(value: dict[str, Any]) -> LaunchConfig:
    assets = tuple(_bytes32(item) for item in value["asset_ids"])
    if len(assets) != 2:
        raise ValueError("V3 launch requires exactly two assets")
    return LaunchConfig(
        (assets[0], assets[1]),
        (int(value["weights"][0]), int(value["weights"][1])),
        (int(value["bootstrap_amounts"][0]), int(value["bootstrap_amounts"][1])),
        int(value["fee_bps"]),
        _bytes32(value["lp_recipient"]),
        int(value["initial_lp"]),
        int(value["expiry_height"]),
        _bytes32(value["salt"]),
    )


def _launch_intent_json(intent: LaunchIntent) -> dict[str, Any]:
    return {
        "xch_settlement_coin_id": intent.xch_settlement_coin_id.hex(),
        "cat_settlement_coin_ids": [coin_id.hex() for coin_id in intent.cat_settlement_coin_ids],
        "asset_ids": [asset_id.hex() for asset_id in intent.asset_ids],
        "weights": list(intent.weights),
        "bootstrap_amounts": list(intent.bootstrap_amounts),
        "fee_bps": intent.fee_bps,
        "lp_recipient": intent.lp_recipient.hex(),
        "min_initial_lp": intent.min_initial_lp,
        "max_initial_lp": intent.max_initial_lp,
        "expiry_height": intent.expiry_height,
        "salt": intent.salt.hex(),
        "commitment": intent.commitment.hex(),
    }


def _pool(value: dict[str, Any]) -> V3Pool:
    declared_version = value.get("protocol_version")
    protocol_version = int(declared_version) if declared_version is not None else (
        # V7 and V8 share the same mint maths and therefore the same join rule;
        # only protocol_version separates them, and the module-hash check below
        # catches a snapshot that guesses wrong.
        7 if value.get("join_rule") == "geometric-invariant-v4" else
        6 if value.get("join_rule") == "geometric-invariant-v3" else
        5 if value.get("join_rule") == "geometric-invariant-v2" else
        4 if value.get("join_rule") == "geometric-invariant-v1" else 3
    )
    if protocol_version >= 11:
        raise ValueError(f"V{protocol_version} pools are not V3Pool snapshots; this lane takes them through "
                         "the versioned offer lane (swap, add, remove, and the route lanes)")
    if protocol_version not in (3, 4, 5, 6, 7, 8, 9, 10):
        raise ValueError(f"unsupported Forge protocol version: {protocol_version}")
    launcher_id = _bytes32(value["launcher_id"])
    assets = tuple(_bytes32(item) for item in value["asset_ids"])
    weights = [int(item) for item in value["weights"]]
    # From V10 the reserve is curried with the launcher, so its puzzle hash is
    # pool-specific; the snapshot hash check below then also proves the reserve
    # belongs to this launcher.
    reserve_inner = reserve_inner_puzzle(protocol_version, launcher_id)
    pool_mod = compiled_program(f"pool_singleton_v{protocol_version}")
    if value.get("pool_module_hash") is not None and value.get("pool_module_hash") != pool_mod.get_tree_hash().hex():
        raise ValueError("pool snapshot module hash does not match this V3 revision")
    if value.get("reserve_inner_puzzle_hash") is not None and value.get("reserve_inner_puzzle_hash") != reserve_inner.get_tree_hash().hex():
        raise ValueError("pool snapshot reserve hash does not match this V3 revision")
    lp_tail = compiled_program(f"forge_lp_cat_tail_v{protocol_version}").curry(launcher_id, protocol_version)
    lp_asset_id = lp_tail.get_tree_hash()
    singleton = singleton_struct(launcher_id)
    # V8 inserts the protocol fee ahead of the tail hash, so the config is
    # nine items rather than seven. The pool puzzle reads it positionally, so
    # the order here has to match pool_singleton_v8.rue exactly.
    config: list[object] = [
        protocol_version,
        pool_mod.get_tree_hash(),
        list(assets),
        weights,
        int(value["fee_bps"]),
    ]
    if protocol_version >= 8:
        protocol_fee_bps = int(value.get("protocol_fee_bps") or 0)
        raw_recipient = str(value.get("protocol_puzzle_hash") or "")
        recipient = _bytes32(raw_recipient) if raw_recipient else ZERO_32
        if protocol_fee_bps > 0 and recipient == ZERO_32:
            raise ValueError("a V8 pool with a protocol fee needs protocol_puzzle_hash")
        config.extend([protocol_fee_bps, recipient])
    config.extend([lp_asset_id, reserve_inner.get_tree_hash()])

    reserves: dict[bytes32, ReserveCoin] = {}
    state_reserves: list[list[object]] = []
    for asset_id, reserve_value in zip(assets, value["reserves"]):
        coin = _coin(reserve_value["coin"])
        expected_hash = (
            reserve_inner.get_tree_hash()
            if asset_id == ZERO_32
            else construct_cat_puzzle(CAT_MOD, asset_id, reserve_inner).get_tree_hash()
        )
        if coin.puzzle_hash != expected_hash:
            raise ValueError("reserve coin does not use the canonical V3/V5 reserve puzzle")
        reserves[asset_id] = ReserveCoin(
            asset_id,
            coin,
            reserve_inner,
            _lineage(reserve_value["lineage_proof"]),
        )
        state_reserves.append([asset_id, coin.name(), coin.amount])
    state: list[object] = [state_reserves, int(value["total_lp"])]
    inner = pool_mod.curry(singleton, config, state)
    pool_coin = _coin(value["pool_coin"])
    return V3Pool(
        launcher_id,
        singleton,
        config,
        state,
        PoolCoin(
            pool_coin,
            inner,
            _bytes32(value["pool_lineage_parent_name"]),
            None
            if value.get("parent_inner_puzzle_hash") is None
            else _bytes32(value["parent_inner_puzzle_hash"]),
            launcher_id,
        ),
        reserves,
        lp_asset_id,
        lp_tail,
    )


def _pool_json(pool: V3Pool) -> dict[str, Any]:
    protocol_version = int(pool.config[0])
    # Config is positional and its length depends on the revision: V8 inserted
    # the protocol fee and its recipient at 5 and 6, which pushed the LP asset
    # id and the reserve inner hash from 5/6 to 7/8. Index those two from the
    # END, where they have always sat, so the next inserted field cannot
    # silently repoint them again.
    #
    # It did exactly that once: this read config[6] and so serialized the
    # PROTOCOL FEE RECIPIENT as the reserve inner hash on every V8+ pool.
    # `persistSuccessor` writes this snapshot into the deployment index and
    # `findForgePool` reads it straight back on the next action, so each pool
    # worked once and then failed `_pool`'s reserve-hash check for good.
    snapshot = {
        "protocol_version": protocol_version,
        "pool_module_hash": bytes32(pool.config[1]).hex(),
        "reserve_inner_puzzle_hash": bytes32(pool.config[-1]).hex(),
        "launcher_id": pool.launcher_id.hex(),
        "pool_coin_id": pool.pool.coin.name().hex(),
        "pool_coin": pool.pool.coin.to_json_dict(),
        "pool_lineage_parent_name": pool.pool.lineage_parent_name.hex(),
        "parent_inner_puzzle_hash": None
        if pool.pool.parent_inner_puzzle_hash is None
        else pool.pool.parent_inner_puzzle_hash.hex(),
        "asset_ids": [bytes32(asset).hex() for asset in pool.config[2]],
        "weights": [int(weight) for weight in pool.config[3]],
        "fee_bps": int(pool.config[4]),
        "total_lp": int(pool.state[1]),
        "lp_asset_id": pool.lp_asset_id.hex(),
        "reserves": [
            {
                "coin": reserve.coin.to_json_dict(),
                "lineage_proof": {
                    "parent_name": reserve.lineage_proof.parent_name.hex(),
                    "inner_puzzle_hash": reserve.lineage_proof.inner_puzzle_hash.hex(),
                    "amount": int(reserve.lineage_proof.amount),
                },
            }
            for reserve in pool.reserves.values()
        ],
    }
    if protocol_version >= 8:
        # Dropping these would rebuild the pool with a zero protocol fee, whose
        # inner puzzle hashes to something else entirely -- a spend against a
        # pool that does not exist.
        snapshot["protocol_fee_bps"] = int(pool.config[5])
        snapshot["protocol_puzzle_hash"] = bytes32(pool.config[6]).hex()

    if protocol_version == 4:
        snapshot["join_rule"] = "geometric-invariant-v1"
    elif protocol_version == 5:
        snapshot["join_rule"] = "geometric-invariant-v2"
    elif protocol_version >= 6:
        snapshot["join_rule"] = "geometric-invariant-v3"
    return snapshot


def _dev_fee(payload: dict[str, Any]) -> tuple[bytes32 | None, int]:
    """Router fee config, always optional: absent means no fee is collected.

    The rate comes from the caller but the *amount* is derived inside the
    builder from the actual output, so a crafted request cannot understate it.
    """
    dev_fee = payload.get("dev_fee") or {}
    raw = dev_fee.get("puzzle_hash") or dev_fee.get("recipient")
    if not raw:
        return None, 0
    text = str(raw).strip()
    if text.startswith(("xch1", "txch1")):
        # The router's fee is paid to this address on THIS network; a txch1 recipient
        # on a mainnet responder (or the reverse) is a misconfiguration, refused.
        import forge_network as _net
        _net.check_address_network(text, "router fee recipient")
    recipient = (decode_puzzle_hash(text) if text.startswith(("xch1", "txch1"))
                 else _bytes32(text))
    return recipient, int(dev_fee.get("bps") or 0)


def _pool_snapshots(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every pool snapshot a request carries, whichever lane it names."""
    found: list[dict[str, Any]] = []
    for key in ("pool", "swapPool", "vault"):
        if isinstance(payload.get(key), dict):
            found.append(payload[key])
    for entry in payload.get("pools") or []:
        if isinstance(entry, dict):
            found.append(entry)
    for group_key in ("legs", "sales", "branches"):
        for group in payload.get(group_key) or []:
            if not isinstance(group, dict):
                continue
            if isinstance(group.get("pool"), dict):
                found.append(group["pool"])
            for entry in group.get("pools") or []:
                if isinstance(entry, dict):
                    found.append(entry)
    return found


def _build_lane(action: str, payload: dict[str, Any], offer: Offer) -> dict[str, Any]:
    """The V11 lane: a keyless settlement of the offer against one pool.

    The leaves bind every spend to a height (`h > last_height`, confirmed within
    the oracle window), so the caller must say what height it is; the responder
    reads the node's peak. The router's own fee goes to the configured dev-fee
    recipient, or to the pool's protocol recipient when none is configured.

    That fee is now a RATE, not a residue. It used to be "everything the pool pays
    above what the trader asked for", which quietly grew with the trader's slippage
    tolerance; the configured bps is passed down so a swap takes exactly that much
    and refunds the rest to the trader. Which leg it comes off is the builder's own
    decision, never the caller's -- see `router_fee_side`.
    """
    off, rt, cre, drv = _lane(payload)
    if payload.get("current_height") is None:
        raise ValueError("a V11 action needs current_height: the leaf binds the spend to the chain height")
    height = int(payload["current_height"])
    surplus_ph, fee_bps = _dev_fee(payload)
    if action in ROUTE_LANES:
        return _build_lane_route(action, payload, offer, height, surplus_ph, fee_bps)
    if action not in off.SETTLERS:
        raise ValueError(f"V11 pools settle through swap, add, remove, {', '.join(sorted(ROUTE_LANES))}; "
                         f"the {action} lane is not available")
    pool = off.snapshot_to_pool(payload["pool"])
    result = off.settle(action, pool, offer, height, surplus_ph, fee_bps)         if _settle_takes_fee(off) else off.settle(action, pool, offer, height, surplus_ph)
    lp_delta = {"add": result.details.get("lp_minted", 0), "remove": -int(result.details.get("burn", 0))}.get(action, 0)
    return {
        "success": True,
        "action": action,
        "transaction_id": result.bundle.name().hex(),
        "bundle": result.bundle.to_json_dict(),
        "pool": off.pool_to_snapshot(result.pool),
        "lp_delta": str(lp_delta),
        "dev_fee_collected": str(result.details.get("router_fee", result.details.get("surplus", 0))),
        "forge": result.details,
    }


def _settle_takes_fee(lane) -> bool:
    """Older lanes' `settle` has no `fee_bps`; only the current one bounds the fee."""
    import inspect
    return "fee_bps" in inspect.signature(lane.settle).parameters


ROUTE_LANES = ("multihop-swap", "split-swap", "flow-balance", "routed-deposit", "vault-route")


def _snapshot_of(pool) -> dict[str, Any]:
    """A pool's snapshot, written by ITS OWN lane: on a route across revisions the successors
    are V14, V15 and V16 pools side by side, and each must come back labelled and shaped by the
    lane that rebuilds it (forge_v16_route._drv)."""
    version = int(sys.modules[type(pool).__module__].PROTOCOL_VERSION)
    return _LANES[version][0].pool_to_snapshot(pool)


def _lane_pool(snapshot: dict[str, Any]):
    """The snapshot says which lane rebuilds the pool; a route may mix the live revisions."""
    lane = _LANES.get(int(snapshot.get("protocol_version") or 0))
    if lane is None or not lane[0].is_pool_snapshot(snapshot):
        raise ValueError("a route can only be built from pools of a revision this build carries (14, 15, 16)")
    return lane[0].snapshot_to_pool(snapshot)


def _build_lane_route(action: str, payload: dict[str, Any], offer: Offer, height: int, surplus_ph: bytes32 | None,
                      fee_bps: int = 0) -> dict[str, Any]:
    """The multi-pool lanes, all through the lane's own route composer. The payload
    contracts are the V10 lanes' (see the branches of `build` below); the successor
    snapshots come back one per distinct pool, in first-use order.

    The router's fee is paid ONCE, on the entry, by the trader's own spend, before the
    route spends a coin. That leaves these lanes with no fee arithmetic of their own: LP,
    protocol and DAO are all inside the puzzle and are already paid out of each hop's own
    output by the leaf. A route pays out exactly what the offer asks for, in the groups
    the trader signed; there is no overage and no one to send it to (F3, 2026-10-07).
    """
    off, rt, cre, drv = _lane(payload)
    fee_ph = surplus_ph if fee_bps > 0 else None
    # `preview`: size the route from the offer's coins and report what it releases and the
    # fee it needs, without settling -- the exact figures an offer must carry.
    preview = bool(payload.get("preview"))
    if action == "multihop-swap":
        pools = [_lane_pool(entry) for entry in payload["pools"]]
        surplus = surplus_ph if surplus_ph is not None else pools[0].protocol_ph
        result = rt.multihop_swap(pools, [_bytes32(str(a)) for a in payload["path"]], offer, height, surplus,
                                  fee_bps, fee_ph, preview=preview)
        extra = {"amounts": [str(a) for a in result.details["amounts"]],
                 "dev_fee_collected": str(result.details.get("router_fee", 0))}
        if "wrap" in result.details:
            extra["wrap"] = {k: str(v) for k, v in result.details["wrap"].items()}
    elif action == "split-swap":
        branches = [([_lane_pool(entry) for entry in b["pools"]], [_bytes32(str(a)) for a in b["path"]], int(b["amountIn"]))
                    for b in payload["branches"]]
        surplus = surplus_ph if surplus_ph is not None else branches[0][0][0].protocol_ph
        # Dexie open offers taken inside the same bundle (fixed-size, so each is taken whole).
        externals = [offer_from_bech32(o) for o in payload.get("dexieOffers") or []]
        result = rt.split_swap(branches, offer, height, surplus, fee_bps, fee_ph, externals=externals, preview=preview)
        extra = {"branch_amounts": [[str(a) for a in amounts] for amounts in result.details["branch_amounts"]],
                 "total_out": str(result.details["total_out"]),
                 "dev_fee_collected": str(result.details.get("router_fee", 0)),
                 **({"taken_offers": [{k: str(v) for k, v in t.items()} for t in result.details["taken_offers"]]}
                    if result.details.get("taken_offers") else {})}
    elif action == "flow-balance":
        specs = [(_lane_pool(leg["pool"]), _bytes32(str(leg["assetIn"])), _bytes32(str(leg["assetOut"])), int(leg["amountIn"]))
                 for leg in payload["legs"]]
        start_raw = str(payload.get("startAsset") or "").strip()
        start = _bytes32(start_raw) if start_raw and start_raw.lower() not in ("txch", "xch", "0" * 64) else ZERO_32
        surplus = surplus_ph if surplus_ph is not None else specs[0][0].protocol_ph
        result = rt.flow_balance(specs, offer, height, surplus, start, fee_bps, fee_ph, preview=preview)
        extra = {"leg_amounts": [[str(a), str(b)] for a, b in result.details["leg_amounts"]],
                 "total_out": str(result.details["total_out"])}
    elif action == "routed-deposit":
        target = _lane_pool(payload["pool"])
        sales = [([_lane_pool(entry) for entry in sale["pools"]], [_bytes32(str(a)) for a in sale["path"]], int(sale["amountIn"]))
                 for sale in payload.get("sales", [])]
        result = rt.routed_deposit(target, sales, offer, height,
                               surplus_ph if surplus_ph is not None else target.protocol_ph, fee_bps, fee_ph, preview=preview)
        target_after = next(p for p in result.pools if p.launcher_id == target.launcher_id)
        extra = {"target": _snapshot_of(target_after),
                 "deposits": {asset: str(amount) for asset, amount in result.details["deposits"].items()},
                 "minted": str(result.details["minted"]), "backing": str(result.details["backing"]),
                 "sale_outputs": [str(a) for a in result.details["sale_outputs"]],
                 "leftover_xch": str(result.details["leftover_xch"])}
    elif action == "vault-route":
        swap_pool, vault = _lane_pool(payload["swapPool"]), _lane_pool(payload["vault"])
        result = rt.vault_route(swap_pool, _bytes32(str(payload["assetIn"])), vault, offer, height,
                                  surplus_ph if surplus_ph is not None else vault.protocol_ph, fee_bps, fee_ph, preview=preview)
        extra = {"swap_out": str(result.details["swap_out"]), "redeemed": str(result.details["redeemed"])}
    else:
        raise ValueError(f"unknown route lane {action!r}")
    if preview:
        return {
            "success": True, "action": action, "preview": True,
            "pools": [_snapshot_of(pool) for pool in result.pools],
            "forge": {k: v for k, v in result.details.items() if k not in ("deposits",)},
            **extra,
        }
    return {
        "success": True,
        "action": action,
        "transaction_id": result.bundle.name().hex(),
        "bundle": result.bundle.to_json_dict(),
        "pools": [_snapshot_of(pool) for pool in result.pools],
        "forge": {k: v for k, v in result.details.items() if k not in ("deposits",)},
        **extra,
    }


def _route_hex(asset) -> str:
    return _bytes32(str(asset)).hex()


def _probe_asset(raw: Any) -> bytes32 | None:
    text = str(raw or "").strip().lower().removeprefix("0x")
    return None if text in ("", "txch", "xch", "0" * 64) else _bytes32(text)


def _probe_offer(probe: dict[str, Any]) -> Offer:
    """A stand-in offer for a preview: keyless `(1)` coins with fabricated parents creating
    the settlements of `offered` (asset -> amount) and asking one unit of each `requested`
    asset. It is never validated or pushed; the composer only reads the settlement amounts
    and the requested assets to size the route."""
    from chia.types.blockchain_format.program import Program
    from chia.types.coin_spend import make_spend
    from chia.wallet.cat_wallet.cat_utils import SpendableCAT, unsigned_spend_bundle_for_spendable_cats
    from chia.wallet.conditions import CreateCoin
    from chia.wallet.puzzle_drivers import PuzzleInfo
    from chia.wallet.trading.offer import OFFER_MOD_HASH
    from chia_rs import SpendBundle

    identity = Program.to(1)
    identity_hash = identity.get_tree_hash()
    trader_ph = bytes32(b"\x11" * 32)
    coins, spends = [], []
    for i, (asset_raw, amount_raw) in enumerate(dict(probe.get("offered") or {}).items()):
        asset, amount = _probe_asset(asset_raw), int(amount_raw)
        if amount <= 0:
            raise ValueError("a probe offers positive amounts only")
        grand = bytes32(bytes([0x50 + i]) * 32)
        conditions = Program.to([[51, OFFER_MOD_HASH, amount]])
        if asset is None:
            coin = Coin(grand, identity_hash, uint64(amount))
            spends.append(make_spend(coin, identity, conditions))
        else:
            # the CAT layer checks the coin's parent against its lineage proof, so the
            # fabricated parent is a CAT of the same inner at the grandparent
            outer = construct_cat_puzzle(CAT_MOD, asset, identity).get_tree_hash()
            coin = Coin(Coin(grand, outer, uint64(amount)).name(), outer, uint64(amount))
            spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
                coin, asset, identity, conditions,
                lineage_proof=LineageProof(grand, identity_hash, uint64(amount)))]).coin_spends)
        coins.append(coin)
    requested = {_probe_asset(raw): [CreateCoin(trader_ph, uint64(1), [trader_ph])] for raw in probe.get("requested") or []}
    if not coins or not requested:
        raise ValueError("a probe needs offered amounts and at least one requested asset")
    driver = {a: PuzzleInfo({"type": "CAT", "tail": "0x" + a.hex()}) for a in requested if a is not None}
    return Offer(Offer.notarize_payments(requested, coins), SpendBundle(spends, G2Element()), driver)


CREATE_LANES = ("prepare-create", "create", "commit-create")


def _lane_registry(record: dict[str, Any], drv):
    """The registry singleton from the registry driver's record (its `registry` section)."""
    from dataclasses import replace as _replace
    from chia.wallet.lineage_proof import LineageProof
    from chia_rs.sized_ints import uint64 as _u64
    # Every curried constant, the protocol (dev fee) recipient included: left out, it fell
    # back to the treasury, which rebuilt the RIGHT registry only while the two were the
    # same wallet. Launched with separate addresses (2026-10-05), it would rebuild a
    # registry that does not exist and every site creation would fail.
    # The scale and window stay the driver's constants, which every V14 record carries:
    # the record reaches here through JavaScript (api/forge-create.js), and JSON there
    # turns price_scale = 2**64 into 18446744073709552000.
    extra = {}
    if record.get("protocol_ph"):
        extra["protocol_ph"] = _bytes32(record["protocol_ph"])
    reg = drv.make_registry(creation_fee=int(record["creation_fee"]), treasury_ph=_bytes32(record["treasury_ph"]),
                            launcher_parent=_bytes32(record["launcher_parent"]), state=list(record["state"]), **extra)
    coin = _coin(record["coin"])
    lin = record["lineage"]
    lineage = LineageProof(_bytes32(lin["parent_name"]), None if lin.get("inner_puzzle_hash") is None else _bytes32(lin["inner_puzzle_hash"]),
                           _u64(int(lin["amount"])))
    return _replace(reg, coin=coin, lineage=lineage)


def _creator_coins(payload: dict[str, Any], cre):
    """The creator's coins with their own puzzle reveals: {xch: {coin, puzzle_reveal}, cats: [{asset_id, coin, inner_puzzle, lineage_proof}]}.

    `cre` is the lane's creation module. This read a module-level `cre` that does not
    exist, so every prepare-create through this dispatcher raised NameError (found
    2026-10-01, the first time a creation ran through api/forge-create end to end)."""
    from chia.types.blockchain_format.program import Program as _P
    from chia.wallet.lineage_proof import LineageProof
    from chia_rs.sized_ints import uint64 as _u64
    xch_in = payload["creator"]["xch"]
    xch = cre.CreatorXch(_coin(xch_in["coin"]), _P.from_bytes(bytes.fromhex(str(xch_in["puzzle_reveal"]).removeprefix("0x"))))
    cats = {}
    for c in payload["creator"].get("cats", []):
        asset = _bytes32(str(c["asset_id"]))
        lp = c["lineage_proof"]
        cats[asset] = cre.CreatorCat(asset, _coin(c["coin"]), _P.from_bytes(bytes.fromhex(str(c["inner_puzzle"]).removeprefix("0x"))),
                                      LineageProof(_bytes32(lp["parent_name"]), _bytes32(lp["inner_puzzle_hash"]), _u64(int(lp["amount"]))))
    return xch, cats


def _creation_config(payload: dict[str, Any], cre) -> "cre.CreationConfig":
    cfg = payload["config"]
    assets = [None if str(a).lower() in ("txch", "xch", "0" * 64, "") else _bytes32(str(a)) for a in cfg["asset_ids"]]
    reserves = [int(x) for x in cfg["reserves"]]
    weights = [int(w) for w in cfg.get("weights") or [1] * len(assets)]
    total_lp = int(cfg.get("total_lp") or (min(reserves) * int(cfg.get("lp_ratio") or 1)))
    fee_bps = int(cfg.get("fee_bps", 30))
    # a name or symbol left empty is derived the way every pool's default is
    import forge_v16_index as _idx      # the LIVE lane: this named V13's while V14 shipped
    canonical = cre.canonical(cre.CreationConfig(assets, reserves, weights, fee_bps, 0, ZERO_32, total_lp))
    hex_ids = [("00" * 32) if a is None else a.hex() for a in canonical.asset_ids]
    state = {"pools": []}
    try:
        import json as _json
        import forge_network as _net
        _default = _net.record_path("v16")
        state = _json.loads(Path(payload.get("record_path") or _default).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- names of nested LP assets fall back to their ids
        pass
    name = str(cfg.get("name") or "") or _idx.emoji_name(hex_ids, canonical.weights, fee_bps, state)
    symbol = str(cfg.get("symbol") or "") or _idx.pool_symbol(hex_ids, canonical.weights, state)
    dao_raw = str(cfg.get("dao_puzzle_hash") or "").strip()
    return cre.CreationConfig(assets, reserves, weights, fee_bps, int(cfg.get("protocol_fee_bps", 5)),
                               _bytes32(str(cfg["protocol_puzzle_hash"])), total_lp, name, symbol,
                               dao_ph=_bytes32(dao_raw) if dao_raw and set(dao_raw.removeprefix("0x")) != {"0"} else ZERO_32,
                               dao_fee_bps=int(cfg.get("dao_fee_bps") or 0))


def _build_lane_create(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Offer-free, keyless creation (phase 8.1): `prepare-create` returns the creator's spends
    to sign and the pool they will own; `create` takes the signature (and the spends as the
    wallet signed them) and returns the bundle plus the record patch the caller commits after
    the push confirms; `commit-create` applies that patch to the registry driver's record."""
    off, rt, cre, drv = _lane(payload)
    if action == "commit-create":
        return cre.commit_record(payload["record_path"], payload["record_patch"])
    registry = _lane_registry(payload["registry"], drv)
    xch, cats = _creator_coins(payload, cre)
    # The creation gate's key proof: approved keys that sign inside the creator's spend
    # (forge_v16_create.plan). Passed only when present, so a kept lane without it still plans.
    proof_keys = [bytes.fromhex(str(k).lower().removeprefix("0x")) for k in (payload["creator"].get("gate_proof_keys") or [])]
    if any(len(k) != 48 for k in proof_keys) or len(proof_keys) > 4:
        raise ValueError("gate_proof_keys are 1 to 4 G1 public keys of 48 bytes")
    plan = cre.plan(registry, payload["registry"]["slots"], _creation_config(payload, cre), xch, cats,
                     _bytes32(str(payload["recipient_puzzle_hash"])), int(payload.get("network_fee") or 0),
                     **({"gate_proof_keys": tuple(proof_keys)} if proof_keys else {}))
    out = {"success": True, "action": action, **cre.plan_json(plan)}
    if action == "create":
        def _hex0x(v: Any) -> str:
            text = str(v)
            return text if text.startswith("0x") else "0x" + text

        def _spend(cs: dict[str, Any]) -> CoinSpend:
            # Sage returns hex without the 0x prefix chia's parser insists on
            coin = cs["coin"]
            return CoinSpend.from_json_dict({
                "coin": {"parent_coin_info": _hex0x(coin["parent_coin_info"]), "puzzle_hash": _hex0x(coin["puzzle_hash"]),
                         "amount": int(coin["amount"])},
                "puzzle_reveal": _hex0x(cs["puzzle_reveal"]), "solution": _hex0x(cs["solution"]),
            })
        signed = [_spend(cs) for cs in payload.get("signed_creator_spends") or []] or None
        signature = G2Element.from_bytes(bytes.fromhex(str(payload["aggregated_signature"]).removeprefix("0x")))
        bundle = cre.finalize(plan, signature, signed)
        out.update({"transaction_id": bundle.name().hex(), "bundle": bundle.to_json_dict(),
                    "record_patch": cre.record_patch(plan, payload["registry"])})
    return out


def build(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", "")).lower()
    if action in CREATE_LANES and isinstance(payload.get("registry"), dict) or action == "commit-create":
        return _build_lane_create(action, payload)
    if payload.get("preview") and not payload.get("offer") and isinstance(payload.get("probe"), dict):
        # A preview sized from amounts alone: a probe offer stands in for the trader's, so
        # a page can ask what a route releases -- and what it must therefore ask for
        # exactly -- before the wallet has built anything (F3, 2026-10-07).
        offer = _probe_offer(payload["probe"])
    else:
        offer = offer_from_bech32(payload["offer"])
    # The action-layer revisions (14 = V13, 15 = V14) take the versioned lane; _lane() picks which.
    if any(int(snapshot.get("protocol_version") or 0) in _LANES for snapshot in _pool_snapshots(payload)):
        return _build_lane(action, payload, offer)
    if action in ("prepare-create", "finalize-create"):
        signer_public_key = G1Element.from_bytes(
            bytes.fromhex(str(payload["signer_public_key"]).removeprefix("0x"))
        )
        preparation = prepare_create_v3(
            offer,
            _launch_config(payload.get("config") or payload["launch_config"]),
            signer_public_key,
            int(payload["current_height"]),
        )
        common = {
            "success": True,
            "action": action,
            "launch_intent": _launch_intent_json(preparation.intent),
            "launcher_id": preparation.launcher_id.hex(),
            "lp_asset_id": preparation.lp_asset_id.hex(),
            "guard_coin_id": preparation.guard_coin.name().hex(),
            "guard_coin_spend": preparation.guard_spend.to_json_dict(),
        }
        if action == "prepare-create":
            return {
                **common,
                "signing_request": {
                    "coinSpends": [preparation.guard_spend.to_json_dict()],
                    "partialSign": True,
                },
            }
        signature_hex = str(payload["guard_signature"]).removeprefix("0x")
        finalized = finalize_create_offer(
            preparation,
            G2Element.from_bytes(bytes.fromhex(signature_hex)),
        )
        return {
            **common,
            "offer": finalized.to_bech32(),
            "aggregated_signature": bytes(finalized.to_spend_bundle().aggregated_signature).hex(),
        }

    if action == "create":
        result = build_create_v3(
            offer,
            _launch_intent(payload["launch_intent"]),
            G1Element.from_bytes(bytes.fromhex(str(payload["signer_public_key"]).removeprefix("0x"))),
            int(payload["current_height"]),
        )
        return {
            "success": True,
            "action": action,
            "bundle": result.bundle.to_json_dict(),
            "guard_coin_id": result.guard_coin.name().hex(),
            "pool": _pool_json(result.pool),
        }

    if action == "multihop-swap":
        # Separate lane from the single-pool `swap` action: it chains N pool
        # spends into one atomic bundle. See forge_multihop_swap for why the
        # single-pool builder cannot express an intermediate leg.
        result = build_multihop_swap(
            [_pool(entry) for entry in payload["pools"]],
            [_bytes32(str(asset_id)) for asset_id in payload["path"]],
            offer,
            *_dev_fee(payload),
        )
        return {
            "success": True,
            "action": action,
            "transaction_id": result.bundle.name().hex(),
            "bundle": result.bundle.to_json_dict(),
            "pools": [_pool_json(pool) for pool in result.pools],
            "amounts": [str(amount) for amount in result.amounts],
            "dev_fee_collected": str(result.dev_fee_collected),
        }

    if action == "vault-route":
        # A swap chained into an LP redemption. The vault cannot trade on its
        # own, so this is the only way its liquidity is reachable, and the LP
        # between the legs is created and burned inside the one bundle.
        result = build_swap_then_redeem(
            _pool(payload["swapPool"]),
            _bytes32(str(payload["assetIn"])),
            _pool(payload["vault"]),
            offer,
        )
        return {
            "success": True,
            "action": action,
            "transaction_id": result.bundle.name().hex(),
            "bundle": result.bundle.to_json_dict(),
            "pools": [_pool_json(pool) for pool in result.pools],
            "swap_out": str(result.swap_out),
            "redeemed": str(result.redeemed),
        }

    if action == "flow-balance":
        # One net swap per pool -- the netted equilibrium of overlapping
        # balancing cycles. See forge_flow_balance for how one producer's exit
        # coin serves several consuming reserves.
        specs = [
            FlowLegSpec(
                _pool(leg["pool"]),
                _bytes32(str(leg["assetIn"])),
                _bytes32(str(leg["assetOut"])),
                int(leg["amountIn"]),
            )
            for leg in payload["legs"]
        ]
        # A flow does not have to be rooted in XCH. `build_flow_balance` has
        # always taken a start asset -- the entry the Offer funds and the exit
        # it is paid back in -- but this bridge never forwarded one, so every
        # flow was assumed native and a CAT-denominated cycle (t8 -> LP -> t8
        # across a vault and its LP pool) was refused as "Offer does not
        # provide the start asset".
        start_raw = str(payload.get("startAsset") or "").strip()
        start_asset = _bytes32(start_raw) if start_raw and start_raw.lower() not in (
            "txch", "xch", "0" * 64) else ZERO_32
        result = build_flow_balance(specs, offer, start_asset)
        return {
            "success": True,
            "action": action,
            "transaction_id": result.bundle.name().hex(),
            "bundle": result.bundle.to_json_dict(),
            "pools": [_pool_json(pool) for pool in result.pools],
            "leg_amounts": [[str(a), str(b)] for a, b in result.leg_amounts],
            "total_out": str(result.total_out),
        }

    if action == "routed-deposit":
        # The depositor's uneven assets, the market sales that balance them,
        # and the multi-asset MODE_ADD, as one bundle. See
        # forge_routed_deposit for why the imbalance fee is worth routing
        # around and how the mint's XCH backing is carved out.
        sales = [
            DepositSale(
                [_pool(entry) for entry in sale["pools"]],
                [_bytes32(str(asset_id)) for asset_id in sale["path"]],
                int(sale["amountIn"]),
            )
            for sale in payload.get("sales", [])
        ]
        result = build_routed_deposit(_pool(payload["pool"]), sales, offer)
        return {
            "success": True,
            "action": action,
            "transaction_id": result.bundle.name().hex(),
            "bundle": result.bundle.to_json_dict(),
            "pools": [_pool_json(pool) for pool in result.pools],
            "target": _pool_json(result.target),
            "deposits": {asset.hex(): str(amount) for asset, amount in result.deposits.items()},
            "minted": str(result.minted),
            "backing": str(result.backing),
            "sale_outputs": [str(amount) for amount in result.sale_outputs],
            "leftover_xch": str(result.leftover_xch),
        }

    if action == "split-swap":
        # Parallel branches settled together; see forge_split_swap for how one
        # Offer's entry coin and requested payment are shared across them.
        specs = [
            SplitBranchSpec(
                [_pool(entry) for entry in branch["pools"]],
                [_bytes32(str(asset_id)) for asset_id in branch["path"]],
                int(branch["amountIn"]),
            )
            for branch in payload["branches"]
        ]
        result = build_split_swap(specs, offer, *_dev_fee(payload))
        return {
            "success": True,
            "action": action,
            "transaction_id": result.bundle.name().hex(),
            "bundle": result.bundle.to_json_dict(),
            "pools": [_pool_json(pool) for pool in result.pools],
            "branch_amounts": [[str(amount) for amount in amounts] for amounts in result.branch_amounts],
            "total_out": str(result.total_out),
            "dev_fee_collected": str(result.dev_fee_collected),
        }

    modes = {"add": MODE_ADD, "swap": MODE_SWAP, "remove": MODE_REMOVE}
    if action not in modes:
        raise ValueError(
            "action must be prepare-create, finalize-create, create, add, swap, "
            "remove, multihop-swap, split-swap, flow-balance, routed-deposit, "
            "or vault-route"
        )
    pool = _pool(payload["pool"])
    # build_transition_v3 is the original two-asset builder and predates the V8
    # protocol-fee config layout, so V8+ pools must go through the N-asset
    # build_transition (which reads the fee fields and, from V9, binds the LP
    # action coin). Older pools keep the v3 path they were tested on.
    if int(pool.config[0]) >= 8:
        result = build_transition(pool, offer, modes[action], *_dev_fee(payload))
    else:
        result = build_transition_v3(pool, offer, modes[action])
    return {
        "success": True,
        "action": action,
        "transaction_id": result.bundle.name().hex(),
        "bundle": result.bundle.to_json_dict(),
        "pool": _pool_json(result.pool),
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        print(json.dumps(build(payload)))
        return 0
    except Exception as error:
        # str() on some exceptions (notably KeyError) renders only the payload,
        # so a missing dict key reached the UI as a bare "<bytes32: ...>" with
        # no indication of what went wrong. Always name the type, and keep a
        # traceback on stderr for the server log.
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({
            "success": False,
            "error": f"[aWizard] {type(error).__name__}: {error}",
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())