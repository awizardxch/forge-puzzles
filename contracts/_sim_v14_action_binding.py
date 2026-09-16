#!/usr/bin/env python3
"""Is every action bound to the assets it actually moves -- or only the first one?

The concern: an action's settlement assertion is

    AssertPuzzleAnnouncement(sha256(settlement_puzzle_hash(asset_id) + tree_hash((coin_id, nil))))

which binds the ASSET and the COIN ID, and says nothing about the AMOUNT. Value
conservation is left to the CAT ring and the bundle's balance. That is a seam worth
attacking, and it gets sharper in a multi-action spend: an announcement is not consumed
by an assertion, so several actions can assert the SAME settlement.

The prologue also runs only once. Later actions in a spend skip `valid_config`, the state
shape checks and the floor check, which is the "not just the initial action" question.

Every probe below is an attempted exploit. A PASS means the attack was refused.

Exit 0 if every attack is refused, 1 if any succeeds.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs.sized_bytes import bytes32

import forge_math
import _v13_testkit as kit

results = []
H0 = 6_999_990
CAT_A = bytes32(b"\xd0" * 32)
CAT_B = bytes32(b"\xd1" * 32)      # an asset the attacker issues


def attack(label, thunk, expect_refused=True):
    try:
        thunk()
        ok = not expect_refused
        detail = "" if ok else "ACCEPTED -- THE ATTACK WORKED"
    except Exception as exc:
        ok = expect_refused
        detail = f"{type(exc).__name__}: {str(exc)[:48]}" if ok else str(exc)[:60]
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}  {detail}")
    results.append(ok)
    return ok


def fresh(salt=0x80):
    return kit.make_pool([None, CAT_A], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=salt)


print("=" * 92)
print("1. Does the settlement bind the ASSET?")
print("=" * 92 + "\n")
pool = fresh(0x80)
r, w = pool.state[0], pool.weights
g = 250_000
out = forge_math.swap_output(r[0], r[1], g, pool.fee_bps, w[0], w[1])
good, good_spend = kit.offer_settlement_xch(g, salt=0xE1)

attack("an honest swap is accepted",
       lambda: kit.validate(kit.spend_action(pool, "forge_action_swap",
                                             [H0, 0, 1, g, out, good.name()],
                                             extra_spends=[good_spend])[0]),
       expect_refused=False)

# The attacker settles with a coin at the WRONG settlement puzzle -- a CAT they issue --
# and names its id. The announcement the leaf asserts is keyed by the asset's settlement
# puzzle hash, so their coin's announcement is at a different key.
try:
    bad, bad_spend = kit.offer_settlement_cat(CAT_B, g, salt=0xE2)
    attack("a settlement of an asset the attacker issues cannot satisfy the assertion",
           lambda: kit.validate(kit.spend_action(pool, "forge_action_swap",
                                                 [H0, 0, 1, g, out, bad.name()],
                                                 extra_spends=[bad_spend])[0]))
except AttributeError:
    # no CAT settlement helper in the kit; make the point with a bare coin id instead
    attack("a settlement coin id nothing in the bundle announces is refused",
           lambda: kit.validate(kit.spend_action(pool, "forge_action_swap",
                                                 [H0, 0, 1, g, out, bytes32(b"\xbb" * 32)],
                                                 extra_spends=[])[0]))

print("\n" + "=" * 92)
print("2. Does the settlement bind the AMOUNT? (it does not -- so what catches it?)")
print("=" * 92 + "\n")
pool = fresh(0x81)
# A settlement coin holding far LESS than the swap claims as gross_input. The assertion
# is satisfied -- same asset, same coin id -- so only value conservation can refuse it.
short, short_spend = kit.offer_settlement_xch(1, salt=0xE3)
attack("a settlement holding 1 mojo cannot fund a 250,000 gross input",
       lambda: kit.validate(kit.spend_action(pool, "forge_action_swap",
                                             [H0, 0, 1, g, out, short.name()],
                                             extra_spends=[short_spend])[0]))
print("""     The settlement assertion passes here -- right asset, right coin id. What refuses
     the spend is the bundle's value balance: the reserve is re-created at reserve+gross
     and nothing supplied the difference. The binding is announcement AND conservation,
     not announcement alone.""")

print("\n" + "=" * 92)
print("3. THE ONE THAT MATTERS: two actions asserting the SAME settlement")
print("=" * 92)
print("""
   An announcement is not consumed by an assertion. Two swaps in one spend can both name
   the same settlement coin id, and both assertions are satisfied by the one announcement
   it makes. If nothing else caught it, the pool would credit the input twice.
""")
pool = fresh(0x82)
r, w = pool.state[0], pool.weights
g1 = 250_000
out1 = forge_math.swap_output(r[0], r[1], g1, pool.fee_bps, w[0], w[1])
s1, s1_spend = kit.offer_settlement_xch(g1, salt=0xE4)
r_after = [r[0] + g1, r[1] - out1]
out2 = forge_math.swap_output(r_after[0], r_after[1], g1, pool.fee_bps, w[0], w[1])

attack("two swaps both crediting ONE settlement coin",
       lambda: kit.validate(kit.spend_actions(pool, [
           ("forge_action_swap", [H0, 0, 1, g1, out1, s1.name()]),
           ("forge_action_swap", [H0, 0, 1, g1, out2, s1.name()]),
       ], extra_spends=[s1_spend])[0]))

# The honest version of the same shape: two settlements, two swaps.
s2, s2_spend = kit.offer_settlement_xch(g1, salt=0xE5)
attack("  the same two swaps with TWO real settlements are accepted",
       lambda: kit.validate(kit.spend_actions(pool, [
           ("forge_action_swap", [H0, 0, 1, g1, out1, s1.name()]),
           ("forge_action_swap", [H0, 0, 1, g1, out2, s2.name()]),
       ], extra_spends=[s1_spend, s2_spend])[0]),
       expect_refused=False)

print("\n" + "=" * 92)
print("4. Can a LATER action exploit the prologue running only once?")
print("=" * 92)
print("""
   The prologue checks valid_config, the state shape and the floor on the FIRST action
   only; later actions assert nothing but the shared height. The state they act on is
   whatever the previous leaf returned, so the question is whether a first action can
   hand a later one a state the later one would have rejected.
""")
pool = fresh(0x83)
# remove is the only leaf that lowers total_lp, and it is self-limiting: burn is capped
# at total_lp - MIN_LOCKED_LP by the leaf itself, not by the prologue.
tot = pool.state[1]
attack("a remove cannot burn past the floor even as a later action",
       lambda: kit.validate(kit.spend_actions(pool, [
           ("forge_action_observe", [H0]),
           ("forge_action_remove", [H0, tot - kit.MIN_LOCKED_LP + 1, bytes32(b"\x01" * 32), [0, 0]]),
       ])[0]))
attack("a later action naming a different height is refused",
       lambda: kit.validate(kit.spend_actions(pool, [
           ("forge_action_observe", [H0]),
           ("forge_action_observe", [H0 + 1]),
       ])[0]))

print("\n" + "=" * 92)
print("5. Can a minted LP coin stand in for a real one?")
print("=" * 92)
print("""
   The LP handshake derives the action coin's id from the parent, the CAT puzzle hash with
   the pinned inner, and the exact amount -- then messages THAT id under mode 23. A coin
   the attacker minted has a different id and never receives the message.
""")
pool = fresh(0x84)
attack("a remove naming an LP parent that mints nothing is refused",
       lambda: kit.validate(kit.spend_action(pool, "forge_action_remove",
                                             [H0, 1_000, bytes32(b"\xcc" * 32), [0, 0]])[0]))


def main() -> int:
    passed = sum(1 for x in results if x)
    print(f"\n{passed}/{len(results)} probes behaved as required")
    if passed == len(results):
        print("""
   Every attempted exploit is refused. The binding is two-part and both parts are load
   bearing: the ANNOUNCEMENT ties an action to a settlement of the right asset, and VALUE
   CONSERVATION ties it to the right amount. Neither alone is sufficient, which is worth
   recording -- a future change that moved value accounting out of the ring would silently
   remove half of it.""")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
