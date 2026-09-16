#!/usr/bin/env python3
"""Every action is bound to the assets it moves, not just the first one (spec 9).

The user's concern, verbatim: "someone could create an action using a minted coin then
try to exploit the action layer." Each probe below is a second action, or a substituted
coin, trying to ride on the first's binding:

  * a settlement of the wrong asset for the leg                      refused
  * a settlement too small to fund the input                          refused by the leaf
  * two swaps in one spend asserting the SAME settlement              refused ONLY when
    the bundle has nothing spare -- see the note below; measured on chain 2026-09-16
  * an add whose settlement holds less than the deposit               refused by the leaf
  * a later action in the same spend burning past the floor           refused
  * a melt coin minted from ordinary mojos standing in for real LP    refused by the TAIL

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs.sized_bytes import bytes32

import _v14_testkit as kit
import forge_math

results = []
CAT = bytes32(b"\xd0" * 32)
RECIPIENT = bytes32(b"\x55" * 32)
H0 = 6_999_990


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def refuses(label, thunk, expect=None):
    try:
        thunk()
    except kit.Rejected as exc:
        code = str(exc).split(": ")[-1]
        ok = expect is None or expect in str(exc)
        return check(label, ok, f"{str(exc)[:52]}" + ("" if ok else f"  expected {expect}"))
    except Exception as exc:
        return check(label, expect is None or expect == "local", f"{type(exc).__name__} (local run)")
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
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x41)
    r, w = pool.state[0], pool.weights
    g1 = 250_000
    out1 = forge_math.swap_output(r[0], r[1], g1, pool.fee_bps, w[0], w[1])
    s1, s1_spend = kit.offer_settlement_xch(g1, salt=0xD1)

    print("one action, bound:")
    accepts("the honest swap", lambda: kit.validate(kit.spend_action(
        pool, "forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)], extra_spends=[s1_spend])[0]))
    x, x_spend = kit.offer_settlement_xch(400_000, salt=0xD2)
    c, c_cat = kit.offer_settlement_cat(CAT, 400_000, salt=0xD2)
    r_out = forge_math.swap_output(r[1], r[0], 400_000, pool.fee_bps, w[1], w[0])
    # a real CAT settlement rides in the ring so it balances; the SOLUTION names the XCH coin
    refuses("a settlement of the WRONG asset named for the leg (an XCH coin for the CAT side), the ring balanced by a real one",
            lambda: kit.validate(kit.spend_action(
                pool, "forge_action_swap", [H0, 1, 0, 400_000, r_out, *kit.settlement_ref(x)],
                extra_spends=[x_spend], extra_cats={CAT: [c_cat]})[0]),
            "132")
    small, small_spend = kit.offer_settlement_xch(g1 - 1, salt=0xD3)
    refuses("a settlement one mojo too small for the input: the leaf refuses before consensus sees it",
            lambda: kit.validate(kit.spend_action(
                pool, "forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(small)], extra_spends=[small_spend])[0]),
            "local")

    print("two actions in one spend, the second riding on the first's coin:")
    r_after = [r[0] + g1, r[1] - out1]
    g2 = 100_000
    out2 = forge_math.swap_output(r_after[0], r_after[1], g2, pool.fee_bps, w[0], w[1])
    # The binding does NOT make a settlement single-use: an assertion is not consumed, so both
    # actions' ASSERT_CONCURRENT_SPENDs are satisfied by the one coin. What refuses the pair is
    # conservation -- and conservation is a property of the WHOLE BUNDLE, not of an action.
    # With nothing spare in the bundle it bites:
    refuses("two swaps asserting the SAME settlement, nothing spare in the bundle: refused",
            lambda: kit.validate(kit.spend_actions(pool, [
                ("forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)]),
                ("forge_action_swap", [H0, 0, 1, g2, out2, *kit.settlement_ref(s1)])], extra_spends=[s1_spend])[0]),
            "20")
    # ...and with slack it does not. Measured on testnet11 on 2026-09-16: on pool H6 at height
    # 4,693,721 exactly this pair confirmed, because the bundle carried a 5 XCH network fee and
    # the second swap was funded out of it (the fee actually paid came to 4,995,000,000). The
    # pool received full value for both swaps, so nothing was stolen -- but "refused by
    # conservation" is true only of a bundle with no slack, and a network fee is slack.
    # `scripts/v14-slack-probe.py` pushes both halves at a live pool.
    slack = kit.xch_settlement(g2, salt=0x7A)        # stands in for the fee the bundle would have paid
    accepts("  ...but with slack in the bundle the SAME pair is accepted: the slack funds the second swap",
            lambda: kit.validate(kit.spend_actions(pool, [
                ("forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)]),
                ("forge_action_swap", [H0, 0, 1, g2, out2, *kit.settlement_ref(s1)])],
                extra_spends=[s1_spend, slack])[0]))
    check("  so the guarantee is 'the value arrived', not 'this coin paid for this action'", True,
          "the puzzle binds which coin and its amount; the bundle decides whether value was supplied")
    s2, s2_spend = kit.offer_settlement_xch(g2, salt=0xD4)
    accepts("  ...and with its own settlement the second swap is accepted",
            lambda: kit.validate(kit.spend_actions(pool, [
                ("forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)]),
                ("forge_action_swap", [H0, 0, 1, g2, out2, *kit.settlement_ref(s2)])], extra_spends=[s1_spend, s2_spend])[0]))

    # A swap followed by a remove that burns past the floor, in one spend.
    def remove_steps(burn, state, eph_pool):
        vf = forge_math.vault_fee_bps(len(state[0]), 10, pool.fee_bps)
        payouts = forge_math.withdrawal_amounts(state[0], burn, state[1], vf)
        return burn, payouts

    st1, _, _, eph1 = kit.run_leaf(pool, "forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)])
    st1_list = kit.state_to_list(st1)
    # the whole supply: one past what LOCKED_BURN = 1 allows. The mirror refuses to size that
    # burn at all, so the payouts come from the deepest legal burn; the leaf's `burn <=
    # total_lp - LOCKED_BURN` fires before it ever looks at them.
    _, payouts = remove_steps(st1_list[1] - kit.LOCKED_BURN, st1_list, pool)
    burn = st1_list[1]
    refuses("a swap then a remove burning the whole supply (past LOCKED_BURN) in one spend is refused by the leaf",
            lambda: kit.run_leaf(pool, "forge_action_remove", [H0, burn, bytes32(b"\x01" * 32), payouts],
                                 ephemeral=eph1, state=st1_list), "local")
    burn, payouts = remove_steps(st1_list[1] - kit.LOCKED_BURN, st1_list, pool)
    st2, _, _, _ = kit.run_leaf(pool, "forge_action_remove", [H0, burn, bytes32(b"\x01" * 32), payouts],
                                ephemeral=eph1, state=st1_list)
    lp_parent, lp_spends = kit.lp_melt_spend(pool, burn, st1_list[1] - burn, st2.get_tree_hash(), salt=0xD5)
    accepts("  ...and burning exactly down to the floor after the swap is accepted",
            lambda: kit.validate(kit.spend_actions(pool, [
                ("forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)]),
                ("forge_action_remove", [H0, burn, lp_parent, payouts])], extra_spends=[s1_spend, *lp_spends])[0]))

    print("a minted coin standing in for a real one:")
    deposits = [1_000_000, 2_000_000]
    minted = forge_math.invariant_lp_mint(r, deposits, pool.state[1], pool.fee_bps, w, version=10)
    sx, sx_spend = kit.offer_settlement_xch(deposits[0], salt=0xD6)
    sa, sa_cat = kit.offer_settlement_cat(CAT, deposits[1], salt=0xD7)
    short_a, short_a_cat = kit.offer_settlement_cat(CAT, deposits[1] - 1, salt=0xD8)
    refuses("an add whose CAT settlement holds one less than the deposit is refused by the leaf",
            lambda: kit.run_leaf(pool, "forge_action_add", [H0, deposits, minted, bytes32(b"\x01" * 32), *kit.settlement_refs([sx, short_a])]),
            "local")
    probe, _, _, _ = kit.run_leaf(pool, "forge_action_add", [H0, deposits, minted, bytes32(b"\x01" * 32), *kit.settlement_refs([sx, sa])])
    lp_parent, lp_spends = kit.lp_mint_spends(pool, minted, pool.state[1] + minted, probe.get_tree_hash(), RECIPIENT, salt=0xD9)
    accepts("the honest add mints through the real eve",
            lambda: kit.validate(kit.spend_action(pool, "forge_action_add", [H0, deposits, minted, lp_parent, *kit.settlement_refs([sx, sa])],
                                                  extra_spends=[sx_spend, *lp_spends], extra_cats={CAT: [sa_cat]})[0]))
    burn = 300_000
    vf = forge_math.vault_fee_bps(2, 10, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(r, burn, pool.state[1], vf)
    probe, _, _, _ = kit.run_leaf(pool, "forge_action_remove", [H0, burn, bytes32(b"\x01" * 32), payouts])
    fake_parent, fake_spends = kit.lp_melt_spend(pool, burn, pool.state[1] - burn, probe.get_tree_hash(), salt=0xDA, fabricated=True)
    refuses("a melt coin MINTED from ordinary mojos (no CAT parent) cannot redeem: the TAIL refuses it",
            lambda: kit.validate(kit.spend_action(pool, "forge_action_remove", [H0, burn, fake_parent, payouts], extra_spends=fake_spends)[0]))
    real_parent, real_spends = kit.lp_melt_spend(pool, burn, pool.state[1] - burn, probe.get_tree_hash(), salt=0xDB)
    accepts("  ...and a melt coin with a real CAT parent redeems",
            lambda: kit.validate(kit.spend_action(pool, "forge_action_remove", [H0, burn, real_parent, payouts], extra_spends=real_spends)[0]))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} action-binding checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
