#!/usr/bin/env python3
"""REJECTED PROPOSAL, kept as a record. Do not build this.

Routing the exit fee to fees_owed closes the splitting behaviour -- and the splitting
behaviour turned out not to be a defect. An LP who trades against their own pool is
supposed to receive their share of the fee back; this proposal would have removed the
only path to that price and made the overcharge permanent. See
_sim_v15_vault_consistency.py for the measurement that settles it.

Closing the vault redemption-fee gap: route the fee OUT instead of leaving it in.

Two corrections to the earlier analysis are what make this necessary:

  * A zero network fee is VALID. The node refuses a NONZERO fee below five mojos per
    cost; zero is fine. So splitting a redemption costs nothing but mempool patience,
    and the "break-even position size" bound does not exist.
  * The fee in question is the pool's own LP fee: `vault_fee_bps` returns `fee_bps` for
    a single-asset pool from V8, because for a vault the crossing IS the trade.

Why splitting recovers it today: the fee stays inside `reserves[i]`, so value per LP
rises, and the exiting holder still holds LP and re-claims their share of it. Do that
enough times and you collect almost all of your own fee back.

The fix is not to charge "per position" -- the puzzle has no identity to accumulate
against, and a holder is just whoever presents LP. The fix is to make the fee
NON-RECLAIMABLE by moving it where a holder has no claim: `fees_owed`, the accrual the
protocol fee already uses, which sits inside the reserve coin but outside `reserves[i]`.

Exit 0 if the fix closes the gap, 1 otherwise.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_math as fm

FEE_BPS = 30
SCALE = 10_000
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def redeem_today(reserve, total_lp, mine, k):
    """As built: the fee stays in the reserve, so value per LP rises behind the exit."""
    res, tot, left, got = reserve, total_lp, mine, 0
    per = max(1, mine // k)
    for i in range(k):
        b = per if i < k - 1 else left
        if b <= 0 or b >= tot:
            break
        pay = fm.withdrawal_amounts([res], b, tot, FEE_BPS)[0]
        got += pay
        res -= pay            # only the PAYOUT leaves; the fee stays behind
        tot -= b
        left -= b
    return got, res


def redeem_accrued(reserve, total_lp, mine, k):
    """Proposed: the fee is moved to fees_owed, so `reserves` falls by the whole gross.

    The coin still holds it -- fees_owed lives inside the reserve coin -- but it is no
    longer part of the tradable reserve, so it does not lift value per LP and the exiting
    holder has no claim on it.
    """
    res, tot, left, got, owed = reserve, total_lp, mine, 0, 0
    per = max(1, mine // k)
    for i in range(k):
        b = per if i < k - 1 else left
        if b <= 0 or b >= tot:
            break
        gross = (res * b) // tot
        pay = (gross * (SCALE - FEE_BPS)) // SCALE
        fee = gross - pay
        got += pay
        owed += fee
        res -= gross          # the WHOLE gross leaves `reserves`; the fee becomes owed
        tot -= b
        left -= b
    return got, res, owed


RESERVE, TOTAL_LP = 238_571, 238_571

print("=" * 92)
print("The gap, and whether moving the fee to fees_owed closes it")
print("=" * 92)
print(f"\n   vault: {RESERVE:,} reserve, {TOTAL_LP:,} LP, {FEE_BPS} bps LP fee\n")
print(f"   {'stake':>7} {'pieces':>7} {'today: recovered':>18} {'accrued: recovered':>20}")

worst_today, worst_fixed = 0.0, 0.0
for share in (0.10, 0.50, 0.90, 0.99999):
    mine = int(TOTAL_LP * share)
    for k in (1, 10, 100):
        one_t, _ = redeem_today(RESERVE, TOTAL_LP, mine, 1)
        many_t, _ = redeem_today(RESERVE, TOTAL_LP, mine, k)
        one_a, _, _ = redeem_accrued(RESERVE, TOTAL_LP, mine, 1)
        many_a, _, _ = redeem_accrued(RESERVE, TOTAL_LP, mine, k)
        gross = (RESERVE * mine) // TOTAL_LP
        paid_t, paid_a = gross - one_t, gross - one_a
        rec_t = (many_t - one_t) / paid_t if paid_t else 0.0
        rec_a = (many_a - one_a) / paid_a if paid_a else 0.0
        worst_today = max(worst_today, rec_t)
        worst_fixed = max(worst_fixed, rec_a)
        if k > 1:
            print(f"   {share:>6.0%} {k:>7} {rec_t:>17.1%} {rec_a:>19.1%}")

print()
check("today, splitting recovers most of the fee", worst_today > 0.5, f"up to {worst_today:.1%}")
check("with the fee accrued instead, splitting recovers nothing", abs(worst_fixed) < 0.005,
      f"{worst_fixed:.3%}")

# And the honest exit must still be unchanged for a single removal.
one_t, _ = redeem_today(RESERVE, TOTAL_LP, TOTAL_LP - 1, 1)
one_a, _, owed = redeem_accrued(RESERVE, TOTAL_LP, TOTAL_LP - 1, 1)
check("a single, honest redemption pays the same either way", one_t == one_a,
      f"{one_t:,} both")
print(f"\n   what changes is where the fee goes: today it stays in `reserves` and lifts")
print(f"   value per LP; accrued, {owed:,} becomes fees_owed and leaves through `collect`.")

print("""
=========================================================================================
WHAT THIS COSTS, STATED PLAINLY
=========================================================================================

   The vault LP fee currently benefits the REMAINING holders -- that is what "stays in the
   reserve" means. Routing it to fees_owed sends it to the protocol fee recipient instead.
   That is a real economic change and not a pure bug fix.

   The argument for making it anyway: the fee does not reliably benefit remaining holders
   today. A large holder recovers most of their own, a small one recovers none, and what
   the small holder forfeits accrues to the large holders who stayed. It is already a
   transfer -- just an arbitrary one, decided by who knows to split.

   Three options, honestly weighted:

   A. ACCRUE IT (this file). Closes the gap completely and uses machinery that already
      exists. Changes the beneficiary from remaining LPs to the fee recipient.

   B. DROP THE VAULT FEE. Vault entry and exit become free. Defensible -- the fee exists
      because the crossing is the trade -- but it changes every vault's economics and
      removes the only charge on single-asset pools.

   C. LEAVE IT. No longer defensible on cost grounds: a zero network fee is valid, so
      splitting is free and the exploit has no size threshold. The only remaining argument
      is that it redistributes between LPs rather than draining the pool.

   A is the recommendation. It is a small change to a leaf that is already being revised,
   and it makes the fee do what it says.
""")


def main() -> int:
    passed = sum(1 for r in results if r)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
