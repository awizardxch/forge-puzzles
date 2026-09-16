#!/usr/bin/env python3
"""A split whose branches run in parallel through a swap pool and a vault.

This is the shape the router wants when one pool holds an asset cheaply and
another holds it deeply: send part of the order down each, so the trade lands at
a better average price *and* leaves the two pools closer to level than either
would alone. Splitting is a balancer that pays for itself.

The vault branch is the awkward one. A single-asset pool cannot trade, so the
branch reaches it by swapping into the vault's LP and then burning that LP for
the underlying -- MODE_REMOVE, not MODE_SWAP. The LP exists only inside the
bundle, and both branches settle against one Offer.
SUPERSEDED by _test_forge_cycles.py, which settles several vault-crossing cycles
as one split against pools it builds itself. Kept for the record; it skips when
the deployment index is empty.
"""
import io
import json
import sys

sys.path.insert(0, ".")

from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin as fs
from _forge_testkit import USER_PH, audit, make_offer, xch_maker_spend
from _test_route_nasset import external_for, load_pools
from forge_offer import ZERO_32
from forge_split_swap import SplitBranchSpec, build_split_swap

NONCE = bytes32.fromhex("ee" * 32)


def main() -> int:
    pools = load_pools()
    vault = next((p for p in pools.values() if len(p.config[2]) == 1), None)
    if vault is None:
        print("no single-asset vault in the deployment index")
        return 2

    lp_asset = bytes32(vault.lp_asset_id)
    underlying = bytes32(vault.config[2][0])
    flp_pool = next((p for p in pools.values()
                     if lp_asset in [bytes32(a) for a in p.config[2]] and len(p.config[2]) > 1), None)
    direct = next((p for p in pools.values()
                   if underlying in [bytes32(a) for a in p.config[2]]
                   and ZERO_32 in [bytes32(a) for a in p.config[2]]
                   and p.launcher_id != vault.launcher_id), None)
    if flp_pool is None or direct is None:
        print("need both a TXCH/LP pool and a TXCH/underlying pool")
        return 2

    print(f"vault  {vault.launcher_id.hex()[:10]}  {underlying.hex()[:8]} reserve={int(vault.state[0][0][2])} lp={int(vault.state[1])}")
    print(f"flp    {flp_pool.launcher_id.hex()[:10]}  TXCH/{lp_asset.hex()[:8]}")
    print(f"direct {direct.launcher_id.hex()[:10]}  TXCH/{underlying.hex()[:8]}")
    print()

    total_in = 1_000_000_000_000
    split_in = total_in // 2
    branches = [
        SplitBranchSpec(pools=[direct], path=[ZERO_32, underlying], amount_in=split_in),
        SplitBranchSpec(pools=[flp_pool, vault], path=[ZERO_32, lp_asset, underlying],
                        amount_in=total_in - split_in),
    ]

    # Ask the route what it yields; these run against the live snapshot.
    probe = build_split_swap(branches, make_offer(
        xch_maker_spend(total_in, 0x91),
        {underlying: [NotarizedPayment(USER_PH, uint64(1), [], NONCE)]}))
    print(f"split 50/50: total out {probe.total_out} "
          f"(branch amounts {[legs[-1] for legs in probe.branch_amounts]})")

    results = []
    spends = xch_maker_spend(total_in, 0x91)
    offer = make_offer(spends, {underlying: [
        NotarizedPayment(USER_PH, uint64(probe.total_out), [], NONCE)]})
    try:
        result = build_split_swap(branches, offer)
    except Exception as exc:
        print(f"  [FAIL] split with a vault branch builds: {type(exc).__name__}: {exc}")
        return 1

    touched = [direct, flp_pool, vault]
    problems, checked = audit(result.bundle, external_for(touched, spends))
    ok = not problems
    print(f"  [{'PASS' if ok else 'FAIL'}] split with a vault branch: "
          f"{len(result.bundle.coin_spends)} spends, {checked} assertions")
    for problem in problems:
        print(f"         - {problem}")
    results.append(ok)

    # All three singletons must advance, or the split was not atomic.
    print(f"  [{'PASS' if len(result.pools) == 3 else 'FAIL'}] all three pools advanced "
          f"({len(result.pools)})")
    results.append(len(result.pools) == 3)

    vault_next = next((p for p in result.pools if p.launcher_id == vault.launcher_id), None)
    if vault_next is not None:
        burned = int(vault.state[1]) - int(vault_next.state[1])
        paid = int(vault.state[0][0][2]) - int(vault_next.state[0][0][2])
        # From V8 a vault charges its LP fee for the crossing, so the exact
        # ratio is net of that fee -- the fee-free expectation was written
        # against V7 vaults and mis-read every live V8+ redemption.
        from forge_math import WEIGHT_SCALE, vault_fee_bps
        fee = vault_fee_bps(1, int(vault.config[0]), int(vault.config[4]))
        expected = (int(vault.state[0][0][2]) * burned * (WEIGHT_SCALE - fee)
                    // (int(vault.state[1]) * WEIGHT_SCALE))
        print(f"  [{'PASS' if paid == expected else 'FAIL'}] vault paid its exact ratio "
              f"(burned {burned} LP -> {paid}, expected {expected})")
        results.append(paid == expected)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} split-with-vault checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
