#!/usr/bin/env python3
"""The router (protocol) fee on a liquidity add: charged on the swapped share only.

Owner, 2026-11-16: "we were trying to avoid charging fees for adding/removing liquidity
using the lp page since that route shouldnt charge the fee if both sides are proportional.
the fee would be charged here if a 1 sided add was desired and the zap trade is needed to
balance the pool. this would have a partial fee only on the swapped fees."

So a proportional add and every remove pay the router nothing, and an unbalanced add pays
the caller's ladder rate (api/_laneFee.js laneRouterFee) on the part of the deposit that is
effectively traded into the pool's other assets -- the same share the curve already charges
its LP fee on (forge_curve.rue `effective_product`, forge_math.effective_amounts), and the
share the V19 design charges the in-puzzle dev and DAO fees on (docs/FORGE_V19_REVIEW_1116.md,
finding 08). The router fee is outside the puzzle, so it is charged by the lanes that build
the add: the V18 add lane (forge_v18_offer.settle_add) and the routed deposit
(forge_v18_route.routed_deposit).

Per asset i, with `K` the sum of the weights, `k_i` the asset's weight, `S = RATIO_SCALE` and
`ratio = min_deposit_ratio(old, deposits)`:

    balanced_i = floor(old_i * ratio / S)
    excess_i   = deposit_i - balanced_i                (0 within DUST: rounding is not a trade)
    swapped_i  = floor(excess_i * (K - k_i) / K)       (0 for a vault: k_i == K)

The fee is paid the way a wallet-built offer pays every other lane: in the LP the add
mints, off the payout (forge_v18_offer.surplus_payments). Its size is the rate on the
swapped share's part of the mint, each asset valued at the pool's own spot (a weighted
pool prices asset i at k_i / old_i):

    fee = floor(minted * bps * sum_i(swapped_i * k_i / old_i) / (10_000 * sum_i(deposit_i * k_i / old_i)))

computed in integers (every term over the common denominator prod(old)). A deposit made
wholly of one asset of a 50/50 pair has half of it swapped, so it pays half the rate; a
zap pays the whole rate on its payout, because a zap is a Swap-tab trade.

src/lib/lpAddFee.ts is the page's mirror; the two must agree to the mojo, and
src/lib/__checks__/lpAddFee.check.ts holds them to it.
"""
from __future__ import annotations

from typing import Sequence

from forge_math import RATIO_SCALE, min_deposit_ratio, normalised_weights

BPS_SCALE = 10_000
# An excess this small is rounding in the min ratio, not a trade: a proportional add sized
# by any client lands within a mojo or two of the pool's ratio, and pays nothing.
DUST = 2


def swapped_share(old: Sequence[int], deposits: Sequence[int], weights: Sequence[int] | None = None) -> list[int]:
    """The part of each asset's deposit that is effectively traded into the others."""
    old = [int(x) for x in old]
    deposits = [int(x) for x in deposits]
    if len(deposits) != len(old):
        raise ValueError("one deposit per reserve is required")
    if any(x <= 0 for x in old) or any(d < 0 for d in deposits):
        raise ValueError("an add's swapped share needs positive reserves and non-negative deposits")
    units = normalised_weights(old, weights)
    total = sum(units)
    if not any(deposits):
        return [0 for _ in old]
    ratio = min_deposit_ratio(old, deposits)
    out = []
    for reserve, deposit, weight in zip(old, deposits, units):
        excess = deposit - (reserve * ratio) // RATIO_SCALE
        if excess <= DUST or weight == total:
            out.append(0)
            continue
        out.append(excess * (total - weight) // total)
    return out


def _valued(amounts: Sequence[int], old: Sequence[int], units: Sequence[int]) -> int:
    """sum_i(amount_i * k_i / old_i), scaled by prod(old) so it stays an integer."""
    total = 0
    for i, (amount, weight) in enumerate(zip(amounts, units)):
        if amount == 0:
            continue
        others = 1
        for j, reserve in enumerate(old):
            if j != i:
                others *= reserve
        total += amount * weight * others
    return total


def add_router_fee(minted: int, old: Sequence[int], deposits: Sequence[int], weights: Sequence[int] | None,
                   fee_bps: int) -> int:
    """The router's fee on an add, in the LP it mints: the rate on the swapped share only."""
    minted, bps = int(minted), int(fee_bps)
    if bps <= 0 or minted <= 0:
        return 0
    swapped = swapped_share(old, deposits, weights)
    if not any(swapped):
        return 0
    old = [int(x) for x in old]
    units = normalised_weights(old, weights)
    whole = _valued([int(d) for d in deposits], old, units)
    if whole <= 0:
        return 0
    fee = minted * bps * _valued(swapped, old, units) // (BPS_SCALE * whole)
    return min(fee, minted)
