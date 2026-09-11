#!/usr/bin/env python3
"""V8 protocol fee, against the compiled puzzles.

The fee sits ON TOP of the liquidity fee and goes to a different party:

  liquidity fee  stays inside the curve and accrues to the reserve, so it is
                 earned by liquidity providers and LP coin holders.
  protocol fee   leaves the pool to a fixed recipient, and the TRADER pays it --
                 the pool releases the same curve output either way and the
                 trader simply receives that output minus the fee.

Unlike the router fee this one cannot be avoided, because the reserve puzzle
creates the recipient coin itself and the pool binds the amount into the
announcement the reserve asserts.
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")

import forge_puzzles
if not forge_puzzles.available("pool_singleton_v8"):
    print("SKIP: the V8 puzzles are archived and absent from this checkout.")
    print("      Superseded revisions are not published; see docs/FORGE_SECURITY_AUDIT.md.")
    raise SystemExit(2)   # 2 == skipped; a skip must never read as a pass

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode as OP
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH
from chia_rs import Coin
from chia_rs.sized_ints import uint64

from forge_math import swap_output

import forge_puzzles

ZERO = bytes(32)
POOL_COIN = bytes.fromhex("22" * 32)
SINGLETON = [bytes.fromhex("33" * 32), bytes.fromhex("11" * 32), bytes.fromhex("44" * 32)]
MODE_SWAP, MODE_ADD, MODE_REMOVE = 0, 1, 2
FEE_BPS = 30
TREASURY = bytes.fromhex("7e" * 32)
MAX_COST = 11_000_000_000


def compiled(name):
    return Program.from_bytes(bytes.fromhex(
        forge_puzzles.hex_path(name).read_text("ascii").strip()))


POOL = compiled("pool_singleton_v8")
RESERVE = compiled("forge_reserve_v8")
TAIL = compiled("forge_lp_cat_tail_v8")


def asset(i):
    return ZERO if i == 0 else bytes([i]) * 32


def reserve_ph(a):
    if a == ZERO:
        return bytes(RESERVE.get_tree_hash())
    return bytes(construct_cat_puzzle(CAT_MOD, a, RESERVE).get_tree_hash())


def settlement_ph(a):
    if a == ZERO:
        return bytes(OFFER_MOD_HASH)
    return bytes(construct_cat_puzzle(CAT_MOD, a, OFFER_MOD).get_tree_hash())


def plans_for(old, new, protocol_bps, mode, treasury=TREASURY):
    plans = []
    for i, (o, nn) in enumerate(zip(old, new)):
        a = asset(i)
        cur = bytes([200 + i, o % 251]) + bytes(30)
        succ = bytes(Coin(cur, reserve_ph(a), uint64(nn)).name())
        if nn < o:
            fee = ((o - nn) * protocol_bps) // 10000 if mode == MODE_SWAP else 0
            # The settlement carries the trader's share only; the fee leaves as
            # its own coin, so the id commits to the smaller amount.
            sid = bytes(Coin(cur, settlement_ph(a), uint64(o - nn - fee)).name())
        elif nn == o:
            sid, fee = ZERO, 0
        else:
            sid, fee = bytes.fromhex("77" * 32), 0
        plans.append([a, cur, o, sid, succ, nn, fee, treasury if fee else ZERO])
    return plans


def run_pool(old, new, weights, total_lp, mode, lp_delta, protocol_bps,
             plans=None, treasury=TREASURY):
    n = len(old)
    assets = [asset(i) for i in range(n)]
    if plans is None:
        plans = plans_for(old, new, protocol_bps, mode, treasury)
    config = [8, POOL.get_tree_hash(), assets, weights, FEE_BPS,
              protocol_bps, treasury, TAIL.get_tree_hash(), RESERVE.get_tree_hash()]
    state = [[[asset(i), bytes([200 + i, o % 251]) + bytes(30), o]
              for i, o in enumerate(old)], total_lp]
    action = [mode, POOL_COIN, plans,
              ZERO if mode == MODE_SWAP else bytes.fromhex("88" * 32), lp_delta]
    try:
        POOL.run_with_cost(MAX_COST, Program.to([SINGLETON, config, state, action]))
        return True
    except Exception:
        return False


def reserve_payouts(plan, mode):
    """Run the reserve puzzle and report what it actually creates."""
    solution = Program.to([
        bytes.fromhex("aa" * 32), plan[0], RESERVE.get_tree_hash(),
        [mode, POOL_COIN, *plan],
    ])
    conditions = conditions_dict_for_solution(RESERVE, solution, MAX_COST)
    out = {}
    for c in conditions.get(OP.CREATE_COIN, []):
        amount = int.from_bytes(c.vars[1], "big") if c.vars[1] else 0
        key = bytes(c.vars[0])
        out[key] = out.get(key, 0) + amount
    return out


def check(label, ok):
    verdict = "PASS" if ok else "FAIL"
    print(f"  [{verdict}] {label}")
    return ok


def main() -> int:
    results = []
    R = [1_000_000, 1_000_000]
    W = [1, 1]
    LP = 1_000_000
    amount_in = 100_000
    released = swap_output(R[0], R[1], amount_in, FEE_BPS)
    new = [R[0] + amount_in, R[1] - released]

    print(f"swap {amount_in} into [1e6, 1e6]: the curve releases {released}")
    print()

    for bps in (0, 5, 25, 100):
        fee = (released * bps) // 10000
        ok = run_pool(R, new, W, LP, MODE_SWAP, 0, bps)
        payouts = reserve_payouts(plans_for(R, new, bps, MODE_SWAP)[1], MODE_SWAP)
        to_trader = payouts.get(bytes(OFFER_MOD_HASH), 0)
        to_treasury = payouts.get(TREASURY, 0)
        kept = payouts.get(bytes(RESERVE.get_tree_hash()), 0)
        good = (ok and to_treasury == fee and to_trader == released - fee
                and to_trader + to_treasury == released and kept == new[1])
        print(f"    {bps:>4}bps  fee={fee:>6}  trader={to_trader:>6}  "
              f"treasury={to_treasury:>6}  reserve keeps {kept}")
        results.append(check(f"{bps}bps: trader pays it, pool unaffected", good))

    print()
    print("the fee cannot be skipped or redirected:")
    bps = 25
    honest = plans_for(R, new, bps, MODE_SWAP)

    skipped = [list(p) for p in honest]
    skipped[1][6] = 0
    skipped[1][7] = ZERO
    results.append(check("a plan claiming no fee is refused",
                         not run_pool(R, new, W, LP, MODE_SWAP, 0, bps, plans=skipped)))

    shaved = [list(p) for p in honest]
    shaved[1][6] = honest[1][6] - 1
    results.append(check("a plan one mojo short is refused",
                         not run_pool(R, new, W, LP, MODE_SWAP, 0, bps, plans=shaved)))

    diverted = [list(p) for p in honest]
    diverted[1][7] = bytes.fromhex("bb" * 32)
    results.append(check("a plan paying a different recipient is refused",
                         not run_pool(R, new, W, LP, MODE_SWAP, 0, bps, plans=diverted)))

    inflated = [list(p) for p in honest]
    inflated[1][6] = honest[1][6] + 1
    results.append(check("a plan overcharging is refused",
                         not run_pool(R, new, W, LP, MODE_SWAP, 0, bps, plans=inflated)))

    print()
    print("liquidity moves are never charged:")
    added = [r + 100_000 for r in R]
    results.append(check("a deposit pays no protocol fee",
                         run_pool(R, added, W, LP, MODE_ADD, 100_000, 25)))
    burn = LP // 4
    withdrawn = [r - (r * burn) // LP for r in R]
    results.append(check("a withdrawal pays no protocol fee",
                         run_pool(R, withdrawn, W, LP, MODE_REMOVE, -burn, 25)))
    charged = plans_for(R, withdrawn, 25, MODE_SWAP)
    results.append(check("a withdrawal that tries to charge one is refused",
                         not run_pool(R, withdrawn, W, LP, MODE_REMOVE, -burn, 25,
                                      plans=charged)))

    print()
    print("config rules:")
    results.append(check("a fee above the cap is refused",
                         not run_pool(R, new, W, LP, MODE_SWAP, 0, 101)))
    results.append(check("a fee with no recipient is refused",
                         not run_pool(R, new, W, LP, MODE_SWAP, 0, 25, treasury=ZERO)))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} protocol-fee checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
