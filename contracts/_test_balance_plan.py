#!/usr/bin/env python3
"""Several balancing cycles settled together, as one Offer.

A single cycle is a multi-hop whose path closes on itself. Several at once are a
split whose every branch closes on itself: one entry coin of TXCH is divided
between them, each crosses its own pools, and all of them pay back into one exit.

That shape needs no new lane. It also needs no new trust: the Offer requests the
whole return, so the requested amount asserts the combined profit. If any branch
fails to produce its share the bundle cannot satisfy the payment and nothing
settles -- the balancer never half-executes.

Branches must be pool-disjoint, because a pool advances once per bundle and a
second branch touching it would price against reserves the first already moved.

SUPERSEDED by _test_forge_cycles.py, which builds its own pools at the shipping
revision instead of loading whatever happens to be in the deployment index. Kept
for the record; it skips when the index is empty.
"""
import sys

sys.path.insert(0, ".")

from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from _test_v6_transition import USER_PH, audit, make_offer, xch_maker_spend
from _test_route_nasset import external_for, load_pools
from forge_offer import ZERO_32
from forge_multihop_swap import _plan_legs
from forge_split_swap import SplitBranchSpec, build_split_swap

NONCE = bytes32.fromhex("ee" * 32)


def cycle_out(pool_in, pool_out, asset, amount):
    try:
        legs = _plan_legs([pool_in, pool_out], [ZERO_32, asset, ZERO_32], amount)
    except Exception:
        return None
    return legs[-1].amount_out - legs[-1].protocol_fee


def best_size(pool_in, pool_out, asset, ceiling):
    best = None
    for step in range(1, 40):
        amount = ceiling * step // 40
        out = cycle_out(pool_in, pool_out, asset, amount)
        if out is None:
            continue
        if best is None or out - amount > best[2]:
            best = (amount, out, out - amount)
    return best


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    pools = load_pools()

    # Every asset two XCH-paired pools disagree on, cheapest source first.
    quotes = {}
    for pool in pools.values():
        assets = [bytes32(a) for a in pool.config[2]]
        if ZERO_32 not in assets:
            continue
        reserves = {bytes32(r[0]): int(r[2]) for r in pool.state[0]}
        if reserves[ZERO_32] <= 0:
            continue
        for asset in assets:
            if asset == ZERO_32 or reserves[asset] <= 0:
                continue
            quotes.setdefault(asset, []).append((pool, reserves[ZERO_32] / reserves[asset]))

    # Build cycles, then keep a pool-disjoint set of the most profitable.
    # Every ordered pair, not just cheapest-to-dearest: the most profitable
    # cycle and the most profitable *disjoint* pair of cycles are different
    # problems, and picking only the extremes leaves nothing to combine.
    candidates = []
    for asset, entries in quotes.items():
        for buy, buy_price in entries:
            for sell, sell_price in entries:
                if buy.launcher_id == sell.launcher_id or buy_price >= sell_price:
                    continue
                sized = best_size(buy, sell, asset, 1_000_000_000_000)
                if sized and sized[2] > 0:
                    candidates.append((sized[2], asset, buy, sell, sized))
    candidates.sort(reverse=True, key=lambda c: c[0])

    # The best disjoint PAIR, which is what this test exercises. Note that the
    # best pair is often worth less than the single best cycle -- taking the most
    # profitable one usually consumes a pool the others need -- so a planner
    # optimising for profit will frequently choose one branch. That is correct
    # product behavior; here we want two, to prove the lane carries them.
    top = candidates[:24]
    chosen, best_total = [], 0
    for i, (p1, a1, b1, s1, z1) in enumerate(top):
        for p2, a2, b2, s2, z2 in top[i + 1:]:
            if {b1.launcher_id, s1.launcher_id} & {b2.launcher_id, s2.launcher_id}:
                continue
            if p1 + p2 > best_total:
                chosen, best_total = [(a1, b1, s1, z1), (a2, b2, s2, z2)], p1 + p2

    if len(chosen) < 2:
        print(f"only {len(chosen)} pool-disjoint cycle(s) available; need two to combine")
        return 2

    branches, total_in, total_out = [], 0, 0
    print("combined balance:")
    for asset, buy, sell, (amount, out, profit) in chosen:
        print(f"  {asset.hex()[:10]}  commit {amount / 1e12:.6f} -> {out / 1e12:.6f}"
              f"  profit {profit / 1e12:.6f} TXCH")
        branches.append(SplitBranchSpec([buy, sell], [ZERO_32, asset, ZERO_32], amount))
        total_in += amount
        total_out += out

    print(f"  total     commit {total_in / 1e12:.6f} -> {total_out / 1e12:.6f}"
          f"  profit {(total_out - total_in) / 1e12:.6f} TXCH")
    print()

    results = []
    results.append(check("the combined plan returns more than it commits", total_out > total_in))

    spends = xch_maker_spend(total_in, 0xB9)
    offer = make_offer(spends, {None: [NotarizedPayment(USER_PH, uint64(total_out), [], NONCE)]})
    try:
        result = build_split_swap(branches, offer)
    except Exception as exc:
        print(f"  [FAIL] the combined balance builds: {type(exc).__name__}: {exc}")
        return 1

    touched = [p for _, buy, sell, _ in chosen for p in (buy, sell)]
    problems, checked = audit(result.bundle, external_for(touched, spends))
    results.append(check(f"bundle audits clean ({checked} assertions, "
                         f"{len(result.bundle.coin_spends)} spends)", not problems))
    for problem in problems:
        print(f"         - {problem}")

    results.append(check(f"every pool advanced ({len(result.pools)} of {len(touched)})",
                         len(result.pools) == len(touched)))
    results.append(check("the router reports the combined output",
                         result.total_out == total_out, f"{result.total_out}"))

    # The Offer is the assertion: asking for more than the plan yields must fail.
    greedy = make_offer(xch_maker_spend(total_in, 0xBA),
                        {None: [NotarizedPayment(USER_PH, uint64(total_out + 10_000_000), [], NONCE)]})
    try:
        build_split_swap(branches, greedy)
        results.append(check("an over-asking Offer is refused", False))
    except Exception:
        results.append(check("an over-asking Offer is refused", True,
                             "one payment asserts the whole plan"))

    # Two branches sharing a pool must be refused: it advances once per bundle.
    shared = [branches[0], SplitBranchSpec(
        [chosen[0][1], chosen[0][2]], [ZERO_32, chosen[0][0], ZERO_32], 1_000_000)]
    try:
        build_split_swap(shared, offer)
        results.append(check("branches sharing a pool are refused", False))
    except Exception:
        results.append(check("branches sharing a pool are refused", True))

    # A wallet's actual settlement coin is the ground truth, not the client's
    # precomputed shares. Fee-inclusive coin selection, a stale quote, or any
    # reserve movement between quoting and signing can leave the declared
    # total a few mojos off from what the wallet really offered -- that must
    # rescale and settle, not reject the whole split outright.
    nudged = make_offer(xch_maker_spend(total_in + 3_000, 0xBB),
                        {None: [NotarizedPayment(USER_PH, uint64(1), [], NONCE)]})
    try:
        build_split_swap(branches, nudged)
        results.append(check("a wallet offering slightly more than declared still settles", True))
    except Exception as exc:
        results.append(check("a wallet offering slightly more than declared still settles", False,
                             f"{type(exc).__name__}: {exc}"))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} combined-balance checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
