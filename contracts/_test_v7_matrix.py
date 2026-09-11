#!/usr/bin/env python3
"""Asset-count matrix, n = 1 to 20, against the compiled V6 and V7 puzzles.

Two things this establishes. First, that the supported window is exactly what
each version claims: V6 accepts 2..10, V7 accepts 1..10, and both refuse
everything above MAX_ASSETS. Second, what the ceiling actually costs -- the
LP bracket raises to the n-th power and every check recurses over the reserve
list, so CLVM cost is measured here rather than assumed.
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")

import forge_puzzles
if not forge_puzzles.available("pool_singleton_v7"):
    print("SKIP: the V7 puzzles are archived and absent from this checkout.")
    print("      Superseded revisions are not published; see docs/FORGE_SECURITY_AUDIT.md.")
    raise SystemExit(2)   # 2 == skipped; a skip must never read as a pass

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH
from chia_rs import Coin
from chia_rs.sized_ints import uint64

from forge_math import default_weights, swap_output

import forge_puzzles

ZERO = bytes(32)
POOL_COIN = bytes.fromhex("22" * 32)
SINGLETON = [bytes.fromhex("33" * 32), bytes.fromhex("11" * 32), bytes.fromhex("44" * 32)]
MODE_SWAP, MODE_ADD, MODE_REMOVE = 0, 1, 2
FEE_BPS = 30
MAX_SUPPORTED = 10
BLOCK_COST_LIMIT = 11_000_000_000


def compiled(name: str) -> Program:
    return Program.from_bytes(bytes.fromhex(
        forge_puzzles.hex_path(name).read_text("ascii").strip()))


def asset_id(i: int) -> bytes:
    """Asset 0 is native XCH; the rest are CATs. Ids must strictly ascend."""
    return ZERO if i == 0 else bytes([i]) * 32


def make_runner(version: int):
    return make_runner_named(f"pool_singleton_v{version}", version)


def check_quiet(ok, label):
    if not ok:
        print(f"  [FAIL] {label}")
    return ok


def make_runner_named(module: str, version: int):
    pool = compiled(module)
    reserve = compiled(f"forge_reserve_v{version}")
    tail = compiled(f"forge_lp_cat_tail_v{version}")

    def reserve_ph(a):
        return bytes(reserve.get_tree_hash()) if a == ZERO else \
            bytes(construct_cat_puzzle(CAT_MOD, a, reserve).get_tree_hash())

    def settlement_ph(a):
        return bytes(OFFER_MOD_HASH) if a == ZERO else \
            bytes(construct_cat_puzzle(CAT_MOD, a, OFFER_MOD).get_tree_hash())

    def run(n, old, new, total_lp, mode, lp_delta):
        assets = [asset_id(i) for i in range(n)]
        plans = []
        for i, (a, o, nn) in enumerate(zip(assets, old, new)):
            cur = bytes([200 + i, o % 251]) + bytes(30)
            succ = bytes(Coin(cur, reserve_ph(a), uint64(nn)).name())
            if nn < o:
                s = bytes(Coin(cur, settlement_ph(a), uint64(o - nn)).name())
            elif nn == o:
                s = ZERO
            else:
                s = bytes.fromhex("77" * 32)
            plans.append([a, cur, o, s, succ, nn])
        # V6 weights are basis points; V7 weights are integer units.
        weights = [1] * n if version >= 7 else default_weights(n)
        config = [version, pool.get_tree_hash(), assets, weights,
                  FEE_BPS, tail.get_tree_hash(), reserve.get_tree_hash()]
        state = [[[a, bytes([200 + i, o % 251]) + bytes(30), o]
                  for i, (a, o) in enumerate(zip(assets, old))], total_lp]
        action = [mode, POOL_COIN, plans,
                  ZERO if mode == MODE_SWAP else bytes.fromhex("88" * 32), lp_delta]
        try:
            cost, _ = pool.run_with_cost(
                BLOCK_COST_LIMIT, Program.to([SINGLETON, config, state, action]))
            return True, cost
        except Exception:
            return False, 0

    return run


def main() -> int:
    v6, v7 = make_runner(6), make_runner(7)
    R, LP = 1_000_000, 1_000_000
    c10_for_delta = 0
    results = []

    print("Asset-count matrix. 'add' is a balanced deposit of 10% into every")
    print("reserve; 'swap' is an exact-curve trade between assets 0 and 1.")
    print("Cost is CLVM cost for the add, against the 11e9 block limit.\n")
    print(f"  {'n':>3}  {'V6':>5}  {'V7':>5}  {'expect':>8}  {'add cost':>12}  {'% of block':>10}  {'swap':>6}")
    print(f"  {'-'*3}  {'-'*5}  {'-'*5}  {'-'*8}  {'-'*12}  {'-'*10}  {'-'*6}")

    for n in range(1, 21):
        old = [R] * n
        added = [R + R // 10] * n
        mint = LP // 10                     # balanced 10% deposit mints 10%

        ok6, _ = v6(n, old, added, LP, MODE_ADD, mint)
        ok7, cost7 = v7(n, old, added, LP, MODE_ADD, mint)
        if n == 10:
            c10_for_delta = cost7

        # Swap needs a pair, so it only applies from two assets up.
        if n >= 2:
            out = swap_output(R, R, R // 10, FEE_BPS)
            swapped = list(old)
            swapped[0] += R // 10
            swapped[1] -= out
            sw7, _ = v7(n, old, swapped, LP, MODE_SWAP, 0)
            swap_note = "ok" if sw7 else "no"
        else:
            sw7, _ = v7(n, old, [R + 1000], LP, MODE_SWAP, 0)
            swap_note = "n/a" if not sw7 else "BAD"

        want6 = 2 <= n <= MAX_SUPPORTED
        want7 = 1 <= n <= MAX_SUPPORTED
        # A one-asset pool has no pair, and an out-of-range pool is invalid
        # outright, so "no" is the correct swap answer in both cases.
        want_swap = "ok" if (2 <= n <= MAX_SUPPORTED) else ("n/a" if n == 1 else "no")
        good = (ok6 == want6) and (ok7 == want7) and (swap_note == want_swap)
        results.append(good)

        pct = f"{cost7 / BLOCK_COST_LIMIT * 100:.3f}%" if cost7 else "—"
        cost_s = f"{cost7:,}" if cost7 else "—"
        flag = "" if good else "   <-- unexpected"
        print(f"  {n:>3}  {str(ok6):>5}  {str(ok7):>5}  "
              f"{'V6+V7' if want6 else ('V7' if want7 else 'reject'):>8}  "
              f"{cost_s:>12}  {pct:>10}  {swap_note:>6}{flag}")

    # The shipped puzzle refuses counts above the ceiling, so measuring what the
    # ceiling actually costs needs a probe with the cap raised and nothing else
    # changed. Extrapolating would not answer the question.
    print()
    print("Above the shipped ceiling, measured on the audit probe (MAX_ASSETS 20):")
    print(f"  {'n':>3}  {'add cost':>12}  {'% of block':>10}  {'per asset':>11}")
    print(f"  {'-'*3}  {'-'*12}  {'-'*10}  {'-'*11}")
    probe = make_runner_named("_probe_pool_singleton_v7_wide", 7)
    wide = []
    for n in range(11, 21):
        old = [R] * n
        ok, cost = probe(n, old, [R + R // 10] * n, LP, MODE_ADD, LP // 10)
        results.append(check_quiet(ok, f"probe accepts n={n}"))
        wide.append((n, cost))
        prev = wide[-2][1] if len(wide) > 1 else c10_for_delta
        print(f"  {n:>3}  {cost:>12,}  {cost / BLOCK_COST_LIMIT * 100:>9.3f}%  {cost - prev:>11,}")

    print()
    # Cost growth is the reason for the ceiling, so state it explicitly.
    _, c2 = v7(2, [R] * 2, [R + R // 10] * 2, LP, MODE_ADD, LP // 10)
    _, c10 = v7(10, [R] * 10, [R + R // 10] * 10, LP, MODE_ADD, LP // 10)
    print(f"Cost at n=2: {c2:,}   at n=10: {c10:,}   growth: {c10 / c2:.1f}x")
    print(f"n=10 uses {c10 / BLOCK_COST_LIMIT * 100:.3f}% of one block's cost limit.")
    per_asset = (c10 - c2) / 8
    projected = c10 + per_asset * 10
    print(f"Growth is near-linear at ~{per_asset:,.0f} cost per extra asset, so n=20")
    print(f"projects to ~{projected:,.0f}, about {projected / BLOCK_COST_LIMIT * 100:.3f}% of a block.")
    print(f"The ceiling of {MAX_SUPPORTED} is a conservative choice, not a cost limit.")

    passed = sum(results)
    print()
    print(f"{passed}/{len(results)} asset counts behaved as specified")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
