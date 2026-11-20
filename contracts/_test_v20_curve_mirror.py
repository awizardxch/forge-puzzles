#!/usr/bin/env python3
"""The off-chain curves against the puzzle's: V20's add side (always), and the frontend's swap side.

V20 (protocol 21): the add mint nets the dev and DAO fees on the swapped share (V19, external
review 2026-11-16, finding 08), and the curve refuses an add whose effective term on any asset is
not positive. forge_math (version 20) must bracket exactly where the compiled
forge_curve.exact_invariant_lp_mint does, and refuse (AddFeesExceedDeposit) exactly the adds the
curve refuses. Part A drives both on a 10,000-case seeded corpus of adds (1-4 assets, weights 1-4,
zero slots, every fee rate, some deposits far above the reserve) and requires the puzzle to accept
the mirror's mint and refuse one more, to refuse every mint the mirror refuses, every accepted add
to mint what version 19 mints, and forge_math.add_output_fees to equal the compiled
add_output_fees. Part B is the V18 suite's swap-side check against the TypeScript cases.
Part C holds the TypeScript ADD quote (src/lib/cfmm.ts calcInvariantLpOutV6,
src/lib/v19AddFees.ts) to forge_math's V20 mint and to the compiled curve, on the grid
src/lib/__checks__/curveMirror.check.ts emits (balanced, one-sided, vault, weighted, multi-asset):
a quote must never exceed what the puzzle mints.

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
    python contracts/_test_v20_curve_mirror.py  # compare them

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
COMPILED = pathlib.Path(__file__).resolve().parent / "v20" / "compiled"


def add_side(count: int = 10_000) -> bool:
    """Part A: forge_math's V20 add mint and add_output_fees against the compiled curve."""
    import random
    from chia.types.blockchain_format.program import Program
    load = lambda name: Program.from_bytes(bytes.fromhex((COMPILED / f"forge_curve.{name}.rue.hex").read_text().strip()))  # noqa: E731
    mint_prog, fees_prog = load("exact_invariant_lp_mint"), load("add_output_fees")
    rng = random.Random(0x1916)
    bad, fee_bad, minted_cases, refused_cases, v19_differs = [], 0, 0, 0, 0
    for k in range(count):
        n = rng.randint(1, 4)
        old = [rng.choice([rng.randint(1, 10 ** 4), rng.randint(1, 10 ** 13)]) for _ in range(n)]
        w = [rng.randint(1, 4) for _ in range(n)] if rng.random() < 0.5 else [1] * n
        dep = [0 if rng.random() < 0.3 else rng.randint(1, max(1, old[i] // rng.choice([1, 10, 1000, 10 ** 6]))) for i in range(n)]
        if not any(dep):
            dep[rng.randrange(n)] = rng.randint(1, 10 ** 9)
        lp = rng.choice([10 ** 6, rng.randint(10 ** 6, 10 ** 13)])
        fee, pf, df = rng.choice([0, 5, 30, 100]), rng.choice([0, 5, 15]), rng.choice([0, 30, 100, 500])
        nxt = [o + d for o, d in zip(old, dep)]
        # V20: the mirror refuses (AddFeesExceedDeposit) exactly the adds whose effective term is not
        # positive; the compiled curve must then refuse every mint (its assert raises).
        try:
            m = forge_math.invariant_lp_mint(old, dep, lp, fee, w, version=20, protocol_fee_bps=pf, dao_fee_bps=df)
        except forge_math.AddFeesExceedDeposit:
            m = None
        else:
            if m != forge_math.invariant_lp_mint(old, dep, lp, fee, w, version=19, protocol_fee_bps=pf, dao_fee_bps=df):
                v19_differs += 1

        def accept(x):
            try:
                return mint_prog.run([old, nxt, w, lp, x, fee, pf, df]).as_int() != 0
            except ValueError:          # the V20 assert (or any raise) refuses the mint
                return False
        if m is None:
            refused_cases += 1
            if accept(1) or accept(forge_math.invariant_lp_mint(old, dep, lp, fee, w, version=19, protocol_fee_bps=pf,
                                                                dao_fee_bps=df) or 1):
                bad.append((k, old, dep, w, lp, fee, pf, df, "refused by the mirror, accepted by the curve"))
        elif m > 0:
            minted_cases += 1
            if not accept(m) or accept(m + 1):
                bad.append((k, old, dep, w, lp, fee, pf, df, m))
        elif accept(1):
            bad.append((k, old, dep, w, lp, fee, pf, df, m))
        got = fees_prog.run([old, nxt, w, fee, pf, df])
        want = forge_math.add_output_fees(old, dep, w, fee, pf, df)
        if ([x.as_int() for x in got.first().as_iter()], [x.as_int() for x in got.rest().as_iter()]) != (list(want[0]), list(want[1])):
            fee_bad += 1
    print(f"part A: {count:,} seeded adds ({minted_cases:,} that mint, {refused_cases:,} refused: an effective term <= 0), "
          "V20 forge_math vs the compiled curve")
    print(f"  [{'PASS' if refused_cases else 'FAIL'}] the corpus reaches the V20 refusal ({refused_cases:,} adds)")
    print(f"  [{'PASS' if not v19_differs else 'FAIL'}] every add the V20 mirror accepts mints exactly what version 19 does  "
          f"({v19_differs} differ)")
    print(f"  [{'PASS' if not bad else 'FAIL'}] the puzzle accepts forge_math's mint and refuses one more  ({len(bad)} disagree)")
    for case in bad[:5]:
        print(f"         case {case}")
    print(f"  [{'PASS' if not fee_bad else 'FAIL'}] forge_math.add_output_fees == forge_curve.add_output_fees  ({fee_bad} disagree)")
    return not bad and not fee_bad and refused_cases > 0 and not v19_differs


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
    if not (COMPILED / "forge_curve.add_output_fees.rue.hex").is_file():
        print("  [skip] V20 build outputs are absent; run scripts/build-v20.py")
        return 2
    if not add_side():
        return 1
    print("part B: the frontend's swap quotes")
    if not CASES.is_file():
        print(f"  [skip] no cases at {CASES.name}; run `node scripts/run-checks.mjs` first (part A passed)")
        return 0
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
    # The V20 grid (protocol-21 quotes, refusal shapes included) when the emitter wrote one; an older
    # cases file carries only V19's protocol-20 grid, which V20 mints identically on.
    return 0 if ts_add_side(payload.get("addCasesV20") or payload.get("addCases") or []) else 1


def ts_add_side(cases: list) -> bool:
    """Part C: the frontend's V20 add quote (src/lib/cfmm.ts calcInvariantLpOutV6 on a protocol-21 pool,
    lib/v19AddFees.ts) against forge_math.invariant_lp_mint(version=20) -- what the V20 lanes mint with
    -- and against the compiled forge_curve.exact_invariant_lp_mint itself: the puzzle must accept the
    quoted mint. Over by one mojo is a refused settlement; under is only a worse price."""
    print("part C: the frontend's V20 add quotes")
    if not cases:
        print("  [FAIL] no addCases in the cases file; run `node scripts/run-checks.mjs` (curveMirror) again")
        return False
    from chia.types.blockchain_format.program import Program
    mint_prog = Program.from_bytes(bytes.fromhex((COMPILED / "forge_curve.exact_invariant_lp_mint.rue.hex").read_text().strip()))
    over, under, refused, minted = [], [], [], 0
    for case in cases:
        old = [int(x) for x in case["reserves"]]
        dep = [int(x) for x in case["deposits"]]
        w = [int(x) for x in case["weights"]]
        lp, fee, pf, df = int(case["totalLp"]), int(case["feeBps"]), int(case["protocolFeeBps"]), int(case["daoFeeBps"])
        ts = int(case["tsMint"])
        try:
            py = forge_math.invariant_lp_mint(old, dep, lp, fee, w, version=20, protocol_fee_bps=pf, dao_fee_bps=df)
        except forge_math.AddFeesExceedDeposit:
            py = 0                      # V20 refuses the add: any positive quote is over
        if ts > py:
            over.append((case, ts, py))
        elif ts < py:
            under.append((case, ts, py))
        if ts > 0:
            minted += 1
            nxt = [o + d for o, d in zip(old, dep)]
            try:
                ok_mint = mint_prog.run([old, nxt, w, lp, ts, fee, pf, df]).as_int() != 0
            except ValueError:
                ok_mint = False
            if not ok_mint:
                refused.append((case, ts))
    print(f"{len(cases)} add cases ({minted} that mint)")
    print(f"  agree exactly : {len(cases) - len(over) - len(under)}")
    print(f"  quoted UNDER  : {len(under)}  (fewer LP; the add still settles)")
    print(f"  quoted OVER   : {len(over)}  (the lane refuses these)")
    print(f"  [{'PASS' if not refused else 'FAIL'}] the compiled V20 curve accepts every quoted mint  ({len(refused)} refused)")
    for case, ts, py in over[:10]:
        print(f"  [FAIL] {case['label']}: frontend {ts:,} vs forge_math {py:,} (over by {ts - py:,})")
    for case, ts in refused[:5]:
        print(f"  [FAIL] {case['label']}: the puzzle refuses a mint of {ts:,}")
    for case, ts, py in under[:3]:
        print(f"  [note] {case['label']}: frontend {ts:,} vs forge_math {py:,} (under by {py - ts:,})")
    ok = not over and not refused
    print("\nthe frontend's V20 add quote never mints more than the puzzle admits" if ok
          else f"\n{len(over) + len(refused)} V20 add quote(s) exceed what the puzzle mints")
    return ok


if __name__ == "__main__":
    raise SystemExit(main())
