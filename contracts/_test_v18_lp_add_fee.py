#!/usr/bin/env python3
"""The router (protocol) fee on a V18 liquidity add: the swapped share only (owner, 2026-11-16).

A proportional add and every remove pay the router nothing. An unbalanced add -- the direct
add lane, or a routed deposit whose sales balance the deposit -- pays the caller's rate on
the part of the deposit that is effectively traded into the pool's other assets
(contracts/forge_lp_add_fee.py), off the LP payout, as every wallet-built lane pays it. The
Swap tab's zap is unchanged: it pays the rate on its whole payout.

Driven through forge_stdin exactly as the lanes send it: `dev_fee` on an add or remove
(api/_forgeResponder.js buildTransition) and `add_fee` on a routed deposit
(buildRoutedDepositTransition). Exit 0 all pass, 1 otherwise, 2 when V18 is not built.
"""
from __future__ import annotations

import json
import sys

from chia_rs import SpendBundle

import forge_math
import forge_stdin
import forge_v18_driver as drv
import forge_v18_offer as v18
from forge_lp_add_fee import add_router_fee, swapped_share
from forge_offer import ZERO_32
from _test_v18_offer_lane import ROUTER_PH, T_A, T_B, TRADER_PH, H, fabricate_offer, make, paid_to

results: list[bool] = []
BPS = 300


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def run(payload):
    return forge_stdin.build(json.loads(json.dumps(payload)))


def hx(asset):
    return (ZERO_32 if asset is None else asset).hex()


def fee_field(bps):
    return {"puzzle_hash": ROUTER_PH.hex(), "bps": bps}


def mint_of(pool, deposits):
    return v18._mint(pool, deposits)


def direct_add(pool, deposits):
    """An add of `deposits` (the XCH entry its deposit; the backing is added here), sized as
    settle_add sizes it: on a pool holding XCH the backing lowers the XCH deposit."""
    deposits = list(deposits)
    honest = mint_of(pool, deposits)
    xi = pool.asset_ids.index(None) if None in pool.asset_ids else -1
    if xi >= 0:
        # settle_add starts from the whole XCH coin as the deposit and walks down to the fixed
        # point; a one-sided CAT add brings a few mojos of XCH above the backing to land on it.
        offered_xch = deposits[xi] + honest + (16 if deposits[xi] == 0 else 0)
        deposits[xi] = offered_xch
        honest = mint_of(pool, deposits)
        for _ in range(12):
            deposits[xi] = offered_xch - honest
            nxt = mint_of(pool, deposits)
            if nxt == honest:
                break
            honest = nxt
    else:
        offered_xch = honest
    offered = {a: d for a, d in zip(pool.asset_ids, deposits) if a is not None and d > 0}
    offered[None] = offered_xch
    return offered, deposits, honest


def add_case(label, pool, deposits, bps, ask=None, expect_ok=True, expect_fee=None, salt=0x41):
    print(f"add on {label} at {bps} bps")
    offered, deps, honest = direct_add(pool, deposits)
    fee = add_router_fee(honest, pool.state[0], deps, pool.weights, bps)
    if expect_fee is not None:
        check("  the fee is the rate on the swapped share", fee == expect_fee, f"{fee} vs {expect_fee}")
    want = honest - fee if ask is None else ask(honest, fee)
    offer = fabricate_offer(offered, {pool.lp_asset_id: want}, salt=salt)
    try:
        out = run({"action": "add", "offer": offer.to_bech32(), "pool": v18.pool_to_snapshot(pool),
                   "current_height": H, "dev_fee": fee_field(bps)})
    except Exception as exc:
        check("  builder accepted the offer", not expect_ok, f"{type(exc).__name__}: {str(exc)[:140]}")
        return None, honest, fee
    if not check("  builder accepted the offer", expect_ok):
        return out, honest, fee
    bundle = SpendBundle.from_json_dict(out["bundle"])
    router = paid_to(bundle, ROUTER_PH, pool.lp_asset_id)
    trader = paid_to(bundle, TRADER_PH, pool.lp_asset_id)
    check("  the mint is the honest mint", int(out["forge"]["lp_minted"]) == honest)
    check("  the router is paid its fee in LP, the depositor the rest", router == fee and trader == honest - fee,
          f"router {router}, depositor {trader} of {honest}")
    check("  the lane reports what the router collected", int(out["dev_fee_collected"]) == fee
          and int(out["forge"].get("router_fee", -1)) == fee, f"{out['dev_fee_collected']}")
    check("  no XCH reaches the router", paid_to(bundle, ROUTER_PH) == 0)
    names = [c.name() for c in bundle.additions()]
    check("  every output is a distinct coin", len(set(names)) == len(names))
    return out, honest, fee


def remove_case(label, pool, burn, bps):
    print(f"remove on {label} at {bps} bps")
    vf = forge_math.vault_fee_bps(len(pool.state[0]), 10, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)
    wants = {a: p for a, p in zip(pool.asset_ids, payouts) if p > 0}
    offer = fabricate_offer({pool.lp_asset_id: burn}, wants, salt=0x47)
    out = run({"action": "remove", "offer": offer.to_bech32(), "pool": v18.pool_to_snapshot(pool),
               "current_height": H, "dev_fee": fee_field(bps)})
    bundle = SpendBundle.from_json_dict(out["bundle"])
    check("  a remove pays the router nothing", all(paid_to(bundle, ROUTER_PH, a) == 0 for a in pool.asset_ids)
          and int(out["forge"].get("router_fee", 0)) == 0)
    check("  the withdrawer gets every payout", all(paid_to(bundle, TRADER_PH, a) == p for a, p in zip(pool.asset_ids, payouts)))


def routed_case(label, payload, offered, pool, expect_fee_positive, salt):
    """A routed deposit as the Liquidity tab settles it: preview (a probe, no offer), then
    the wallet-built offer asking the mint less the router's fee the preview named."""
    print(f"routed deposit: {label}")
    sized = dict(offered) if None in pool.asset_ids else {**offered, None: 10**6}
    probe = {"offered": {hx(a): str(v) for a, v in sized.items()}, "requested": [hx(pool.lp_asset_id)]}
    preview = run({**payload, "preview": True, "probe": probe})
    minted, fee = int(preview["minted"]), int(preview.get("router_fee", 0))
    check("  the preview names a fee" if expect_fee_positive else "  the preview names no fee",
          (fee > 0) == expect_fee_positive, f"{fee} of {minted}")
    real = dict(offered)
    if None not in pool.asset_ids:
        real[None] = minted
    offer = fabricate_offer(real, {pool.lp_asset_id: minted - fee}, salt=salt)
    live = run({**payload, "offer": offer.to_bech32()})
    again = run({**payload, "offer": offer.to_bech32()})
    bundle = SpendBundle.from_json_dict(live["bundle"])
    check("  live mints what the preview said", int(live["minted"]) == minted, f"{live['minted']} vs {minted}")
    check("  the router is paid exactly the previewed fee, the depositor the rest",
          paid_to(bundle, ROUTER_PH, pool.lp_asset_id) == fee and paid_to(bundle, TRADER_PH, pool.lp_asset_id) == minted - fee,
          f"router {paid_to(bundle, ROUTER_PH, pool.lp_asset_id)}")
    check("  the lane reports the fee it built", int(live.get("router_fee", -1)) == fee)
    check("  the same request builds the same bundle (a dry run is the live build)", again["transaction_id"] == live["transaction_id"])
    greedy = fabricate_offer(real, {pool.lp_asset_id: minted - fee + 1}, salt=salt)
    if fee > 0:
        try:
            run({**payload, "offer": greedy.to_bech32()})
            check("  asking past the mint less the fee is refused", False)
        except Exception as exc:
            check("  asking past the mint less the fee is refused", "quote again" in str(exc) or "fee" in str(exc), str(exc)[:120])
    return minted, fee


def main() -> int:
    if not drv.v18_available():
        print("  [skip] V18 puzzles not built")
        return 2
    pair = make([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x41)
    cats = make([T_A, T_B], [800_000, 600_000], [1, 1], salt=0x42)
    triple = make([None, T_A, T_B], [20_000_000_000, 400_000, 300_000], [2, 1, 1], salt=0x43)
    vault = make([T_A], [1_000_000], [1], salt=0x44)
    ab = make([T_A, T_B], [800_000, 600_000], [1, 1], salt=0x52)

    print("the swapped share (forge_lp_add_fee)")
    check("  proportional: nothing is swapped", swapped_share([1000, 2000], [100, 200], [1, 1]) == [0, 0])
    check("  within rounding (a mojo or two): nothing is swapped", swapped_share([1000, 2000], [102, 200], [1, 1]) == [0, 0])
    check("  one-sided on a pair: half the deposit", swapped_share([1000, 2000], [100, 0], [1, 1]) == [50, 0])
    check("  weighted 2/1/1, one-sided in the heavy asset: half", swapped_share([20, 4, 3], [8, 0, 0], [2, 1, 1]) == [4, 0, 0])
    check("  weighted 2/1/1, one-sided in a light asset: three quarters", swapped_share([20, 4, 3], [0, 8, 0], [2, 1, 1]) == [0, 6, 0])
    check("  vault: nothing is swapped", swapped_share([1000], [500], [1]) == [0])
    check("  exempt (0 bps): no fee", add_router_fee(10**9, [1000, 2000], [100, 0], [1, 1], 0) == 0)

    # The worked example: 50,000 A into the XCH/A pair (10% of the A reserve), one-sided.
    # Half of it is swapped, so the fee is half the rate on the mint: floor(mint * 300 * 1/2 / 10_000).
    _, deps, mint = direct_add(pair, [0, 50_000])
    worked = add_router_fee(mint, pair.state[0], deps, pair.weights, BPS)
    print(f"  worked example: reserves {pair.state[0]}, deposit {deps}, mint {mint}, fee {worked}")
    check("  worked example: the fee is floor(mint * 150 / 10_000)", worked == mint * 150 // 10_000, f"{worked}")

    add_case("pair, proportional", pair, [500_000_000, 25_000], BPS, expect_fee=0)
    add_case("cats, proportional", cats, [40_000, 30_000], BPS, expect_fee=0)
    add_case("triple, proportional", triple, [600_000_000, 12_000, 9_000], BPS, expect_fee=0)
    add_case("pair, one-sided A (the worked example)", pair, [0, 50_000], BPS, expect_fee=worked, salt=0x42)
    add_case("pair, one-sided XCH", pair, [1_000_000_000, 0], BPS, salt=0x43)
    add_case("cats, unbalanced", cats, [60_000, 5_000], BPS, salt=0x44)
    add_case("triple (weighted 2/1/1), one-sided B", triple, [0, 0, 30_000], BPS, salt=0x45)
    add_case("vault", vault, [100_000], BPS, expect_fee=0, salt=0x46)
    add_case("pair, one-sided A, exempt", pair, [0, 50_000], 0, expect_fee=0, salt=0x47)
    add_case("pair, one-sided A, asking the whole mint (the fee not deducted)", pair, [0, 50_000], BPS,
             ask=lambda honest, fee: honest, expect_ok=False, salt=0x48)
    print("add on pair, one-sided A, in the money past the band: the maker gets the improvement")
    offered, deps, honest = direct_add(pair, [0, 50_000])
    fee = add_router_fee(honest, pair.state[0], deps, pair.weights, BPS)
    under = fee // 10 + 2 + 1_000
    offer = fabricate_offer(offered, {pair.lp_asset_id: honest - fee - under}, salt=0x49)
    out = run({"action": "add", "offer": offer.to_bech32(), "pool": v18.pool_to_snapshot(pair),
               "current_height": H, "dev_fee": fee_field(BPS)})
    b = SpendBundle.from_json_dict(out["bundle"])
    router = paid_to(b, ROUTER_PH, pair.lp_asset_id)
    check("  the router is paid its fee, no more", fee <= router <= fee + 1, f"{router} vs {fee}")
    check("  the maker gets the mint less the router's take", paid_to(b, TRADER_PH, pair.lp_asset_id) == honest - router)

    remove_case("pair", pair, pair.state[1] // 20, BPS)
    remove_case("triple", triple, triple.state[1] // 30, BPS)

    add_fee = fee_field(BPS)
    # One-sided A into the triple, part of the A sold into B on A/B first (the Liquidity tab's
    # routed plan): the swapped share of the gross deposit pays, the rest does not.
    rdep = {"action": "routed-deposit", "current_height": H, "pool": v18.pool_to_snapshot(triple),
            "sales": [{"pools": [v18.pool_to_snapshot(ab)], "path": [hx(T_A), hx(T_B)], "amountIn": 30_000}],
            "add_fee": add_fee}
    minted, fee = routed_case("one-sided A into the triple, selling part into B", rdep, {None: 30_000_000, T_A: 80_000}, triple, True, 0x61)
    check("  the fee is the rate on the gross deposit's swapped share",
          fee == add_router_fee(minted, triple.state[0], [30_000_000 - minted, 80_000, 0], triple.weights, BPS), f"{fee}")
    zap = {"action": "routed-deposit", "current_height": H, "pool": v18.pool_to_snapshot(pair),
           "sales": [{"pools": [v18.pool_to_snapshot(pair)], "path": [hx(None), hx(T_A)], "amountIn": 1_000_000_000}],
           "add_fee": add_fee}
    # XCH only into the pair, part of it swapped into A inside the pool: the offered XCH is
    # the deposit plus the backing; the preview's mint says how much of it is backing.
    probe = run({**zap, "preview": True, "probe": {"offered": {hx(None): "3000000000"}, "requested": [hx(pair.lp_asset_id)]}})
    routed_case("XCH only into the pair, swapped inside the pool", zap, {None: 3_000_000_000}, pair, True, 0x62)
    check("  the fee is the rate on the gross deposit's swapped share (XCH less the backing)",
          int(probe.get("router_fee", -1)) == add_router_fee(int(probe["minted"]), pair.state[0],
                                                     [3_000_000_000 - int(probe["minted"]), 0], pair.weights, BPS))
    plain = {"action": "routed-deposit", "current_height": H, "pool": v18.pool_to_snapshot(cats), "sales": [], "add_fee": add_fee}
    routed_case("proportional, no sales", plain, {T_A: 40_000, T_B: 30_000}, cats, False, 0x63)
    exempt = {**rdep, "add_fee": fee_field(0)}
    routed_case("one-sided A into the triple, exempt", exempt, {None: 30_000_000, T_A: 80_000}, triple, False, 0x64)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} LP-add router-fee checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
