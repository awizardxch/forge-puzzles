#!/usr/bin/env python3
"""The router fee is charged once per swap, on the final output.

Each pool takes its own LP fee inside the curve, so a 2-hop route pays that
twice -- that is the price of using two pools' liquidity. The *router* fee is
different: it pays for routing, the router runs once, so it is charged once. A
3-hop route and a 1-hop route of the same size owe the same rate.

Intermediates are never skimmed: they stay strictly ephemeral, which is what
makes the route atomic.
"""
import sys

sys.path.insert(0, ".")

from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from _test_route_nasset import T6, T11, external_for, load_pools, require_pools
from _forge_testkit import DEV_BPS, DEV_PH, USER_PH, audit, make_offer, payouts_to, xch_maker_spend
from forge_offer import ZERO_32
from forge_multihop_swap import build_multihop_swap
from forge_split_swap import SplitBranchSpec, build_split_swap

NONCE = bytes32.fromhex("ee" * 32)


def check(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def main() -> int:
    pools = load_pools()
    v5, v6, v4 = require_pools(pools, "86c3cd1a", "86d0f02b", "d666d3fe")
    results = []

    # ---- 2-hop: fee charged once on the final output ------------------------
    print("multi-hop TXCH -> T6 -> T11 (2 hops, 2 LP fees, 1 router fee):")
    amount_in = 400_000_000_000
    probe = build_multihop_swap([v6, v4], [ZERO_32, T6, T11],
                                make_offer(xch_maker_spend(amount_in, 0xA1),
                                           {T11: [NotarizedPayment(USER_PH, uint64(1), [], NONCE)]}))
    gross = probe.amounts[-1]
    expected = gross * DEV_BPS // 10_000
    quoted = gross - expected

    spends = xch_maker_spend(amount_in, 0xA1)
    offer = make_offer(spends, {T11: [NotarizedPayment(USER_PH, uint64(quoted), [], NONCE)]})
    with_fee = build_multihop_swap([v6, v4], [ZERO_32, T6, T11], offer, DEV_PH, DEV_BPS)
    without = build_multihop_swap([v6, v4], [ZERO_32, T6, T11], offer)

    paid = payouts_to(with_fee.bundle, T11)
    problems, checked = audit(with_fee.bundle, external_for([v6, v4], spends))
    rate = with_fee.dev_fee_collected * 10_000 / gross
    print(f"    in={amount_in} gross={gross} fee={with_fee.dev_fee_collected} ({rate:.1f}bps) "
          f"trader={paid.get(USER_PH, 0)}")
    results.append(check(f"charged once at {DEV_BPS}bps, not {DEV_BPS * 2}bps for 2 hops",
                         with_fee.dev_fee_collected == expected))
    results.append(check("dev coin created for exactly the fee", paid.get(DEV_PH, 0) == expected))
    results.append(check("dev + trader == gross (no value invented)",
                         paid.get(DEV_PH, 0) + paid.get(USER_PH, 0) == gross))
    results.append(check("zero when no fee requested", without.dev_fee_collected == 0))
    results.append(check(f"bundle audits clean ({checked} assertions)", not problems))
    for problem in problems:
        print(f"         - {problem}")

    # ---- split: one fee on the combined output, not per branch --------------
    print()
    print("split 60% V5 (1 hop) + 40% V6->V4 (2 hops), 3 hops total:")
    total_in = 1_000_000_000_000
    branches = [
        SplitBranchSpec(pools=[v5], path=[ZERO_32, T11], amount_in=600_000_000_000),
        SplitBranchSpec(pools=[v6, v4], path=[ZERO_32, T6, T11], amount_in=400_000_000_000),
    ]
    probe2 = build_split_swap(branches, make_offer(xch_maker_spend(total_in, 0xA2),
                                                   {T11: [NotarizedPayment(USER_PH, uint64(1), [], NONCE)]}))
    gross2 = probe2.total_out
    expected2 = gross2 * DEV_BPS // 10_000
    quoted2 = gross2 - expected2

    spends2 = xch_maker_spend(total_in, 0xA2)
    offer2 = make_offer(spends2, {T11: [NotarizedPayment(USER_PH, uint64(quoted2), [], NONCE)]})
    split = build_split_swap(branches, offer2, DEV_PH, DEV_BPS)
    paid2 = payouts_to(split.bundle, T11)
    problems2, checked2 = audit(split.bundle, external_for([v5, v6, v4], spends2))
    rate2 = split.dev_fee_collected * 10_000 / gross2
    print(f"    in={total_in} gross={gross2} fee={split.dev_fee_collected} ({rate2:.1f}bps) "
          f"trader={paid2.get(USER_PH, 0)}")
    results.append(check(f"charged once at {DEV_BPS}bps across 3 hops and 2 branches",
                         split.dev_fee_collected == expected2))
    results.append(check("dev coin created for exactly the fee", paid2.get(DEV_PH, 0) == expected2))
    results.append(check("dev + trader == combined gross",
                         paid2.get(DEV_PH, 0) + paid2.get(USER_PH, 0) == gross2))
    results.append(check(f"bundle audits clean ({checked2} assertions)", not problems2))
    for problem in problems2:
        print(f"         - {problem}")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} router-fee checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
