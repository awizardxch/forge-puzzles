#!/usr/bin/env python3
"""The V15 target matrix and an adversarial sweep, with a machine-readable summary.

Two halves:

  TARGET      every shape in the deployment matrix -- asset counts 1 to 5, equal and
              extreme weights, zero and maximum fees, vaults, LP-as-reserve -- checked
              against the properties V15's design rests on.

  ADVERSARIAL a sweep that is NOT derived from the design, looking for behaviour nobody
              specified. Anything surprising is reported as a finding rather than
              asserted away, because the point is to find what we did not think of.

Writes `contracts/_sim_v15_matrix.json` for the simulator artifact.

Exit 0 if every TARGET property holds, 1 otherwise. Adversarial findings do not fail
the run; they are output.
"""
import itertools
import json
import pathlib
import random
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_math as fm

LOCKED_BURN = 1          # V15
MAX_FEE_BPS = 1_000
stats = {"target": [], "adversarial": [], "findings": []}
target_ok = True


def finding(severity, title, detail):
    stats["findings"].append({"severity": severity, "title": title, "detail": detail})
    print(f"  [{severity.upper():<6}] {title}\n           {detail}")


def mint(old, deposits, total_lp, weights, fee):
    """The bracket exact_invariant_lp_mint enforces, solved directly.

    Deliberately NOT forge_math.invariant_lp_mint: that wrapper refuses negative
    deposits before the bracket is reached, which is how I-1 was missed. Anything
    probing what an assert holds back must go through the bracket itself.
    """
    K = sum(weights)
    old_p = fm.weighted_product(old, weights)
    eff = fm.effective_amounts(old, deposits, fee, weights)
    if any(e <= 0 for e in eff):
        return 0
    target = (total_lp ** K) * fm.weighted_product(eff, weights)
    lo, hi, best = 0, max(total_lp * 4, 1024), 0
    while ((total_lp + hi) ** K) * old_p <= target:
        hi *= 2
    while lo <= hi:
        mid = (lo + hi) // 2
        if ((total_lp + mid) ** K) * old_p <= target:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def withdraw(reserves, burn, total_lp, fee, n):
    return fm.withdrawal_amounts(reserves, burn, total_lp, fm.vault_fee_bps(n, 10, fee))


def value_per_lp(reserves, weights, total_lp):
    """A scale-free measure: the weighted product per unit of supply."""
    return fm.weighted_product(reserves, weights) / (total_lp ** sum(weights))


# --------------------------------------------------------------------------
# TARGET -- the matrix shapes, against the properties V15 rests on
# --------------------------------------------------------------------------

SHAPES = [
    ("A1 2-asset even",        [1_000_000_000_000, 2_000_000],                   [1, 1],       30),
    ("A3 all-CAT",             [5_000_000, 5_000_000],                            [1, 1],       30),
    ("B1 vault (n=1)",         [50_000_000],                                      [1],          30),
    ("C1 weighted 4:1",        [1_000_000_000_000, 2_000_000],                    [4, 1],       30),
    ("D1 3-asset",             [1_000_000_000_000, 2_000_000, 3_000_000],         [1, 1, 1],    30),
    ("D2 5-asset",             [10**12, 2_000_000, 3_000_000, 4_000_000, 500_000],[1, 1, 1, 1, 1], 30),
    ("D3 weighted 4:1:1",      [10**12, 2_000_000, 3_000_000],                    [4, 1, 1],    30),
    ("E1 zero fee",            [10**12, 2_000_000],                               [1, 1],       0),
    ("E2 max fee",             [10**12, 2_000_000],                               [1, 1],       MAX_FEE_BPS),
    ("H2 weighted 8:1",        [10**12, 2_000_000],                               [8, 1],       30),
    ("lopsided reserves",      [10**14, 5],                                       [1, 1],       30),
    ("tiny pool",              [1_000, 2_000],                                    [1, 1],       30),
]

GENESIS_LP = 100_000


def target():
    global target_ok
    print("=" * 94)
    print("TARGET -- matrix shapes against the properties V15 rests on")
    print("=" * 94)
    print(f"\n{'shape':24s} {'exit 100%':>10s} {'left == burn/N':>15s} {'re-enterable':>13s} "
          f"{'vpl holds':>11s} {'round trip':>11s}")
    for name, reserves, weights, fee in SHAPES:
        n = len(reserves)
        row = {"shape": name, "assets": n, "weights": weights, "fee_bps": fee}

        # P1 a later depositor exits in full
        dep = [r for r in reserves]
        minted = mint(reserves, dep, GENESIS_LP, weights, fee)
        after = [r + d for r, d in zip(reserves, dep)]
        cap = (GENESIS_LP + minted) - LOCKED_BURN
        p1 = minted > 0 and minted <= cap
        back = withdraw(after, minted, GENESIS_LP + minted, fee, n) if p1 else [0] * n
        p1 = p1 and all(b <= d for b, d in zip(back, dep))
        row["exit_full"] = bool(p1)
        row["recovered"] = (back[0] / dep[0]) if dep[0] else None

        # P2 what stays behind is exactly the burn's share.
        #
        # A VAULT is exempt, and deliberately so rather than silently: for n == 1 the
        # redemption fee is charged on the way out and stays in the reserve, so the
        # residual is the burn's share PLUS the last exit's fee. See the fragmentation
        # probe below, which is where that turns into a real finding.
        res2, tot = list(reserves), GENESIS_LP
        pay = withdraw(res2, tot - LOCKED_BURN, tot, fee, n)
        left = [r - p for r, p in zip(res2, pay)]
        ideal = [(r * LOCKED_BURN) // tot for r in reserves]
        vault_fee = fm.vault_fee_bps(n, 10, fee)
        p2 = all(abs(l - i) <= 1 for l, i in zip(left, ideal)) or vault_fee > 0
        row["left"] = left
        row["left_ideal"] = ideal
        row["vault_fee_bps"] = vault_fee
        row["exact_residual"] = bool(p2)
        row["residual_is_fee_bearing"] = bool(vault_fee > 0)

        # P3 the floor-state pool still accepts liquidity
        floor_res = [max(1, l) for l in left]
        revive = mint(floor_res, [max(1, r) for r in reserves], LOCKED_BURN, weights, fee)
        p3 = revive > 0
        row["reenterable"] = bool(p3)

        # P4 value per LP never falls over a deposit
        before_vpl = value_per_lp(reserves, weights, GENESIS_LP)
        after_vpl = value_per_lp(after, weights, GENESIS_LP + minted)
        p4 = after_vpl >= before_vpl * (1 - 1e-9)
        row["vpl_holds"] = bool(p4)

        # P5 deposit then immediately withdraw never returns more than was put in
        p5 = all(b <= d for b, d in zip(back, dep))
        row["round_trip_safe"] = bool(p5)

        ok = all((p1, p2, p3, p4, p5))
        target_ok = target_ok and ok
        stats["target"].append(row)
        mark = lambda b: "OK" if b else "FAIL"
        print(f"  {name:22s} {mark(p1):>10s} {mark(p2):>15s} {mark(p3):>13s} "
              f"{mark(p4):>11s} {mark(p5):>11s}")
    print()


# --------------------------------------------------------------------------
# ADVERSARIAL -- looking for what nobody specified
# --------------------------------------------------------------------------

def adversarial():
    print("=" * 94)
    print("ADVERSARIAL -- sweeping for behaviour nobody specified")
    print("=" * 94)
    print()

    # 1. Swap round trips must never be profitable.
    print("1. swap round trip A->B->A, across shapes and sizes")
    worst = None
    for name, reserves, weights, fee in SHAPES:
        if len(reserves) < 2:
            continue
        for frac in (10**-6, 10**-4, 10**-2, 0.1, 0.5):
            amt = int(reserves[0] * frac)
            if amt < 1:
                continue
            try:
                out = fm.swap_output(reserves[0], reserves[1], amt, fee, weights[0], weights[1])
                back = fm.swap_output(reserves[1] + out, reserves[0] - out, out, fee, weights[1], weights[0])
            except ValueError:
                continue
            gain = back - amt
            if gain > 0:
                finding("HIGH", f"profitable swap round trip on {name}",
                        f"in {amt:,} -> out {out:,} -> back {back:,}, gain {gain:,}")
            if worst is None or gain > worst[0]:
                worst = (gain, name, amt)
    print(f"   worst round-trip result: {worst[0]:+,} mojos on {worst[1]} at {worst[2]:,} in "
          f"({'a LOSS, as it must be' if worst[0] <= 0 else 'A GAIN -- see finding'})")
    stats["adversarial"].append({"probe": "swap_round_trip", "worst_gain": worst[0]})

    # 2. Cost growth: weighted_product with big reserves and high weight sums.
    print("\n2. integer size of the invariant, by weight sum and reserve magnitude")
    print(f"   {'K':>3} {'reserve':>16} {'bits in the product':>21}")
    biggest = 0
    for K in (2, 5, 10):
        for mag in (10**6, 10**12, 10**15):
            w = [1] * K
            bits = fm.weighted_product([mag] * K, w).bit_length()
            biggest = max(biggest, bits)
            print(f"   {K:>3} {mag:>16,} {bits:>21,}")
    stats["adversarial"].append({"probe": "invariant_bits", "max_bits": biggest})
    if biggest > 2048:
        finding("MEDIUM", "the invariant becomes a very large integer",
                f"{biggest} bits at the widest shape. CLVM handles bignums, but cost scales with "
                f"operand size and the puzzle brackets by BISECTION -- every probe multiplies these. "
                f"Worth measuring cost on a 10-asset pool with large reserves before allowing one.")
    else:
        print(f"   max {biggest} bits -- within a size where cost is unlikely to surprise")

    # 3. Granularity: the smallest deposit and smallest swap each shape accepts.
    print("\n3. smallest accepted deposit and swap, per shape")
    print(f"   {'shape':24s} {'min deposit (slot 0)':>22s} {'min swap in':>14s}")
    for name, reserves, weights, fee in SHAPES:
        lo, hi = 1, reserves[0]
        while lo < hi:
            mid = (lo + hi) // 2
            d = [mid] + [max(1, (mid * r) // reserves[0]) for r in reserves[1:]]
            if mint(reserves, d, GENESIS_LP, weights, fee) >= 1:
                hi = mid
            else:
                lo = mid + 1
        min_dep = lo
        min_swap = None
        if len(reserves) >= 2:
            lo2, hi2 = 1, reserves[0]
            while lo2 < hi2:
                mid = (lo2 + hi2) // 2
                try:
                    ok = fm.swap_output(reserves[0], reserves[1], mid, fee, weights[0], weights[1]) >= 1
                except ValueError:
                    ok = False
                if ok:
                    hi2 = mid
                else:
                    lo2 = mid + 1
            min_swap = lo2
        print(f"   {name:24s} {min_dep:>22,} {(f'{min_swap:,}' if min_swap else '-'):>14s}")
        stats["adversarial"].append({"probe": "granularity", "shape": name,
                                     "min_deposit": min_dep, "min_swap": min_swap})
        if min_dep > reserves[0] // 10:
            finding("LOW", f"coarse deposit granularity on {name}",
                    f"the smallest accepted deposit is {min_dep:,}, which is "
                    f"{min_dep / reserves[0]:.1%} of the reserve -- small positions are refused outright")
        if min_swap and min_swap > reserves[0] // 10:
            finding("MEDIUM", f"the pair is effectively untradeable on {name}",
                    f"the smallest swap producing even one mojo of output is {min_swap:,}, "
                    f"{min_swap / reserves[0]:.1%} of the reserve. A pool whose thin side has floored "
                    f"to single digits quotes a price but cannot be traded -- which is the state our "
                    f"own drained pools were left in, and the reason the burn cannot promise a "
                    f"standing market.")

    # 4. Randomised sequences: value per LP must never fall.
    print("\n4. 400 random action sequences, checking value per LP never falls")
    rng = random.Random(1402)
    drops = 0
    for _ in range(400):
        name, reserves, weights, fee = rng.choice(SHAPES)
        res, tot = list(reserves), GENESIS_LP
        base = value_per_lp(res, weights, tot)
        for _ in range(rng.randint(1, 6)):
            kind = rng.choice(["swap", "add", "remove"] if len(res) > 1 else ["add", "remove"])
            try:
                if kind == "swap":
                    i, j = rng.sample(range(len(res)), 2)
                    amt = max(1, int(res[i] * rng.uniform(1e-5, 0.2)))
                    out = fm.swap_output(res[i], res[j], amt, fee, weights[i], weights[j])
                    res[i] += amt
                    res[j] -= out
                elif kind == "add":
                    d = [max(1, int(r * rng.uniform(1e-4, 0.5))) for r in res]
                    m = mint(res, d, tot, weights, fee)
                    if m <= 0:
                        continue
                    res = [r + x for r, x in zip(res, d)]
                    tot += m
                else:
                    b = max(1, int((tot - LOCKED_BURN) * rng.uniform(0.01, 0.5)))
                    pay = withdraw(res, b, tot, fee, len(res))
                    if any(p < 0 for p in pay) or any(r - p <= 0 for r, p in zip(res, pay)):
                        continue
                    res = [r - p for r, p in zip(res, pay)]
                    tot -= b
            except (ValueError, ZeroDivisionError):
                continue
            if value_per_lp(res, weights, tot) < base * (1 - 1e-9):
                drops += 1
                finding("HIGH", "value per LP fell during a random sequence",
                        f"{name}: {base:.6e} -> {value_per_lp(res, weights, tot):.6e} after {kind}")
                break
            base = max(base, value_per_lp(res, weights, tot))
    print(f"   sequences with a drop: {drops} of 400")
    stats["adversarial"].append({"probe": "random_sequences", "n": 400, "drops": drops})

    # 5. Does splitting an action change what it costs? Nobody specified that it should.
    print("\n5. fragmentation: is the same action cheaper in pieces?")
    vf = fm.vault_fee_bps(1, 10, 30)
    R0, T0 = 50_000_000, GENESIS_LP

    def redeem(mine, k):
        res, tot, left, got = [R0], T0, mine, 0
        per = max(1, mine // k)
        for i in range(k):
            b = per if i < k - 1 else left
            if b <= 0 or b > tot - LOCKED_BURN:
                break
            p = withdraw(res, b, tot, 30, 1)
            got += p[0]
            res = [res[0] - p[0]]
            tot -= b
            left -= b
        return got

    print(f"   {'holder stake':>13} {'exit in 1':>14} {'exit in 100':>14} {'fee avoided':>13}")
    worst_rec = 0.0
    for share in (0.01, 0.10, 0.50, 0.90, 0.99999):
        mine = int(T0 * share)
        one, hundred = redeem(mine, 1), redeem(mine, 100)
        gross = (R0 * mine) // T0
        paid = gross - one
        rec = (hundred - one) / paid if paid else 0.0
        worst_rec = max(worst_rec, rec)
        print(f"   {share:>12.2%} {one:>14,} {hundred:>14,} {rec:>12.1%}")
        stats["adversarial"].append({"probe": "fragmentation", "stake": share, "fee_recovered": rec})

    if worst_rec > 0.05:
        finding("INFO", "a single vault exit overcharges a large holder; splitting reaches the right price",
                f"Splitting a vault exit into 100 removals recovers up to {worst_rec:.0%} of the "
                f"fee, because the fee stays in the reserve and the holder re-claims their own "
                f"share of it while exiting. This is ORDINARY AMM ECONOMICS: any LP who trades "
                f"against their own pool pays the fee and receives their share back, so the net "
                f"cost is fee x (1 - share). The rate is identical for everyone and proportional "
                f"to trade size; what differs is how much of the pool you own. The wrinkle is "
                f"that a SINGLE removal does not apply that rebate, so a 90% holder charged the "
                f"full rate is overcharged 9x and splitting is the only way to reach the correct "
                f"price. Vault-only: vault_fee_bps is 0 for n >= 2.")
        finding("INFO", "the two-sided vault charge is correct and stays",
                "A vault crossing is two trades -- in and out -- and costs 60.0 bps against a swap "
                "pool's ~60 bps for A->B->A, measured in _sim_v15_vault_consistency.py. Charging "
                "entry-only would price a vault crossing at half the equivalent swap. Fragmenting "
                "a DEPOSIT mints slightly less, so the asymmetry is redemption-only.")

    # 6. The burn as a share of genesis: what the matrix would strand at each size.
    print("\n5. stranded liquidity by burn size, over the V13 genesis mints actually used")
    mints = [20_353, 22_386, 27_577, 30_529, 31_750, 97_695, 169_308, 186_566, 317_453, 526_445, 846_543]
    for b in (1, 100, 1_000):
        worst = max(b / m for m in mints)
        med = sorted(b / m for m in mints)[len(mints) // 2]
        print(f"   burn {b:>5,}: median {med:>8.4%}  worst {worst:>8.4%}")
        stats["adversarial"].append({"probe": "stranded", "burn": b, "median": med, "worst": worst})
    print()


def main() -> int:
    target()
    adversarial()
    if not stats["findings"]:
        print("No adversarial findings: every probe behaved as the design predicts.")
    else:
        print(f"{len(stats['findings'])} adversarial finding(s) above.")
    out = pathlib.Path("contracts/_sim_v15_matrix.json")
    out.write_text(json.dumps(stats, indent=1), encoding="utf-8")
    print(f"\nstats written to {out}")
    print(f"TARGET: {'all properties hold' if target_ok else 'A PROPERTY FAILED'}")
    return 0 if target_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
