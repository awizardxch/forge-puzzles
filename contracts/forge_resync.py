#!/usr/bin/env python3
"""Resync a pool's deployment-index snapshot to its current on-chain tip.

The responder persists a successor snapshot at push time; a crash between push
and persist, a mempool eviction, or a reorg leaves the index pointing at a coin
that is spent (or never confirmed) while the real singleton moved on. This
script walks the singleton lineage from the stale snapshot to the unspent tip,
rebuilds the tip's snapshot, and PROVES it: the rebuilt config/state re-curried
through the real puzzles must hash to the tip's on-chain puzzle hash, or the
script refuses. It can therefore never emit a snapshot the chain does not
confirm.

Config comes from the stale snapshot via forge_stdin's `_pool`, which already
validates the module and reserve hashes for every revision V4..V10 — nothing
here re-implements config parsing. State is reconstructed from the walked
reserve tips plus the stale `total_lp`; the final hash check verifies all of
it, so a transition that changed the LP supply (a lost add or remove) fails
the check and is reported for manual reconciliation rather than guessed at.

stdin:  JSON {launcher_id, pool: <stale builder-format snapshot>, node_url?}
stdout: JSON {success, changed, tip_coin_id, snapshot} on success;
        {success: false, error, code} on failure.
exit:   0 success, 1 failure
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from forge_stdin import _pool, _pool_json

ZERO32_HEX = "00" * 32
DEFAULT_NODE = "https://testnet11.api.coinset.org"
MAX_WALK_DEPTH = 32


class ResyncError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _strip_0x(value: Any) -> str:
    text = str(value or "")
    return text[2:] if text.lower().startswith("0x") else text


def _rpc(node_url: str, route: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{node_url.rstrip('/')}/{route}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "aWizard-Forge/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _coin_record(node_url: str, coin_id: str) -> dict | None:
    return _rpc(node_url, "get_coin_record_by_name", {"name": _strip_0x(coin_id)}).get("coin_record")


def _children(node_url: str, coin_id: str) -> list[dict]:
    records = _rpc(
        node_url,
        "get_coin_records_by_parent_ids",
        {"parent_ids": [_strip_0x(coin_id)]},
    ).get("coin_records")
    return records if isinstance(records, list) else []


def _coin_id_of(record: dict) -> str:
    """Coin id via the canonical chia Coin hashing — never hand-rolled."""
    from chia_rs import Coin
    from chia_rs.sized_bytes import bytes32

    coin = record["coin"]
    return Coin(
        bytes32.fromhex(_strip_0x(coin["parent_coin_info"])),
        bytes32.fromhex(_strip_0x(coin["puzzle_hash"])),
        int(coin["amount"]),
    ).name().hex()


def _walk_to_unspent_tip(
    node_url: str,
    start_coin_id: str,
    launcher_id: str,
    child_filter,
) -> tuple[str, dict]:
    """Follow spends from start (falling back to the launcher) to the unspent tip."""
    coin_id = _strip_0x(start_coin_id)
    record = _coin_record(node_url, coin_id)
    if record is None:
        # The indexed coin never confirmed (evicted push). Restart from the
        # launcher, whose spend created the real eve singleton.
        coin_id = _strip_0x(launcher_id)
        record = _coin_record(node_url, coin_id)
        if record is None:
            raise ResyncError(f"Neither the indexed coin nor launcher {launcher_id} exists on-chain.", "LAUNCHER_NOT_FOUND")

    for _ in range(MAX_WALK_DEPTH):
        if not record.get("spent"):
            return coin_id, record
        candidates = [child for child in _children(node_url, coin_id) if child_filter(child)]
        if not candidates:
            raise ResyncError(f"Coin {coin_id} is spent but no successor matches the walk filter.", "LINEAGE_BROKEN")
        candidates.sort(key=lambda child: int(child.get("confirmed_block_index") or 0))
        record = candidates[-1]
        coin_id = _coin_id_of(record)
    raise ResyncError(f"Singleton lineage deeper than {MAX_WALK_DEPTH} from {start_coin_id}.", "WALK_TOO_DEEP")


def _pool_lineage(node_url: str, launcher_id: str, tip_coin: dict) -> tuple[str, str | None]:
    """(lineage parent name, parent inner puzzle hash | None for the eve)."""
    from chia.types.blockchain_format.program import Program

    parent_id = _strip_0x(tip_coin["parent_coin_info"])
    parent = _coin_record(node_url, parent_id)
    if parent is None:
        raise ResyncError(f"Pool parent {parent_id} not found on-chain.", "LINEAGE_BROKEN")

    if parent_id == _strip_0x(launcher_id):
        return _strip_0x(parent["coin"]["parent_coin_info"]), None

    spent_height = int(parent.get("spent_block_index") or 0)
    if not spent_height:
        raise ResyncError(f"Pool parent {parent_id} is unspent; cannot derive lineage.", "LINEAGE_BROKEN")
    solution = _rpc(node_url, "get_puzzle_and_solution", {"coin_id": parent_id, "height": spent_height})
    reveal = _strip_0x((solution.get("coin_solution") or {}).get("puzzle_reveal", ""))
    if not reveal:
        raise ResyncError(f"Missing puzzle reveal for pool parent {parent_id}.", "LINEAGE_BROKEN")
    _mod, args = Program.from_bytes(bytes.fromhex(reveal)).uncurry()
    inner = args.rest().first()
    return _strip_0x(parent["coin"]["parent_coin_info"]), bytes(inner.get_tree_hash()).hex()


def _reserve_tip_entry(node_url: str, stale_entry: dict, launcher_id: str) -> tuple[dict, bool]:
    """Walk one reserve coin to its unspent tip; returns (entry, moved)."""
    stale_coin = stale_entry["coin"]
    reserve_puzzle_hash = _strip_0x(stale_coin["puzzle_hash"]).lower()
    # The reserve puzzle is constant for the pool+asset, so the successor
    # always sits at the same puzzle hash.
    stale_coin_id = _coin_id_of({"coin": stale_coin})

    def is_reserve(child: dict) -> bool:
        return _strip_0x(child["coin"]["puzzle_hash"]).lower() == reserve_puzzle_hash

    tip_id, tip_record = _walk_to_unspent_tip(node_url, stale_coin_id, launcher_id, is_reserve)
    if tip_id == stale_coin_id:
        return stale_entry, False

    tip_coin = tip_record["coin"]
    parent_id = _strip_0x(tip_coin["parent_coin_info"])
    parent = _coin_record(node_url, parent_id)
    if parent is None:
        raise ResyncError(f"Reserve parent {parent_id} not found on-chain.", "LINEAGE_BROKEN")
    parent_coin = parent["coin"]
    if _strip_0x(parent_coin["puzzle_hash"]).lower() != reserve_puzzle_hash:
        raise ResyncError(
            f"Reserve tip parent {parent_id} is not the previous reserve coin.", "LINEAGE_BROKEN",
        )

    stale_lineage = stale_entry.get("lineage_proof") or {}
    native = stale_lineage.get("inner_puzzle_hash") in (None, "")
    if native:
        lineage = {
            "parent_name": parent_id,
            "inner_puzzle_hash": None,
            "amount": int(parent_coin["amount"]),
        }
    else:
        # A CAT reserve's parent is the previous reserve coin, whose inner
        # puzzle is the (constant) reserve inner — recorded in the stale proof.
        lineage = {
            "parent_name": _strip_0x(parent_coin["parent_coin_info"]),
            "inner_puzzle_hash": _strip_0x(stale_lineage["inner_puzzle_hash"]),
            "amount": int(parent_coin["amount"]),
        }

    return (
        {
            "coin": {
                "parent_coin_info": _strip_0x(tip_coin["parent_coin_info"]),
                "puzzle_hash": _strip_0x(tip_coin["puzzle_hash"]),
                "amount": int(tip_coin["amount"]),
            },
            "lineage_proof": lineage,
        },
        True,
    )


def resync(payload: dict[str, Any]) -> dict[str, Any]:
    stale_snapshot = payload["pool"]
    # A V11-or-later pool's state is curried, not read from reserve tips, so its tip is
    # rebuilt by replaying the spends between the snapshot and the tip. Each revision
    # has its own replay module because each has its own leaf set and solution shape.
    #
    # This used to test for 11 alone. V12 is protocol 13, so every V12 pool fell past it
    # into the V4..V10 branch and came back "V13 pools are not V3Pool snapshots" -- and
    # because a resync is what the browser does when it finds a pool behind the chain,
    # that refusal stopped the swap rather than repairing it. Add the version here when
    # a revision ships; there is no sensible default for an unknown one.
    REPLAY_LANES = {11: "forge_v11_resync", 13: "forge_v12_resync", 14: "forge_v13_resync", 15: "forge_v14_resync"}
    lane = REPLAY_LANES.get(int((stale_snapshot or {}).get("protocol_version") or 0))
    if lane is not None:
        import importlib
        module = importlib.import_module(lane)
        try:
            return module.resync(payload)
        except module.ResyncError as exc:
            raise ResyncError(str(exc), exc.code) from exc
    node_url = str(payload.get("node_url") or DEFAULT_NODE)
    launcher_id = _strip_0x(payload["launcher_id"]).lower()

    # Validates config, module hash, and reserve hashes for V4..V10 — the
    # stale snapshot must itself be authentic before it can seed a resync.
    stale_pool = _pool(stale_snapshot)
    if stale_pool.launcher_id.hex() != launcher_id:
        raise ResyncError("Stale snapshot names a different launcher.", "LAUNCHER_MISMATCH")

    def is_singleton(child: dict) -> bool:
        return int(child["coin"]["amount"]) == 1

    tip_id, tip_record = _walk_to_unspent_tip(
        node_url, _strip_0x(stale_snapshot["pool_coin_id"]), launcher_id, is_singleton,
    )
    stale_tip_id = _strip_0x(stale_snapshot["pool_coin_id"]).lower()
    if tip_id.lower() == stale_tip_id:
        return {"success": True, "changed": False, "tip_coin_id": tip_id, "snapshot": stale_snapshot}

    tip_coin = tip_record["coin"]
    lineage_parent, parent_inner_hash = _pool_lineage(node_url, launcher_id, tip_coin)

    reserves = []
    for entry in stale_snapshot["reserves"]:
        walked, _moved = _reserve_tip_entry(node_url, entry, launcher_id)
        reserves.append(walked)

    candidate = dict(stale_snapshot)
    candidate["pool_coin_id"] = tip_id
    candidate["pool_coin"] = {
        "parent_coin_info": _strip_0x(tip_coin["parent_coin_info"]),
        "puzzle_hash": _strip_0x(tip_coin["puzzle_hash"]),
        "amount": int(tip_coin["amount"]),
    }
    candidate["pool_lineage_parent_name"] = lineage_parent
    candidate["parent_inner_puzzle_hash"] = parent_inner_hash
    candidate["reserves"] = reserves
    # total_lp is carried from the stale snapshot: swaps preserve it, and the
    # hash check below catches the case where it moved.

    rebuilt = _pool(candidate)
    rebuilt_ph = rebuilt.pool.puzzle.get_tree_hash().hex()
    live_ph = _strip_0x(tip_coin["puzzle_hash"]).lower()
    if rebuilt_ph.lower() != live_ph:
        raise ResyncError(
            "Rebuilt tip state does not hash to the on-chain puzzle "
            f"({rebuilt_ph} != {live_ph}). A transition that changed the LP supply "
            "(a lost add or remove) cannot be resynced automatically; reconcile manually.",
            "TIP_STATE_MISMATCH",
        )

    snapshot = _pool_json(rebuilt)
    return {"success": True, "changed": True, "tip_coin_id": tip_id, "snapshot": snapshot}


def main() -> int:
    try:
        print(json.dumps(resync(json.loads(sys.stdin.read() or "{}"))))
        return 0
    except ResyncError as exc:
        print(json.dumps({"success": False, "error": str(exc), "code": exc.code}))
        return 1
    except Exception as exc:  # noqa: BLE001 - the responder needs the type name
        import traceback

        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}", "code": "RESYNC_ERROR"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
