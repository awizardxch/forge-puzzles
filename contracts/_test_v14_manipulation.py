#!/usr/bin/env python3
"""Intra-bundle manipulation -- the flash-loan analog -- on V11 (phase 5, first adversarial lane).

Chia has no loans, but one bundle can chain actions on one pool: swap to move the
price, add or remove at the moved ratio, swap back to restore it. The question
the audit asks is whether any such sequence leaves the actor holding more than
they started with, valued at the prices before they touched the pool. Two pins:

  * at the puzzle: a swap/add/swap and a swap/remove/swap, each as ONE pool spend
    with three actions, validated by the mempool's own validator, and the actor's
    end position priced at the pre-sequence spot -- never above the start;
  * at the mirrors: hundreds of random sequences of swaps, adds and removes (the
    puzzle refuses any figure the mirrors do not produce, `_test_v11_actions.py`),
    the actor's net at pre-sequence prices never positive, and the pool's value
    per LP never falling across any action.

Exits 0 when every check passes, 1 otherwise, 2 when the V11 build is absent.
"""
from __future__ import annotations

import random
import sys
from fractions import Fraction

sys.path.insert(0, ".")

import forge_math  # noqa: E402
import _v14_testkit as kit  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

results: list[bool] = []
H0 = 6_999_990
CAT = bytes32(b"\xd7" * 32)
RECIPIENT = bytes32(b"\x55" * 32)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


# ── the mirrors, threaded like the puzzle threads state ─────────────────────────────────────────

class Mirror:
    """A pool's state as the leaves see it, moved by the same mirrors the puzzle pins."""

    def __init__(self, reserves, total_lp, weights, fee_bps, protocol_fee_bps):
        self.r = list(reserves); self.lp = total_lp; self.w = list(weights)
        self.fee_bps = fee_bps; self.pfee_bps = protocol_fee_bps

    def swap(self, i, j, gross):
        honest = forge_math.swap_output(self.r[i], self.r[j], gross, self.fee_bps, self.w[i], self.w[j])
        pfee = honest * self.pfee_bps // 10_000
        self.r[i] += gross; self.r[j] -= honest
        return honest, honest - pfee

    def add(self, deposits):
        minted = forge_math.invariant_lp_mint(self.r, deposits, self.lp, self.fee_bps, self.w, version=10)
        self.r = [a + d for a, d in zip(self.r, deposits)]; self.lp += minted
        return minted

    def remove(self, burn):
        vf = forge_math.vault_fee_bps(len(self.r), 10, self.fee_bps)
        payouts = forge_math.withdrawal_amounts(self.r, burn, self.lp, vf)
        self.r = [a - p for a, p in zip(self.r, payouts)]; self.lp -= burn
        return payouts

    def spot(self, i):
        """Base units per unit of asset i (asset 0 is the base)."""
        return Fraction(self.r[0] * self.w[i], self.r[i] * self.w[0])

    def value_per_lp_key(self):
        """K * L0^sum(w) comparisons: value per LP = K^(1/sum w) / L, exact in integers."""
        k = 1
        for a, w in zip(self.r, self.w):
            k *= a ** w
        return k, self.lp, sum(self.w)


def value_at(pre: Mirror, holdings: dict, lp: int, post: Mirror) -> Fraction:
    """The actor's position in base units at the PRE-sequence spot: assets directly, LP as its
    share of the post-sequence reserves priced at the pre-sequence spot."""
    total = Fraction(0)
    for i, amount in holdings.items():
        total += amount * (1 if i == 0 else pre.spot(i))
    if lp and post.lp:
        for i, reserve in enumerate(post.r):
            total += Fraction(reserve * lp, post.lp) * (1 if i == 0 else pre.spot(i))
    return total


def main() -> int:
    if not kit.v14_available():
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py"); return 2

    # ── at the puzzle ───────────────────────────────────────────────────────────────────────────
    print("swap / add / swap in one pool spend:")
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x71)
    m0 = Mirror(pool.state[0], pool.state[1], pool.weights, pool.fee_bps, pool.protocol_fee_bps)
    pre = Mirror(pool.state[0], pool.state[1], pool.weights, pool.fee_bps, pool.protocol_fee_bps)
    g1 = 2_000_000                                   # 20% of the XCH reserve: a real price move
    gross1, net1 = m0.swap(0, 1, g1)
    x_dep, a_dep = 1_000_000, net1 // 2               # add at the moved ratio (roughly)
    minted = m0.add([x_dep, a_dep])
    a_back = net1 - a_dep
    gross3, net3 = m0.swap(1, 0, a_back)             # swap the rest back, restoring the price
    # the actor: started with g1 + x_dep XCH; ends with net3 XCH and `minted` LP
    start = Fraction(g1 + x_dep)
    end = value_at(pre, {0: net3}, minted, m0)
    check("the actor ends with less than they started, at pre-sequence prices",
          end < start, f"start {start} end {float(end):,.0f} (lost {float(start - end):,.0f} mojo-equivalents to fees)")
    k0, l0, sw = pre.value_per_lp_key(); k1, l1, _ = m0.value_per_lp_key()
    check("value per LP did not fall across the sequence", k1 * l0 ** sw >= k0 * l1 ** sw)

    # the same three actions as ONE spend, through the mempool's validator
    s1, s1_spend = kit.offer_settlement_xch(g1, salt=0xE1)
    sx, sx_spend = kit.offer_settlement_xch(x_dep, salt=0xE2)
    sa, sa_cat = kit.offer_settlement_cat(CAT, a_dep, salt=0xE3)
    s3, s3_cat = kit.offer_settlement_cat(CAT, a_back, salt=0xE4)
    # thread the leaves to learn the state root right after the add (the LP eve binds it)
    st1, _, _, eph1 = kit.run_leaf(pool, "forge_action_swap", [H0, 0, 1, g1, gross1, *kit.settlement_ref(s1)])
    st2, _, _, _ = kit.run_leaf(pool, "forge_action_add", [H0, [x_dep, a_dep], minted, bytes32(b"\x01" * 32), *kit.settlement_refs([sx, sa])],
                                ephemeral=eph1, state=kit.state_to_list(st1))
    lp_parent, lp_spends = kit.lp_mint_spends(pool, minted, pool.state[1] + minted, st2.get_tree_hash(), RECIPIENT, salt=0xE5)
    steps = [("forge_action_swap", [H0, 0, 1, g1, gross1, *kit.settlement_ref(s1)]),
             ("forge_action_add", [H0, [x_dep, a_dep], minted, lp_parent, *kit.settlement_refs([sx, sa])]),
             ("forge_action_swap", [H0, 1, 0, a_back, gross3, *kit.settlement_ref(s3)])]
    bundle, new_state = kit.spend_actions(pool, steps, extra_spends=[s1_spend, sx_spend, *lp_spends], extra_cats={CAT: [sa_cat, s3_cat]})
    try:
        conds, additions = kit.validate(bundle)
        check("the three-action spend validates", True, f"cost {conds.cost:,}")
        st = kit.state_to_list(new_state)
        check("  the puzzle's end state is the mirrors' end state", st[0] == m0.r and st[1] == m0.lp, f"{st[0]} lp {st[1]}")
        check("  the actor's XCH payout is exactly the mirrors' net", (kit.SETTLE_PH if hasattr(kit, 'SETTLE_PH') else bytes32(kit.OFFER_MOD_HASH), net3) in additions)
    except kit.Rejected as exc:
        check("the three-action spend validates", False, str(exc)[:120])
    # greedy variants the puzzle must refuse: claiming the pre-move quote on the swap back
    stale = forge_math.swap_output(pre.r[1], pre.r[0], a_back, pool.fee_bps, pool.weights[1], pool.weights[0])
    if stale != gross3:
        try:
            kit.validate(kit.spend_actions(pool, [steps[0], steps[1], ("forge_action_swap", [H0, 1, 0, a_back, stale, *kit.settlement_ref(s3)])],
                                           extra_spends=[s1_spend, sx_spend, *lp_spends], extra_cats={CAT: [sa_cat, s3_cat]})[0])
            check("  the swap back cannot claim the pre-sequence quote", False, "accepted")
        except Exception as exc:  # noqa: BLE001
            check("  the swap back cannot claim the pre-sequence quote", True, type(exc).__name__)
    try:
        kit.validate(kit.spend_actions(pool, [steps[0], ("forge_action_add", [H0, [x_dep, a_dep], minted + 1, lp_parent, *kit.settlement_refs([sx, sa])]), steps[2]],
                                       extra_spends=[s1_spend, sx_spend, *lp_spends], extra_cats={CAT: [sa_cat, s3_cat]})[0])
        check("  the add cannot mint one LP above the moved-state mirror", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("  the add cannot mint one LP above the moved-state mirror", True, type(exc).__name__)

    print("swap / remove / swap in one pool spend:")
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x72)
    m0 = Mirror(pool.state[0], pool.state[1], pool.weights, pool.fee_bps, pool.protocol_fee_bps)
    pre = Mirror(pool.state[0], pool.state[1], pool.weights, pool.fee_bps, pool.protocol_fee_bps)
    burn = 500_000                                   # the actor already holds a tenth of the LP
    g1 = 2_000_000
    gross1, net1 = m0.swap(0, 1, g1)
    payouts = m0.remove(burn)                        # withdraw at the moved ratio: more A, less XCH
    a_back = net1 + payouts[1]
    gross3, net3 = m0.swap(1, 0, a_back)             # sell every A back
    start = Fraction(g1) + value_at(pre, {}, burn, pre)
    end = Fraction(payouts[0] + net3)
    check("the actor ends with less than they started, at pre-sequence prices",
          end < start, f"start {float(start):,.0f} end {float(end):,.0f}")
    k0, l0, sw = pre.value_per_lp_key(); k1, l1, _ = m0.value_per_lp_key()
    check("value per LP did not fall across the sequence", k1 * l0 ** sw >= k0 * l1 ** sw)
    s1, s1_spend = kit.offer_settlement_xch(g1, salt=0xF1)
    s3, s3_cat = kit.offer_settlement_cat(CAT, a_back, salt=0xF3)
    st1, _, _, eph1 = kit.run_leaf(pool, "forge_action_swap", [H0, 0, 1, g1, gross1, *kit.settlement_ref(s1)])
    st2, _, _, _ = kit.run_leaf(pool, "forge_action_remove", [H0, burn, bytes32(b"\x01" * 32), payouts],
                                ephemeral=eph1, state=kit.state_to_list(st1))
    lp_parent, lp_spends = kit.lp_melt_spend(pool, burn, pool.state[1] - burn, st2.get_tree_hash(), salt=0xF5)
    steps = [("forge_action_swap", [H0, 0, 1, g1, gross1, *kit.settlement_ref(s1)]),
             ("forge_action_remove", [H0, burn, lp_parent, payouts]),
             ("forge_action_swap", [H0, 1, 0, a_back, gross3, *kit.settlement_ref(s3)])]
    try:
        bundle, new_state = kit.spend_actions(pool, steps, extra_spends=[s1_spend, *lp_spends], extra_cats={CAT: [s3_cat]})
        conds, additions = kit.validate(bundle)
        check("the three-action spend validates", True, f"cost {conds.cost:,}")
        st = kit.state_to_list(new_state)
        check("  the puzzle's end state is the mirrors' end state", st[0] == m0.r and st[1] == m0.lp, f"{st[0]} lp {st[1]}")
    except Exception as exc:  # noqa: BLE001
        check("the three-action spend validates", False, f"{type(exc).__name__}: {str(exc)[:120]}")
    try:
        bumped = [payouts[0] + 1, payouts[1]]
        kit.validate(kit.spend_actions(pool, [steps[0], ("forge_action_remove", [H0, burn, lp_parent, bumped]), steps[2]],
                                       extra_spends=[s1_spend, *lp_spends], extra_cats={CAT: [s3_cat]})[0])
        check("  the remove cannot pay one mojo above the moved-state share", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("  the remove cannot pay one mojo above the moved-state share", True, type(exc).__name__)

    # ── at the mirrors: random sequences ────────────────────────────────────────────────────────
    print("random sequences on the mirrors (the puzzle refuses any other figure):")
    rng = random.Random(0x5EED)
    worst = Fraction(0); sequences = 0; value_drops = 0; positive = 0
    for trial in range(400):
        n = rng.choice([2, 3, 4])
        weights = [rng.randint(1, 4) for _ in range(n)]
        reserves = [rng.randint(1_000_000, 50_000_000) for _ in range(n)]
        total_lp = rng.randint(500_000, 5_000_000)
        fee_bps = rng.choice([0, 5, 30, 100, 200]); pfee = rng.choice([0, 5, 50, 100])
        m = Mirror(reserves, total_lp, weights, fee_bps, pfee)
        pre = Mirror(reserves, total_lp, weights, fee_bps, pfee)
        holdings = {i: 0 for i in range(n)}; lp_held = total_lp // 10; spent = Fraction(0)
        start = value_at(pre, {}, lp_held, pre)
        ok_value = True
        for _ in range(rng.randint(2, 5)):
            kind = rng.choice(["swap", "swap", "add", "remove"])
            k0, l0, sw = m.value_per_lp_key()
            if kind == "swap":
                i, j = rng.sample(range(n), 2)
                gross = max(1, m.r[i] * rng.randint(1, 30) // 100)
                if holdings[i] >= gross:
                    holdings[i] -= gross
                else:
                    spent += (gross - holdings[i]) * (1 if i == 0 else pre.spot(i)); holdings[i] = 0
                _, net = m.swap(i, j, gross); holdings[j] += net
            elif kind == "add":
                deposits = [max(0, m.r[i] * rng.randint(0, 20) // 100) for i in range(n)]
                if sum(deposits) == 0:
                    continue
                for i, d in enumerate(deposits):
                    if holdings[i] >= d:
                        holdings[i] -= d
                    else:
                        spent += (d - holdings[i]) * (1 if i == 0 else pre.spot(i)); holdings[i] = 0
                lp_held += m.add(deposits)
            else:
                if lp_held <= 1:
                    continue
                burn = max(1, lp_held * rng.randint(10, 90) // 100)
                if burn >= m.lp:
                    continue
                for i, p in enumerate(m.remove(burn)):
                    holdings[i] += p
                lp_held -= burn
            k1, l1, _ = m.value_per_lp_key()
            if k1 * l0 ** sw < k0 * l1 ** sw:
                ok_value = False
        sequences += 1
        end = value_at(pre, holdings, lp_held, m)
        net = end - start - spent
        if net > 0:
            positive += 1
        if not ok_value:
            value_drops += 1
        if net > worst:
            worst = net
    check(f"{sequences} random sequences: the actor is never richer at pre-sequence prices", positive == 0,
          f"positive outcomes {positive}, best net {float(worst):,.3f}")
    check("value per LP never fell across any action of any sequence", value_drops == 0, f"drops {value_drops}")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} manipulation checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
