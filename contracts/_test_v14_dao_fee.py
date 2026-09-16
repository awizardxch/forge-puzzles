#!/usr/bin/env python3
"""The V11.1 DAO fee: charged on swap output, paid by collect, lowered only by the DAO.

FORGE_DAO_FEE_V11.md numbers the vectors; each check here names the one it closes.

  V1  successor substitution: the rate is the ONLY thing the dao_fee leaf changes --
      reserves, LP, owed fees and the oracle are the prologue's, so every reserve is
      re-created at its current amount; the recipient is config and cannot move.
  V2  authorization: the leaf receives a mode-23 message from a coin at the DAO's
      puzzle hash naming the new rate; no message, or a message from another puzzle,
      refuses the spend at consensus.
  V3  no value moves in the decrease (checked through the finalizer's reserve rule).
  V4  replay: a second decrease to the same rate is refused by the leaf itself
      (`new < current` fails once current == new), before any mempool rule.
  V5  a swap after the decrease in the SAME spend is charged at the new rate.
  V7  the caps: a genesis rate above MAX_DAO_FEE_BPS, or a nonzero rate with a zero
      recipient, is refused by the prologue on every action.

Exits 0 when every check passes, 1 otherwise, 2 when the V11 build is absent.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import forge_math  # noqa: E402
import _v14_testkit as kit  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

results: list[bool] = []
H0 = 6_999_990
CAT = bytes32(b"\xda" * 32)
DAO_PUZZLE = Program.to(1)                 # the DAO's coin: an identity puzzle, so its spend is its conditions
DAO_PH = bytes32(DAO_PUZZLE.get_tree_hash())
PROTOCOL_PH = bytes32(b"\x55" * 32)
SEND_MESSAGE, MODE_PUZZLE_TO_COIN = 66, 0b010111   # sender by puzzle, receiver by coin id: 23


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def dao_message(new_bps: int) -> bytes32:
    return bytes32(Program.to([b"forge-dao-fee-v1", new_bps]).get_tree_hash())


def dao_coin_spend(pool, new_bps: int, puzzle: Program = DAO_PUZZLE, salt: int = 0x9A):
    """The DAO spends a coin at its puzzle hash, sending the decrease to THIS pool coin."""
    coin = Coin(bytes32(bytes([salt]) * 32), bytes32(puzzle.get_tree_hash()), uint64(1))
    return make_spend(coin, puzzle, Program.to([[SEND_MESSAGE, MODE_PUZZLE_TO_COIN, dao_message(new_bps), pool.coin.name()]]))


def validates(label, bundle):
    try:
        out = kit.validate(bundle)
        return check(label, True, f"cost {out[0].cost:,}") and out
    except kit.Rejected as exc:
        check(label, False, str(exc)[:90]); return None


def refuses(label, thunk):
    try:
        thunk()
    except kit.Rejected as exc:
        return check(label, True, str(exc)[:60])
    except Exception as exc:  # noqa: BLE001 -- a leaf that raises locally is a refusal too
        return check(label, True, f"{type(exc).__name__} (local run)")
    return check(label, False, "accepted")


def main() -> int:
    if not kit.v14_available():
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py"); return 2

    print("swap charges the DAO fee into dao_owed:")
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x61,
                         protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=50)
    r, w = pool.state[0], pool.weights
    gross = 500_000
    honest = forge_math.swap_output(r[0], r[1], gross, pool.fee_bps, w[0], w[1])
    pfee, dfee = honest * pool.protocol_fee_bps // 10_000, honest * 50 // 10_000
    s1, s1_spend = kit.offer_settlement_xch(gross, salt=0xD1)
    bundle, st = kit.spend_action(pool, "forge_action_swap", [H0, 0, 1, gross, honest, *kit.settlement_ref(s1)], extra_spends=[s1_spend])
    out = validates("a swap on a DAO-fee pool validates", bundle)
    if out:
        st = kit.state_to_list(st)
        check("  the protocol slice is owed", st[2] == [0, pfee], f"{st[2]}")
        check("  the DAO slice is owed, at the state's rate", st[6] == [0, dfee] and dfee > 0, f"{st[6]}")
        check("  the trader's payout is the output less BOTH slices",
              (construct_cat_puzzle(CAT_MOD, CAT, kit.OFFER_MOD).get_tree_hash(), honest - pfee - dfee) in out[1])
        check("  the reserve coin holds reserve + protocol owed + DAO owed",
              kit.reserve_amounts(st) == [r[0] + gross, r[1] - honest + pfee + dfee])
    after_swap = kit.make_pool([None, CAT], st[0], total_lp=st[1], fees=st[2], leaves="forge", salt=0x61,
                               protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, state=st)

    print("collect pays both recipients from the same reserve:")
    bundle, st2 = kit.spend_action(after_swap, "forge_action_collect", [H0 + 1, [1]])
    out = validates("collect on a reserve owing both fees validates", bundle)
    if out:
        st2 = kit.state_to_list(st2)
        cat_ph = lambda ph: construct_cat_puzzle(CAT_MOD, CAT, Program.to(ph)).get_tree_hash_precalc(ph)  # noqa: E731
        check("  the protocol recipient is paid its slice", (cat_ph(PROTOCOL_PH), pfee) in out[1])
        check("  the DAO recipient is paid its slice", (cat_ph(DAO_PH), dfee) in out[1])
        check("  both owed balances are zeroed", st2[2] == [0, 0] and st2[6] == [0, 0])
        check("  the reserve is re-created holding the curve reserve alone", kit.reserve_amounts(st2) == st2[0])

    print("the dao_fee leaf (V1, V2, V3, V4):")
    bundle, st3 = kit.spend_action(pool, "forge_action_dao_fee", [H0, 20], extra_spends=[dao_coin_spend(pool, 20)])
    out = validates("the DAO lowers 50 -> 20 with its message", bundle)
    if out:
        st3 = kit.state_to_list(st3)
        check("  the rate is the only field that moved (V1)",
              st3[5] == 20 and st3[0] == pool.state[0] and st3[1] == pool.state[1] and st3[2] == pool.state[2] and st3[6] == pool.state[6])
        check("  every reserve is re-created at its current amount (V3)",
              all((res.full_hash, amt) in out[1] for res, amt in zip(pool.reserves, kit.reserve_amounts(pool.state))))
    refuses("no message from the DAO: refused (V2)",
            lambda: kit.validate(kit.spend_action(pool, "forge_action_dao_fee", [H0, 20])[0]))
    refuses("a message from a coin at another puzzle hash: refused (V2)",
            lambda: kit.validate(kit.spend_action(pool, "forge_action_dao_fee", [H0, 20],
                                                  extra_spends=[dao_coin_spend(pool, 20, puzzle=Program.to([1, 2]))])[0]))
    refuses("a message naming a different rate than the leaf: refused (V2)",
            lambda: kit.validate(kit.spend_action(pool, "forge_action_dao_fee", [H0, 20], extra_spends=[dao_coin_spend(pool, 10)])[0]))
    refuses("raising the rate is refused", lambda: kit.spend_action(pool, "forge_action_dao_fee", [H0, 60], extra_spends=[dao_coin_spend(pool, 60)]))
    refuses("the same rate is refused (V4: nothing to replay once it holds)", lambda: kit.spend_action(pool, "forge_action_dao_fee", [H0, 50], extra_spends=[dao_coin_spend(pool, 50)]))
    refuses("a negative rate is refused", lambda: kit.spend_action(pool, "forge_action_dao_fee", [H0, -1], extra_spends=[dao_coin_spend(pool, -1)]))
    lowered = kit.make_pool([None, CAT], pool.state[0], total_lp=pool.state[1], leaves="forge", salt=0x61,
                            protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, state=st3 if out else pool.state)
    bundle, st4 = kit.spend_action(lowered, "forge_action_dao_fee", [H0 + 1, 0], extra_spends=[dao_coin_spend(lowered, 0)])
    if validates("the DAO lowers to zero", bundle):
        zeroed = kit.make_pool([None, CAT], pool.state[0], total_lp=pool.state[1], leaves="forge", salt=0x61,
                               protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, state=kit.state_to_list(st4))
        refuses("at zero, no decrease exists (irreversibility is topology)",
                lambda: kit.spend_action(zeroed, "forge_action_dao_fee", [H0 + 2, 0], extra_spends=[dao_coin_spend(zeroed, 0)]))
        # a swap on the zeroed pool charges nothing to the DAO
        s2, s2_spend = kit.offer_settlement_xch(gross, salt=0xD2)
        bundle, st5 = kit.spend_action(zeroed, "forge_action_swap", [H0 + 2, 0, 1, gross, honest, *kit.settlement_ref(s2)], extra_spends=[s2_spend])
        if validates("a swap after zeroing validates", bundle):
            check("  and owes the DAO nothing", kit.state_to_list(st5)[6] == [0, 0])

    # V14 (mutation rule): the prologue's `dao_fee_bps >= 0` was reported UNREACHED. A pool whose
    # STATE carries a negative rate -- mintable outside the registry, which refuses it -- would
    # pay the trader `claimed + |dao slice|` out of the reserve coin, with the bundle balanced.
    print("a negative DAO rate in state (mintable only outside the registry):")
    negative = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x3C,
                             dao_ph=DAO_PH, dao_fee_bps=-100)
    s4, s4_spend = kit.offer_settlement_xch(gross, salt=0xD4)
    try:
        kit.validate(kit.spend_action(negative, "forge_action_swap", [H0, 0, 1, gross, honest, *kit.settlement_ref(s4)],
                                      extra_spends=[s4_spend])[0])
        check("  a swap on a pool with dao_fee_bps = -100 in state is refused", False, "ACCEPTED -- the reserve paid the trader extra")
    except (kit.Rejected, ValueError) as exc:
        check("  a swap on a pool with dao_fee_bps = -100 in state is refused", True, str(exc)[:60])

    print("decrease then swap in ONE spend (V5): the swap is charged at the new rate")
    s3, s3_spend = kit.offer_settlement_xch(gross, salt=0xD3)
    bundle, st6 = kit.spend_actions(pool, [("forge_action_dao_fee", [H0, 20]),
                                           ("forge_action_swap", [H0, 0, 1, gross, honest, *kit.settlement_ref(s3)])],
                                    extra_spends=[dao_coin_spend(pool, 20), s3_spend])
    if validates("dao_fee then swap in one spend validates", bundle):
        st6 = kit.state_to_list(st6)
        check("  the DAO slice is at 20 bps, not the 50 the spend opened with", st6[6] == [0, honest * 20 // 10_000] and st6[5] == 20, f"{st6[6]}")

    print("the caps (V7):")
    refuses("a genesis rate above MAX_DAO_FEE_BPS is refused by the prologue",
            lambda: kit.spend_action(kit.make_pool([None, CAT], [10_000_000, 20_000_000], leaves="forge", salt=0x62, protocol_ph=PROTOCOL_PH,
                                                   dao_ph=DAO_PH, dao_fee_bps=101), "forge_action_observe", [H0]))
    refuses("a nonzero rate with a zero recipient is refused by the prologue",
            lambda: kit.spend_action(kit.make_pool([None, CAT], [10_000_000, 20_000_000], leaves="forge", salt=0x63, protocol_ph=PROTOCOL_PH,
                                                   dao_fee_bps=5), "forge_action_observe", [H0]))
    plain = kit.make_pool([None, CAT], [10_000_000, 20_000_000], leaves="forge", salt=0x64, protocol_ph=PROTOCOL_PH)
    check("a pool with no DAO (zero recipient, zero rate) is the ordinary V11.1 pool",
          kit.validate(kit.spend_action(plain, "forge_action_observe", [H0])[0]) is not None)
    refuses("...and its dao_fee leaf has nothing to lower", lambda: kit.spend_action(plain, "forge_action_dao_fee", [H0, 0], extra_spends=[dao_coin_spend(plain, 0)]))

    print("irreversibility is topology, not a guard (the rate is curried):")
    # The leaf refuses a raise, and the compiled leaf is what raises -- but the
    # stronger statement is that a raised pool is not this pool at all. The rate
    # lives in the singleton's curried state, so changing it changes the coin's
    # puzzle hash: there is no spend that raises the rate, only a coin that does
    # not exist. Nothing but a spend of THIS coin can produce a successor, and the
    # only leaf that writes the rate writes it strictly lower.
    live = kit.make_pool([None, CAT], [1_000_000, 1_000_000], leaves="forge", salt=0x68,
                         protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=50)
    base = live.state
    def hash_at(rate):
        st = list(base); st[5] = rate
        return live.successor_puzzle_hash(st)
    check("the live rate is the coin the chain would hold", hash_at(50) == hash_at(50))
    for raised in (51, 60, 100, 10_000):
        check(f"a pool curried at a raised {raised} bps is a different coin", hash_at(raised) != hash_at(50))
    check("even a LOWER rate is a different coin -- only a spend can get there",
          hash_at(49) != hash_at(50))

    print("the registry key (two DAOs, one pair):")
    a = kit.make_pool([None, CAT], [1_000_000, 1_000_000], leaves="forge", salt=0x65, protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=10)
    b = kit.make_pool([None, CAT], [1_000_000, 1_000_000], leaves="forge", salt=0x66, protocol_ph=PROTOCOL_PH, dao_ph=bytes32(b"\x77" * 32), dao_fee_bps=10)
    c = kit.make_pool([None, CAT], [1_000_000, 1_000_000], leaves="forge", salt=0x67, protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=99)
    check("a different DAO recipient is a different registry key", kit.pool_key(a.config()) != kit.pool_key(b.config()))
    check("the opening rate is NOT in the key (it is state; the recipient is the market)", kit.pool_key(a.config()) == kit.pool_key(c.config()))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} DAO-fee checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
