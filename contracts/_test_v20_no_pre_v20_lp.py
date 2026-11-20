#!/usr/bin/env python3
"""V20 lane rule (owner, 2026-11-17 for V19, carried to V20 2026-11-18): a V20 pool holds only V20
assets -- XCH, plain CATs and V20 LP -- never the LP of a V14..V19 Forge pool (V19 LP included).

The puzzle cannot tell an LP's revision (an LP is a CAT like any other), so this is a lane and
router rule: forge_v20_create.plan refuses such a config before anything is signed, from the LP
asset ids forge_stdin.pre_v20_lp_assets reads out of the older registries' records; the batch move
(forge_v20_move) never seeds a twin with older LP, it unpairs it first. Exit 0 when every check
passes.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_stdin  # noqa: E402
import forge_v16_driver as drv16  # noqa: E402
import forge_v19_driver as drv19  # noqa: E402
import forge_v20_create as cre  # noqa: E402
import forge_v20_driver as drv  # noqa: E402
import forge_v20_move as mv  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

results: list[bool] = []
CAT = bytes32(b"\xc1" * 32)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def config(asset_ids):
    return cre.CreationConfig(asset_ids, [10 ** 9] * len(asset_ids), [1] * len(asset_ids), 30, 5, bytes32(b"\x7a" * 32), 10 ** 6)


def plan_refusal(asset_ids, pre):
    try:
        cre.plan(drv.make_registry(), {}, config(asset_ids), None, {}, bytes32(b"\x56" * 32), 0, pre_v20_lp=pre)
    except cre.OfferRejected as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 -- got past the rule into the coin handling
        return f"(passed the rule: {type(exc).__name__})"
    return "(accepted)"


def main() -> int:
    old16 = drv16.make_pool([None, CAT], [10 ** 9, 10 ** 9], total_lp=10 ** 6, leaves="forge", salt=0x31)
    v20 = drv.make_pool([None, CAT], [10 ** 9, 10 ** 9], total_lp=10 ** 6, leaves="forge", salt=0x32)
    old19 = drv19.make_pool([None, CAT], [10 ** 9, 10 ** 9], total_lp=10 ** 6, leaves="forge", salt=0x34)
    with tempfile.TemporaryDirectory() as tmp:
        rec16 = Path(tmp) / "v16.json"
        rec16.write_text(json.dumps({"pools": [{"label": "R", "lp_asset_id": old16.lp_asset_id.hex()}]}), encoding="utf-8")
        rec18 = Path(tmp) / "v18.json"
        rec18.write_text(json.dumps({"pools": [{"label": "R18", "lp_asset_id": "0x" + ("ab" * 32)}]}), encoding="utf-8")
        payload = {f"{t}_record_path": str(Path(tmp) / f"absent-{t}.json") for t in forge_stdin.PRE_V20_RECORD_TAGS}
        rec19 = Path(tmp) / "v19.json"
        rec19.write_text(json.dumps({"pools": [{"label": "R19", "lp_asset_id": old19.lp_asset_id.hex()}]}), encoding="utf-8")
        payload.update({"v16_record_path": str(rec16), "v18_record_path": str(rec18), "v19_record_path": str(rec19)})
        pre = forge_stdin.pre_v20_lp_assets(payload)
    print("the lane reads the older registries' LP ids:")
    check("  forge_stdin.pre_v20_lp_assets lists the V16, the V18 and the V19 records' LP",
          set(pre) == {old16.lp_asset_id.hex(), "ab" * 32, old19.lp_asset_id.hex()}, str(sorted(pre.values())))
    check("  the V19 record is one of the tags it reads", "v19" in forge_stdin.PRE_V20_RECORD_TAGS)
    check("  an extra id the router knows by other means is added", "cd" * 32 in forge_stdin.pre_v20_lp_assets(
        {**{f"{t}_record_path": "/nonexistent" for t in forge_stdin.PRE_V20_RECORD_TAGS}, "pre_v20_lp_asset_ids": ["0x" + "cd" * 32]}))

    print("forge_v20_create.plan refuses before signing:")
    msg = plan_refusal([None, old16.lp_asset_id], pre)
    check("  a config holding V16 LP is refused with the reason", "pre-V20 Forge pool" in msg and "V16 R" in msg, msg[:110])
    msg = plan_refusal([bytes32(b"\xab" * 32), CAT], pre)
    check("  a config holding V18 LP is refused", "pre-V20 Forge pool" in msg, msg[:80])
    msg = plan_refusal([None, old19.lp_asset_id], pre)
    check("  a config holding V19 LP is refused (V19 LP is pre-V20)", "pre-V20 Forge pool" in msg and "V19 R19" in msg, msg[:110])
    msg = plan_refusal([None, v20.lp_asset_id], pre)
    check("  V20 LP is a V20 asset: not refused by this rule", "pre-V20" not in msg, msg[:80])
    msg = plan_refusal([None, CAT], pre)
    check("  a plain CAT is not refused by this rule", "pre-V20" not in msg, msg[:80])
    msg = plan_refusal([None, old16.lp_asset_id], {})
    check("  (with no records held there is nothing to refuse against: the router must hold them)", "pre-V20" not in msg)

    print("the batch move never seeds a twin with older LP:")
    graph = mv.Graph({old16.lp_asset_id: old16}, [])
    try:
        graph.v20_asset(old16.lp_asset_id)
        check("  V16 LP with no V20 twin of its market is refused (move the inner market first)", False, "mapped")
    except mv.MoveRefused as exc:
        check("  V16 LP with no V20 twin of its market is refused (move the inner market first)", "no V20 twin" in str(exc), str(exc)[:80])
    twin = drv.make_pool([None, CAT], [10 ** 9, 10 ** 9], total_lp=10 ** 6, leaves="forge", salt=0x33)
    graph.v20.append(twin)
    check("  once the market's V20 twin exists, V16 LP maps to the twin's V20 LP",
          graph.v20_asset(old16.lp_asset_id) == twin.lp_asset_id)
    check("  a plain asset maps to itself", graph.v20_asset(CAT) == CAT and graph.v20_asset(None) is None)
    g19 = mv.Graph({old19.lp_asset_id: old19}, [])
    try:
        g19.v20_asset(old19.lp_asset_id)
        check("  V19 LP with no V20 twin of its market is refused", False, "mapped")
    except mv.MoveRefused as exc:
        check("  V19 LP with no V20 twin of its market is refused", "no V20 twin" in str(exc), str(exc)[:80])
    twin19 = drv.make_pool([None, CAT], [10 ** 9, 10 ** 9], total_lp=10 ** 6, leaves="forge", salt=0x35)
    g19.v20.append(twin19)
    check("  once its V20 twin exists, V19 LP maps to the twin's V20 LP", g19.v20_asset(old19.lp_asset_id) == twin19.lp_asset_id)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} pre-V20 LP checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
