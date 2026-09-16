#!/usr/bin/env python3
"""The frontend's curve against the one the puzzle is quoted with.

`calcSwapOut` in src/lib/cfmm.ts is a hand-written mirror of `exact_swap_output`
in forge_curve.rue. Nothing compared them until 2026-09-13, when a 1 TXCH split
was refused with "route releases 26,395,040, trader asks 26,628,829" -- the
frontend had promised 0.88% more than the pools would pay, the trader had already
signed, and the offer was left resting in their wallet.

Every other frontend check compares TypeScript against TypeScript, so a mirror
that drifts is invisible there by construction. This reads the cases the
TypeScript side emits (src/lib/__checks__/curveMirror.check.ts writes
`_curve_mirror_cases.json`) and runs the identical inputs through
`forge_math.swap_output`, which is what the builder quotes and what the leaf's
`exact_swap_output` brackets.

A disagreement of even one mojo matters in one direction: the frontend must never
quote MORE than the pool releases, because the builder refuses outright. Quoting
less is merely a worse price.

    node scripts/run-checks.mjs                 # emit the cases
    python contracts/_test_v14_curve_mirror.py  # compare them

Exit codes: 0 agree, 1 a disagreement, 2 no cases file.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import forge_math  # noqa: E402

CASES = pathlib.Path(__file__).resolve().parent / "_curve_mirror_cases.json"


def puzzle_out(case: dict) -> int:
    """What the pool actually releases to the trader, as the builder computes it.

    `swap_output` is the curve net of the swap fee. The protocol and DAO slices come
    off the release afterwards, each floored on its own -- two divmods, not one on the
    summed rate, exactly as `forge_action_swap` does it.
    """
    curve = forge_math.swap_output(
        int(case["reserveIn"]), int(case["reserveOut"]), int(case["amountIn"]),
        int(case["swapFeeBps"]), int(case["weightIn"]), int(case["weightOut"]),
    )
    protocol = curve * int(case["protocolFeeBps"]) // 10_000
    dao = curve * int(case["daoFeeBps"]) // 10_000
    return curve - protocol - dao


def main() -> int:
    if not CASES.is_file():
        print(f"no cases at {CASES.name}; run `node scripts/run-checks.mjs` first")
        return 2
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    over = []       # frontend quoted MORE than the pool pays: the offer cannot settle
    under = []      # frontend quoted less: a worse price, not a failure
    for case in cases:
        ts = int(case["tsOut"])
        py = puzzle_out(case)
        if ts > py:
            over.append((case, ts, py))
        elif ts < py:
            under.append((case, ts, py))

    print(f"{len(cases)} cases from {payload.get('generatedAt', 'unknown time')}")
    print(f"  agree exactly : {len(cases) - len(over) - len(under)}")
    print(f"  quoted UNDER  : {len(under)}  (a worse price; the offer still settles)")
    print(f"  quoted OVER   : {len(over)}  (the builder refuses these outright)")

    for case, ts, py in over[:10]:
        gap = ts - py
        print(f"  [FAIL] {case['label']}")
        print(f"         frontend {ts:,} vs pool {py:,}  over by {gap:,} "
              f"({gap / py * 100:.4f}%)")
    for case, ts, py in under[:3]:
        print(f"  [note] {case['label']}: frontend {ts:,} vs pool {py:,} "
              f"(under by {py - ts:,})")

    if over:
        print(f"\n{len(over)} case(s) quote more than the pool releases")
        return 1
    print("\nthe frontend never quotes more than the pool releases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
