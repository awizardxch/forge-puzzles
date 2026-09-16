#!/usr/bin/env python3
"""The fifth review's documentation corrections, measured rather than asserted (spec 4.3).

  O-1  A future-dated `h` does not force `h == birth`. `birth` is the coin's creation
       height (consensus), `h` is the height the spend claims; the prologue requires
       birth > last_height and h >= birth, and credits the oracle exactly over
       (last_height, birth] at the previous spot and [birth, h] at the current one.
       Measured: a spend at h = birth + 5 is accepted and the accumulator is that sum.

  exact  The architecture said the mint and the payouts are bounded by "<=". The
       leaves enforce EXACT brackets: one LP less than the mirror's figure is refused
       as surely as one more, and a payout one mojo under the share as surely as one
       over. Measured both ways on both leaves.

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
H0 = 6_999_980


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def verdict(thunk) -> str:
    try:
        thunk()
        return "ACCEPTED"
    except kit.Rejected as exc:
        return str(exc).split(": ")[-1].split(" ")[0]
    except Exception as exc:
        return f"local:{type(exc).__name__}"


def main() -> int:
    if not kit.v14_available():
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py")
        return 2

    print("O-1  the oracle under a future-dated h:")
    # A pool whose last spend claimed height 100 and whose successor was born at 104.
    base = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x60, last_height=100)
    state = list(base.state)
    spot0 = kit.spots(state[0], base.weights)
    state[3] = [100, [0], spot0]
    pool = kit.replace(kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x60, state=state), birth=104)
    h = 109
    bundle, new_state = kit.spend_action(pool, "forge_action_observe", [h])
    v = verdict(lambda: kit.validate(bundle))
    check(f"a spend claiming h = birth + 5 is {v}: h is not forced to equal birth", v == "ACCEPTED")
    st = kit.state_to_list(new_state)
    expected = [0 + spot0[0] * (104 - 100) + spot0[0] * (h - 104)]
    check(f"  the accumulator is credited over (last_height, birth] and [birth, h]: {st[3][1]} == {expected}",
          st[3][1] == expected and st[3][0] == h)
    check("  which is exactly kit.expected_oracle's figure", st[3] == kit.expected_oracle(pool.state, pool.weights, h, birth=104))
    v = verdict(lambda: kit.spend_action(pool, "forge_action_observe", [103]))
    check(f"  and h below birth is {v}", v != "ACCEPTED")
    v = verdict(lambda: kit.spend_action(kit.replace(pool, birth=100), "forge_action_observe", [h]))
    check(f"  and a birth not above last_height is {v}", v != "ACCEPTED")

    print("exact  the mint bracket, one less and one more:")
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x61)
    deposits = [1_000_000, 2_000_000]
    honest = forge_math.invariant_lp_mint(pool.state[0], deposits, pool.state[1], pool.fee_bps, pool.weights, version=10)
    sx, sx_spend = kit.offer_settlement_xch(deposits[0], salt=0xA1)
    sa, sa_cat = kit.offer_settlement_cat(CAT, deposits[1], salt=0xA2)

    def add(lp_delta, salt):
        probe, _, _, _ = kit.run_leaf(pool, "forge_action_add", [H0, deposits, lp_delta, bytes32(b"\x01" * 32), *kit.settlement_refs([sx, sa])])
        lp_parent, lp_spends = kit.lp_mint_spends(pool, lp_delta, pool.state[1] + lp_delta, probe.get_tree_hash(), RECIPIENT, salt=salt)
        return kit.spend_action(pool, "forge_action_add", [H0, deposits, lp_delta, lp_parent, *kit.settlement_refs([sx, sa])],
                                extra_spends=[sx_spend, *lp_spends], extra_cats={CAT: [sa_cat]})[0]

    for delta, label, expect in ((honest, "the mirror's figure", True), (honest + 1, "one more", False), (honest - 1, "one less", False)):
        v = verdict(lambda: kit.validate(add(delta, 0xA3 + (delta - honest) % 3)))
        check(f"minting {label} ({delta:,}) is {v}", (v == "ACCEPTED") == expect)

    print("exact  the payout bracket, one under and one over:")
    three = kit.make_pool([None, CAT, bytes32(b"\xd1" * 32)], [3_000_000, 4_000_000, 5_000_000], total_lp=3_000_000, leaves="forge", salt=0x62)
    burn = 300_000
    share = forge_math.withdrawal_amounts(three.state[0], burn, three.state[1], 0)

    def remove(payouts, salt):
        probe, _, _, _ = kit.run_leaf(three, "forge_action_remove", [H0, burn, bytes32(b"\x01" * 32), payouts])
        lp_parent, lp_spends = kit.lp_melt_spend(three, burn, three.state[1] - burn, probe.get_tree_hash(), salt=salt)
        return kit.spend_action(three, "forge_action_remove", [H0, burn, lp_parent, payouts], extra_spends=lp_spends)[0]

    for payouts, label, expect in ((share, "the proportional share", True),
                                   ([share[0] + 1, *share[1:]], "one mojo over on asset 0", False),
                                   ([share[0] - 1, *share[1:]], "one mojo under on asset 0", False)):
        v = verdict(lambda: kit.validate(remove(payouts, 0x90 + len(label) % 7)))
        check(f"paying {label} {payouts} is {v}", (v == "ACCEPTED") == expect)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} review-correction checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
