#!/usr/bin/env python3
"""forge_math against the compiled V7 puzzle, including one-asset pools.

Same discipline as the V6 suite: for each case the Python math produces a value,
then the puzzle is run twice — once with that value, which must be accepted, and
once with value + 1, which must be rejected. That pins the result exactly rather
than merely showing it is permitted.
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

import forge_puzzles

from forge_math import (
    default_weights,
    invariant_lp_mint,
    swap_output,
    withdrawal_amounts,
)

VERSION = 7
ZERO = bytes(32)
POOL_COIN = bytes.fromhex("22" * 32)
SINGLETON = [bytes.fromhex("33" * 32), bytes.fromhex("11" * 32), bytes.fromhex("44" * 32)]
MODE_SWAP, MODE_ADD, MODE_REMOVE = 0, 1, 2
FEE_BPS = 30


def compiled(name: str) -> Program:
    return Program.from_bytes(bytes.fromhex(
        forge_puzzles.hex_path(name).read_text("ascii").strip()))


POOL = compiled(f"pool_singleton_v{VERSION}")
RESERVE = compiled(f"forge_reserve_v{VERSION}")
TAIL = compiled(f"forge_lp_cat_tail_v{VERSION}")


def asset(i: int) -> bytes:
    return ZERO if i == 0 else bytes([i]) * 32


def reserve_ph(a):
    return bytes(RESERVE.get_tree_hash()) if a == ZERO else \
        bytes(construct_cat_puzzle(CAT_MOD, a, RESERVE).get_tree_hash())


def settlement_ph(a):
    return bytes(OFFER_MOD_HASH) if a == ZERO else \
        bytes(construct_cat_puzzle(CAT_MOD, a, OFFER_MOD).get_tree_hash())


def accepts(old, new, total_lp, mode, lp_delta, first_is_cat=False):
    n = len(old)
    offset = 1 if first_is_cat else 0
    assets = [asset(i + offset) for i in range(n)]
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
    # V7 weights are integer units; all ones is the equal-weight pool.
    config = [VERSION, POOL.get_tree_hash(), assets, [1] * n,
              FEE_BPS, TAIL.get_tree_hash(), RESERVE.get_tree_hash()]
    state = [[[a, bytes([200 + i, o % 251]) + bytes(30), o]
              for i, (a, o) in enumerate(zip(assets, old))], total_lp]
    action = [mode, POOL_COIN, plans,
              ZERO if mode == MODE_SWAP else bytes.fromhex("88" * 32), lp_delta]
    try:
        POOL.run_with_cost(11_000_000_000, Program.to([SINGLETON, config, state, action]))
        return True
    except Exception:
        return False


def check(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def main() -> int:
    results = []

    print("mint == puzzle, exactly (value accepted, value + 1 refused):")
    cases = [
        ("1-asset XCH vault", [1_000_000_000_000], [100_000_000_000], 1_000_000_000_000, False),
        ("1-asset XCH, coarse LP", [1_000_000_000_000], [100_000_000_000], 1_000, False),
        ("1-asset CAT vault", [1_000_000], [100_000], 1_000_000, True),
        ("2-asset balanced", [1_000_000, 1_000_000], [100_000, 100_000], 1_000_000, False),
        ("3-asset unbalanced", [1_000_000, 2_000_000, 4_000_000], [100_000, 50_000, 0], 10_000, False),
        ("6-asset balanced", [1_000_000] * 6, [100_000] * 6, 1_000_000, False),
        ("7-asset single-sided", [1_000_000] * 7, [200_000] + [0] * 6, 1_000_000, False),
        ("10-asset balanced", [1_000_000] * 10, [100_000] * 10, 1_000_000, False),
    ]
    for label, old, deposits, lp, is_cat in cases:
        # V7 semantics: equal unit weights, and a vault crossing is free --
        # the LP fee on it arrived in V8.
        mint = invariant_lp_mint(old, deposits, lp, FEE_BPS, [1] * len(old), VERSION)
        new = [o + d for o, d in zip(old, deposits)]
        exact = (accepts(old, new, lp, MODE_ADD, mint, is_cat)
                 and not accepts(old, new, lp, MODE_ADD, mint + 1, is_cat))
        results.append(check(f"{label:26} mint={mint}", exact and mint > 0))

    print()
    print("withdrawal == puzzle:")
    for label, old, burn, lp, is_cat in [
        ("1-asset XCH 25%", [1_000_000_000_000], 250_000_000_000, 1_000_000_000_000, False),
        ("1-asset CAT 10%", [1_000_000], 100_000, 1_000_000, True),
        ("6-asset 25%", [1_000_000] * 6, 2_500, 10_000, False),
        ("10-asset dust burn", [1_000_003] * 10, 7, 10_000, False),
    ]:
        out = withdrawal_amounts(old, burn, lp)
        new = [o - w for o, w in zip(old, out)]
        greedy = list(new); greedy[0] -= 1
        good = (accepts(old, new, lp, MODE_REMOVE, -burn, is_cat)
                and not accepts(old, greedy, lp, MODE_REMOVE, -burn, is_cat))
        results.append(check(f"{label:26} out={out[0]}…", good))

    print()
    print("swap == puzzle (N-asset, others frozen; impossible at n=1):")
    for label, old, i_in, i_out, amount, lp in [
        ("2-asset", [1_000_000, 2_000_000], 0, 1, 100_000, 10_000),
        ("6-asset 0->4", [1_000_000] * 6, 0, 4, 100_000, 10_000),
        ("10-asset 9->0", [1_000_000] * 10, 9, 0, 75_000, 10_000),
    ]:
        out = swap_output(old[i_in], old[i_out], amount, FEE_BPS)
        new = list(old); new[i_in] += amount; new[i_out] -= out
        greedy = list(new); greedy[i_out] -= 1
        good = accepts(old, new, lp, MODE_SWAP, 0) and not accepts(old, greedy, lp, MODE_SWAP, 0)
        results.append(check(f"{label:26} out={out}", good))

    results.append(check("1-asset pool cannot swap at all",
                         not accepts([1_000_000], [1_100_000], 1_000_000, MODE_SWAP, 0)))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} agree with the compiled V7 puzzle")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
