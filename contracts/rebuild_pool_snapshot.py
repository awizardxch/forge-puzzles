#!/usr/bin/env python3
"""Rebuild a missing poolSnapshot for an already-deployed pool.

Pools created before the router persisted their snapshot have no entry for the
transition responder to work from. The current puzzle reveal recorded in the
deployment index fully determines the pool config and state, so the snapshot can
be recovered from it plus the reserve coin records on chain.

The rebuilt config/state are re-hashed and compared against the pool coin's
on-chain puzzle hash before anything is written, so a bad reconstruction fails
loudly rather than producing a snapshot that yields invalid spends.

usage: rebuild_pool_snapshot.py --launcher-coin-id <hex> [--node-url <url>] [--write]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton

ZERO32 = b"\x00" * 32
DEFAULT_NODE = "https://testnet11.api.coinset.org"
JOIN_RULES = {4: "geometric-invariant-v1", 5: "geometric-invariant-v2"}


def strip_0x(value: str) -> str:
    if isinstance(value, str) and value.lower().startswith("0x"):
        return value[2:]
    return value if isinstance(value, str) else ""


def rpc(node_url: str, route: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{node_url.rstrip('/')}/{route}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "aWizard-Forge/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def coin_record(node_url: str, coin_id: str) -> dict:
    record = rpc(node_url, "get_coin_record_by_name", {"name": strip_0x(coin_id)}).get("coin_record")
    if not record:
        raise RuntimeError(f"Coin record not found: {coin_id}")
    return record


def compiled(name: str) -> Program:
    path = Path(__file__).parent / "compiled" / f"{name}.clvm.hex"
    return Program.from_bytes(bytes.fromhex(path.read_text(encoding="ascii").strip()))


def uncurry_pool(puzzle_reveal_hex: str) -> tuple[Program, Program, Program]:
    """Return (singleton_struct, config, state) from a full pool singleton reveal."""
    outer = Program.from_bytes(bytes.fromhex(strip_0x(puzzle_reveal_hex)))
    _singleton_mod, singleton_args = outer.uncurry()
    pool_inner = singleton_args.rest().first()
    # pool_singleton_v{n} is curried as (singleton_struct, config, state).
    _pool_mod, pool_args = pool_inner.uncurry()
    return (
        pool_args.first(),
        pool_args.rest().first(),
        pool_args.rest().rest().first(),
    )


def pool_lineage(node_url: str, launcher_id: str, pool_coin: dict) -> tuple[str, str | None]:
    """Singleton lineage: (parent's parent coin id, parent's inner puzzle hash).

    Only a pool whose parent *is* the launcher may use the eve form with a null
    inner hash; anything further down the chain must name the previous
    singleton's inner puzzle, or the spend asserts the launcher as its parent
    and fails with ASSERT_MY_PARENT_ID_FAILED.
    """
    parent_id = strip_0x(pool_coin["parent_coin_info"])
    parent = coin_record(node_url, parent_id)

    if parent_id == launcher_id:
        return strip_0x(parent["coin"]["parent_coin_info"]), None

    spent_height = int(parent.get("spent_block_index") or 0)
    if not spent_height:
        raise RuntimeError(f"Pool parent {parent_id} is unspent; cannot derive singleton lineage")

    solution = rpc(node_url, "get_puzzle_and_solution", {"coin_id": parent_id, "height": spent_height})
    reveal = strip_0x((solution.get("coin_solution") or {}).get("puzzle_reveal", ""))
    if not reveal:
        raise RuntimeError(f"Missing puzzle reveal for pool parent {parent_id}")

    # puzzle_for_singleton curries (singleton_struct, inner_puzzle).
    _singleton_mod, args = Program.from_bytes(bytes.fromhex(reveal)).uncurry()
    inner_puzzle = args.rest().first()
    return strip_0x(parent["coin"]["parent_coin_info"]), bytes(inner_puzzle.get_tree_hash()).hex()


def lineage_for(node_url: str, coin: dict, is_native: bool) -> dict:
    """CAT spends need the parent's inner puzzle hash; native coins do not."""
    parent_id = strip_0x(coin["parent_coin_info"])
    parent = coin_record(node_url, parent_id)
    parent_coin = parent["coin"]

    if is_native:
        return {
            "parent_name": parent_id,
            "inner_puzzle_hash": None,
            "amount": int(parent_coin["amount"]),
        }

    spent_height = int(parent.get("spent_block_index") or 0)
    if not spent_height:
        raise RuntimeError(f"Reserve parent {parent_id} is unspent; cannot derive lineage proof")

    solution = rpc(node_url, "get_puzzle_and_solution", {"coin_id": parent_id, "height": spent_height})
    reveal = strip_0x((solution.get("coin_solution") or {}).get("puzzle_reveal", ""))
    if not reveal:
        raise RuntimeError(f"Missing puzzle reveal for reserve parent {parent_id}")

    _cat_mod, cat_args = Program.from_bytes(bytes.fromhex(reveal)).uncurry()
    inner_puzzle = cat_args.rest().rest().first()
    return {
        "parent_name": strip_0x(parent_coin["parent_coin_info"]),
        "inner_puzzle_hash": bytes(inner_puzzle.get_tree_hash()).hex(),
        "amount": int(parent_coin["amount"]),
    }


def rebuild(launcher_id: str, node_url: str) -> dict:
    project_root = Path(__file__).parent.parent
    index_path = project_root / ".awizard" / "deployment-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))

    plan_key = f"stage1:{launcher_id}"
    plan = index.get(plan_key)
    if not plan:
        raise ValueError(f"No deployment plan for launcher {launcher_id}")

    batch_key, batch = next(iter((plan.get("batches") or {}).items()))
    reveal_hex = batch.get("currentPuzzleReveal")
    if not reveal_hex:
        raise ValueError("Batch has no currentPuzzleReveal to rebuild from")

    singleton_struct, config, state = uncurry_pool(reveal_hex)

    protocol_version = int(config.at("f").as_int())
    pool_module_hash = bytes(config.at("rf").as_atom())
    asset_ids = [bytes(item.as_atom()) for item in config.at("rrf").as_iter()]
    weights = [int(item.as_int()) for item in config.at("rrrf").as_iter()]
    fee_bps = int(config.at("rrrrf").as_int())
    lp_asset_id = bytes(config.at("rrrrrf").as_atom())
    reserve_inner_hash = bytes(config.at("rrrrrrf").as_atom())

    reserve_states = [
        (bytes(item.at("f").as_atom()), bytes(item.at("rf").as_atom()), int(item.at("rrf").as_int()))
        for item in state.at("f").as_iter()
    ]
    total_lp = int(state.at("rf").as_int())

    reserve_inner = compiled(f"forge_reserve_v{protocol_version}")
    if bytes(reserve_inner.get_tree_hash()) != reserve_inner_hash:
        raise RuntimeError("Compiled reserve puzzle does not match the on-chain pool config")

    # Prove the reconstruction: re-curry and compare to the live puzzle hash.
    pool_mod = compiled(f"pool_singleton_v{protocol_version}")
    rebuilt_inner = pool_mod.curry(singleton_struct, config, state)
    rebuilt_puzzle = puzzle_for_singleton(bytes.fromhex(launcher_id), rebuilt_inner)
    rebuilt_ph = bytes(rebuilt_puzzle.get_tree_hash()).hex()

    pool_coin_id = strip_0x(batch.get("currentCoinId") or "")
    live = coin_record(node_url, pool_coin_id)
    live_ph = strip_0x(live["coin"]["puzzle_hash"])
    if rebuilt_ph != live_ph:
        raise RuntimeError(f"Rebuilt puzzle hash {rebuilt_ph} != on-chain {live_ph}")

    lineage_parent_name, parent_inner_puzzle_hash = pool_lineage(node_url, launcher_id, live["coin"])

    reserves = []
    for asset_id, coin_name, amount in reserve_states:
        is_native = asset_id == ZERO32
        record = coin_record(node_url, coin_name.hex())["coin"]
        expected_ph = (
            bytes(reserve_inner.get_tree_hash())
            if is_native
            else bytes(construct_cat_puzzle(CAT_MOD, asset_id, reserve_inner).get_tree_hash())
        )
        if strip_0x(record["puzzle_hash"]) != expected_ph.hex():
            raise RuntimeError(f"Reserve coin {coin_name.hex()} puzzle hash does not match pool config")
        if int(record["amount"]) != amount:
            raise RuntimeError(f"Reserve coin {coin_name.hex()} amount does not match pool state")

        reserves.append({
            "asset_id": asset_id.hex(),
            "coin": {
                "parent_coin_info": strip_0x(record["parent_coin_info"]),
                "puzzle_hash": strip_0x(record["puzzle_hash"]),
                "amount": int(record["amount"]),
            },
            "lineage_proof": lineage_for(node_url, record, is_native),
        })

    snapshot = {
        "protocol_version": protocol_version,
        "join_rule": JOIN_RULES.get(protocol_version, f"geometric-invariant-v{protocol_version - 3}"),
        "pool_module_hash": pool_module_hash.hex(),
        "reserve_inner_puzzle_hash": reserve_inner_hash.hex(),
        "launcher_id": launcher_id,
        "lp_asset_id": lp_asset_id.hex(),
        "pool_coin_id": pool_coin_id,
        "pool_coin": {
            "parent_coin_info": strip_0x(live["coin"]["parent_coin_info"]),
            "puzzle_hash": live_ph,
            "amount": int(live["coin"]["amount"]),
        },
        "pool_lineage_parent_name": lineage_parent_name,
        "parent_inner_puzzle_hash": parent_inner_puzzle_hash,
        "asset_ids": [asset_id.hex() for asset_id in asset_ids],
        "weights": weights,
        "fee_bps": fee_bps,
        "total_lp": total_lp,
        "reserves": reserves,
        "reserve_puzzle_kinds": ["native" if a == ZERO32 else "cat" for a, _, _ in reserve_states],
        "lp_tail_hash": lp_asset_id.hex(),
    }

    return {"plan_key": plan_key, "batch_key": batch_key, "snapshot": snapshot, "index_path": str(index_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher-coin-id", required=True)
    parser.add_argument("--node-url", default=DEFAULT_NODE)
    parser.add_argument("--write", action="store_true", help="persist the snapshot into the deployment index")
    args = parser.parse_args()

    launcher_id = strip_0x(args.launcher_coin_id.strip().lower())
    result = rebuild(launcher_id, args.node_url)

    if args.write:
        index_path = Path(result["index_path"])
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index[result["plan_key"]]["batches"][result["batch_key"]]["poolSnapshot"] = result["snapshot"]
        index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"success": True, "written": bool(args.write), "snapshot": result["snapshot"]}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        sys.exit(1)
