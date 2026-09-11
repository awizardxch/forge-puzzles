#!/usr/bin/env python3
"""A one-asset vault cannot trade, but its receipt token can.

The vault puzzle refuses to swap because there is no second reserve to move.
That describes the puzzle, not the position: the LP token it mints is an
ordinary CAT, so pairing it in a second pool creates a real market for it -- and
by extension a route to the underlying, priced against whatever it is paired
with.

This builds exactly that arrangement against the compiled V7 puzzle: a vault
over one asset, then a two-asset pool holding that vault's LP asset id, and a
swap across it.
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
VAULT_LAUNCHER = bytes.fromhex("5a" * 32)
PARTNER_CAT = bytes.fromhex("c0" * 32)


def compiled(name):
    return Program.from_bytes(bytes.fromhex(
        forge_puzzles.hex_path(name).read_text("ascii").strip()))


POOL, RESERVE, TAIL = compiled("pool_singleton_v7"), compiled("forge_reserve_v7"), compiled("forge_lp_cat_tail_v7")


def lp_asset_id(launcher_id: bytes) -> bytes:
    """The receipt token a pool mints is a CAT whose tail is bound to its launcher."""
    return bytes(TAIL.curry(launcher_id, 7).get_tree_hash())


def reserve_ph(a):
    return bytes(RESERVE.get_tree_hash()) if a == ZERO else \
        bytes(construct_cat_puzzle(CAT_MOD, a, RESERVE).get_tree_hash())


def settlement_ph(a):
    return bytes(OFFER_MOD_HASH) if a == ZERO else \
        bytes(construct_cat_puzzle(CAT_MOD, a, OFFER_MOD).get_tree_hash())


def run(assets, old, new, weights, total_lp, mode, lp_delta):
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
    config = [7, POOL.get_tree_hash(), list(assets), weights, FEE_BPS,
              TAIL.get_tree_hash(), RESERVE.get_tree_hash()]
    state = [[[a, bytes([200 + i, o % 251]) + bytes(30), o]
              for i, (a, o) in enumerate(zip(assets, old))], total_lp]
    action = [mode, POOL_COIN, plans, ZERO if mode == MODE_SWAP else bytes.fromhex("88" * 32), lp_delta]
    try:
        POOL.run_with_cost(11_000_000_000, Program.to([SINGLETON, config, state, action]))
        return True
    except Exception:
        return False


def invariant(reserves, weights):
    v = 1
    for r, k in zip(reserves, weights):
        v *= r ** k
    return v


def solve_out(reserves, weights, i_in, i_out, gross):
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
    return best


def check(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def main() -> int:
    results = []
    receipt = lp_asset_id(VAULT_LAUNCHER)

    print(f"vault over one CAT, launcher {VAULT_LAUNCHER.hex()[:12]}…")
    print(f"  its receipt token is CAT {receipt.hex()[:16]}…")
    R, LP = 1_000_000, 1_000_000

    # The vault mints against its single reserve and refuses to trade.
    results.append(check("vault mints its proportional share",
                         run([PARTNER_CAT], [R], [R + 100_000], [1], LP, MODE_ADD, 100_000)))
    results.append(check("vault refuses to swap — no second reserve",
                         not run([PARTNER_CAT], [R], [R + 100_000], [1], LP, MODE_SWAP, 0)))
    print()

    # Now pair that receipt in a two-asset pool. Asset ids must ascend.
    other = bytes.fromhex("e1" * 32)
    pair = sorted([receipt, other])
    i_receipt = pair.index(receipt)
    i_other = 1 - i_receipt
    print(f"pool holding the receipt paired with CAT {other.hex()[:12]}…")
    print(f"  asset order: [{pair[0].hex()[:10]}…, {pair[1].hex()[:10]}…]  receipt at index {i_receipt}")

    old = [R, R]
    for label, weights in [("50/50", [1, 1]), ("80/20 toward the receipt", None)]:
        if weights is None:
            weights = [4, 1] if i_receipt == 0 else [1, 4]
        out = solve_out(old, weights, i_other, i_receipt, 100_000)
        good = list(old)
        good[i_other] += 100_000
        good[i_receipt] -= out
        greedy = list(good)
        greedy[i_receipt] -= 1
        ok = run(pair, old, good, weights, LP, MODE_SWAP, 0)
        tight = not run(pair, old, greedy, weights, LP, MODE_SWAP, 0)
        results.append(check(f"{label}: buy {out} receipt for 100,000 — pinned exactly", ok and tight))

    # And the reverse direction: sell the receipt back.
    out_back = solve_out(old, [1, 1], i_receipt, i_other, 100_000)
    good = list(old)
    good[i_receipt] += 100_000
    good[i_other] -= out_back
    results.append(check(f"receipt sells back for {out_back} — the market runs both ways",
                         run(pair, old, good, [1, 1], LP, MODE_SWAP, 0)))

    # Liquidity can be added to the receipt pool like any other.
    results.append(check("receipt pool accepts liquidity",
                         run(pair, old, [R + 100_000, R + 100_000], [1, 1], LP, MODE_ADD, 100_000)))

    print()
    print("So a one-asset pool is not a dead end: it mints a tradeable claim on a")
    print("single reserve, and the trading happens one level up.")
    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} composability checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
