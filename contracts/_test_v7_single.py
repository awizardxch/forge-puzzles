#!/usr/bin/env python3
"""V7 single-asset pools, against the compiled puzzle.

A one-asset pool is a vault: deposits mint a receipt CAT against the single
reserve, burning it redeems the share, and there is nothing to swap. This checks
the compiled V7 puzzle actually behaves that way for a native (XCH) reserve and
for a CAT reserve, and that V6 still refuses n = 1 so the change is real.
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

from forge_math import default_weights

from forge_math import swap_output

import forge_puzzles

ZERO = bytes(32)
POOL_COIN = bytes.fromhex("22" * 32)
SINGLETON = [bytes.fromhex("33" * 32), bytes.fromhex("11" * 32), bytes.fromhex("44" * 32)]
MODE_SWAP, MODE_ADD, MODE_REMOVE = 0, 1, 2
FEE_BPS = 30
A_CAT = bytes.fromhex("a1" * 32)


def compiled(name: str) -> Program:
    return Program.from_bytes(bytes.fromhex(
        forge_puzzles.hex_path(name).read_text("ascii").strip()))


def runner(version: int):
    pool = compiled(f"pool_singleton_v{version}")
    reserve = compiled(f"forge_reserve_v{version}")
    tail = compiled(f"forge_lp_cat_tail_v{version}")

    def reserve_ph(asset_id):
        return bytes(reserve.get_tree_hash()) if asset_id == ZERO else \
            bytes(construct_cat_puzzle(CAT_MOD, asset_id, reserve).get_tree_hash())

    def settlement_ph(asset_id):
        return bytes(OFFER_MOD_HASH) if asset_id == ZERO else \
            bytes(construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash())

    def accepts(assets, old, new, total_lp, mode, lp_delta, weights=None):
        n = len(assets)
        plans = []
        for i, (a, o, nn) in enumerate(zip(assets, old, new)):
            current = bytes([200 + i, o % 251]) + bytes(30)
            successor = bytes(Coin(current, reserve_ph(a), uint64(nn)).name())
            if nn < o:
                s = bytes(Coin(current, settlement_ph(a), uint64(o - nn)).name())
            elif nn == o:
                s = ZERO
            else:
                s = bytes.fromhex("77" * 32)
            plans.append([a, current, o, s, successor, nn])
        if weights is None:
            # V7 weights are integer units; all ones is the equal-weight pool.
            weights = [1] * n if version >= 7 else default_weights(n)
        config = [version, pool.get_tree_hash(), list(assets), weights,
                  FEE_BPS, tail.get_tree_hash(), reserve.get_tree_hash()]
        state = [[[a, bytes([200 + i, o % 251]) + bytes(30), o]
                  for i, (a, o) in enumerate(zip(assets, old))], total_lp]
        action = [mode, POOL_COIN, plans,
                  ZERO if mode == MODE_SWAP else bytes.fromhex("88" * 32), lp_delta]
        try:
            pool.run_with_cost(11_000_000_000, Program.to([SINGLETON, config, state, action]))
            return True
        except Exception:
            return False

    return accepts


def check(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def main() -> int:
    v7 = runner(7)
    v6 = runner(6)
    results = []

    for label, asset in [("native XCH", ZERO), ("a CAT", A_CAT)]:
        print(f"single-asset pool holding {label}:")
        R, LP = 1_000_000, 1_000_000

        # Deposit mints exactly its proportional share.
        for dep in (100_000, 1, 500_000):
            share = LP * dep // R
            results.append(check(
                f"deposit {dep} mints {share}",
                v7([asset], [R], [R + dep], LP, MODE_ADD, share)))
            results.append(check(
                f"deposit {dep} rejects {share + 1} (over-mint)",
                not v7([asset], [R], [R + dep], LP, MODE_ADD, share + 1)))

        # Withdrawal returns the share.
        burn = LP // 4
        back = R * burn // LP
        results.append(check(f"burn {burn} returns {back}",
                             v7([asset], [R], [R - back], LP, MODE_REMOVE, -burn)))
        results.append(check(f"burn {burn} refuses to return {back + 1}",
                             not v7([asset], [R], [R - back - 1], LP, MODE_REMOVE, -burn)))

        # A one-asset pool cannot trade: no pair exists.
        results.append(check("swap is impossible (reserve up)",
                             not v7([asset], [R], [R + 1000], LP, MODE_SWAP, 0)))
        results.append(check("swap is impossible (reserve down)",
                             not v7([asset], [R], [R - 1000], LP, MODE_SWAP, 0)))

        # A deposit that mints nothing is refused, as on wider pools.
        results.append(check("zero-mint deposit refused",
                             not v7([asset], [R], [R + 100_000], LP, MODE_ADD, 0)))

        # V6 must still refuse the same pool, or nothing actually changed.
        results.append(check("V6 refuses this pool (the floor is real)",
                             not v6([asset], [R], [R + 100_000], LP, MODE_ADD, 100_000)))
        print()

    # An unusual LP-to-asset ratio must still hold exactly.
    print("single-asset vault at a non-unity ratio (1 LP = 1e9 mojo):")
    R, LP = 1_000_000_000_000, 1_000
    for dep in (100_000_000_000, 500_000_000_000):
        share = LP * dep // R
        results.append(check(f"deposit {dep:,} mints {share}",
                             v7([ZERO], [R], [R + dep], LP, MODE_ADD, share)))
        results.append(check(f"deposit {dep:,} rejects {share + 1}",
                             not v7([ZERO], [R], [R + dep], LP, MODE_ADD, share + 1)))
    print()

    # Wider pools must be untouched: V7 has to give the same verdict as V6 on
    # every case, or lifting the floor changed more than intended.
    print("V7 agrees with V6 on wider pools:")
    B_CAT = bytes.fromhex("b2" * 32)
    for label, assets in [("2 assets, XCH + CAT", [ZERO, A_CAT]),
                          ("3 assets, XCH + 2 CATs", [ZERO, A_CAT, B_CAT]),
                          ("2 assets, CAT + CAT", [A_CAT, B_CAT])]:
        n = len(assets)
        base = [1_000_000] * n
        amount_in = 100_000
        out = swap_output(base[0], base[1], amount_in, FEE_BPS)

        cases = [
            ("exact-curve swap accepted", list(base), MODE_SWAP, 0, True),
            ("one mojo more out refused", list(base), MODE_SWAP, 0, False),
            ("balanced add accepted", list(base), MODE_ADD, 100_000, True),
        ]
        # swap at the canonical output
        good = list(base); good[0] += amount_in; good[1] -= out
        greedy = list(base); greedy[0] += amount_in; greedy[1] -= out + 1
        added = [r + 100_000 for r in base]

        for lbl, new_state, mode, delta in [
            ("exact-curve swap accepted", good, MODE_SWAP, 0),
            ("one mojo more out refused", greedy, MODE_SWAP, 0),
            ("balanced add accepted", added, MODE_ADD, 100_000),
        ]:
            a7 = v7(assets, base, new_state, 1_000_000, mode, delta)
            a6 = v6(assets, base, new_state, 1_000_000, mode, delta)
            results.append(check(f"{label}: {lbl} — V7 {a7}, V6 {a6}", a7 == a6))
    print()

    passed = sum(results)
    print(f"{passed}/{len(results)} V7 single-asset checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
