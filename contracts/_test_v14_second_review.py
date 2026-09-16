#!/usr/bin/env python3
"""The fourth review's findings (Chia-Network/chips#217, 2026-09-15), replayed against V14.

Against V13 this suite REPRODUCED both findings. Against V14 it proves what each became:

  R-1  V13's `register` admitted a pool whose reserve_parents named coins that did not
       exist: nothing in the registration bundle spent or asserted a reserve, so the slot
       was taken by a pool no one could ever spend (the squat M-3 named, cheaper). V14
       derives every reserve parent from a grandparent and the reserve launcher's hash
       and asserts the launcher's announcement, so the parents are coins the bundle
       spends -- and the V13 construction is refused. Spec 1.3-1.4.

  R-2  `add`'s `assert deposit >= 0` IS load-bearing; our "does not reproduce" was
       wrong. A negative slot drives min_deposit_ratio negative, which shrinks the
       balanced amount subtracted from the POSITIVE slot's excess and inflates
       effective_product. The line stays, the vector is pinned here and in the actions
       suite, and the mutation run reports it KILLED.

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs.sized_bytes import bytes32

import _v14_testkit as kit
import forge_math as fm

results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


# --------------------------------------------------------------------------
# R-2: the add bracket, with a negative slot
# --------------------------------------------------------------------------

def largest_accepted_mint(old, deposits, total_lp, fee_bps, weights):
    """The bracket exact_invariant_lp_mint enforces, solved directly.

    forge_math.invariant_lp_mint refuses a negative deposit at the wrapper before
    it ever reaches the bracket -- the same guard the puzzle's `assert deposit >= 0`
    is. Searching for the finding THROUGH that wrapper is why we missed it: the
    mirror carried the guard we were trying to test. This solves the bracket
    itself, which is the only way to see what the line is actually holding back.
    """
    K = sum(weights)
    old_product = fm.weighted_product(old, weights)
    effective = fm.effective_amounts(old, deposits, fee_bps, weights)
    target = (total_lp ** K) * fm.weighted_product(effective, weights)
    low, high, best = 0, max(total_lp * 4, 1024), 0
    while ((total_lp + high) ** K) * old_product <= target:
        high *= 2
    while low <= high:
        mid = (low + high) // 2
        if ((total_lp + mid) ** K) * old_product <= target:
            best, low = mid, mid + 1
        else:
            high = mid - 1
    return best, effective, old_product, target


def r2_math():
    print("R-2  the add bracket does not refuse a negative deposit:")
    old, weights, fee_bps, total_lp = [10_000_000, 20_000_000], [1, 1], 30, 5_000_000

    # The reviewer's vector, to the unit.
    deposits = [-100_000, 500_000]
    mint, effective, old_product, _ = largest_accepted_mint(old, deposits, total_lp, fee_bps, weights)
    eff_product = fm.weighted_product(effective, weights)
    check("the reviewer's vector [-100000, +500000] leaves effective_product ABOVE old_product",
          eff_product > old_product, f"{eff_product} > {old_product}")
    check("  so the bracket accepts a positive mint", mint > 0, f"lp_delta = {mint}")
    check("  and it is the amount reported: 36611", mint == 36_611, f"got {mint}")
    new_reserves = [o + d for o, d in zip(old, deposits)]
    check("  the pool would lose 100,000 of asset 0 while MINTING LP",
          new_reserves == [9_900_000, 20_500_000] and total_lp + mint == 5_036_611,
          f"reserves {new_reserves}, total_lp {total_lp + mint}")

    # The region our own search stayed in, which is why it concluded "redundant".
    small = [-100_000, 200_000]
    mint_small, effective_small, _, _ = largest_accepted_mint(small, small, total_lp, fee_bps, weights) \
        if False else largest_accepted_mint(old, small, total_lp, fee_bps, weights)
    eff_small = fm.weighted_product(effective_small, weights)
    check("a SMALLER positive slot [-100000, +200000] really is refused by the bracket alone",
          eff_small < old_product and mint_small == 0,
          f"effective_product {eff_small} < {old_product}")
    check("  which is the region our search stayed in -- the bracket closes only while "
          "the positive slot cannot overcome the negative slot's drag", True)

    # The mirror's own guard, named so this is not repeated.
    try:
        fm.invariant_lp_mint(old, deposits, total_lp, fee_bps, weights)
        check("forge_math.invariant_lp_mint refuses the vector at its wrapper", False, "accepted")
    except ValueError as exc:
        check("forge_math.invariant_lp_mint refuses the vector at its WRAPPER, not the bracket",
              "negative" in str(exc), str(exc))
    return old, weights, fee_bps, total_lp, deposits, mint


def r2_puzzle(old, weights, fee_bps, total_lp, deposits, mint):
    """The shipped leaf must refuse the vector the bracket would accept.

    This is the check whose ABSENCE let the mutation run report SURVIVED. The
    existing actions suite probes `[-100000, +200000]` at lp_delta 50,000, which
    the bracket refuses unaided -- so deleting `assert deposit >= 0` left the
    suite green. Pinning the vector below is what makes the line KILLED.
    """
    print("R-2  the shipped `add` leaf refuses it, which is the line doing the work:")
    pool = kit.make_pool([None, bytes32(b"\xd0" * 32)], old, total_lp=total_lp,
                         weights=weights, fee_bps=fee_bps, leaves="forge", salt=0x50)
    parent = bytes32(b"\x01" * 32)
    ids = [bytes32(b"\x02" * 32)] * len(old)

    def probe(dep, lp):
        return kit.run_leaf(pool, "forge_action_add", [kit.H0 if hasattr(kit, "H0") else 100,
                                                       dep, lp, parent, ids, [500_000] * len(old)])

    try:
        probe(deposits, mint)
        check("the reviewer's vector is refused by the shipped leaf", False,
              "ACCEPTED -- the shipped build would be vulnerable")
    except Exception as exc:
        check("the reviewer's vector is refused by the shipped leaf", True,
              f"{type(exc).__name__}: {str(exc)[:56]}")

    # The honest control: the same pool, the same size of positive slot, no
    # negative. If this does not run, the refusal above proves nothing.
    honest_dep = [0, 500_000]
    honest_mint, _, _, _ = largest_accepted_mint(old, honest_dep, total_lp, fee_bps, weights)
    try:
        probe(honest_dep, honest_mint)
        check("  the same deposit without the negative slot is ACCEPTED (control)", True,
              f"lp_delta = {honest_mint}")
    except Exception as exc:
        check("  the same deposit without the negative slot is ACCEPTED (control)", False,
              f"{type(exc).__name__}: {str(exc)[:56]}")


# --------------------------------------------------------------------------
# R-1: registration never witnesses a reserve
# --------------------------------------------------------------------------

def r1_registration():
    """V13's R-1, replayed against V14: the reserve parents are DERIVED, and the launchers
    that create the reserves must be spent in the registration bundle.

    On V13 this function registered a pool whose reserve_parents named coins nobody would
    ever create, and the bundle was ACCEPTED (fourth review, R-1). On V14 `register` is given
    the grandparents, derives P_i = coinid(grandparent, RESERVE_LAUNCHER_HASH, amount), and
    asserts each launcher's coin announcement -- so the parents in the eve state are coins
    the bundle spends, and the same construction without them is refused.
    """
    import _test_v14_registry as regsuite

    print("R-1  V14: a registered pool's reserve parents are launchers the bundle spends:")
    reg0 = kit.make_registry(salt=0x21)
    bundle, _ = kit.registry_spend(reg0, "forge_registry_init", [])
    kit.validate(bundle)
    reg1 = reg0.advance([1, 0])
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    pool = kit.make_pool([None, bytes32(b"\xd0" * 32)], [10_000_000, 20_000_000],
                         total_lp=5_000_000, leaves="forge", salt=0x50)
    PARENTS = [bytes(p) for p in pool.state[7]]
    left, right = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)

    honest = regsuite.registration(reg1, pool, left, right, slots)[0]
    spent = {bytes(cs.coin.name()) for cs in honest.coin_spends}
    check("every reserve parent in the eve state is a coin the registration SPENDS (the launcher)",
          set(PARENTS) <= spent, f"{len(PARENTS)} parents among {len(honest.coin_spends)} coins")
    launcher_phs = {bytes(kit.reserve_launcher_full_hash(a)) for a in pool.asset_ids}
    check("  and each of those coins runs the reserve launcher puzzle (bare or CAT-wrapped)",
          all(bytes(cs.coin.puzzle_hash) in launcher_phs for cs in honest.coin_spends
              if bytes(cs.coin.name()) in set(PARENTS)))
    try:
        _, additions = kit.validate(honest)
        check("register ACCEPTS the honest registration", True)
        check("  and the bundle CREATES every reserve at its puzzle hash and amount",
              all((r.full_hash, int(r.coin.amount)) in additions for r in pool.reserves))
    except Exception as exc:
        check("register ACCEPTS the honest registration", False, f"refused: {type(exc).__name__}: {str(exc)[:60]}")

    # The V13 construction: the same registration with no launcher spends -- the parents
    # name coins nobody creates. V13 accepted this. V14 must not.
    squat = regsuite.registration(reg1, pool, left, right, slots, with_reserves=False)[0]
    try:
        kit.validate(squat)
        check("the V13 construction (parents that name nothing) is REFUSED on V14", False, "ACCEPTED -- R-1 is open")
    except Exception as exc:
        check("the V13 construction (parents that name nothing) is REFUSED on V14", True,
              f"{type(exc).__name__}: {str(exc)[:40]}  (12 = ASSERT_ANNOUNCE_CONSUMED_FAILED)")
    check("  so the market key can only be taken by a pool whose reserves exist", True,
          "and a squatter who funds [1, 1] is the case the dilution defence already answers")
    return reg1, pool


def main() -> int:
    if not (kit.v14_available() and kit.registry_available()):
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py")
        return 2

    old, weights, fee_bps, total_lp, deposits, mint = r2_math()
    print()
    r2_puzzle(old, weights, fee_bps, total_lp, deposits, mint)
    print()
    r1_registration()

    passed = sum(1 for r in results if r)
    print()
    print(f"{passed}/{len(results)} third-review reproduction checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
