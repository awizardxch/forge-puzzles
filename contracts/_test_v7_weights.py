#!/usr/bin/env python3
"""Weighted V7 pools against the compiled puzzle.

V7 weights are integer units, not basis points: a pool keeps
prod(reserve_i ** weight_i) constant, K is the sum of the weights, and
percentages shown to a user are weight_i / K. Equal weights are all ones.

Both the mint and the swap are verified by bracketing rather than solved, so no
root is ever taken on chain. This checks the puzzle agrees with that model, that
an unequal split really does move the price, and that equal weights reproduce
the constant product they replace.
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

ZERO = bytes(32)
POOL_COIN = bytes.fromhex("22" * 32)
SINGLETON = [bytes.fromhex("33" * 32), bytes.fromhex("11" * 32), bytes.fromhex("44" * 32)]
MODE_SWAP, MODE_ADD, MODE_REMOVE = 0, 1, 2
FEE_BPS = 30
BLOCK = 11_000_000_000


def compiled(name):
    return Program.from_bytes(bytes.fromhex(
        forge_puzzles.hex_path(name).read_text("ascii").strip()))


POOL, RESERVE, TAIL = compiled("pool_singleton_v7"), compiled("forge_reserve_v7"), compiled("forge_lp_cat_tail_v7")


def asset(i):
    return ZERO if i == 0 else bytes([i]) * 32


def reserve_ph(a):
    return bytes(RESERVE.get_tree_hash()) if a == ZERO else \
        bytes(construct_cat_puzzle(CAT_MOD, a, RESERVE).get_tree_hash())


def settlement_ph(a):
    return bytes(OFFER_MOD_HASH) if a == ZERO else \
        bytes(construct_cat_puzzle(CAT_MOD, a, OFFER_MOD).get_tree_hash())


def run(old, new, weights, total_lp, mode, lp_delta):
    n = len(old)
    assets = [asset(i) for i in range(n)]
    plans = []
    for i, (a, o, nn) in enumerate(zip(assets, old, new)):
        cur = bytes([200 + i, o % 251]) + bytes(30)
        succ = bytes(Coin(cur, reserve_ph(a), uint64(nn)).name())
        if nn < o:
            sid = bytes(Coin(cur, settlement_ph(a), uint64(o - nn)).name())
        elif nn == o:
            sid = ZERO
        else:
            sid = bytes.fromhex("77" * 32)
        plans.append([a, cur, o, sid, succ, nn])
    config = [7, POOL.get_tree_hash(), assets, weights, FEE_BPS,
              TAIL.get_tree_hash(), RESERVE.get_tree_hash()]
    state = [[[a, bytes([200 + i, o % 251]) + bytes(30), o]
              for i, (a, o) in enumerate(zip(assets, old))], total_lp]
    action = [mode, POOL_COIN, plans, ZERO if mode == MODE_SWAP else bytes.fromhex("88" * 32), lp_delta]
    try:
        cost, _ = POOL.run_with_cost(BLOCK, Program.to([SINGLETON, config, state, action]))
        return True, cost
    except Exception:
        return False, 0


def invariant(reserves, weights):
    v = 1
    for r, k in zip(reserves, weights):
        v *= r ** k
    return v


def solve_out(reserves, weights, i_in, i_out, gross):
    """Largest output holding the weighted invariant — the mirror the builder needs."""
    eff = gross * (10000 - FEE_BPS) // 10000
    base = invariant(reserves, weights)
    nxt = list(reserves)
    nxt[i_in] += eff
    lo, hi, best = 0, reserves[i_out] - 1, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        nxt[i_out] = reserves[i_out] - mid
        if invariant(nxt, weights) >= base:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best, eff


def solve_mint(old, deposits, weights, total_lp, fee_bps):
    """Largest mint the weighted bracket accepts."""
    K = sum(weights)
    ratio = min((old[i] + deposits[i]) * 10**12 // old[i] for i in range(len(old))) - 10**12
    ratio = min(deposits[i] * 10**12 // old[i] for i in range(len(old)))
    eff = []
    for i, (o, d) in enumerate(zip(old, deposits)):
        balanced = o * ratio // 10**12
        excess = d - balanced
        fee = excess * fee_bps * (K - weights[i]) // (K * 10000)
        eff.append(o + balanced + excess - fee)
    target = (total_lp ** K) * invariant([e for e in eff], weights)
    oldp = invariant(old, weights)
    lo, hi, best = 0, max(total_lp * 4, 1024), 0
    while ((total_lp + hi) ** K) * oldp <= target:
        hi *= 2
    while lo <= hi:
        mid = (lo + hi) // 2
        if ((total_lp + mid) ** K) * oldp <= target:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def check(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def main() -> int:
    results = []
    R = [1_000_000, 1_000_000]

    print("weighted swap: 100,000 in, reserves [1e6, 1e6]")
    print(f"    {'split':>8} {'units':>8} {'out':>9}  puzzle accepts / refuses one more")
    for label, w in [("50/50", [1, 1]), ("66/33", [2, 1]), ("75/25", [3, 1]),
                     ("80/20", [4, 1]), ("20/80", [1, 4])]:
        out, eff = solve_out(R, w, 0, 1, 100_000)
        # The reserve grows by the GROSS input: the fee stays in the pool and is
        # simply excluded from the curve when the invariant is checked.
        good = list(R); good[0] += 100_000; good[1] -= out
        greedy = list(good); greedy[1] -= 1
        ok, cost = run(R, good, w, 1_000_000, MODE_SWAP, 0)
        tight, _ = run(R, greedy, w, 1_000_000, MODE_SWAP, 0)
        print(f"    {label:>8} {str(w):>8} {out:>9}  accept={ok} refuse_greedy={not tight}  cost={cost:,}")
        results.append(check(f"{label} swap pinned exactly", ok and not tight))

    print()
    print("equal weights still reproduce the constant product:")
    for gross in (1_000, 100_000, 500_000):
        out, eff = solve_out([1_000_000, 2_000_000], [1, 1], 0, 1, gross)
        closed = 2_000_000 * eff // (1_000_000 + eff)
        results.append(check(f"gross {gross}: bracket {out} == closed form {closed}", out == closed))

    print()
    print("weighted mint:")
    for label, w, deposits in [("50/50 balanced", [1, 1], [100_000, 100_000]),
                               ("80/20 balanced", [4, 1], [100_000, 100_000]),
                               ("80/20 single-sided", [4, 1], [100_000, 0]),
                               ("3-asset 2:1:1", [2, 1, 1], [100_000, 100_000, 100_000])]:
        old = [1_000_000] * len(w)
        mint = solve_mint(old, deposits, w, 1_000_000, FEE_BPS)
        new = [o + d for o, d in zip(old, deposits)]
        ok, _ = run(old, new, w, 1_000_000, MODE_ADD, mint)
        over, _ = run(old, new, w, 1_000_000, MODE_ADD, mint + 1)
        results.append(check(f"{label:22} mint={mint} pinned", ok and not over and mint > 0))

    print()
    print("config rules:")
    results.append(check("weight below 1 refused",
                         not run(R, [R[0] + 100_000, R[1]], [0, 1], 1_000_000, MODE_ADD, 1)[0]))
    results.append(check("weight above the cap refused",
                         not run(R, [R[0] + 100_000, R[1]], [9, 1], 1_000_000, MODE_ADD, 1)[0]))
    big = [1_000_000] * 3
    results.append(check("total weight above the cap refused",
                         not run(big, [b + 1000 for b in big], [8, 8, 8], 1_000_000, MODE_ADD, 1)[0]))
    results.append(check("single asset holding all weight still works",
                         run([1_000_000], [1_100_000], [1], 1_000_000, MODE_ADD, 100_000)[0]))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} weighted V7 checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
