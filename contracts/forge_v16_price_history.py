#!/usr/bin/env python3
"""A V16 pool's reserves at every state it has held, read back from the chain.

A pool singleton curries its whole state into its inner puzzle
(`ACTION_LAYER.curry(finalizer, merkle_root, state)`), and every SPENT pool coin's
puzzle reveal is on chain. So the reserves a coin held -- and therefore the mid
price every pool reader computes from them -- are read straight out of that reveal:
no replay, no index, nothing that can drift from consensus.

The walk runs backward along the singleton: each pool coin's parent is the coin
before it, and the eve coin's parent is the launcher itself (forge_v16_driver
`make_pool`), which is where it ends. It stops early at any coin the caller already
holds, so a cached history only ever walks the spends that are new since.

The tip is usually unspent, so its state has no reveal yet; the caller already has
it (the deployment index snapshot every Markets figure is computed from) and adds it
as the "now" point. The walk still reports the tip's confirmed height and time, which
is when that current state began.

stdin:  JSON {launcher_id, segments: [{start, budget}], known: [coin ids], node_url?}
stdout: JSON {success, points: [...], segments: [{start, walked, reached, next}], tip}
        point = {coin_id, parent, height, timestamp, spent_height, reserves, total_lp,
                 oracle: {last_height, cums, last_spot}}
        reached = "genesis" | "known" | "budget"
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.singleton import get_inner_puzzle_from_singleton, get_singleton_id_from_puzzle  # noqa: E402

import forge_v16_driver as drv  # noqa: E402
import forge_network as _forge_network  # noqa: E402

DEFAULT_NODE = _forge_network.node_url()
# Spends one request may walk. Two node calls each; a pool's whole history is tens of
# spends, so a first fill finishes in one or two requests and later ones walk a handful.
MAX_BUDGET = 200

Rpc = Callable[[str, dict], dict]


class HistoryError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _strip(value: Any) -> str:
    text = str(value or "")
    return (text[2:] if text.lower().startswith("0x") else text).lower()


def node_rpc(node_url: str) -> Rpc:
    def call(route: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{node_url.rstrip('/')}/{route}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "aWizard-Forge/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    return call


def state_from_reveal(reveal: Program, launcher_id: bytes) -> list:
    """The pool state curried into a V16 pool coin's puzzle reveal.

    Refuses anything that is not this launcher's singleton wrapping the V16 action
    layer: a reveal from another singleton, or a pool of another revision, would
    otherwise be read as prices this pool never quoted.
    """
    singleton_id = get_singleton_id_from_puzzle(reveal)
    if singleton_id is None or bytes(singleton_id) != bytes(launcher_id):
        raise HistoryError("a coin on the walk is not this pool's singleton", "NOT_THIS_POOL")
    inner = get_inner_puzzle_from_singleton(reveal)
    if inner is None:
        raise HistoryError("a pool coin's reveal has no singleton inner puzzle", "NOT_A_POOL")
    mod, args = inner.uncurry()
    if drv.ACTION_LAYER is None or mod.get_tree_hash() != drv.ACTION_LAYER.get_tree_hash():
        raise HistoryError("a pool coin's inner puzzle is not the V16 action layer", "NOT_V16")
    curried = list(args.as_iter())
    if len(curried) != 3:
        raise HistoryError("a pool coin's action layer has an unexpected curry", "NOT_V16")
    return drv.state_to_list(curried[2])


def walk(rpc: Rpc, launcher_id: str, start: str, known: set[str], budget: int) -> dict:
    """Walk back from `start` until the launcher, a known coin, or the budget.

    Returns {points, walked, reached, next, tip}. `points` are newest first; `next` is
    the coin to resume from when the budget ran out.
    """
    launcher = _strip(launcher_id)
    launcher_bytes = bytes.fromhex(launcher)
    coin_id = _strip(start)
    points: list[dict] = []
    tip: dict | None = None
    walked = 0
    while True:
        if coin_id == launcher:
            return {"points": points, "walked": walked, "reached": "genesis", "next": None, "tip": tip}
        if coin_id in known:
            return {"points": points, "walked": walked, "reached": "known", "next": None, "tip": tip}
        if walked >= budget:
            return {"points": points, "walked": walked, "reached": "budget", "next": coin_id, "tip": tip}
        record = (rpc("get_coin_record_by_name", {"name": "0x" + coin_id}) or {}).get("coin_record")
        if not record:
            raise HistoryError(f"pool coin {coin_id[:12]} is unknown to the node", "COIN_UNKNOWN")
        parent = _strip(record["coin"]["parent_coin_info"])
        height = int(record.get("confirmed_block_index") or 0)
        timestamp = int(record.get("timestamp") or 0)
        if not record.get("spent"):
            # Only the tip can be unspent, and its state is the caller's snapshot.
            if points:
                raise HistoryError("an unspent coin below the tip", "LINEAGE_BROKEN")
            tip = {"coin_id": coin_id, "height": height, "timestamp": timestamp}
        else:
            spent_height = int(record["spent_block_index"])
            spend = rpc("get_puzzle_and_solution", {"coin_id": "0x" + coin_id, "height": spent_height})["coin_solution"]
            reveal = Program.from_bytes(bytes.fromhex(_strip(spend["puzzle_reveal"])))
            if reveal.get_tree_hash().hex() != _strip(record["coin"]["puzzle_hash"]):
                raise HistoryError("a reveal does not hash to its coin's puzzle", "REVEAL_MISMATCH")
            state = state_from_reveal(reveal, launcher_bytes)
            if tip is None and not points:
                tip = {"coin_id": coin_id, "height": height, "timestamp": timestamp, "spent": True}
            last_height, cums, last_spot = state[3]
            points.append({
                "coin_id": coin_id, "parent": parent, "height": height, "timestamp": timestamp,
                "spent_height": spent_height,
                "reserves": [str(int(r)) for r in state[0]], "total_lp": str(int(state[1])),
                # The pool's own price record, as the spend that created this coin left it:
                # cumulative spot x blocks (2^64-scaled, each asset in asset 0) up to
                # `last_height`, and the PRE-spend spot it credited. Zero at genesis.
                "oracle": {"last_height": int(last_height), "cums": [str(int(c)) for c in cums],
                           "last_spot": [str(int(s)) for s in last_spot]},
            })
            walked += 1
        coin_id = parent


def history(payload: dict[str, Any], rpc: Rpc | None = None) -> dict[str, Any]:
    launcher_id = _strip(payload.get("launcher_id"))
    if len(launcher_id) != 64:
        raise HistoryError("launcher_id must be 32 bytes of hex", "BAD_REQUEST")
    rpc = rpc or node_rpc(str(payload.get("node_url") or DEFAULT_NODE))
    known = {_strip(c) for c in payload.get("known") or []}
    points: list[dict] = []
    segments: list[dict] = []
    tip = None
    for index, segment in enumerate(payload.get("segments") or []):
        budget = max(0, min(int(segment.get("budget") or MAX_BUDGET), MAX_BUDGET))
        out = walk(rpc, launcher_id, segment["start"], known | {p["coin_id"] for p in points}, budget)
        if index == 0:
            tip = out["tip"]
        points.extend(out["points"])
        segments.append({"start": _strip(segment["start"]), "walked": out["walked"],
                         "reached": out["reached"], "next": out["next"]})
    return {"success": True, "points": points, "segments": segments, "tip": tip}


def main() -> int:
    try:
        print(json.dumps(history(json.loads(sys.stdin.read() or "{}"))))
        return 0
    except HistoryError as exc:
        print(json.dumps({"success": False, "error": str(exc), "code": exc.code}))
        return 1
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}", "code": "HISTORY_ERROR"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
