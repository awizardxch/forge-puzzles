#!/usr/bin/env python3
"""Resync a V11 pool's snapshot to its on-chain tip by replaying its spends.

A V11 singleton's state is curried into its inner puzzle, so an unspent tip
cannot be read directly; but every spend between the stale snapshot and the tip
is on chain with its leaves and solutions. This walks them: while the recorded
coin is spent, fetch that spend, decode which leaves ran (the action layer's
solution carries the leaf programs and their solutions), run them on the
recorded state exactly as the puzzle did, and advance. The rebuilt tip is
proven the same way `snapshot_to_pool` proves any snapshot: the re-curried inner
must hash to the live coin's puzzle hash.

Same contract as forge_resync.py, which dispatches here for protocol_version 11:

stdin:  JSON {launcher_id, pool: <stale V11 snapshot>, node_url?}
stdout: JSON {success, changed, tip_coin_id, snapshot} | {success: false, error, code}
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402

import forge_v13_driver as drv  # noqa: E402
from forge_v13_driver import replace  # noqa: E402
from forge_v13_offer import pool_to_snapshot, snapshot_to_pool  # noqa: E402

DEFAULT_NODE = "https://testnet11.api.coinset.org"
MAX_WALK_DEPTH = 64


class ResyncError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _strip(value: Any) -> str:
    text = str(value or "")
    return text[2:] if text.lower().startswith("0x") else text


def _rpc(node_url: str, route: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{node_url.rstrip('/')}/{route}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "aWizard-Forge/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _leaf_names(pool: drv.V13Pool) -> dict[bytes, str]:
    """Uncurried leaf module hash -> leaf name, for the leaves this pool commits to."""
    return {bytes(prog.uncurry()[0].get_tree_hash()): name for name, prog in pool.leaves.items()}


def replay_spend(pool: drv.V13Pool, solution: Program) -> tuple[drv.V13Pool, list[str]]:
    """Apply one on-chain spend of `pool.coin` (its singleton solution) to `pool`."""
    names_by_mod = _leaf_names(pool)
    inner_solution = list(solution.as_iter())[2]
    puzzles, _selectors, solutions = list(inner_solution.as_iter())[:3]
    ephemeral, new_state, names = None, pool.state, []
    for leaf_prog, leaf_sol in zip(puzzles.as_iter(), solutions.as_iter()):
        mod, _ = leaf_prog.uncurry()
        name = names_by_mod.get(bytes(mod.get_tree_hash()))
        if name is None:
            raise ResyncError("an on-chain spend ran a leaf this pool does not commit to", "UNKNOWN_LEAF")
        names.append(name)
        # V13: an on-chain leaf solution is `[h, birth, ...]`, and `run_leaf` inserts
        # `pool.birth` after `h` itself. Passing the chain's solution through unchanged
        # therefore gives `[h, pool.birth, birth, ...]` -- every later argument shifts, the
        # replayed state is wrong, and the successor it derives is a coin that never
        # existed ("tip unknown"). Drop the birth the chain already carries, and take it as
        # the pool's own: consensus accepted that spend, so the value it asserted IS this
        # coin's birth height.
        raw = list(leaf_sol.as_iter())
        if len(raw) >= 2:
            pool = replace(pool, birth=int(raw[1].as_int()))
            raw = [raw[0], *raw[2:]]
        new_state, _tagged, _base, ephemeral = drv.run_leaf(
            pool, name, raw, ephemeral=ephemeral, state=new_state)
        new_state = drv.state_to_list(new_state)
    return pool.advance(new_state), names


def resync(payload: dict[str, Any]) -> dict[str, Any]:
    node_url = str(payload.get("node_url") or DEFAULT_NODE)
    launcher_id = _strip(payload["launcher_id"]).lower()
    try:
        pool = snapshot_to_pool(payload["pool"])
    except ValueError as exc:
        raise ResyncError(f"stale snapshot is not an authentic V11 snapshot: {exc}", "SNAPSHOT_INVALID") from exc
    if pool.launcher_id.hex() != launcher_id:
        raise ResyncError("Stale snapshot names a different launcher.", "LAUNCHER_MISMATCH")

    steps: list[str] = []
    for _ in range(MAX_WALK_DEPTH):
        coin_id = pool.coin.name().hex()
        record = (_rpc(node_url, "get_coin_record_by_name", {"name": "0x" + coin_id}) or {}).get("coin_record")
        if not record:
            raise ResyncError(f"pool coin {coin_id[:12]} is unknown to the node", "TIP_UNKNOWN")
        if not record.get("spent"):
            break
        spend = _rpc(node_url, "get_puzzle_and_solution",
                     {"coin_id": "0x" + coin_id, "height": int(record["spent_block_index"])})["coin_solution"]
        solution = Program.from_bytes(bytes.fromhex(_strip(spend["solution"])))
        pool, names = replay_spend(pool, solution)
        steps.append("+".join(names))
    else:
        raise ResyncError(f"more than {MAX_WALK_DEPTH} spends behind the tip; resync in two passes", "WALK_TOO_DEEP")

    tip_id = pool.coin.name().hex()
    live = (_rpc(node_url, "get_coin_record_by_name", {"name": "0x" + tip_id}) or {}).get("coin_record")
    if not live:
        raise ResyncError("replayed tip is not a coin the node knows", "TIP_STATE_MISMATCH")
    if _strip(live["coin"]["puzzle_hash"]).lower() != pool.coin.puzzle_hash.hex():
        raise ResyncError("replayed tip does not hash to the on-chain puzzle", "TIP_STATE_MISMATCH")
    # V13: take the tip's birth height from the CHAIN, not from the replay.
    # `advance()` fills birth in from the state's `last_height`, which is the height the
    # previous spend CLAIMED -- and a spend confirms a block or more after the height it
    # claims, so the two are almost never equal. The next spend asserts
    # ASSERT_MY_BIRTH_HEIGHT against the coin's real confirmed_block_index, so shipping
    # the replay's guess in the snapshot makes the following swap fail on chain with
    # ASSERT_MY_BIRTH_HEIGHT_FAILED. The record-keeping path already reads the confirmed
    # height; this one did not.
    birth = int(live.get("confirmed_block_index") or 0)
    if birth <= 0:
        raise ResyncError("node gave the tip no confirmed height", "TIP_STATE_MISMATCH")
    pool = replace(pool, birth=birth)
    return {"success": True, "changed": bool(steps), "tip_coin_id": tip_id, "steps": steps,
            "birth": birth, "snapshot": pool_to_snapshot(pool)}


def main() -> int:
    try:
        print(json.dumps(resync(json.loads(sys.stdin.read() or "{}"))))
        return 0
    except ResyncError as exc:
        print(json.dumps({"success": False, "error": str(exc), "code": exc.code}))
        return 1
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}", "code": "RESYNC_ERROR"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
