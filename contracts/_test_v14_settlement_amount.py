#!/usr/bin/env python3
"""The derived-id settlement binding (spec 1.8), against the real V14 leaves.

A leaf is given its settlement's PARENT and AMOUNT, derives the id, asserts the coin was
spent (ASSERT_CONCURRENT_SPEND, code 132) and that the amount covers the action's input.

Every attack below keeps the bundle VALUE-BALANCED with an extra plain coin, so that
conservation (MINTING_COIN, 20) cannot be the thing that refuses it: the refusal has to
come from the leaf's own binding. That is the point of the second half -- V13 relied on
conservation alone (spec 1.8, _test_v14_before_after.py).

  * the honest settlement passes -- including one holding MORE than the input, which is
    every router-fee swap on the public lane (the fee is carved out of the same coin)
  * a settlement too small for the amount named is refused: the derived id names a coin
    that does not exist
  * the right amount at the wrong puzzle (an XCH coin for a CAT leg) is refused
  * two routes that look like bindings and are not, pinned so they are not retried:
    the announcement NONCE binds nothing (the solver chooses it), and OFFER_MOD makes no
    COIN announcement (an assertion on one can never be satisfied)

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import hashlib
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.coin_spend import make_spend
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import _v14_testkit as kit
import forge_math

results = []
CAT = bytes32(b"\xd0" * 32)
SURPLUS = bytes32(b"\x66" * 32)
H0 = 6_999_990
ASSERT_COIN_ANN = 61
CONCURRENT_SPEND_FAILED, MINTING_COIN, ANNOUNCE_CONSUMED_FAILED = "132", "20", "12"


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def code_of(exc) -> str:
    return str(exc).split(": ")[-1].split(" ")[0]


def refuses(label, thunk, expect=None):
    try:
        thunk()
    except kit.Rejected as exc:
        ok = expect is None or code_of(exc) == str(expect)
        return check(label, ok, f"code {code_of(exc)}" + ("" if ok else f"  expected {expect}"))
    except Exception as exc:
        # QA-2: only a CLVM failure counts as the leaf refusing locally.
        reason = kit.refusal_reason(exc)
        ok = reason is not None and (expect is None or expect == "local")
        return check(label, ok, "ValueError (local run)" if reason else f"probe broke: {type(exc).__name__}: {str(exc)[:60]}")
    return check(label, False, "ACCEPTED")


def accepts(label, thunk):
    try:
        out = thunk()
    except kit.Rejected as exc:
        check(label, False, str(exc)[:90])
        return None
    check(label, True)
    return out


def main() -> int:
    if not kit.v14_available():
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py")
        return 2
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x40)
    r, w = pool.state[0], pool.weights
    gross = 250_000
    honest = forge_math.swap_output(r[0], r[1], gross, pool.fee_bps, w[0], w[1])

    def swap(parent, amount, extra_spends=(), extra_cats=None, asset_in=0, asset_out=1, g=gross, out=honest):
        return kit.spend_action(pool, "forge_action_swap", [H0, asset_in, asset_out, g, out, parent, amount],
                                extra_spends=list(extra_spends), extra_cats=extra_cats or {})[0]

    def balance(mojos, salt):
        """A plain coin releasing `mojos` into the bundle, so conservation holds no matter
        what the settlement holds. Whatever refuses after this is the puzzle."""
        return kit.xch_settlement(mojos, salt=salt)

    print("honest shapes:")
    s, s_spend = kit.offer_settlement_xch(gross, salt=0xC0)
    accepts("a settlement holding exactly gross_input is accepted", lambda: kit.validate(swap(*kit.settlement_ref(s), [s_spend])))
    fee = 7_500
    parent = bytes32(b"\xc5" * 32)
    big = Coin(parent, bytes32(OFFER_MOD_HASH), uint64(gross + fee))
    big_spend = make_spend(big, OFFER_MOD, kit.Program.to([[big.name()], (big.name(), [[SURPLUS, fee, [SURPLUS]]])]))
    out = accepts("a settlement holding gross + router fee, paying the fee itself, is accepted",
                  lambda: kit.validate(swap(parent, gross + fee, [big_spend])))
    if out:
        check("  the router received its fee from the same coin", (SURPLUS, fee) in out[1])
    refuses("  ...but naming gross_input as the amount would refuse that same honest swap: no coin has that id",
            lambda: kit.validate(swap(parent, gross, [big_spend])), CONCURRENT_SPEND_FAILED)

    print("attacks, with the bundle value-balanced so only the puzzle can refuse:")
    tiny = Coin(parent, bytes32(OFFER_MOD_HASH), uint64(1))
    tiny_spend = make_spend(tiny, OFFER_MOD, kit.Program.to([[tiny.name()]]))
    refuses("a one-mojo settlement named with a 250,000 amount: the derived id is spent by nobody",
            lambda: kit.validate(swap(parent, gross, [tiny_spend, balance(gross - 1, 0x71)])), CONCURRENT_SPEND_FAILED)
    refuses("a settlement holding LESS than gross_input, named honestly, is refused by the leaf itself",
            lambda: kit.validate(swap(*kit.settlement_ref(tiny), [tiny_spend, balance(gross - 1, 0x72)])), "local")
    refuses("a settlement with a different parent than named",
            lambda: kit.validate(swap(bytes32(b"\xc6" * 32), gross, [s_spend])), CONCURRENT_SPEND_FAILED)
    # The CAT leg fed with an XCH coin of the right amount. A real CAT settlement is ALSO in
    # the ring so the ring balances and conservation holds; the solution names the XCH coin.
    # The derived id uses the CAT settlement puzzle, so nothing in the bundle has it.
    r_out = forge_math.swap_output(r[1], r[0], 400_000, pool.fee_bps, w[1], w[0])
    x, x_spend = kit.offer_settlement_xch(400_000, salt=0xC7)
    c, c_cat = kit.offer_settlement_cat(CAT, 400_000, salt=0xC8)
    refuses("the right amount at the wrong puzzle (an XCH coin named for a CAT leg) is refused",
            lambda: kit.validate(swap(*kit.settlement_ref(x), [x_spend], {CAT: [c_cat]}, asset_in=1, asset_out=0, g=400_000, out=r_out)),
            CONCURRENT_SPEND_FAILED)
    accepts("  and the same leg naming the CAT settlement is accepted",
            lambda: kit.validate(swap(*kit.settlement_ref(c), [], {CAT: [c_cat]}, asset_in=1, asset_out=0, g=400_000, out=r_out)))

    print("the two routes that do not bind, pinned:")
    # 1. The nonce. A settlement can be solved with ANY nonce; here the tiny coin announces
    #    the nonce that IS the honest coin's derived id, so the V10-style puzzle announcement
    #    is satisfied -- and only the concurrent-spend assert catches that the coin named was
    #    never spent. The nonce binds nothing.
    claimed_id = kit.coin_id(parent, bytes32(OFFER_MOD_HASH), gross)
    liar_spend = make_spend(tiny, OFFER_MOD, kit.Program.to([[claimed_id]]))
    puzzle_ann = hashlib.sha256(bytes(OFFER_MOD_HASH) + kit.Program.to((claimed_id, None)).get_tree_hash()).digest()
    guard = kit.Program.to((1, [[63, puzzle_ann]]))
    gc = Coin(bytes32(b"\x5a" * 32), bytes32(guard.get_tree_hash()), uint64(1))
    accepts("a 1-mojo settlement can make the puzzle announcement for a 250,000 coin's id (the nonce is solver-chosen)",
            lambda: kit.validate(SpendBundle([liar_spend, make_spend(gc, guard, kit.Program.to([]))], G2Element())))
    refuses("  so the leaf's puzzle announcement alone would pass; the derived-id spend assert is what refuses it",
            lambda: kit.validate(swap(parent, gross, [liar_spend, balance(gross - 1, 0x73)])), CONCURRENT_SPEND_FAILED)
    # 2. OFFER_MOD makes no coin announcement, so an ASSERT_COIN_ANNOUNCEMENT keyed on the
    #    settlement can never be satisfied. Not a route to a binding.
    coin_ann = hashlib.sha256(bytes(s.name()) + kit.Program.to((s.name(), None)).get_tree_hash()).digest()
    guard2 = kit.Program.to((1, [[ASSERT_COIN_ANN, coin_ann]]))
    gc2 = Coin(bytes32(b"\x5b" * 32), bytes32(guard2.get_tree_hash()), uint64(1))
    refuses("OFFER_MOD makes no COIN announcement: asserting one from a settlement is never satisfied",
            lambda: kit.validate(SpendBundle([s_spend, make_spend(gc2, guard2, kit.Program.to([]))], G2Element())),
            ANNOUNCE_CONSUMED_FAILED)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} settlement-amount checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
