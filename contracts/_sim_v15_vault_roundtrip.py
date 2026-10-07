#!/usr/bin/env python3
"""Where should a vault's LP fee be charged: entry, exit, or both?

The argument under test, and it is a good one: a fee that stays in the reserve lands in a
pool the payer is part of, so they get their share of it back. That works on ENTRY -- the
fee reduces the LP minted and the value stays in a pool the depositor now owns. It does
not work on EXIT: the last thing a holder does is leave, and the fee they pay on the way
out sits in a pool they no longer hold.

So a sole holder who deposits and withdraws should end where they started, and today they
do not. Splitting the exit is how they claw it back -- which reframes the "exploit" as a
correction to an over-charge.

Three designs compared:

  A  BOTH        fee charged on the deposit AND on the redemption, both staying in the
                 reserve. What is built today.
  B  EXIT-ACCRUED  fee charged on redemption only, routed to fees_owed so it cannot be
                 re-claimed. Closes the splitting gap, and breaks the round trip.
  C  ENTRY-ONLY  fee charged on the deposit only, staying in the reserve. Nothing to
                 recover on the way out, because nothing is charged there.

Exit 0 always: this compares designs.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_math as fm

FEE = 30
SCALE = 10_000
W1 = [1]


def vault_mint(res, deposit, total_lp, charge_entry: bool):
    """LP minted for a single-asset deposit, with or without the entry fee."""
    fee = FEE if charge_entry else 0
    eff = fm.effective_amounts([res], [deposit], fee, W1)
    if eff[0] <= 0 or res <= 0:
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


def round_trip(design, start_res, start_lp, deposit, others_hold=0):
    """Deposit, then immediately withdraw everything minted. What comes back?"""
    charge_entry = design in ("A", "C")
    charge_exit = design in ("A", "B")
    res, tot = start_res, start_lp
    minted = vault_mint(res, deposit, tot, charge_entry)
    if minted <= 0:
        return None
    res += deposit
    tot += minted
    # withdraw all of it in one removal
    gross = (res * minted) // tot
    if charge_exit:
        pay = (gross * (SCALE - FEE)) // SCALE
    else:
        pay = gross
    return pay, minted, deposit


print("=" * 94)
print("1. A sole holder deposits, then immediately withdraws. Do they end where they began?")
print("=" * 94)
print("""
   The pool starts at its floor: 1,000 reserve against 1,000 LP, all of it at the burn
   address. Alice deposits 1,000,000 and is then essentially the whole pool.
""")
print(f"   {'design':14s} {'deposited':>12} {'returned':>12} {'difference':>12} {'round trip':>12}")
for design, label in (("A", "A both"), ("B", "B exit-accrued"), ("C", "C entry-only")):
    pay, minted, dep = round_trip(design, 1_000, 1_000, 1_000_000)
    print(f"   {label:14s} {dep:>12,} {pay:>12,} {pay - dep:>12,} {pay / dep:>11.4%}")
print("""
   A charges twice and returns least. B charges once and still loses it, because the exit
   fee is routed away from the pool Alice owns. Only C returns her deposit: the entry fee
   landed in a pool she then held all of, so it came back with the rest.

   That is the argument made plainly -- a fee paid INTO a pool you are part of is not a
   cost, it is a transfer to yourself plus the other holders in proportion.
""")

print("=" * 94)
print("2. Does C still charge someone who should pay?")
print("=" * 94)
print("""
   The worry with entry-only: does a trader crossing the vault get in free? No -- they pay
   on the way in, and the payment lands with whoever is already there.
""")
print(f"   {'existing holders':>18} {'design':14s} {'newcomer returns':>18} {'they paid':>11}")
for others in (0, 500_000, 5_000_000):
    for design, label in (("A", "A both"), ("C", "C entry-only")):
        res = 1_000 + others
        tot = 1_000 + others
        out = round_trip(design, res, tot, 1_000_000)
        pay, minted, dep = out
        print(f"   {others:>18,} {label:14s} {pay:>18,} {dep - pay:>10,}")
    print()
print("""   With other holders present, a newcomer who enters and leaves pays in both designs --
   under C the charge is simply taken once, on entry, and shared with the holders who were
   already there. That is exactly who the fee is meant to reward.
""")

print("=" * 94)
print("3. Is there anything left to recover by splitting under C?")
print("=" * 94)
res, tot = 238_571, 238_571
mine = tot - 1


def exit_in_pieces(design, res, tot, mine, k):
    charge_exit = design in ("A", "B")
    r, t, left, got = res, tot, mine, 0
    per = max(1, mine // k)
    for i in range(k):
        b = per if i < k - 1 else left
        if b <= 0 or b >= t:
            break
        gross = (r * b) // t
        pay = (gross * (SCALE - FEE)) // SCALE if charge_exit else gross
        got += pay
        r -= pay if design == "A" else gross
        t -= b
        left -= b
    return got


print(f"\n   {'design':14s} {'exit in 1':>12} {'exit in 100':>13} {'gain from splitting':>21}")
for design, label in (("A", "A both"), ("C", "C entry-only")):
    one = exit_in_pieces(design, res, tot, mine, 1)
    many = exit_in_pieces(design, res, tot, mine, 100)
    print(f"   {label:14s} {one:>12,} {many:>13,} {many - one:>21,}")
print("""
   Under C there is no exit fee, so there is nothing to claw back and splitting gains
   nothing. The behaviour that looked like an exploit disappears because the thing it was
   exploiting is gone.
""")

print("=" * 94)
print("WHERE THIS LEAVES THE RECOMMENDATION")
print("=" * 94)
print("""
   My earlier proposal -- route the exit fee to fees_owed -- closes the splitting gap and
   makes the sole-holder round trip WORSE, because it moves the fee somewhere the payer
   can never reach. It answers the symptom.

   Charging on entry only answers the cause. A fee that stays in the reserve is only fair
   if the payer is still in the pool afterwards, and on the way out they are not. Entry is
   the moment where "the fee lands in a pool you are part of" is actually true.

   What it changes: a vault currently charges a round trip twice. Under C it charges once.
   That is a reduction in vault revenue and should be called that, not dressed up as a
   pure fix.

   One thing to check before building it: `effective_amounts` already charges the whole
   deposit for a vault (fee_base = deposit, not excess), so the entry side needs no change
   at all. C is subtraction -- stop charging on the way out -- rather than new machinery.
""")
