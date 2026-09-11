#!/usr/bin/env python3
"""Check forge_math agrees with the compiled V6 puzzle to the integer.

For each case the Python math produces a value, and the puzzle is then run twice:
once with that value (must accept) and once with value+1 (must reject). That
pins the result exactly rather than merely showing it is allowed.
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")

import forge_puzzles
if not forge_puzzles.available("pool_singleton_v6"):
    print("SKIP: the V6 puzzles are archived and absent from this checkout.")
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
    solve_native_add_split,
    swap_output,
    withdrawal_amounts,
)


def compiled(name: str) -> Program:
    return Program.from_bytes(bytes.fromhex(forge_puzzles.hex_path(name).read_text("ascii").strip()))


POOL = compiled("pool_singleton_v6")
RESERVE = compiled("forge_reserve_v6")
TAIL = compiled("forge_lp_cat_tail_v6")

ZERO = bytes(32)
POOL_COIN = bytes.fromhex("22" * 32)
SINGLETON = [bytes.fromhex("33" * 32), bytes.fromhex("11" * 32), bytes.fromhex("44" * 32)]
MODE_SWAP, MODE_ADD, MODE_REMOVE = 0, 1, 2
FEE_BPS = 30


def asset(index: int) -> bytes:
    return ZERO if index == 0 else bytes([index]) * 32


def reserve_ph(asset_id: bytes) -> bytes:
    if asset_id == ZERO:
        return bytes(RESERVE.get_tree_hash())
    return bytes(construct_cat_puzzle(CAT_MOD, asset_id, RESERVE).get_tree_hash())


def settlement_ph(asset_id: bytes) -> bytes:
    if asset_id == ZERO:
        return bytes(OFFER_MOD_HASH)
    return bytes(construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash())


def coin_id(index: int, amount: int) -> bytes:
    return bytes([200 + index, amount % 251]) + bytes(30)


def accepts(old, new, total_lp, mode, lp_delta) -> bool:
    count = len(old)
    plans = []
    for i, (o, n) in enumerate(zip(old, new)):
        current = coin_id(i, o)
        successor = bytes(Coin(current, reserve_ph(asset(i)), n).name())
        if n < o:
            settlement = bytes(Coin(current, settlement_ph(asset(i)), o - n).name())
        elif n == o:
            settlement = ZERO
        else:
            settlement = bytes.fromhex("77" * 32)
        plans.append([asset(i), current, o, settlement, successor, n])

    config = [
        6, POOL.get_tree_hash(), [asset(i) for i in range(count)],
        default_weights(count), FEE_BPS, TAIL.get_tree_hash(), RESERVE.get_tree_hash(),
    ]
    state = [[[asset(i), coin_id(i, a), a] for i, a in enumerate(old)], total_lp]
    action = [mode, POOL_COIN, plans, ZERO if mode == MODE_SWAP else bytes.fromhex("88" * 32), lp_delta]

    try:
        POOL.run_with_cost(11_000_000_000, Program.to([SINGLETON, config, state, action]))
        return True
    except Exception:
        return False


def check(label: str, ok: bool) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def main() -> int:
    results = []

    print("mint math == puzzle (exact, not just allowed):")
    for label, old, deposits, lp in [
        ("2-asset balanced", [1_000_000, 1_000_000], [100_000, 100_000], 1_000_000),
        ("2-asset single-sided", [1_000_000, 1_000_000], [100_000, 0], 1_000_000),
        ("3-asset balanced", [1_000_000, 2_000_000, 4_000_000], [100_000, 200_000, 400_000], 10_000),
        ("3-asset unbalanced", [1_000_000, 2_000_000, 4_000_000], [100_000, 50_000, 0], 10_000),
        ("3-asset single-sided", [5_000_000, 3_000_000, 2_000_000], [0, 0, 250_000], 7_777),
        ("5-asset unbalanced", [1_000_000] * 5, [50_000, 10_000, 0, 30_000, 5_000], 20_000),
    ]:
        mint = invariant_lp_mint(old, deposits, lp, FEE_BPS)
        new = [o + d for o, d in zip(old, deposits)]
        exact = accepts(old, new, lp, MODE_ADD, mint) and not accepts(old, new, lp, MODE_ADD, mint + 1)
        results.append(check(f"{label:24} mint={mint}", exact and mint > 0))

    print()
    print("withdrawal math == puzzle:")
    for label, old, burn, lp in [
        ("2-asset 10%", [1_000_000, 2_000_000], 1_000, 10_000),
        ("3-asset 25%", [1_000_000, 2_000_000, 4_000_000], 2_500, 10_000),
        ("3-asset dust burn", [1_000_003, 2_000_007, 4_000_011], 7, 10_000),
    ]:
        out = withdrawal_amounts(old, burn, lp)
        new = [o - w for o, w in zip(old, out)]
        results.append(check(f"{label:24} out={out}", accepts(old, new, lp, MODE_REMOVE, -burn)))

    print()
    print("swap math == puzzle (N-asset, others frozen):")
    for label, old, i_in, i_out, amount, lp in [
        ("2-asset", [1_000_000, 2_000_000], 0, 1, 100_000, 10_000),
        ("3-asset 0->2", [1_000_000, 2_000_000, 4_000_000], 0, 2, 100_000, 10_000),
        ("3-asset 2->1", [1_000_000, 2_000_000, 4_000_000], 2, 1, 400_000, 10_000),
        ("5-asset 3->0", [1_000_000] * 5, 3, 0, 75_000, 10_000),
    ]:
        out = swap_output(old[i_in], old[i_out], amount, FEE_BPS)
        new = list(old)
        new[i_in] += amount
        new[i_out] -= out
        good = accepts(old, new, lp, MODE_SWAP, 0)
        # One extra unit out must be rejected: the curve is pinned, not bounded.
        greedy = list(new)
        greedy[i_out] -= 1
        results.append(check(f"{label:24} out={out}", good and not accepts(old, greedy, lp, MODE_SWAP, 0)))

    print()
    print("native XCH add split (deposit + mint == settlement):")
    for label, reserves, native_index, deposits, lp, total_xch in [
        ("3-asset, XCH only", [1_000_000_000_000, 1_000, 1_000], 0, [0, 0, 0], 1_000, 900_000_000_000),
        ("3-asset, XCH + one CAT", [1_000_000_000_000, 1_000, 1_000], 0, [0, 200, 0], 1_000, 500_000_000),
    ]:
        try:
            native_deposit, mint = solve_native_add_split(
                total_xch, reserves, native_index, deposits, lp, FEE_BPS)
            trial = list(deposits)
            trial[native_index] = native_deposit
            new = [r + d for r, d in zip(reserves, trial)]
            ok = (native_deposit + mint == total_xch) and accepts(reserves, new, lp, MODE_ADD, mint)
            results.append(check(f"{label:24} deposit={native_deposit} mint={mint}", ok))
        except ValueError as exc:
            results.append(check(f"{label:24} -> {exc}", False))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} agree with the compiled puzzle")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
