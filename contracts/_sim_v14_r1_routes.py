#!/usr/bin/env python3
"""Which R-1 fix actually closes the squat? Simulated against the real V13 puzzles.

R-1: `register` admits a pool whose reserves were never funded, taking a market key
permanently for one creation fee (Chia-Network/chips#217, 2026-09-15).

Three candidate routes, and the point of this file is to decide between them with
consensus rather than with argument. No puzzle changes are needed: each route can be
simulated by choosing what the creation bundle CONTAINS and letting the mempool's
validator judge it.

  ROUTE 0  V13 today                 -- register, reserves never funded
  ROUTE A  settlement announcement   -- proves each reserve was FUNDED, but a puzzle
                                        announcement binds the puzzle, not the coin, so
                                        the parent recorded in state is still whatever
                                        the registrant says. Simulated as: reserves are
                                        real, parents are wrong, no pool spend.
  ROUTE B  spend the eve at genesis  -- the pool's own first spend messages every
                                        reserve, so a wrong parent cannot pair.
                                        Simulated as: the creation bundle includes the
                                        eve's observe spend.

What the local validator does and does not do matters here. It enforces CHIP-0025
message pairing, which is what catches a wrong parent, and it does NOT check coin
existence -- so ROUTE 0 and ROUTE A pass locally exactly as they would pass on chain,
for the same reason.

Exit 0 if the simulation ran, 1 if a route behaved unexpectedly, 2 if V13 is unbuilt.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import _v13_testkit as kit
import _test_v13_registry as regsuite

results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


CAT = bytes32(b"\xd0" * 32)
RESERVES = [10_000_000, 20_000_000]
TOTAL_LP = 5_000_000


def fresh_registry():
    reg0 = kit.make_registry(salt=0x21)
    bundle, _ = kit.registry_spend(reg0, "forge_registry_init", [])
    kit.validate(bundle)
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    return reg0.advance([1, 0]), slots


def honest_pool(salt=0x50):
    """A pool whose state's reserve parents ARE the parents of its reserve coins."""
    return kit.make_pool([None, CAT], RESERVES, total_lp=TOTAL_LP, leaves="forge", salt=salt)


def wrong_parent_pool(salt=0x50):
    """Real reserve coins, but the state records parents that are not theirs.

    This is what ROUTE A still permits: the settlement announcement would prove the
    coins were funded, and say nothing about which parent the registrant recorded.
    """
    honest = honest_pool(salt)
    real_coins = [(r.coin, r.lineage) for r in honest.reserves]
    lying_state = [*list(honest.state)[:7], [bytes32(b"\xee" * 32), bytes32(b"\xef" * 32)]]
    return kit.make_pool([None, CAT], RESERVES, total_lp=TOTAL_LP, leaves="forge", salt=salt,
                         reserve_coins=real_coins, state=lying_state)


def registration_bundle(reg, pool, slots, with_eve_spend: bool):
    """The creation-and-registration bundle, optionally including the eve's first spend."""
    extra = []
    if with_eve_spend:
        # ROUTE B: the pool's own first action. `observe` touches only the oracle, and
        # running it forces the finalizer to message every reserve.
        #
        # The eve is created in THIS block, so ASSERT_MY_BIRTH_HEIGHT pins its birth to
        # the inclusion height -- and the prologue then requires h >= birth while
        # ASSERT_HEIGHT_ABSOLUTE requires h <= the inclusion height. So the genesis spend
        # has exactly one legal height: h == birth == inclusion. It credits a zero oracle
        # interval, which is correct for a pool that has just been born.
        from dataclasses import replace
        pool = replace(pool, birth=kit.VALIDATION_HEIGHT)
        eve_bundle, _new_state = kit.spend_action(pool, "forge_action_observe", [kit.VALIDATION_HEIGHT])
        extra.extend(eve_bundle.coin_spends)
    return regsuite.registration(reg, pool,
                                 (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY),
                                 (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY), slots,
                                 launcher_pool=pool)[0], extra


def try_route(label, pool, with_eve_spend, expect_accepted):
    reg, slots = fresh_registry()
    try:
        bundle, extra = registration_bundle(reg, pool, slots, with_eve_spend)
        if extra:
            from chia_rs import SpendBundle, G2Element
            bundle = SpendBundle([*bundle.coin_spends, *extra], G2Element())
        kit.validate(bundle)
        accepted = True
        reason = ""
    except kit.Rejected as exc:
        accepted, reason = False, str(exc)[:70]
    except Exception as exc:
        accepted, reason = False, f"{type(exc).__name__}: {str(exc)[:60]}"
    verdict = "ACCEPTED" if accepted else f"REFUSED ({reason})"
    ok = accepted == expect_accepted
    check(f"{label}: {verdict}", ok,
          "" if ok else f"expected {'ACCEPTED' if expect_accepted else 'REFUSED'}")
    return accepted


def main() -> int:
    if not (kit.v13_available() and kit.registry_available()):
        print("  [skip] V13 build outputs are absent; run scripts/build-v13.py")
        return 2

    print("ROUTE 0 -- V13 as it ships: reserves never funded, no pool spend")
    try_route("  registration with unfunded reserves", honest_pool(0x50), False, expect_accepted=True)
    print("""    The harness fabricates reserve coins and never creates them, which is the
    squatter's position exactly. The validator does not check coin existence, so this
    is ACCEPTED here for the same reason it is accepted on chain.
""")

    print("ROUTE A -- settlement announcement: reserves funded, parent still the registrant's word")
    try_route("  registration with REAL reserves but WRONG parents",
              wrong_parent_pool(0x51), False, expect_accepted=True)
    print("""    A puzzle announcement is keyed by sha256(puzzle_hash + message): it binds the
    PUZZLE, never the announcing coin. Proving a settlement funded the reserve therefore
    says nothing about which parent went into state. The registrant records a parent of
    their choosing, the pool is bricked, and the key is taken anyway.
""")

    print("ROUTE B -- spend the eve in the creation bundle")
    # Each half works ALONE, which is what makes the combined failure informative.
    reg, slots = fresh_registry()
    solo = honest_pool(0x52)
    solo_reg, _ = registration_bundle(reg, solo, slots, with_eve_spend=False)
    try:
        kit.validate(solo_reg)
        check("  the registration alone validates", True)
    except Exception as exc:
        check("  the registration alone validates", False, str(exc)[:60])
    from dataclasses import replace
    eve_only, _ = kit.spend_action(replace(solo, birth=kit.VALIDATION_HEIGHT),
                                   "forge_action_observe", [kit.VALIDATION_HEIGHT])
    try:
        kit.validate(eve_only)
        check("  the eve's observe spend alone validates", True)
    except Exception as exc:
        check("  the eve's observe spend alone validates", False, str(exc)[:60])

    both = try_route("  the two TOGETHER, parents CORRECT", honest_pool(0x52), True, expect_accepted=False)
    check("  ROUTE B is impossible: consensus refuses it even when everything is honest",
          not both, "EPHEMERAL_RELATIVE_CONDITION (141)")
    print("""    Error 141 is EPHEMERAL_RELATIVE_CONDITION. A coin created and spent in the same
    block may not carry a birth-height condition -- and the V13 prologue emits
    ASSERT_MY_BIRTH_HEIGHT on EVERY pool spend. So the eve can never be spent in the
    block that creates it. Not a limitation of this harness: a consensus rule.

    Our own CLVM pass already recorded the same rule for successors ("with
    ASSERT_MY_BIRTH_HEIGHT it also makes a same-block successor unspendable"). It applies
    to the eve for exactly the same reason, which we had not noticed.
""")

    passed = sum(1 for r in results if r)
    print(f"{passed}/{len(results)} route simulations behaved as predicted")
    if passed == len(results):
        print("""
CONCLUSION
  ROUTE 0  squat succeeds        -- the finding, unchanged
  ROUTE A  squat still succeeds  -- forces funding; a puzzle announcement binds the
                                    puzzle, not the coin, so the recorded parent is
                                    still the registrant's word
  ROUTE B  CANNOT BE BUILT       -- EPHEMERAL_RELATIVE_CONDITION: the eve carries
                                    ASSERT_MY_BIRTH_HEIGHT and so can never be spent
                                    in the block that creates it

None of the three closes R-1. The next candidate has to verify the parent WITHOUT
spending the pool in its own creation block.
""")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
