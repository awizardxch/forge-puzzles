#!/usr/bin/env python3
"""The flow builder: one net swap per pool, merges and splits included.

The split lane settles pool-disjoint branch cycles. The flow lane settles the
netted equilibrium — a pool's output divided between several consumers, several
producers feeding one consumer — which is what lets overlapping balancing
cycles level everything in ONE offer instead of a sequence.

The load-bearing case here is the merge: one producer's exit coin named by two
consuming reserves at once. That is the split lane's shared-entry mechanism
applied one level down, and this suite proves the generalization audits and
conserves rather than assuming the precedent transfers.
"""
import sys

sys.path.insert(0, ".")

from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from _forge_testkit import (
    NONCE, USER_PH, X, Z, audit, cat_maker_spend, external_of, make_offer,
    make_pool, paid_to, xch_maker_spend,
)
from forge_offer import ZERO_32
from forge_flow_balance import FlowLegSpec, build_flow_balance
from forge_multihop_swap import _plan_legs, build_multihop_swap

results = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    results.append(ok)
    return ok


def main() -> int:
    # ── the merge flow: one producer, two consumers ──────────────────────────
    # Three pools quoting Z at staggered prices. The netted equilibrium buys Z
    # where it is cheap and sells it at BOTH dearer pools — cheap's single
    # output feeding mid and dear together, one swap per pool.
    cheap = make_pool(sorted([X, Z]), {X: 5_000_000, Z: 12_000_000}, 0xA1)
    mid = make_pool(sorted([X, Z]), {X: 5_000_000, Z: 5_500_000}, 0xA2)
    dear = make_pool(sorted([X, Z]), {X: 5_000_000, Z: 5_000_000}, 0xA3)

    print("merge flow: cheap(X→Z) feeding mid(Z→X) and dear(Z→X):")
    amount_in = 400_000
    produced = _plan_legs([cheap], [X, Z], amount_in)[0]
    z_out = produced.amount_out - produced.protocol_fee
    # Split the Z between the two consumers roughly by their depth; the builder
    # rescales to exact conservation itself, so these are intent, not truth.
    to_mid = z_out * 55 // 100
    to_dear = z_out - to_mid
    mid_leg = _plan_legs([mid], [Z, X], to_mid)[0]
    dear_leg = _plan_legs([dear], [Z, X], to_dear)[0]
    total_out = (mid_leg.amount_out - mid_leg.protocol_fee) + (dear_leg.amount_out - dear_leg.protocol_fee)
    profit = total_out - amount_in
    print(f"    commit {amount_in:,} X -> {total_out:,} X   profit {profit:+,}")
    check("the netted flow is profitable at this size", profit > 0)

    spends = cat_maker_spend(X, amount_in, 0xB1)
    offer = make_offer(spends, {X: [NotarizedPayment(USER_PH, uint64(total_out), [], NONCE)]})
    specs = [
        FlowLegSpec(cheap, X, Z, amount_in),
        FlowLegSpec(mid, Z, X, to_mid),
        FlowLegSpec(dear, Z, X, to_dear),
    ]
    try:
        result = build_flow_balance(specs, offer, start_asset=X)
        problems, checks = audit(result.bundle, external_of([cheap, mid, dear], spends))
        check(f"the merge flow builds and audits clean ({checks} assertions, "
              f"{len(result.bundle.coin_spends)} spends)", not problems)
        for p in problems:
            print(f"         - {p}")
        check("all three pools advanced", len(result.pools) == 3)
        check("the whole return is paid to the trader",
              paid_to(result.bundle, X, USER_PH) == total_out,
              f"{paid_to(result.bundle, X, USER_PH)} == {total_out}")

        # Conservation, from the settled successor states rather than the plan:
        # Z released by cheap must equal Z absorbed by mid and dear.
        def z_reserve(pool):
            for asset_id, _coin_id, amount in pool.state[0]:
                if bytes32(asset_id) == Z:
                    return int(amount)
            raise AssertionError("no Z reserve")
        released = 12_000_000 - z_reserve(result.pools[0])
        absorbed = (z_reserve(result.pools[1]) - 5_500_000) + (z_reserve(result.pools[2]) - 5_000_000)
        # The producer's reserve releases its protocol fee too, paid straight
        # to the recipient; only the remainder reaches the consumers.
        producer_fee = _plan_legs([cheap], [X, Z], amount_in)[0].protocol_fee
        check("Z conserves across the merge to the mojo (net of the protocol fee)",
              released == absorbed + producer_fee,
              f"released {released:,} == absorbed {absorbed:,} + fee {producer_fee:,}")
    except Exception as exc:  # noqa: BLE001
        check(f"merge flow builds: {type(exc).__name__}: {str(exc)[:80]}", False)

    # ── the Offer is the profit assertion ────────────────────────────────────
    greedy_spends = cat_maker_spend(X, amount_in, 0xB2)
    greedy = make_offer(greedy_spends, {X: [NotarizedPayment(USER_PH, uint64(total_out + 1), [], NONCE)]})
    try:
        build_flow_balance(specs, greedy, start_asset=X)
        check("an over-asking Offer is refused", False)
    except Exception:
        check("an over-asking Offer is refused", True,
              "the requested amount asserts the profit")

    # ── a disjoint flow degenerates to the cycle the multihop lane settles ───
    # Same two pools, same amounts, both builders: the flow must reach the same
    # successor reserves the proven lane does, or the generalization drifted.
    print()
    print("disjoint flow vs the proven multihop cycle:")
    cyc_a = make_pool(sorted([X, Z]), {X: 4_000_000, Z: 9_000_000}, 0xC1)
    cyc_b = make_pool(sorted([X, Z]), {X: 9_000_000, Z: 4_000_000}, 0xC2)
    cyc_a2 = make_pool(sorted([X, Z]), {X: 4_000_000, Z: 9_000_000}, 0xC3)
    cyc_b2 = make_pool(sorted([X, Z]), {X: 9_000_000, Z: 4_000_000}, 0xC4)
    cycle_in = 250_000
    leg1 = _plan_legs([cyc_a], [X, Z], cycle_in)[0]
    leg2 = _plan_legs([cyc_b], [Z, X], leg1.amount_out - leg1.protocol_fee)[0]
    cycle_out_amount = leg2.amount_out - leg2.protocol_fee

    flow_spends = cat_maker_spend(X, cycle_in, 0xB3)
    flow_offer = make_offer(flow_spends, {X: [NotarizedPayment(USER_PH, uint64(cycle_out_amount), [], NONCE)]})
    hop_spends = cat_maker_spend(X, cycle_in, 0xB4)
    hop_offer = make_offer(hop_spends, {X: [NotarizedPayment(USER_PH, uint64(cycle_out_amount), [], NONCE)]})
    try:
        flow = build_flow_balance([
            FlowLegSpec(cyc_a, X, Z, cycle_in),
            FlowLegSpec(cyc_b, Z, X, leg1.amount_out - leg1.protocol_fee),
        ], flow_offer, start_asset=X)
        hops = build_multihop_swap([cyc_a2, cyc_b2], [X, Z, X], hop_offer)
        # Coin ids differ between the two pool instances by construction, so
        # compare the reserve AMOUNTS the lanes settle to, sorted per pool.
        def reserve_amounts(pool):
            return sorted(int(amount) for _a, _c, amount in pool.state[0])
        flow_states = sorted(str(reserve_amounts(p)) for p in flow.pools)
        hop_states = sorted(str(reserve_amounts(p)) for p in hops.pools)
        check("both lanes reach identical successor reserves", flow_states == hop_states,
              f"{flow_states} == {hop_states}")
        # hops.amounts[-1] is the gross leg output; the trader receives it net
        # of the protocol fee — compare what each lane actually pays out.
        check("both lanes pay the trader the same output",
              paid_to(flow.bundle, X, USER_PH) == paid_to(hops.bundle, X, USER_PH),
              f"{paid_to(flow.bundle, X, USER_PH)} == {paid_to(hops.bundle, X, USER_PH)}")
        fp, fc = audit(flow.bundle, external_of([cyc_a, cyc_b], flow_spends))
        check(f"the degenerate flow audits clean ({fc} assertions)", not fp)
        for p in fp:
            print(f"         - {p}")
    except Exception as exc:  # noqa: BLE001
        check(f"disjoint comparison builds: {type(exc).__name__}: {str(exc)[:80]}", False)

    # ── the wrap flow: buy the underlying, MINT the vault's LP, sell it dear ─
    # The mirror of the redeem shape, and the roadmap-2d lane: the underlying
    # is cheap in the pools and dear THROUGH the vault, so extraction deposits
    # into the vault (MODE_ADD), mints LP mid-bundle, and sells the LP on. The
    # mint's XCH backing is carved out of the entry by the fixed point, so the
    # bundle balances without the Offer knowing anything about it.
    print()
    print("wrap flow: XCH -> Z (swap), Z -> LP (vault MINT), LP -> XCH (swap):")
    # Z is priced like a real CAT -- ~100 XCH mojos per Z mojo -- so the mint's
    # one-XCH-mojo-per-LP-mojo backing is a ~1% cost, not a doubling. (At XCH
    # parity the backing genuinely halves wrap profitability; that is the
    # economics, not a bug: minting LP costs its face value in XCH.)
    zvault = make_pool([Z], {Z: 10_000_000}, 0xD1)
    lp_asset = bytes32(zvault.lp_asset_id)
    pool_xz = make_pool(sorted([ZERO_32, Z]), {ZERO_32: 8_000_000_000, Z: 80_000_000}, 0xD2)
    pair_lp = make_pool(sorted([ZERO_32, lp_asset]), {ZERO_32: 8_000_000_000, lp_asset: 40_000_000}, 0xD3)

    wrap_in = 3_000_000
    wrap_request = 4_000_000  # well under the ~5.9M the chain returns: the profit assertion
    wrap_spends = xch_maker_spend(wrap_in, 0xB5)
    wrap_offer = make_offer(wrap_spends, {None: [NotarizedPayment(USER_PH, uint64(wrap_request), [], NONCE)]})
    wrap_specs = [
        FlowLegSpec(pool_xz, ZERO_32, Z, wrap_in),
        FlowLegSpec(zvault, Z, lp_asset, 1),
        FlowLegSpec(pair_lp, lp_asset, ZERO_32, 1),
    ]
    try:
        wrap = build_flow_balance(wrap_specs, wrap_offer)
        wp, wc = audit(wrap.bundle, external_of([pool_xz, zvault, pair_lp], wrap_spends))
        check(f"the wrap flow builds and audits clean ({wc} assertions, "
              f"{len(wrap.bundle.coin_spends)} spends)", not wp)
        for p in wp:
            print(f"         - {p}")
        check("all three pools advanced", len(wrap.pools) == 3)

        deposit, minted = wrap.leg_amounts[1]
        vault_after = next(p for p in wrap.pools if p.launcher_id == zvault.launcher_id)
        reserve_after = next(int(amount) for asset, _c, amount in vault_after.state[0]
                             if bytes32(asset) == Z)
        check("the vault's reserve grew by the whole deposit",
              reserve_after == 10_000_000 + deposit,
              f"{reserve_after:,} == {10_000_000 + deposit:,}")
        check("the vault's LP supply grew by the mint",
              int(vault_after.state[1]) == 10_000_000 + minted,
              f"supply {int(vault_after.state[1]):,}")
        check("the V8+ crossing fee makes the mint smaller than the deposit",
              0 < minted < deposit, f"minted {minted:,} < deposited {deposit:,}")
        check("the whole return is paid to the trader",
              paid_to(wrap.bundle, ZERO_32, USER_PH) == wrap.total_out,
              f"{paid_to(wrap.bundle, ZERO_32, USER_PH):,} == {wrap.total_out:,}")
        check("the flow is profitable after the backing carve",
              wrap.total_out > wrap_in,
              f"{wrap.total_out:,} out of {wrap_in:,} in")
        # The mint consumed `minted` XCH of bundle value: what entered the
        # first pool is the entry minus the backing, which must cover the mint
        # with at most the bisection's integer slack left over as fee.
        backing_used = wrap_in - wrap.leg_amounts[0][0]
        check("the entry under-deposits by the minted backing (mojo slack at most)",
              minted <= backing_used <= minted + 2,
              f"backing {backing_used:,} for mint {minted:,}")
    except Exception as exc:  # noqa: BLE001
        check(f"wrap flow builds: {type(exc).__name__}: {str(exc)[:80]}", False)

    # A CAT entry has no free XCH to back a mint with.
    cat_entry_specs = [
        FlowLegSpec(zvault, Z, lp_asset, 10),
        FlowLegSpec(pair_lp, lp_asset, ZERO_32, 10),
        FlowLegSpec(pool_xz, ZERO_32, Z, 10),
    ]
    try:
        build_flow_balance(cat_entry_specs, wrap_offer, start_asset=Z)
        check("a wrap in a CAT-entry flow is refused", False)
    except ValueError as exc:
        check("a wrap in a CAT-entry flow is refused", 'XCH entry' in str(exc), str(exc)[:60])

    # ── the chained flow: one pool crossed twice, two pairs, one bundle ──────
    # A 3-asset pool cheap on BOTH its CATs needs two pairwise crossings to
    # level (MODE_SWAP trades one pair; the third reserve freezes). One bundle
    # can still do it: the second crossing spends the first crossing's
    # successor coin — consensus allows spending a coin the same bundle
    # creates — so the untouched capital in the entry funds both.
    print()
    print("chained flow: P3 crossed twice (XCH->Z, then XCH->W on its successor):")
    W = bytes32(bytes([0x4D]) * 32)
    p3 = make_pool(sorted([ZERO_32, Z, W]), {ZERO_32: 8_000_000_000, Z: 80_000_000, W: 80_000_000}, 0xE1)
    pz = make_pool(sorted([ZERO_32, Z]), {ZERO_32: 8_000_000_000, Z: 40_000_000}, 0xE2)
    pw = make_pool(sorted([ZERO_32, W]), {ZERO_32: 8_000_000_000, W: 40_000_000}, 0xE3)

    entry_each = 100_000_000
    chain_in = 2 * entry_each
    chain_request = 250_000_000  # comfortably under the ~380M the two branches return
    chain_spends = xch_maker_spend(chain_in, 0xB6)
    chain_offer = make_offer(chain_spends, {None: [NotarizedPayment(USER_PH, uint64(chain_request), [], NONCE)]})
    chain_specs = [
        FlowLegSpec(p3, ZERO_32, Z, entry_each),
        FlowLegSpec(pz, Z, ZERO_32, 1),
        FlowLegSpec(p3, ZERO_32, W, entry_each),
        FlowLegSpec(pw, W, ZERO_32, 1),
    ]
    try:
        chain = build_flow_balance(chain_specs, chain_offer)
        cp, cc = audit(chain.bundle, external_of([p3, pz, pw], chain_spends))
        check(f"the chained flow builds and audits clean ({cc} assertions, "
              f"{len(chain.bundle.coin_spends)} spends)", not cp)
        for prob in cp:
            print(f"         - {prob}")
        check("successor list carries each pool once", len(chain.pools) == 3)

        p3_tip = next(p for p in chain.pools if p.launcher_id == p3.launcher_id)
        spent_ids = {cs.coin.name() for cs in chain.bundle.coin_spends}
        first_successor = bytes32(p3_tip.pool.coin.parent_coin_info)
        check("the second crossing spent the first crossing's successor in-bundle",
              first_successor in spent_ids and first_successor != p3.pool.coin.name())
        check("the original pool coin was spent too", p3.pool.coin.name() in spent_ids)

        def amounts_of(pool):
            return {bytes32(a): int(amount) for a, _c, amount in pool.state[0]}
        tip = amounts_of(p3_tip)
        z_in1, z_out1 = chain.leg_amounts[0]
        w_in2, w_out2 = chain.leg_amounts[2]
        check("the tip reflects BOTH crossings",
              tip[ZERO_32] == 8_000_000_000 + z_in1 + w_in2
              and tip[Z] == 80_000_000 - z_out1
              and tip[W] == 80_000_000 - w_out2,
              f"XCH {tip[ZERO_32]:,} Z {tip[Z]:,} W {tip[W]:,}")
        check("the whole return is paid to the trader",
              paid_to(chain.bundle, ZERO_32, USER_PH) == chain.total_out,
              f"{paid_to(chain.bundle, ZERO_32, USER_PH):,} == {chain.total_out:,}")
        check("both crossings together are profitable",
              chain.total_out > chain_in,
              f"{chain.total_out:,} out of {chain_in:,} in")
    except Exception as exc:  # noqa: BLE001
        check(f"chained flow builds: {type(exc).__name__}: {str(exc)[:90]}", False)

    # The same ordered pair twice is a netting failure, not a flow.
    try:
        build_flow_balance([
            FlowLegSpec(p3, ZERO_32, Z, 1000),
            FlowLegSpec(pz, Z, ZERO_32, 1),
            FlowLegSpec(p3, ZERO_32, Z, 1000),
        ], chain_offer)
        check("the same pool pair twice is refused", False)
    except ValueError as exc:
        check("the same pool pair twice is refused", 'net same-pair' in str(exc), str(exc)[:60])

    # ── contract refusals ────────────────────────────────────────────────────
    print()
    print("contract refusals:")
    try:
        build_flow_balance([FlowLegSpec(cheap, X, Z, 1000)], offer, start_asset=X)
        check("a single-leg flow is refused", False)
    except ValueError:
        check("a single-leg flow is refused", True)

    # A pool crossed twice is legal now (chained advance) -- but a round trip
    # through one pool pays its fee twice for movement that cancels, so the
    # profit assertion refuses it: the return cannot cover the request.
    try:
        build_flow_balance([
            FlowLegSpec(cheap, X, Z, 1000),
            FlowLegSpec(cheap, Z, X, 1000),
        ], offer, start_asset=X)
        check("a round trip through one pool cannot meet the profit assertion", False)
    except ValueError as exc:
        check("a round trip through one pool cannot meet the profit assertion", True,
              str(exc)[:60])

    passed = sum(1 for r in results if r)
    print()
    print(f"{passed}/{len(results)} flow-balance checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
