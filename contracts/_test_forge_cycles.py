#!/usr/bin/env python3
"""Balancing cycles at the shipping revision.

Two pools quoting the same asset at different rates is an arbitrage, and closing
it is what leaves them level. The shape is a route whose path CLOSES ON ITSELF --
`path[0] == path[-1]` -- so the whole thing is one bundle: buy where the asset is
cheap, sell where it is dear, end up holding more of what you started with.

Several such cycles at once are a split whose every branch closes on itself: one
entry coin divided between them, each crossing its own pools, all paying back
into one exit. Branches must be pool-disjoint, because a pool advances once per
bundle and a second branch touching it would price against reserves the first
already moved.

**No separate profit assertion is needed, and that is the point.** The Offer
requests the whole return, so the requested amount IS the assertion: a bundle
that cannot produce it cannot satisfy the notarised payment, and nothing settles.
There is no window in which the balancer pays to move value between pools.

This replaces the coverage of `_test_arb_cycle` / `_test_balance_plan`, which
loaded pools from the deployment index and so tested whatever revision happened
to be deployed -- V7, latterly nothing. These build their own pools, so they test
the code that ships.
"""
import sys

sys.path.insert(0, ".")

from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from _forge_testkit import (
    NONCE, USER_PH, X, Y, Z, audit, cat_maker_spend, external_of, make_offer,
    make_pool, paid_to,
)
import forge_multihop_swap as mh
from forge_multihop_swap import _Leg, _plan_legs, build_multihop_swap
from forge_split_swap import SplitBranchSpec, build_split_swap


def cycle_out(pools, path, amount):
    """What a cycle returns, net of every protocol fee along the way."""
    legs = _plan_legs(pools, path, amount)
    return legs[-1].amount_out - legs[-1].protocol_fee


def best_size(pools, path, ceiling):
    """Largest profit over a coarse sweep; cycles are concave, so this is near enough."""
    best = None
    for step in range(1, 25):
        amount = ceiling * step // 25
        try:
            out = cycle_out(pools, path, amount)
        except Exception:
            continue
        if best is None or out - amount > best[2]:
            best = (amount, out, out - amount)
    return best


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    results = []

    # Two pools that disagree on Z: cheap in one, dear in the other.
    cheap = make_pool(sorted([X, Z]), {X: 5_000_000, Z: 10_000_000}, 0xC1)
    dear = make_pool(sorted([X, Z]), {X: 10_000_000, Z: 5_000_000}, 0xC2)

    print("a single cycle, settled as a multi-hop that closes on itself:")
    amount, out, profit = best_size([cheap, dear], [X, Z, X], 1_000_000)
    print(f"    commit {amount:,} -> {out:,}   profit {profit:+,}")
    results.append(check("the cycle returns more than it commits", profit > 0))

    spends = cat_maker_spend(X, amount, 0xD1)
    offer = make_offer(spends, {X: [NotarizedPayment(USER_PH, uint64(out), [], NONCE)]})
    try:
        result = build_multihop_swap([cheap, dear], [X, Z, X], offer)
        problems, checks = audit(result.bundle, external_of([cheap, dear], spends))
        results.append(check(f"it builds and audits clean ({checks} assertions, "
                             f"{len(result.bundle.coin_spends)} spends)", not problems))
        for p in problems:
            print(f"         - {p}")
        results.append(check("both pools advanced", len(result.pools) == 2))
        results.append(check("the whole return is paid back to the trader",
                             paid_to(result.bundle, X, USER_PH) == out,
                             f"{paid_to(result.bundle, X, USER_PH)} == {out}"))
    except Exception as exc:
        results.append(check(f"it builds: {type(exc).__name__}: {str(exc)[:70]}", False))

    # The Offer is the assertion. Asking for a mojo more than the cycle yields
    # must fail to build, or the balancer could settle at a loss.
    greedy_spends = cat_maker_spend(X, amount, 0xD2)
    greedy = make_offer(greedy_spends, {X: [NotarizedPayment(USER_PH, uint64(out + 1), [], NONCE)]})
    try:
        build_multihop_swap([cheap, dear], [X, Z, X], greedy)
        results.append(check("an over-asking Offer is refused", False))
    except Exception:
        results.append(check("an over-asking Offer is refused", True,
                             "the requested amount asserts the profit"))

    # Overcorrecting past level stops paying: the curve is concave, so pushing
    # far beyond the balance point gives back less than it takes.
    huge = amount * 8
    try:
        over = cycle_out([cheap, dear], [X, Z, X], huge)
        results.append(check("overcorrecting past level is unprofitable",
                             over - huge < profit, f"{over - huge:+,} at 8x the size"))
    except Exception:
        results.append(check("overcorrecting past level is unprofitable", True, "refused outright"))

    print()
    print("several disjoint cycles, settled together as one split:")
    # A second, independent pair so the two cycles share no pool.
    cheap2 = make_pool(sorted([X, Z]), {X: 4_000_000, Z: 9_000_000}, 0xC3)
    dear2 = make_pool(sorted([X, Z]), {X: 9_000_000, Z: 4_000_000}, 0xC4)
    amount2, out2, profit2 = best_size([cheap2, dear2], [X, Z, X], 1_000_000)
    print(f"    branch A  commit {amount:,} -> {out:,}")
    print(f"    branch B  commit {amount2:,} -> {out2:,}")

    total_in, total_out = amount + amount2, out + out2
    branches = [
        SplitBranchSpec([cheap, dear], [X, Z, X], amount),
        SplitBranchSpec([cheap2, dear2], [X, Z, X], amount2),
    ]
    split_spends = cat_maker_spend(X, total_in, 0xD3)
    split_offer = make_offer(split_spends,
                             {X: [NotarizedPayment(USER_PH, uint64(total_out), [], NONCE)]})
    try:
        result = build_split_swap(branches, split_offer)
        touched = [cheap, dear, cheap2, dear2]
        problems, checks = audit(result.bundle, external_of(touched, split_spends))
        results.append(check(f"the combined plan builds and audits clean ({checks} assertions, "
                             f"{len(result.bundle.coin_spends)} spends)", not problems))
        for p in problems:
            print(f"         - {p}")
        results.append(check("every pool advanced", len(result.pools) == len(touched),
                             f"{len(result.pools)} of {len(touched)}"))
        results.append(check("the router reports the combined return",
                             result.total_out == total_out, f"{result.total_out}"))
        results.append(check("the combined plan returns more than it commits",
                             total_out > total_in, f"{total_out - total_in:+,}"))
    except Exception as exc:
        results.append(check(f"the combined plan builds: {type(exc).__name__}: {str(exc)[:70]}", False))

    # One payment covers the whole plan, so over-asking must fail for the split too.
    greedy2 = make_offer(cat_maker_spend(X, total_in, 0xD4),
                         {X: [NotarizedPayment(USER_PH, uint64(total_out + 1), [], NONCE)]})
    try:
        build_split_swap(branches, greedy2)
        results.append(check("an over-asking combined Offer is refused", False))
    except Exception:
        results.append(check("an over-asking combined Offer is refused", True))

    # Two branches through one pool would price twice against reserves that move once.
    shared = [branches[0], SplitBranchSpec([cheap, dear], [X, Z, X], amount // 2)]
    try:
        build_split_swap(shared, split_offer)
        results.append(check("branches sharing a pool are refused", False))
    except Exception:
        results.append(check("branches sharing a pool are refused", True))

    # ── cycles that cross a vault ───────────────────────────────────────────
    #
    # A vault holds one asset and cannot trade, so its liquidity is reachable
    # only through its LP: acquire that LP on a pool which trades it, then burn
    # it at the vault for the underlying. When the LP trades BELOW the vault's
    # redemption ratio, doing both closes a cycle -- and the two halves have to
    # be one bundle, or the LP sits exposed between them at whatever price the
    # second half finds.
    #
    # This is the shape that used to be refused: `build_multihop_swap` would not
    # settle a route ENDING in a redemption, so the Balancer marked these rows
    # "No lane" and left them alone.
    print()
    print("a cycle that crosses a vault, ending in the redemption:")
    vault = make_pool([X], {X: 10_000_000}, 0xE1, protocol_bps=0)
    lp = bytes32(vault.lp_asset_id)
    reserve, supply = int(vault.state[0][0][2]), int(vault.state[1])
    # The vault's LP trading below its redemption ratio is the whole opportunity.
    pair = make_pool(sorted([X, lp]), {X: 5_000_000, lp: 6_000_000}, 0xE2)

    v_amount, v_out, v_profit = best_size([pair, vault], [X, lp, X], 400_000)
    burn = _plan_legs([pair, vault], [X, lp, X], v_amount)[-1].amount_in
    print(f"    commit {v_amount:,} -> buy {burn:,} LP -> redeem {v_out:,}"
          f"   profit {v_profit:+,}")
    results.append(check("the vault-crossing cycle returns more than it commits", v_profit > 0))

    v_spends = cat_maker_spend(X, v_amount, 0xE9)
    v_offer = make_offer(v_spends, {X: [NotarizedPayment(USER_PH, uint64(v_out), [], NONCE)]})
    try:
        result = build_multihop_swap([pair, vault], [X, lp, X], v_offer)
        problems, checks = audit(result.bundle, external_of([pair, vault], v_spends))
        results.append(check(f"it settles as ONE multi-hop and audits clean ({checks} "
                             f"assertions, {len(result.bundle.coin_spends)} spends)", not problems))
        for problem in problems:
            print(f"         - {problem}")
        results.append(check("both the pair and the vault advanced", len(result.pools) == 2))
        results.append(check("the redemption is paid to the trader, not left ephemeral",
                             paid_to(result.bundle, X, USER_PH) == v_out,
                             f"{paid_to(result.bundle, X, USER_PH)} == {v_out}"))
    except Exception as exc:
        results.append(check(f"it settles as one multi-hop: {type(exc).__name__}: "
                             f"{str(exc)[:70]}", False))

    # A vault takes its LP fee on the crossing, so redeeming N LP returns less
    # than N at the bare reserve-to-supply ratio. If it ever returned MORE, the
    # cycle would be minting value out of the vault's other holders.
    results.append(check("the vault withheld its fee on the crossing",
                         v_out < burn * reserve // supply,
                         f"{v_out:,} < {burn * reserve // supply:,}"))

    # The Offer is the assertion here too.
    greedy_v = make_offer(cat_maker_spend(X, v_amount, 0xEA),
                          {X: [NotarizedPayment(USER_PH, uint64(v_out + 1), [], NONCE)]})
    try:
        build_multihop_swap([pair, vault], [X, lp, X], greedy_v)
        results.append(check("an over-asking vault cycle is refused", False))
    except Exception:
        results.append(check("an over-asking vault cycle is refused", True))

    # The builder is NOT what stops an over-redemption -- the vault's puzzle is.
    # Doctoring the planner to claim more than the ratio allows still produces a
    # bundle, and that bundle dies inside the pool spend. Worth proving directly:
    # if the only thing between a vault and its reserves were our own arithmetic,
    # then every re-implementation of that arithmetic would be a way to drain it.
    honest = mh._plan_legs
    for label, extra in (("by one mojo", 1), ("by ten percent", max(1, v_out // 10))):
        def doctored(pools, path, amount, _extra=extra):
            legs = honest(pools, path, amount)
            last = legs[-1]
            legs[-1] = _Leg(last.pool, last.asset_in, last.asset_out, last.amount_in,
                            last.amount_out + _extra, last.kind, last.protocol_fee)
            return legs
        mh._plan_legs = doctored
        try:
            spends = cat_maker_spend(X, v_amount, 0xEB)
            offer = make_offer(spends, {X: [NotarizedPayment(USER_PH, uint64(v_out), [], NONCE)]})
            bundle = build_multihop_swap([pair, vault], [X, lp, X], offer).bundle
            problems, _ = audit(bundle, external_of([pair, vault], spends))
            rejected = any("failed:" in problem for problem in problems)
            results.append(check(f"the vault puzzle rejects over-redeeming {label}", rejected,
                                 "raised inside the pool spend" if rejected
                                 else "BUNDLE VALIDATED -- vault drainable"))
        except Exception:
            results.append(check(f"the vault puzzle rejects over-redeeming {label}", True,
                                 "refused before it could be built"))
        finally:
            mh._plan_legs = honest

    # A redemption burns LP the route has not acquired yet if it goes first.
    try:
        build_multihop_swap([vault, pair], [lp, X, lp], v_offer)
        results.append(check("a route opening with a redemption is refused", False))
    except Exception:
        results.append(check("a route opening with a redemption is refused", True))

    print()
    print("a longer cycle, reaching the vault through an intermediate asset:")
    xy = make_pool(sorted([X, Y]), {X: 5_000_000, Y: 5_000_000}, 0xE3)
    ylp = make_pool(sorted([Y, lp]), {Y: 4_000_000, lp: 9_000_000}, 0xE4)
    l_amount, l_out, l_profit = best_size([xy, ylp, vault], [X, Y, lp, X], 400_000)
    print(f"    commit {l_amount:,} -> {l_out:,}   profit {l_profit:+,}   X -> Y -> LP -> X")
    l_spends = cat_maker_spend(X, l_amount, 0xEC)
    l_offer = make_offer(l_spends, {X: [NotarizedPayment(USER_PH, uint64(l_out), [], NONCE)]})
    try:
        result = build_multihop_swap([xy, ylp, vault], [X, Y, lp, X], l_offer)
        problems, checks = audit(result.bundle, external_of([xy, ylp, vault], l_spends))
        results.append(check(f"a three-pool vault cycle builds and audits clean ({checks} "
                             f"assertions, {len(result.bundle.coin_spends)} spends)", not problems))
        for problem in problems:
            print(f"         - {problem}")
        results.append(check("all three pools advanced", len(result.pools) == 3))
    except Exception as exc:
        results.append(check(f"a three-pool vault cycle builds: {type(exc).__name__}: "
                             f"{str(exc)[:70]}", False))

    print()
    print("several vault cycles, settled together as one split:")
    vault2 = make_pool([X], {X: 8_000_000}, 0xE5, protocol_bps=0)
    lp2 = bytes32(vault2.lp_asset_id)
    pair_a = make_pool(sorted([X, lp]), {X: 5_000_000, lp: 6_000_000}, 0xE6)
    pair_b = make_pool(sorted([X, lp2]), {X: 4_000_000, lp2: 5_200_000}, 0xE7)
    a_in, a_out, _ = best_size([pair_a, vault], [X, lp, X], 300_000)
    b_in, b_out, _ = best_size([pair_b, vault2], [X, lp2, X], 300_000)
    print(f"    branch A  commit {a_in:,} -> {a_out:,}   through vault 1")
    print(f"    branch B  commit {b_in:,} -> {b_out:,}   through vault 2")

    v_branches = [SplitBranchSpec([pair_a, vault], [X, lp, X], a_in),
                  SplitBranchSpec([pair_b, vault2], [X, lp2, X], b_in)]
    vs_in, vs_out = a_in + b_in, a_out + b_out
    vs_spends = cat_maker_spend(X, vs_in, 0xED)
    vs_offer = make_offer(vs_spends, {X: [NotarizedPayment(USER_PH, uint64(vs_out), [], NONCE)]})
    touched = [pair_a, vault, pair_b, vault2]
    try:
        result = build_split_swap(v_branches, vs_offer)
        problems, checks = audit(result.bundle, external_of(touched, vs_spends))
        results.append(check(f"two vault cycles settle in one bundle ({checks} assertions, "
                             f"{len(result.bundle.coin_spends)} spends)", not problems))
        for problem in problems:
            print(f"         - {problem}")
        results.append(check("both vaults and both pairs advanced",
                             len(result.pools) == len(touched),
                             f"{len(result.pools)} of {len(touched)}"))
        results.append(check("the combined vault plan returns more than it commits",
                             vs_out > vs_in, f"{vs_out - vs_in:+,}"))
    except Exception as exc:
        results.append(check(f"two vault cycles settle in one bundle: {type(exc).__name__}: "
                             f"{str(exc)[:70]}", False))

    greedy_vs = make_offer(cat_maker_spend(X, vs_in, 0xEE),
                           {X: [NotarizedPayment(USER_PH, uint64(vs_out + 1), [], NONCE)]})
    try:
        build_split_swap(v_branches, greedy_vs)
        results.append(check("an over-asking combined vault Offer is refused", False))
    except Exception:
        results.append(check("an over-asking combined vault Offer is refused", True))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} cycle checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
