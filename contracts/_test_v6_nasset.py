#!/usr/bin/env python3
"""Execute the compiled V6 pool singleton across N-asset cases.

Compiling proves nothing about the math, so each case below actually runs the
puzzle and asserts it accepts or rejects. Rejection cases matter most: an
N-asset pool must refuse a "swap" that quietly moves a third reserve.
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

import forge_puzzles

POOL_V6 = Program.from_bytes(
    bytes.fromhex(forge_puzzles.hex_path("pool_singleton_v6").read_text("ascii").strip())
)
RESERVE_V6 = Program.from_bytes(
    bytes.fromhex(forge_puzzles.hex_path("forge_reserve_v6").read_text("ascii").strip())
)
TAIL_V6 = Program.from_bytes(
    bytes.fromhex(forge_puzzles.hex_path("forge_lp_cat_tail_v6").read_text("ascii").strip())
)

ZERO = bytes(32)
LAUNCHER = bytes.fromhex("11" * 32)
POOL_COIN = bytes.fromhex("22" * 32)
SINGLETON = [bytes.fromhex("33" * 32), LAUNCHER, bytes.fromhex("44" * 32)]

MODE_SWAP, MODE_ADD, MODE_REMOVE = 0, 1, 2


def asset(index: int) -> bytes:
    """Ascending asset ids; index 0 is native XCH (the zero asset id)."""
    return ZERO if index == 0 else bytes([index]) * 32


def weights_for(n: int) -> list[int]:
    base = 10000 // n
    out = [base] * n
    out[0] += 10000 - base * n  # remainder rides on the first weight
    return out


def config_for(n: int, fee_bps: int = 30, weights: list[int] | None = None) -> list:
    return [
        6,
        POOL_V6.get_tree_hash(),
        [asset(i) for i in range(n)],
        weights if weights is not None else weights_for(n),
        fee_bps,
        TAIL_V6.get_tree_hash(),
        RESERVE_V6.get_tree_hash(),
    ]


def coin_id(index: int, amount: int) -> bytes:
    return bytes([200 + index, amount % 251]) + bytes(30)


def state_for(amounts: list[int], total_lp: int) -> list:
    return [[[asset(i), coin_id(i, a), a] for i, a in enumerate(amounts)], total_lp]


def reserve_ph(asset_id: bytes) -> bytes:
    """Native XCH sits at the bare reserve puzzle; CATs are wrapped."""
    if asset_id == ZERO:
        return bytes(RESERVE_V6.get_tree_hash())
    return bytes(construct_cat_puzzle(CAT_MOD, asset_id, RESERVE_V6).get_tree_hash())


def settlement_ph(asset_id: bytes) -> bytes:
    if asset_id == ZERO:
        return bytes(OFFER_MOD_HASH)
    return bytes(construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash())


def plans_for(old: list[int], new: list[int]) -> list:
    """The puzzle derives successor and settlement coin ids, so build real ones."""
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
    return plans


def run(n, old, new, total_lp, mode, lp_delta, fee_bps=30, weights=None):
    action = [
        mode,
        POOL_COIN,
        plans_for(old, new),
        ZERO if mode == MODE_SWAP else bytes.fromhex("88" * 32),
        lp_delta,
    ]
    solution = Program.to([SINGLETON, config_for(n, fee_bps, weights), state_for(old, total_lp), action])
    POOL_V6.run_with_cost(11_000_000_000, solution)


def expect(label, ok_expected, fn):
    try:
        fn()
        actual = True
        detail = ""
    except Exception as exc:
        actual = False
        detail = str(exc)[:60]
    verdict = "PASS" if actual == ok_expected else "FAIL"
    want = "accept" if ok_expected else "reject"
    got = "accepted" if actual else "rejected"
    print(f"  [{verdict}] {label:52} want {want}, {got}")
    if verdict == "FAIL" and detail:
        print(f"           {detail}")
    return verdict == "PASS"


def swap_out(reserve_in, reserve_out, amount_in, fee_bps=30):
    eff = amount_in * (10000 - fee_bps) // 10000
    return reserve_out * eff // (reserve_in + eff)


def main() -> int:
    results = []
    print("2-asset (V5 parity):")
    results.append(expect("swap: one up, one down", True,
        lambda: run(2, [1_000_000, 2_000_000], [1_100_000, 2_000_000 - swap_out(1_000_000, 2_000_000, 100_000)],
                    10_000, MODE_SWAP, 0)))
    results.append(expect("add: proportional double", True,
        lambda: run(2, [1_000_000, 2_000_000], [2_000_000, 4_000_000], 10_000, MODE_ADD, 10_000)))

    print()
    print("3-asset (new in V6):")
    three = [1_000_000, 2_000_000, 4_000_000]
    results.append(expect("swap: asset0 -> asset1, asset2 frozen", True,
        lambda: run(3, three, [1_100_000, 2_000_000 - swap_out(1_000_000, 2_000_000, 100_000), 4_000_000],
                    10_000, MODE_SWAP, 0)))
    results.append(expect("swap: third reserve also moved", False,
        lambda: run(3, three, [1_100_000, 2_000_000 - swap_out(1_000_000, 2_000_000, 100_000), 3_999_999],
                    10_000, MODE_SWAP, 0)))
    results.append(expect("add: proportional double (lp * 2)", True,
        lambda: run(3, three, [2_000_000, 4_000_000, 8_000_000], 10_000, MODE_ADD, 10_000)))
    results.append(expect("add: claims more LP than invariant allows", False,
        lambda: run(3, three, [2_000_000, 4_000_000, 8_000_000], 10_000, MODE_ADD, 12_000)))
    results.append(expect("remove: burn 10% pro rata", True,
        lambda: run(3, three, [900_000, 1_800_000, 3_600_000], 10_000, MODE_REMOVE, -1_000)))

    print()
    print("validation guards:")
    results.append(expect("weights must sum to scale", False,
        lambda: run(3, three, [1_100_000, 2_000_000 - swap_out(1_000_000, 2_000_000, 100_000), 4_000_000],
                    10_000, MODE_SWAP, 0, weights=[3333, 3333, 3333])))
    results.append(expect("weights must be near-equal", False,
        lambda: run(3, three, [1_100_000, 2_000_000 - swap_out(1_000_000, 2_000_000, 100_000), 4_000_000],
                    10_000, MODE_SWAP, 0, weights=[8000, 1000, 1000])))

    print()
    print("10-asset ceiling:")
    ten = [1_000_000 * (i + 1) for i in range(10)]
    ten_next = list(ten)
    ten_next[0] += 100_000
    ten_next[1] -= swap_out(ten[0], ten[1], 100_000)
    results.append(expect("swap in a 10-asset pool", True,
        lambda: run(10, ten, ten_next, 10_000, MODE_SWAP, 0)))

    eleven = [1_000_000 * (i + 1) for i in range(11)]
    eleven_next = list(eleven)
    eleven_next[0] += 100_000
    eleven_next[1] -= swap_out(eleven[0], eleven[1], 100_000)
    results.append(expect("11 assets exceeds MAX_ASSETS", False,
        lambda: run(11, eleven, eleven_next, 10_000, MODE_SWAP, 0)))

    print()
    print("imbalance fee (fee-bypass guard):")
    # A balanced add must be unaffected by the imbalance fee.
    results.append(expect("balanced add still mints full proportional LP", True,
        lambda: run(2, [1_000_000, 1_000_000], [1_100_000, 1_100_000], 1_000_000, MODE_ADD, 100_000)))
    # Single-asset add: the pre-fee mint (48808) must now be rejected as too much.
    results.append(expect("unbalanced add rejected at un-feed LP amount", False,
        lambda: run(2, [1_000_000, 1_000_000], [1_100_000, 1_000_000], 1_000_000, MODE_ADD, 48_808)))
    results.append(expect("unbalanced add accepted at fee-adjusted LP amount", True,
        lambda: run(2, [1_000_000, 1_000_000], [1_100_000, 1_000_000], 1_000_000, MODE_ADD, 48_737)))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} cases behaved as expected")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
