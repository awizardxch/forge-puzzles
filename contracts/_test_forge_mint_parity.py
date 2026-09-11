#!/usr/bin/env python3
"""The Python mint maths agrees with the shipping puzzle, weights and all.

`forge_math.invariant_lp_mint` decides how much LP a deposit earns, and the
puzzle then brackets the same value exactly -- it accepts one number and nothing
else. So any disagreement is not a rounding nuisance, it is a deposit that cannot
settle at all.

Two ways they used to disagree, both invisible to the existing suites because
nothing exercised a weighted or a V8+ vault ADD:

  * the exponent -- the puzzle raises the invariant to the SUM OF THE WEIGHTS,
    the reference raised it to the asset count. Equal at [1,1]; wrong at [4,1].
  * the imbalance fee share -- the puzzle charges (K - k_i)/K of the excess, the
    reference charged (n - 1)/n. Equal weights hide it, and a balanced deposit
    hides it further because the excess is zero either way.
  * the vault -- from V8 a single-asset pool charges the LP fee on the WHOLE
    deposit (crossing it is the only trade it has). The reference charged
    nothing, so every V8+ vault deposit was rejected.

Each case below asks the real compiled puzzle to accept the reference's number,
and to refuse one more, which is what "brackets it exactly" means.
"""
import hashlib
import sys

sys.path.insert(0, ".")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD
from chia_rs.sized_bytes import bytes32

import forge_puzzles
from forge_offer import ZERO_32, compiled_program, reserve_inner_puzzle
from forge_math import invariant_lp_mint, swap_output

MODE_ADD = 1
FORGE = forge_puzzles.FORGE_VERSION
LAUNCHER = bytes32(b"\x10" * 32)
LP_ASSET = bytes32(bytes.fromhex("aa" * 32))
POOL_MOD = bytes32(bytes.fromhex("cc" * 32))
SINGLETON = [bytes32(b"\x0a" * 32), bytes32(b"\x0b" * 32), bytes32(b"\x0c" * 32)]
POOL_COIN = bytes32(b"\x01" * 32)
CATS = [bytes32(bytes.fromhex(f"{b:02x}" * 32)) for b in (0xD1, 0xD2, 0xD3)]


def amt(n: int) -> bytes:
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 8) // 8, "big")


def coin_id(parent: bytes32, ph: bytes32, a: int) -> bytes32:
    return bytes32(hashlib.sha256(bytes(parent) + bytes(ph) + amt(a)).digest())


def cat_ph(asset_id: bytes32, inner: bytes32) -> bytes32:
    return construct_cat_puzzle(CAT_MOD, asset_id, Program.to(inner)).get_tree_hash_precalc(inner)


def reserve_ph(asset_id: bytes32) -> bytes32:
    inner = reserve_inner_puzzle(FORGE, LAUNCHER).get_tree_hash()
    return inner if asset_id == ZERO_32 else cat_ph(asset_id, inner)


def settle_ph(asset_id: bytes32) -> bytes32:
    return bytes32(OFFER_MOD.get_tree_hash()) if asset_id == ZERO_32 else \
        construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash()


def mint_eve_id(parent: bytes32) -> bytes32:
    mint = compiled_program("forge_lp_mint_inner_FORGE").get_tree_hash()
    return coin_id(parent, cat_ph(LP_ASSET, mint), 1)


def assets_for(count: int) -> list[bytes32]:
    """Native first (it sorts lowest), then CATs in canonical order."""
    return [ZERO_32] + CATS[:count - 1]


def try_mint(reserves, deposits, weights, total_lp, fee_bps, mint) -> bool:
    assets = assets_for(len(reserves))
    cfg = [FORGE, POOL_MOD, assets, list(weights), fee_bps, 0, ZERO_32,
           LP_ASSET, reserve_inner_puzzle(FORGE, LAUNCHER).get_tree_hash()]
    coins = [bytes32(bytes([0x20 + i]) * 32) for i in range(len(reserves))]
    state = [[[a, c, r] for a, c, r in zip(assets, coins, reserves)], total_lp]

    plans = []
    for asset, coin, old, dep in zip(assets, coins, reserves, deposits):
        new = old + dep
        # A growing reserve is fed by the Offer's settlement, which the pool only
        # requires to be named; an untouched one names nothing.
        settle = coin_id(coin, settle_ph(asset), dep) if dep > 0 else ZERO_32
        plans.append([asset, coin, old, settle,
                      coin_id(coin, reserve_ph(asset), new), new, 0, ZERO_32])

    eve_parent = bytes32(b"\x51" * 32)
    action = [MODE_ADD, POOL_COIN, plans, mint_eve_id(eve_parent), mint, eve_parent]
    inner = compiled_program("pool_singleton_FORGE").curry(SINGLETON, cfg, state)
    try:
        inner.run(Program.to([action]))
        return True
    except Exception:
        return False


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    results = []
    lp, fee = 100_000, 30
    big, small = 10_000_000_000_000, 5_000_000

    cases = [
        ("equal [1,1], balanced",      [big, small], [10**9, 500],   [1, 1]),
        ("equal [1,1], imbalanced",    [big, small], [10**10, 500],  [1, 1]),
        ("equal [1,1], single-sided",  [big, small], [10**10, 0],    [1, 1]),
        ("weighted [4,1], imbalanced", [big, small], [10**10, 500],  [4, 1]),
        ("weighted [1,4], imbalanced", [big, small], [10**10, 500],  [1, 4]),
        ("weighted [7,1], imbalanced", [big, small], [10**10, 500],  [7, 1]),
        ("weighted [2,1,1]",           [big, small, 8_000_000], [10**10, 500, 800], [2, 1, 1]),
        ("weighted [3,2,1]",           [big, small, 8_000_000], [10**9, 500, 800],  [3, 2, 1]),
        ("vault [1], fee on",          [big],        [10**9],        [1]),
        ("vault [4], fee on",          [big],        [10**9],        [4]),
    ]

    for label, reserves, deposits, weights in cases:
        mint = invariant_lp_mint(reserves, deposits, lp, fee, weights, FORGE)
        accepted = try_mint(reserves, deposits, weights, lp, fee, mint)
        refused_more = not try_mint(reserves, deposits, weights, lp, fee, mint + 1)
        results.append(check(f"{label:<28} mint={mint}",
                             mint > 0 and accepted and refused_more,
                             "" if accepted else "puzzle refused the reference's mint"))

    # A zero-fee pool must agree too, so the parity is not an artefact of the fee.
    print()
    for label, reserves, deposits, weights in cases[3:4] + cases[8:9]:
        mint = invariant_lp_mint(reserves, deposits, lp, 0, weights, FORGE)
        ok = (try_mint(reserves, deposits, weights, lp, 0, mint)
              and not try_mint(reserves, deposits, weights, lp, 0, mint + 1))
        results.append(check(f"{label:<28} at zero fee, mint={mint}", ok))

    # ---- swaps -----------------------------------------------------------
    # The same defect lived in the swap maths: the closed form is exact only when
    # the traded pair's weights are equal, and the puzzle brackets the output.
    print()
    print("swap output agrees with the puzzle:")
    WS = 10_000
    def puzzle_swap(rin, rout, amt, win, wout, fee_bps):
        eff = (amt * (WS - fee_bps)) // WS
        charged, floor = rin + eff, rin ** win * rout ** wout
        low, high, best = 1, rout - 1, None
        while low <= high:
            mid = (low + high) // 2
            if charged ** win * mid ** wout >= floor:
                best, high = mid, mid - 1
            else:
                low = mid + 1
        return rout - best

    amount_in = 100_000_000_000
    for weights in ([1, 1], [4, 1], [1, 4], [7, 1], [2, 3]):
        expected = puzzle_swap(big, small, amount_in, weights[0], weights[1], fee)
        got = swap_output(big, small, amount_in, fee, weights[0], weights[1])
        results.append(check(f"weights {str(weights):<8} out={got}", got == expected,
                             "" if got == expected else f"puzzle says {expected}"))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} parity cases agree with the puzzle")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
