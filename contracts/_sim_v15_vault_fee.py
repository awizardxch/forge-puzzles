#!/usr/bin/env python3
"""SUPERSEDED IN PART. The LP-ratio half stands; the economics half does not.

The break-even table below assumes each removal costs a network fee. It does not: a ZERO
network fee is valid (the node refuses a NONZERO fee under five mojos per cost), so
splitting is free and no position-size threshold exists. Kept because the ratio result is
still correct and worth having, and because a wrong bound is worth showing next to the
reason it was wrong. The settled reading is in _sim_v15_vault_consistency.py.

The vault redemption fee: what the LP ratio changes, and whether splitting pays.

The finding: on an N=1 pool the redemption fee stays in the reserve, so a holder who
leaves in pieces keeps re-claiming their own share of it. Recovery scales with stake.

This file answers the two questions that decide whether it matters to us:

  1. does a higher genesis LP ratio make it worse?
  2. is it economic once each removal costs a network fee?

The withdrawal formula is a flat multiplicative haircut:

    payout_i = (reserve_i * burn * (10000 - fee_bps)) // (total_lp * 10000)

Exit 0 always: this quantifies a known finding.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_math as fm

FEE_BPS = 30              # the vault's LP fee, charged on redemption
TX_FEE = 5_000_000_000    # mojos per transaction, as measured for V13 pushes
MOJOS_PER_XCH = 10 ** 12


def redeem(reserve, total_lp, mine, k, fee=FEE_BPS):
    """Withdraw `mine` LP in k pieces; return what the holder receives."""
    res, tot, left, got = reserve, total_lp, mine, 0
    per = max(1, mine // k)
    for i in range(k):
        b = per if i < k - 1 else left
        if b <= 0 or b >= tot:
            break
        pay = fm.withdrawal_amounts([res], b, tot, fee)[0]
        got += pay
        res -= pay
        tot -= b
        left -= b
    return got


def recovery(reserve, total_lp, mine, k):
    one = redeem(reserve, total_lp, mine, 1)
    many = redeem(reserve, total_lp, mine, k)
    gross = (reserve * mine) // total_lp
    paid = gross - one
    return (many - one) / paid if paid else 0.0, one, many, paid


print("=" * 94)
print("1. Does the genesis LP ratio change it?")
print("=" * 94)
print("""
   A vault mints total_lp = reserve * lp_ratio at genesis. Our V13 vaults all used
   ratio 1 (B1: 238,571 t8 -> 238,571 LP). The question is whether a higher ratio makes
   the fee more recoverable.
""")
RESERVE = 238_571
print(f"   {'lp_ratio':>9} {'total_lp':>12} {'holder 90%':>12} {'recovery k=100':>15} {'max useful k':>13}")
for ratio in (1, 10, 100, 1_000):
    tot = RESERVE * ratio
    mine = int(tot * 0.90)
    rec, _one, _many, _paid = recovery(RESERVE, tot, mine, 100)
    print(f"   {ratio:>9,} {tot:>12,} {mine:>12,} {rec:>14.1%} {mine:>13,}")
print("""
   The ratio does not change the recovery rate: the haircut is proportional, so the
   arithmetic is scale-free. What it changes is the CEILING on how finely a position can
   be split -- each piece must burn at least one LP unit. At ratio 1 a 90% holder of our
   B1 vault could already split into 214,713 pieces, which is far past any useful number.

   So a higher ratio does not make this worse. It was never the binding constraint.
""")

print("=" * 94)
print("2. What IS the binding constraint: every piece costs a transaction")
print("=" * 94)
print(f"""
   Each removal is its own spend at {TX_FEE / MOJOS_PER_XCH:.3f} XCH. So splitting trades
   network fees against recovered vault fees, and the exploit only pays above a size.
""")
print(f"   {'pieces':>7} {'recovery':>10} {'network cost':>14} {'break-even position':>22}")
tot = RESERVE
mine = tot - 1
for k in (2, 5, 10, 50, 100, 500):
    rec, _o, _m, _p = recovery(RESERVE, tot, mine, k)
    cost = k * TX_FEE
    # recovered value = fee rate * position * recovery; solve for the position that pays
    rate = FEE_BPS / 10_000 * rec
    breakeven = (cost / rate) if rate > 0 else float("inf")
    print(f"   {k:>7} {rec:>9.1%} {cost / MOJOS_PER_XCH:>13.3f} XCH "
          f"{breakeven / MOJOS_PER_XCH:>17,.1f} XCH")
print("""
   Read the last column as: "a vault position worth less than this loses money by
   splitting". Ten pieces recovers most of what a hundred does and costs a tenth as much,
   so the practical attacker uses few pieces and needs a position in the tens of XCH.
""")

print("=" * 94)
print("3. Does it matter to OUR vaults?")
print("=" * 94)
ours = [("B1 t8 vault", 238_571), ("F1 t6 vault", 83_263), ("H3 t14 vault", 61_058)]
print(f"\n   {'vault':16s} {'genesis reserve':>16} {'fee at stake (90% holder)':>27} {'10 pieces cost':>16}")
for name, res in ours:
    mine = int(res * 0.90)
    gross = (res * mine) // res
    at_stake = gross * FEE_BPS / 10_000
    print(f"   {name:16s} {res:>16,} {at_stake:>26,.0f} {10 * TX_FEE / MOJOS_PER_XCH:>15.3f} XCH")
print("""
   These are CAT mojos against XCH network fees, so the comparison needs a price -- but at
   any plausible testnet valuation the fee at stake is a few hundred mojos and ten pieces
   cost 0.05 XCH. Splitting our own vaults would cost orders of magnitude more than it
   recovers. The finding is real and it is not live at our sizes.
""")

print("=" * 94)
print("WHAT TO DO ABOUT IT")
print("=" * 94)
print("""
   Three options, and the measurements above argue for the first:

   A. LEAVE IT, DOCUMENT IT. The fee is recoverable by anyone willing to pay more in
      network fees than they save, which bounds it to large positions. It redistributes
      between LPs rather than draining the pool: what a splitter keeps is what the
      remaining holders would have gained. Nobody can take more than the fee they were
      charged.

   B. CHARGE ON THE POSITION, NOT THE REMOVAL. If the vault fee were assessed against a
      holder's total exit rather than each removal, splitting would gain nothing. There is
      no way to do that in this design: the puzzle sees one burn at a time and has no
      identity to accumulate against.

   C. DROP THE VAULT FEE. It exists because for a vault the deposit IS the trade, so a
      redemption is the only crossing there is. Removing it makes vault entry and exit
      free and changes the economics of every vault we have.

   A is the honest answer while the measured break-even sits in the tens of XCH. It should
   be written down rather than left for a reviewer to find, and revisited if vaults ever
   hold positions where the arithmetic flips.
""")
