#!/usr/bin/env python3
"""A balancing cycle that crosses a vault, settled atomically.

The widest price gaps sit behind vaults. An asset can be cheap inside one --
reachable only by buying the vault's LP and burning it -- while a pool that
trades the asset directly quotes it many times higher. Closing that gap needs a
route which swaps in, redeems, and sells on:

    TXCH --swap--> vaultLP --redeem--> asset --swap--> TXCH

Splitting it across two transactions works but leaves the asset held between
them, exposed to whatever price the second half finds. This settles both halves
and the redemption in one bundle, so either the whole cycle lands or none of it
does, and the Offer's requested amount asserts the profit.
SUPERSEDED by _test_forge_cycles.py, whose vault-cycle section builds its own
vault and pair at the shipping revision instead of loading whatever the
deployment index happens to hold. Kept for the record; it skips when the index
is empty.
"""
import sys

sys.path.insert(0, ".")

from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from _test_v6_transition import USER_PH, audit, make_offer, xch_maker_spend
from _test_route_nasset import external_for, load_pools
from forge_offer import ZERO_32
from forge_multihop_swap import _plan_legs, build_multihop_swap

NONCE = bytes32.fromhex("ee" * 32)


def quote(pools, path, amount):
    try:
        legs = _plan_legs(pools, path, amount)
    except Exception:
        return None
    return legs[-1].amount_out - legs[-1].protocol_fee


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    pools = load_pools()
    vault = next((p for p in pools.values() if len(p.config[2]) == 1), None)
    if vault is None:
        print("no single-asset vault in the deployment index")
        return 2

    lp_asset = bytes32(vault.lp_asset_id)
    underlying = bytes32(vault.config[2][0])
    lp_market = next((p for p in pools.values()
                      if lp_asset in [bytes32(a) for a in p.config[2]]
                      and ZERO_32 in [bytes32(a) for a in p.config[2]]), None)
    direct = next((p for p in pools.values()
                   if underlying in [bytes32(a) for a in p.config[2]]
                   and ZERO_32 in [bytes32(a) for a in p.config[2]]), None)
    if lp_market is None or direct is None:
        print("need both a TXCH/LP pool and a TXCH/underlying pool")
        return 2

    route = [lp_market, vault, direct]
    path = [ZERO_32, lp_asset, underlying, ZERO_32]
    print(f"cycle  TXCH -> {lp_asset.hex()[:8]} -> {underlying.hex()[:8]} -> TXCH")
    print(f"       {lp_market.launcher_id.hex()[:10]} / {vault.launcher_id.hex()[:10]}"
          f" / {direct.launcher_id.hex()[:10]}")
    print()

    best = None
    for step in range(1, 60):
        amount = 2_000_000_000_000 * step // 60
        out = quote(route, path, amount)
        if out is None:
            continue
        if best is None or out - amount > best[2]:
            best = (amount, out, out - amount)
    if best is None or best[2] <= 0:
        print("no profitable size against the live reserves")
        return 2

    amount_in, amount_out, profit = best
    print(f"commit  {amount_in / 1e12:.6f} TXCH")
    print(f"returns {amount_out / 1e12:.6f} TXCH")
    print(f"profit  {profit / 1e12:+.6f} TXCH  ({profit / amount_in * 100:.1f}% on capital)")
    print()

    results = []
    results.append(check("the cycle returns more than it commits", profit > 0))

    legs = _plan_legs(route, path, amount_in)
    results.append(check("the middle hop is a redemption, not a trade",
                         legs[1].kind == "vault-redeem"))
    results.append(check("the redemption pays the vault's exact ratio",
                         legs[1].amount_out
                         == int(vault.state[0][0][2]) * legs[1].amount_in // int(vault.state[1])))

    spends = xch_maker_spend(amount_in, 0xE4)
    offer = make_offer(spends, {None: [NotarizedPayment(USER_PH, uint64(amount_out), [], NONCE)]})
    try:
        result = build_multihop_swap(route, path, offer)
    except Exception as exc:
        print(f"  [FAIL] the vault cycle builds as one bundle: {type(exc).__name__}: {exc}")
        return 1

    problems, checked = audit(result.bundle, external_for(route, spends))
    results.append(check(f"bundle audits clean ({checked} assertions, "
                         f"{len(result.bundle.coin_spends)} spends)", not problems))
    for problem in problems:
        print(f"         - {problem}")

    results.append(check(f"all three pools advanced ({len(result.pools)})",
                         len(result.pools) == 3))

    vault_next = next((p for p in result.pools if p.launcher_id == vault.launcher_id), None)
    if vault_next is not None:
        burned = int(vault.state[1]) - int(vault_next.state[1])
        results.append(check("the vault's LP supply fell by the burn",
                             burned == legs[1].amount_in, f"{burned} LP"))

    # The Offer is the assertion: asking beyond the cycle's yield must fail.
    greedy = make_offer(xch_maker_spend(amount_in, 0xE5),
                        {None: [NotarizedPayment(USER_PH, uint64(amount_out + 10_000_000), [], NONCE)]})
    try:
        build_multihop_swap(route, path, greedy)
        results.append(check("an over-asking Offer is refused", False))
    except Exception:
        results.append(check("an over-asking Offer is refused", True))

    # A route ENDING in a redemption belongs to the vault-route lane, which owns
    # the payout and surplus for that shape.
    try:
        _plan_legs([lp_market, vault], [ZERO_32, lp_asset, underlying], amount_in)
        build_multihop_swap([lp_market, vault], [ZERO_32, lp_asset, underlying],
                            make_offer(xch_maker_spend(amount_in, 0xE6),
                                       {underlying: [NotarizedPayment(USER_PH, uint64(1), [], NONCE)]}))
        results.append(check("a route ending in a redemption is refused here", False))
    except Exception:
        results.append(check("a route ending in a redemption is refused here", True,
                             "that shape is the vault-route lane"))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} vault-cycle checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
