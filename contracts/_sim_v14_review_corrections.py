#!/usr/bin/env python3
"""Two documentation claims a fifth review found wrong, checked against the puzzles.

  F2  FORGE_V13_CLVM_PASS.md says O-1 is closed because a future-dated spend "has
      birth >= h, so h == birth". That is false: birth is the coin's CREATION height,
      not the inclusion height of its spend, and ASSERT_HEIGHT_ABSOLUTE only delays
      inclusion. So the proof does not hold. Is the CONCLUSION still true -- is the
      oracle exact when h is future-dated -- for a different reason?

  F3  FORGE_V13_ARCHITECTURE.md describes the mint as `lp_delta <= invariant mint` and
      payouts as `<= pro-rata`. The leaves call exact_invariant_lp_mint and
      exact_withdrawal. Are those brackets EXACT, so that asking for less is refused?

Exit 0 if both are settled as reported, 1 otherwise.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs.sized_bytes import bytes32

import forge_math
import _v13_testkit as kit

results = []
CAT = bytes32(b"\xd0" * 32)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def refused(thunk):
    try:
        thunk()
        return False, "ACCEPTED"
    except Exception as exc:
        return True, f"{type(exc).__name__}: {str(exc)[:40]}"


# ---------------------------------------------------------------------------
# F2  a future-dated h, and whether the accounting stays exact
# ---------------------------------------------------------------------------
print("=" * 90)
print("F2  a future-dated h: the stated proof is wrong -- is the conclusion?")
print("=" * 90)
print("""
   birth is the pool coin's creation height. Naming a future h does not move it; the
   prologue's h >= birth is satisfied by any h after the coin was born, so 'h == birth'
   is not forced. The real question is what the accumulator does when a spend claims h
   and is included later, at H > h.
""")
BIRTH, LAST = 6_999_900, 6_999_850          # coin born at BIRTH; previous spend claimed LAST
pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                     leaves="forge", salt=0x90, last_height=LAST)
from dataclasses import replace
pool = replace(pool, birth=BIRTH)
spot0 = kit.spots(pool.state[0], pool.weights)

h_claimed = 6_999_990                        # future relative to BIRTH, in the window
state1, *_ = kit.run_leaf(pool, "forge_action_observe", [h_claimed])
st1 = kit.state_to_list(state1)
cums1, last1, last_spot1 = st1[3][1], st1[3][0], st1[3][2]
expected1 = [c + ls * (BIRTH - LAST) + s * (h_claimed - BIRTH)
             for c, ls, s in zip(pool.state[3][1], pool.state[3][2], spot0)]
check("the spend credits (LAST, BIRTH] at last_spot then [BIRTH, h] at spot -- exactly",
      cums1 == expected1, f"last_height -> {last1}")
check("  it records last_spot = the spot it priced, for whoever credits the tail",
      list(last_spot1) == list(spot0))

# Now the spend is INCLUDED at H > h (the future-dating). The successor is born at H.
H_incl = h_claimed + 20
succ = replace(pool.advance(st1), birth=H_incl)
state2, *_ = kit.run_leaf(succ, "forge_action_observe", [H_incl + 5])
st2 = kit.state_to_list(state2)
tail = [ls * (H_incl - h_claimed) for ls in last_spot1]
current = [s * (H_incl + 5 - H_incl) for s in kit.spots(st1[0], pool.weights)]
expected2 = [c + t + cur for c, t, cur in zip(cums1, tail, current)]
check("the NEXT spend credits the tail (h, H_incl] at the recorded spot, then its own interval",
      st2[3][1] == expected2, f"tail of {H_incl - h_claimed} blocks credited at last_spot")
total_blocks = (H_incl + 5) - LAST
check("  every block from LAST to the second spend is credited exactly once",
      all(c2 - c0 == ls * (BIRTH - LAST) + s0 * (h_claimed - BIRTH) + ls1 * (H_incl - h_claimed) + s1 * 5
          for c2, c0, ls, s0, ls1, s1 in zip(st2[3][1], pool.state[3][1], pool.state[3][2], spot0,
                                              last_spot1, kit.spots(st1[0], pool.weights))),
      f"{total_blocks} blocks, none dropped, none double-counted")
print("""
   So O-1 IS closed under V13 -- but by the exact two-interval accounting, not by the
   argument the pass gives. A future-dated spend credits [birth, h]; the gap between h
   and its actual inclusion is credited by the next spend at the spot this one recorded.
   Nothing is under-weighted. The stated proof was wrong; the conclusion survives, and
   this is the proof that should replace it.
""")

# ---------------------------------------------------------------------------
# F3  are the mint and withdrawal brackets exact?
# ---------------------------------------------------------------------------
print("=" * 90)
print("F3  exact, or an upper bound? Ask for one less and see.")
print("=" * 90 + "\n")
H0 = 6_999_990
pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                     leaves="forge", salt=0x91)
dep = [100_000, 200_000]
honest = forge_math.invariant_lp_mint(pool.state[0], dep, pool.state[1], pool.fee_bps, pool.weights, version=10)
parent, ids = bytes32(b"\x01" * 32), [bytes32(b"\x02" * 32)] * 2

ok, why = refused(lambda: kit.run_leaf(pool, "forge_action_add", [H0, dep, honest, parent, ids]))
check(f"add at the bracketed mint ({honest:,}) is accepted", not ok, why if ok else "")
ok, why = refused(lambda: kit.run_leaf(pool, "forge_action_add", [H0, dep, honest - 1, parent, ids]))
check(f"add asking for ONE LESS ({honest - 1:,}) is REFUSED -- the mint is exact, not <=", ok, why)
ok, why = refused(lambda: kit.run_leaf(pool, "forge_action_add", [H0, dep, honest + 1, parent, ids]))
check(f"add asking for one more is refused too (two-sided)", ok, why)

burn = 1_000_000
vf = forge_math.vault_fee_bps(2, 10, pool.fee_bps)
pay = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)
ok, why = refused(lambda: kit.run_leaf(pool, "forge_action_remove", [H0, burn, parent, pay]))
check(f"remove at the exact pro-rata payout {pay} is accepted", not ok, why if ok else "")
less = [pay[0] - 1, pay[1]]
ok, why = refused(lambda: kit.run_leaf(pool, "forge_action_remove", [H0, burn, parent, less]))
check(f"remove asking for ONE LESS on asset 0 is REFUSED -- payouts are exact, not <=", ok, why)
print("""
   The architecture page's '<=' is wrong in both places. An implementer who requested
   less than the bracket -- a natural thing to do defensively -- would be refused, and
   the document would have told them it was allowed.
""")


def main() -> int:
    passed = sum(1 for r in results if r)
    print(f"{passed}/{len(results)} checks settled as reported")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
