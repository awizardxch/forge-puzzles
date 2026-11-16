#!/usr/bin/env python3
"""forge_resync's rebase from an earlier state (`fallback_pools`), offline.

A pool whose indexed coin never existed (a push the mempool dropped) cannot be walked forward
from that coin. Testnet V14 4f3ffda4 sat on such a coin for four weeks: the registry rebuild is
V16-only, so every other revision answered TIP_UNKNOWN forever. The rebase walks from an earlier
state the caller knows instead, through the pool's own revision lane.

Pinned here, with a fake replay lane and a fake node (no network):
  * no fallback given: the refusal is exactly the lane's, as before;
  * a fallback for another launcher or revision is never walked;
  * the indexed coin itself is never re-walked as a "fallback";
  * a fallback the lane accepts is returned as a change, its steps prefixed with the rebase;
  * a fallback the lane refuses leaves TIP_UNKNOWN, naming why;
  * a reached tip with a spend in the mempool is TIP_IN_MEMPOOL, never a rollback;
  * a V16 pool still tries the registry rebuild FIRST; a fallback is only tried after it fails,
    and never past its TIP_IN_MEMPOOL.

    cd projects/chia-cfmm
    <venv python> contracts/_test_resync_fallback.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import forge_resync as fr  # noqa: E402

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    FAILED += 0 if ok else 1


L = "11" * 32
PHANTOM = "aa" * 32
REAL = "bb" * 32


def snap(coin: str, launcher: str = L, version: int = 15) -> dict:
    return {"launcher_id": launcher, "pool_coin_id": coin, "protocol_version": version}


def fake_lane(known: set[str]):
    """A replay lane: a snapshot whose coin the node 'holds' resyncs to it; anything else is TIP_UNKNOWN."""
    mod = types.SimpleNamespace()

    class ResyncError(Exception):
        def __init__(self, message, code):
            super().__init__(message)
            self.code = code
    mod.ResyncError = ResyncError
    mod.calls = []

    def resync(payload):
        coin = payload["pool"]["pool_coin_id"]
        mod.calls.append(coin)
        if coin not in known:
            raise ResyncError(f"pool coin {coin[:12]} is unknown to the node", "TIP_UNKNOWN")
        return {"success": True, "changed": False, "tip_coin_id": coin, "steps": [], "snapshot": payload["pool"]}
    mod.resync = resync
    return mod


def run(payload, lane_name="forge_v14_resync", known=frozenset({REAL}), mempool=frozenset(), registry=None):
    lane = fake_lane(set(known))
    import importlib
    saved_import, saved_rpc, saved_recover = importlib.import_module, fr._rpc, fr._recover_from_registry
    importlib.import_module = lambda name: lane if name == lane_name else saved_import(name)
    fr._rpc = lambda node, route, body: {"mempool_items": ["x"] if body.get("coin_name", "")[2:] in mempool else []}
    if registry is not None:
        fr._recover_from_registry = registry
    try:
        return fr.resync(payload), lane
    except fr.ResyncError as exc:
        return exc, lane
    finally:
        importlib.import_module, fr._rpc, fr._recover_from_registry = saved_import, saved_rpc, saved_recover


def base(**extra):
    return {"launcher_id": L, "node_url": "http://node.invalid", "pool": snap(PHANTOM), **extra}


print("V14 lane (no registry rebuild):")
out, lane = run(base())
check("no fallback: the lane's own TIP_UNKNOWN, unchanged", isinstance(out, fr.ResyncError) and out.code == "TIP_UNKNOWN"
      and str(out) == f"pool coin {PHANTOM[:12]} is unknown to the node", str(out))

out, lane = run(base(fallback_pools=[snap(REAL)]))
check("a fallback the node holds: rebased, changed, its coin named",
      isinstance(out, dict) and out["changed"] is True and out["tip_coin_id"] == REAL and out["rebased_from"] == REAL
      and out["steps"][0] == f"rebase from {REAL[:12]}", str(out)[:200])

out, lane = run(base(fallback_pools=[snap(REAL, launcher="22" * 32)]))
check("another launcher's snapshot is never walked", isinstance(out, fr.ResyncError) and out.code == "TIP_UNKNOWN"
      and lane.calls == [PHANTOM], f"{out} calls={lane.calls}")

out, lane = run(base(fallback_pools=[snap(REAL, version=17)]))
check("another revision's snapshot is never walked", isinstance(out, fr.ResyncError) and lane.calls == [PHANTOM])

out, lane = run(base(fallback_pools=[snap(PHANTOM)]))
check("the indexed coin is not re-walked as its own fallback",
      isinstance(out, fr.ResyncError) and out.code == "TIP_UNKNOWN" and lane.calls == [PHANTOM], f"{out} {lane.calls}")

out, lane = run(base(fallback_pools=[snap("cc" * 32)]))
check("a fallback the node does not hold: TIP_UNKNOWN naming it",
      isinstance(out, fr.ResyncError) and out.code == "TIP_UNKNOWN" and "no earlier state reached the chain" in str(out), str(out))

out, lane = run(base(fallback_pools=[snap(REAL)]), mempool={REAL})
check("a reached tip with a spend pending: TIP_IN_MEMPOOL, no rollback",
      isinstance(out, fr.ResyncError) and out.code == "TIP_IN_MEMPOOL", str(out))

print("\nV16 lane (registry rebuild first):")
order = []


def registry_ok(payload, exc):
    order.append("registry")
    return {"success": True, "changed": True, "tip_coin_id": "dd" * 32, "steps": ["chain-recover (3 spends)"], "recovered": True}


def registry_fails(code):
    def f(payload, exc):
        order.append("registry")
        raise fr.ResyncError("rebuild failed", code)
    return f


V16 = {"pool": snap(PHANTOM, version=17)}
order.clear()
out, lane = run(base(**V16, fallback_pools=[snap(REAL, version=17)]), lane_name="forge_v16_resync", registry=registry_ok)
check("the registry rebuild answers first, the fallback is not walked",
      isinstance(out, dict) and out.get("recovered") is True and order == ["registry"] and lane.calls == [PHANTOM])

order.clear()
out, lane = run(base(**V16, fallback_pools=[snap(REAL, version=17)]), lane_name="forge_v16_resync", registry=registry_fails("TIP_UNKNOWN"))
check("the registry rebuild fails: the fallback is walked after it",
      isinstance(out, dict) and out["rebased_from"] == REAL and order == ["registry"] and lane.calls == [PHANTOM, REAL])

order.clear()
out, lane = run(base(**V16, fallback_pools=[snap(REAL, version=17)]), lane_name="forge_v16_resync", registry=registry_fails("TIP_IN_MEMPOOL"))
check("the registry rebuild's TIP_IN_MEMPOOL is final", isinstance(out, fr.ResyncError) and out.code == "TIP_IN_MEMPOOL"
      and lane.calls == [PHANTOM])

order.clear()
out, lane = run(base(**V16), lane_name="forge_v16_resync", registry=registry_fails("TIP_UNKNOWN"))
check("no fallback: the registry rebuild's refusal, as before", isinstance(out, fr.ResyncError) and str(out) == "rebuild failed")

print("\nV17 lane (registry rebuild first, as for V16 since PR 382):")
V17 = {"pool": snap(PHANTOM, version=18)}
order.clear()
out, lane = run(base(**V17, fallback_pools=[snap(REAL, version=18)]), lane_name="forge_v17_resync", registry=registry_ok)
check("the registry rebuild answers first", isinstance(out, dict) and out.get("recovered") is True and lane.calls == [PHANTOM])
order.clear()
out, lane = run(base(**V17, fallback_pools=[snap(REAL, version=18)]), lane_name="forge_v17_resync", registry=registry_fails("TIP_UNKNOWN"))
check("the registry rebuild fails: the fallback is walked after it",
      isinstance(out, dict) and out["rebased_from"] == REAL and order == ["registry"] and lane.calls == [PHANTOM, REAL])

print(f"\n{'resync fallback: walks only a verified earlier state of the same pool' if FAILED == 0 else str(FAILED) + ' CHECK(S) FAILED'}")
sys.exit(1 if FAILED else 0)
