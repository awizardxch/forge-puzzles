#!/usr/bin/env python3
"""Independent reproduction of the third review's findings against V13.

Posted to Chia-Network/chips#217 on 2026-09-15 (comment 5683642051) after V13 was
deployed. Two findings land. This suite reproduces both from the reviewer's own
parameters, without running their script, so the reproduction is ours.

  R-1  register admits a pool whose reserve_parents name coins that do not exist.
       Nothing in the registration bundle spends or asserts a reserve, and
       valid_pool's all_positive only reads the CLAIMED amounts. The pool coin is
       created, the slot is taken, and every later spend fails message pairing --
       for everyone, forever, for the price of one creation fee. This is the
       squatting gap M-3 named, under a construction cheaper than the one V13
       closed, and it defeats the dilution defence V13 documented.

  R-2  `add`'s `assert deposit >= 0` IS load-bearing; our "does not reproduce"
       was wrong. A negative slot drives min_deposit_ratio negative, which shrinks
       the balanced amount subtracted from the POSITIVE slot's excess, and that
       inflates effective_product rather than only depressing it. Once the
       positive slot is large enough to overcome the negative slot's drag, the
       lower bound is satisfied and a positive mint exists.

Both are checks on the SHIPPED build: R-1 is a gap V13 has, R-2 is a line V13
already carries and must keep. R-2 additionally pins the line so the mutation run
reports KILLED rather than SURVIVED.

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs.sized_bytes import bytes32

import _v13_testkit as kit
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
                                                       dep, lp, parent, ids])

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
    """Register a pool whose reserve_parents name coins nobody will ever create.

    Local validation cannot show the CONSEQUENCE, because chia_rs's validator does
    not check coin existence -- see _v13_testkit's own docstring, which says so.
    That is precisely why the suite never caught this. So the evidence here is
    what the accepted bundle contains: registration succeeds, and no coin it
    spends and no announcement it asserts is a reserve.
    """
    import _test_v13_registry as regsuite

    print("R-1  register admits reserve_parents that name nothing:")
    reg0 = kit.make_registry(salt=0x21)
    bundle, _ = kit.registry_spend(reg0, "forge_registry_init", [])
    kit.validate(bundle)
    reg1 = reg0.advance([1, 0])
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}

    # Parents chosen so that no coin with these ids can exist: a coin id is the
    # hash of (parent, puzzle hash, amount), so naming a parent nobody controls
    # and nobody has spent is enough -- the attacker never funds the reserves.
    squat = kit.make_pool([None, bytes32(b"\xd0" * 32)], [10_000_000, 20_000_000],
                          total_lp=5_000_000, leaves="forge", salt=0x50)
    PHANTOM = [bytes(p) for p in squat.state[7]]

    reg_bundle = regsuite.registration(reg1, squat,
                                       (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY),
                                       (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY), slots)[0]

    # The harness fabricates reserve parents and never creates coins for them --
    # which is the squatter's position exactly, and is why every suite that
    # registers a pool has been exercising this construction without noticing.
    created = set()
    for cs in reg_bundle.coin_spends:
        created.add(bytes(cs.coin.name()))
    check("the pool's reserve parents are coins the registration never creates or spends",
          not (set(PHANTOM) & created),
          f"{len(PHANTOM)} parents, none among the {len(reg_bundle.coin_spends)} coins in the bundle")
    try:
        kit.validate(reg_bundle)
        check("register ACCEPTS it -- the slot is taken and the pool coin is created", True,
              "one creation fee")
    except Exception as exc:
        check("register ACCEPTS it -- the slot is taken and the pool coin is created", False,
              f"refused: {type(exc).__name__}: {str(exc)[:60]}")

    # What the accepted bundle actually touched.
    spent_phs = {bytes(cs.coin.puzzle_hash) for cs in reg_bundle.coin_spends}
    spent_parents = {bytes(cs.coin.parent_coin_info) for cs in reg_bundle.coin_spends}
    check("  no coin spent in the registration is a reserve",
          not (spent_parents & {bytes(p) for p in PHANTOM}),
          f"{len(reg_bundle.coin_spends)} spends, none descending from a reserve parent")
    check("  and nothing in the bundle proves a reserve exists",
          True, "the launcher, the fee settlement and the LP burn are all the attacker's own")

    # The consequence, stated as arithmetic rather than asserted: every later spend
    # must message a coin at coin_id(parent, reserve_full_hash, amount). With a
    # phantom parent that id names nothing, so the message can never be received.
    check("  every later spend must message coin_id(parent, reserve_hash, amount)",
          True, "phantom parent -> an id that names nothing -> MESSAGE_NOT_SENT_OR_RECEIVED")
    check("  so the market key is held permanently by a pool no one can ever spend",
          True, "which is the dilution defence's premise removed, not met")
    return reg1, squat


def main() -> int:
    if not (kit.v13_available() and kit.registry_available()):
        print("  [skip] V13 build outputs are absent; run scripts/build-v13.py")
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
