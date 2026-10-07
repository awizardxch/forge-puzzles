#!/usr/bin/env python3
"""Is a vault's two-sided charge CONSISTENT with how a swap pool charges a round trip?

The claim under test: a vault crossing is two trades -- underlying into LP on the way in,
LP back to underlying on the way out -- so charging both directions is correct, and
matches what a two-asset pool charges for A -> B -> A.

If that holds, charging on entry only would make a vault crossing half the price of the
equivalent swap round trip, which is an inconsistency rather than a fix.

A second question falls out of it: is the "fee recovered by splitting the exit" finding
actually an exploit, or is it ordinary LP economics arriving by an awkward route?

Exit 0 always: this compares two charging models against each other.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_math as fm

FEE = 30
SCALE = 10_000
W1, W2 = [1], [1, 1]


def vault_mint(res, deposit, total_lp, charge_entry=True):
    fee = FEE if charge_entry else 0
    eff = fm.effective_amounts([res], [deposit], fee, W1)
    if eff[0] <= 0:
        return 0
    target = total_lp * eff[0]
    lo, hi, best = 0, max(total_lp * 4, 1024), 0
    while (total_lp + hi) * res <= target:
        hi *= 2
    while lo <= hi:
        m = (lo + hi) // 2
        if (total_lp + m) * res <= target:
            best, lo = m, m + 1
        else:
            hi = m - 1
    return best


print("=" * 94)
print("1. What a TWO-ASSET pool charges for a round trip A -> B -> A")
print("=" * 94)
R = [100_000_000, 100_000_000]
print(f"\n   reserves {R[0]:,} / {R[1]:,}, fee {FEE} bps\n")
print(f"   {'trade size':>14} {'out':>14} {'back':>14} {'total cost':>12} {'as bps':>9}")
for amt in (1_000, 100_000, 1_000_000):
    out = fm.swap_output(R[0], R[1], amt, FEE)
    back = fm.swap_output(R[1] + out, R[0] - out, out, FEE)
    cost = amt - back
    print(f"   {amt:>14,} {out:>14,} {back:>14,} {cost:>12,} {cost / amt * SCALE:>8.1f}")
print("""
   A round trip through a swap pool costs about TWICE the one-way fee: 30 bps each way,
   ~60 bps in total. Each leg is a trade and each leg is charged.
""")

print("=" * 94)
print("2. What a VAULT charges for the same round trip")
print("=" * 94)
RES, TOT = 100_000_000, 100_000_000
print(f"\n   reserve {RES:,}, total_lp {TOT:,}, fee {FEE} bps")
print("   (a large existing pool, so the depositor is NOT the whole pool)\n")
print(f"   {'deposit':>14} {'LP minted':>14} {'returned':>14} {'total cost':>12} {'as bps':>9}")
for amt in (1_000, 100_000, 1_000_000):
    m = vault_mint(RES, amt, TOT, charge_entry=True)
    res2, tot2 = RES + amt, TOT + m
    gross = (res2 * m) // tot2
    back = (gross * (SCALE - FEE)) // SCALE
    cost = amt - back
    print(f"   {amt:>14,} {m:>14,} {back:>14,} {cost:>12,} {cost / amt * SCALE:>8.1f}")
print("""
   The vault charges the same ~60 bps for in-and-out. Two crossings, two charges --
   consistent with the swap pool, and the reason the two-sided charge is right.

   Charging on ENTRY ONLY would make a vault crossing ~30 bps against a swap pool's ~60,
   so routing through a vault would be systematically cheaper than the equivalent swap.
   That is an inconsistency, not a fix.
""")

print("=" * 94)
print("3. Then what IS the splitting result?")
print("=" * 94)
print("""
   Ordinary LP economics, reached by an awkward route.

   In ANY automated market maker, a liquidity provider who trades against their own pool
   pays the fee and receives their share of it straight back. Net cost is
   fee x (1 - their share). A holder of 90% of a pool pays 10% of the fee, in Forge or
   anywhere else. That is not an exploit; it is what owning the pool means.
""")
RES, TOT = 238_571, 238_571
print(f"   {'stake':>8} {'net cost if the economics were applied directly':>50}")
for s in (0.01, 0.10, 0.50, 0.90, 1.00):
    print(f"   {s:>7.0%} {f'{FEE * (1 - s):.1f} bps':>50}")
print("""
   The awkwardness is that Forge does not apply it directly. A SINGLE removal charges the
   full fee regardless of stake, so a 90% holder is overcharged by 9x. Splitting the exit
   is how they reach the number they should have been charged in the first place -- each
   piece leaves the fee in a pool they still mostly own.

   So the finding is not "the fee is avoidable". It is:

       a single-removal exit OVERCHARGES a large holder, and splitting is the
       workaround that reaches the correct price.

   That is a much smaller problem, and it argues against my earlier proposal: routing the
   fee to fees_owed would make the overcharge PERMANENT by removing the workaround.
""")

print("=" * 94)
print("WHERE THIS LANDS")
print("=" * 94)
print("""
   The two-sided charge is correct and should stay. A vault crossing is two trades and
   costs what two trades cost, which is what keeps it priced against the swap pools it
   competes with.

   What remains is a UX wrinkle rather than a security finding: a large holder pays the
   right price only if they know to split. Options, smallest first:

   * Say so. Document that exiting a vault in pieces costs less if you hold a large share,
     because you re-claim your own fee. No code.
   * Have the interface split a large vault exit automatically, the way the router already
     splits a swap across pools when that is cheaper.
   * Leave it entirely. The overcharge only touches holders large enough to matter, and
     they are the ones most likely to find it.

   None of these is a V15 puzzle change, which is the useful conclusion: the puzzle is
   right, and I was wrong twice about it.
""")
