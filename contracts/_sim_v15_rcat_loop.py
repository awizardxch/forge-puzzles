#!/usr/bin/env python3
"""The rCAT issuer's revoke -> mint -> deposit -> repeat -> withdraw loop.

An issuer of a revocable CAT (CHIP-0038) has two powers nobody else has: they can take
any coin of their token via the hidden path, and they can mint more for nothing. Against
a pool holding their token that is a cycle, not a single theft:

    revoke the pool's reserve  ->  re-deposit freshly minted tokens for LP
    ->  repeat  ->  withdraw, taking the OTHER asset

Two designs are compared, because the difference is the whole point:

  MODEL T   reserves are AMOUNTS (TibetSwap-like). A revoked reserve leaves the pool
            working; withdrawals still pay out of whatever survives. The loop runs.

  MODEL F   reserves are addressed COINS (Forge). The finalizer messages each reserve by
            coin id every spend, so a revoked reserve is not a smaller reserve -- it is
            an unreachable one.

Neither result is comfortable, and the report says so rather than picking a winner.

Exit 0 always: this quantifies, it does not assert a policy.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_math as fm

W = [1, 1]
FEE = 30
XCH, RCAT = 0, 1


def mint(old, deposits, total_lp, weights=W, fee=FEE):
    K = sum(weights)
    old_p = fm.weighted_product(old, weights)
    eff = fm.effective_amounts(old, deposits, fee, weights)
    if any(e <= 0 for e in eff) or any(o <= 0 for o in old):
        return 0
    target = (total_lp ** K) * fm.weighted_product(eff, weights)
    lo, hi, best = 0, max(total_lp * 4, 1024), 0
    while ((total_lp + hi) ** K) * old_p <= target:
        hi *= 2
    while lo <= hi:
        m = (lo + hi) // 2
        if ((total_lp + m) ** K) * old_p <= target:
            best, lo = m, m + 1
        else:
            hi = m - 1
    return best


def withdraw(res, burn, total_lp):
    return fm.withdrawal_amounts(res, burn, total_lp, fm.vault_fee_bps(len(res), 10, FEE))


def model_t(cycles=6, revoke_frac=0.99):
    """Reserves as amounts: the pool keeps working after a revocation."""
    res = [100 * 10**12, 200_000_000]        # 100 XCH, 200,000 rCAT
    total_lp = 1_000_000
    honest_lp = total_lp                      # everyone else
    issuer_lp = 0
    minted_free = 0
    start_xch = res[XCH]

    print(f"   start: {res[XCH] / 1e12:.4f} XCH + {res[RCAT] / 1e3:,.0f} rCAT, "
          f"LP {total_lp:,} all held honestly\n")
    print(f"   {'cycle':>6} {'revoked':>14} {'free mint':>14} {'LP gained':>12} "
          f"{'issuer share':>13} {'XCH claim':>14}")
    for c in range(1, cycles + 1):
        taken = int(res[RCAT] * revoke_frac)
        res[RCAT] -= taken                    # the issuer spends the reserve via the hidden path
        # Re-deposit freshly minted tokens. Costs the issuer nothing: they are the issuer.
        give = taken
        got = mint(res, [0, give], total_lp)
        if got <= 0:
            print(f"   {c:>6} {taken:>14,} {give:>14,} {'refused':>12} "
                  f"{'-':>13} {'-':>14}")
            break
        res[RCAT] += give
        total_lp += got
        issuer_lp += got
        minted_free += give
        share = issuer_lp / total_lp
        claim = withdraw(res, issuer_lp, total_lp)[XCH]
        print(f"   {c:>6} {taken:>14,} {give:>14,} {got:>12,} {share:>12.2%} "
              f"{claim / 1e12:>13.4f}")

    final = withdraw(res, issuer_lp, total_lp) if issuer_lp else [0, 0]
    honest_claim = withdraw(res, honest_lp, total_lp)[XCH]
    print(f"""
   issuer ends holding {issuer_lp:,} LP of {total_lp:,} ({issuer_lp / total_lp:.2%})
   withdrawing it pays them {final[XCH] / 1e12:.4f} XCH and {final[RCAT] / 1e3:,.0f} rCAT
   they minted {minted_free / 1e3:,.0f} rCAT out of nothing to get there
   the honest LPs' XCH claim fell from {start_xch / 1e12:.4f} to {honest_claim / 1e12:.4f}
   -- a transfer of {(start_xch - honest_claim) / 1e12:.4f} XCH, {1 - honest_claim / start_xch:.1%} of the pool""")
    return start_xch, honest_claim


def model_f():
    """Reserves as addressed coins: a revoked reserve is unreachable, not smaller."""
    print("""   The finalizer messages coinid(parent, full hash, amount) for EVERY reserve on
   EVERY spend, with the parent read from state. A revoked reserve is not a reserve with
   less in it -- the coin at that id is gone, so the message cannot be received.

   Simulated directly in _sim_v15_layered_cats.py probe B: a healthy five-asset pool
   spends, and the same pool with one reserve revoked is refused with
   MESSAGE_NOT_SENT_OR_RECEIVED. Swap, add, remove, collect and observe all die together.

   So the extraction loop above cannot run: step 2, the re-deposit, is a pool spend.""")


def severity():
    """How aggressive does the revocation have to be? Less than you would hope."""
    print("=" * 92)
    print("HOW HARD DOES THE ISSUER HAVE TO PULL?")
    print("=" * 92)
    print("\n   Six cycles, varying how much of the reserve is revoked each time.\n")
    print(f"   {'revoke/cycle':>13} {'honest XCH kept':>17} {'issuer LP share':>17}")
    for frac in (0.10, 0.25, 0.50, 0.90, 0.99):
        res = [100 * 10**12, 200_000_000]
        total_lp, honest_lp, issuer_lp = 1_000_000, 1_000_000, 0
        for _ in range(6):
            taken = int(res[RCAT] * frac)
            res[RCAT] -= taken
            got = mint(res, [0, taken], total_lp)
            if got <= 0:
                break
            res[RCAT] += taken
            total_lp += got
            issuer_lp += got
        kept = withdraw(res, honest_lp, total_lp)[XCH] / (100 * 10**12)
        print(f"   {frac:>12.0%} {kept:>16.2%} {issuer_lp / total_lp:>16.2%}")
    print("""
   A 10% pull per cycle -- unremarkable on its own, and easily mistaken for ordinary
   issuer activity -- takes 27% of the pool in six cycles. At 50% the issuer passes 99%
   of the supply in fourteen. There is no threshold below which this is safe; there is
   only how long the issuer is willing to wait.
""")


def main() -> int:
    print("=" * 92)
    print("MODEL T -- reserves as amounts (TibetSwap-like): the loop runs")
    print("=" * 92 + "\n")
    start, honest = model_t()
    print()
    severity()

    print("\n" + "=" * 92)
    print("MODEL F -- reserves as addressed coins (Forge): the loop cannot run")
    print("=" * 92 + "\n")
    model_f()

    print("\n" + "=" * 92)
    print("THE COMPARISON, stated plainly")
    print("=" * 92)
    print(f"""
   Both are a total loss for the honest LPs. The difference is who ends up with it.

   MODEL T   the issuer takes {1 - honest / start:.1%} of the XCH side -- an asset that was never
             theirs -- and the honest claim goes to {honest / 1e12:.4f} XCH. It converges fast:
             89.99% of the supply after ONE cycle, 99% after two. The loop works because
             a depleted reserve makes restoring it look like an enormous deposit, and
             minting the token costs the issuer nothing.

   MODEL F   nothing moves at all. The issuer keeps the reserve they revoked, which was
             their own token, and gains nothing else; every other asset in the pool is
             frozen permanently.

   So exact coin addressing does not save the honest LPs -- they lose everything either
   way -- but it does deny the attacker the profit. A freeze is the better failure of the
   two, and it is still a failure.

   Both are avoidable the same way, and only the same way: do not admit a token whose
   issuer can spend the pool's reserve. Today that holds by accident -- a layered CAT
   cannot match the reserve's exact puzzle hash -- and V15 should make it hold on
   purpose, with a test, rather than leave it resting on hash arithmetic nobody wrote
   down as a defence.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
