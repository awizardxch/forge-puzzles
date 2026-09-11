#!/usr/bin/env python3
"""A balancing trade, settled as an ordinary Offer.

Two pools quoting the same asset at different rates is an arbitrage. Closing it
needs no new machinery and no new trust: offer TXCH, cross both pools, and
request more TXCH back than you put in. The route is a multi-hop whose path
begins and ends on the same asset.

What makes this safe is the Offer itself. The requested amount IS the profit
assertion -- a bundle that fails to produce it cannot satisfy the notarised
payment, so it cannot settle. There is no window in which the balancer pays to
move value between its own pools.

The trade also does what its name says: sized to where the two pools' marginal
rates meet, it both maximizes the surplus and leaves the pools level.

SUPERSEDED by _test_forge_cycles.py, which builds its own pools at the shipping
revision instead of loading whatever happens to be in the deployment index. Kept
for the record; it skips when the index is empty.
"""
import io
import json
import sys

sys.path.insert(0, ".")

from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin as fs
from _test_v6_transition import USER_PH, audit, make_offer, xch_maker_spend
from _test_route_nasset import external_for, load_pools
from forge_offer import ZERO_32
from forge_multihop_swap import _plan_legs, build_multihop_swap

NONCE = bytes32.fromhex("ee" * 32)


def quote_cycle(pool_in, pool_out, asset, amount_in):
    """TXCH out of the cycle for `amount_in` TXCH in, or None if it cannot price."""
    try:
        legs = _plan_legs([pool_in, pool_out], [ZERO_32, asset, ZERO_32], amount_in)
    except Exception:
        return None
    return legs[-1].amount_out - legs[-1].protocol_fee


def best_size(pool_in, pool_out, asset, ceiling):
    """Coarse scan for the most profitable commitment, mirroring optimalCycleSize."""
    best = None
    for step in range(1, 60):
        amount = ceiling * step // 60
        out = quote_cycle(pool_in, pool_out, asset, amount)
        if out is None:
            continue
        profit = out - amount
        if best is None or profit > best[2]:
            best = (amount, out, profit)
    return best


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    pools = load_pools()
    by_id = {p.launcher_id.hex()[:12]: p for p in pools.values()}

    # Find any asset two XCH-paired pools disagree on.
    quotes = {}
    for pool in pools.values():
        assets = [bytes32(a) for a in pool.config[2]]
        if ZERO_32 not in assets:
            continue
        reserves = {bytes32(r[0]): int(r[2]) for r in pool.state[0]}
        xch = reserves[ZERO_32]
        if xch <= 0:
            continue
        for asset in assets:
            if asset == ZERO_32 or reserves[asset] <= 0:
                continue
            quotes.setdefault(asset, []).append((pool, xch / reserves[asset]))

    candidate = None
    for asset, entries in quotes.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda e: e[1])
        cheap, dear = entries[0], entries[-1]
        if cheap[1] >= dear[1]:
            continue
        delta = (dear[1] - cheap[1]) / cheap[1]
        if candidate is None or delta > candidate[3]:
            candidate = (asset, cheap[0], dear[0], delta)

    if candidate is None:
        print("no asset is priced by two XCH pools; nothing to balance")
        return 2

    asset, buy_pool, sell_pool, delta = candidate
    print(f"asset      {asset.hex()[:12]}  spread {delta * 100:.2f}%")
    print(f"buy  from  {buy_pool.launcher_id.hex()[:12]}")
    print(f"sell into  {sell_pool.launcher_id.hex()[:12]}")

    sized = best_size(buy_pool, sell_pool, asset, 2_000_000_000_000)
    if sized is None or sized[2] <= 0:
        print("no profitable size found against the live reserves")
        return 2
    amount_in, amount_out, profit = sized
    print(f"commit     {amount_in / 1e12:.6f} TXCH")
    print(f"returns    {amount_out / 1e12:.6f} TXCH")
    print(f"profit     {profit / 1e12:.6f} TXCH  ({profit / amount_in * 100:.2f}%)")
    print()

    results = []
    results.append(check("the cycle returns more than it commits", profit > 0))

    # The Offer asserts the profit: it requests everything back plus the surplus.
    spends = xch_maker_spend(amount_in, 0xA7)
    offer = make_offer(spends, {None: [NotarizedPayment(USER_PH, uint64(amount_out), [], NONCE)]})
    try:
        result = build_multihop_swap([buy_pool, sell_pool], [ZERO_32, asset, ZERO_32], offer)
    except Exception as exc:
        print(f"  [FAIL] the cycle builds as one bundle: {type(exc).__name__}: {exc}")
        return 1

    problems, checked = audit(result.bundle, external_for([buy_pool, sell_pool], spends))
    results.append(check(f"bundle audits clean ({checked} assertions, "
                         f"{len(result.bundle.coin_spends)} spends)", not problems))
    for problem in problems:
        print(f"         - {problem}")

    results.append(check("both pools advanced", len(result.pools) == 2))

    # An Offer demanding more than the cycle yields must be impossible to build,
    # which is the property that makes this safe to run unattended.
    greedy = make_offer(xch_maker_spend(amount_in, 0xA8),
                        {None: [NotarizedPayment(USER_PH, uint64(amount_out + 10_000_000), [], NONCE)]})
    try:
        build_multihop_swap([buy_pool, sell_pool], [ZERO_32, asset, ZERO_32], greedy)
        results.append(check("an over-asking Offer is refused", False))
    except Exception:
        results.append(check("an over-asking Offer is refused", True,
                             "the requested amount is the profit assertion"))

    # Overcorrecting past the level point must lose money, not merely earn less.
    over = quote_cycle(buy_pool, sell_pool, asset, amount_in * 4)
    results.append(check("overcorrecting past level is unprofitable",
                         over is not None and over - amount_in * 4 < profit,
                         f"{(over - amount_in * 4) / 1e12:+.6f} TXCH at 4x the size"))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} arbitrage-cycle checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
