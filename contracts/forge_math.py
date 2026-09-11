#!/usr/bin/env python3
"""N-asset pool math for Forge V6, mirroring pool_singleton_v6.rue exactly.

Every function here has a counterpart in the CLVM puzzle. They must agree to the
integer: the puzzle recomputes each result and rejects the spend on any
mismatch, which surfaces as "LP minimum does not equal canonical mint" or a bare
clvm raise rather than a useful error. `_test_v6_math.py` checks agreement by
driving the compiled puzzle with values produced here.

V6 differs from V5 in two ways that matter to callers:

* **N assets** rather than exactly two.
* **Imbalance fee on adds.** LP mints against the *effective* deposit — balanced
  portion in full, excess net of swap fee on the (n-1)/n share that the pool has
  to swap internally. Without it, an unbalanced add followed by a pro-rata
  remove is a fee-free swap.
"""
from __future__ import annotations

from math import prod
from typing import Sequence

WEIGHT_SCALE = 10_000
# The revision that first charged the LP fee on a vault crossing; before it, a
# single-asset pool kept the exact reserve-to-LP ratio it was created with.
VAULT_FEE_VERSION = 8
RATIO_SCALE = 1_000_000_000_000
MAX_ASSETS = 10
# V6 requires two assets, V7 allows one. The puzzle enforces its own floor, so
# the shared mirror accepts one upward rather than duplicating the module.
MIN_ASSETS = 1
MINIMUM_LOCKED_LP = 1


def default_weights(count: int) -> list[int]:
    """Equal weights, with the indivisible remainder spread one bps per asset.

    The puzzle requires every weight to be exactly ``base`` or ``base + 1``, so
    the remainder cannot be piled onto one asset. Doing that made 6- and 7-asset
    pools impossible to create -- 10000 leaves 4 over in both cases, putting the
    first weight 4 bps above base and failing ``weights_near_equal``.
    """
    base = WEIGHT_SCALE // count
    remainder = WEIGHT_SCALE - base * count
    return [base + 1] * remainder + [base] * (count - remainder)


def valid_weights(weights: Sequence[int]) -> bool:
    """Mirror of valid_assets' weight rule: sum to scale, equal within 1 bps."""
    count = len(weights)
    if count < MIN_ASSETS or count > MAX_ASSETS:
        return False
    base = WEIGHT_SCALE // count
    return sum(weights) == WEIGHT_SCALE and all(w in (base, base + 1) for w in weights)


def min_deposit_ratio(old: Sequence[int], deposits: Sequence[int]) -> int:
    """Largest proportional deposit contained in the offered amounts."""
    return min((deposit * RATIO_SCALE) // reserve for reserve, deposit in zip(old, deposits))


def normalised_weights(old: Sequence[int], weights: Sequence[int] | None) -> list[int]:
    """Integer weight units, defaulting to one per asset.

    Callers that predate weighted pools pass nothing and get the equal-weight
    behavior they always had, which is the V6 shape.
    """
    if weights is None:
        return [1] * len(old)
    if len(weights) != len(old):
        raise ValueError("one weight per reserve is required")
    if any(int(w) < 1 for w in weights):
        raise ValueError("weights are positive integer units")
    return [int(w) for w in weights]


def effective_amounts(
    old: Sequence[int],
    deposits: Sequence[int],
    fee_bps: int,
    weights: Sequence[int] | None = None,
    version: int = VAULT_FEE_VERSION,
) -> list[int]:
    """Reserves as the LP mint sees them, mirroring `effective_product`.

    The imbalance fee falls on the share of the excess that is effectively a
    trade into the other assets, which in weight units is (K - k_i)/K -- NOT
    (n - 1)/n. Those coincide only when every weight is one, so an 80/20 pool
    priced with the old formula disagreed with the puzzle and its deposits were
    rejected outright.

    A vault (one asset holding all the weight) is the other special case: the
    imbalance share is zero there, but for a vault the deposit IS the trade, so
    the puzzle charges the full LP fee on the whole deposit instead.
    """
    units = normalised_weights(old, weights)
    total_weight = sum(units)
    ratio = min_deposit_ratio(old, deposits)
    effective = []
    for reserve, deposit, weight in zip(old, deposits, units):
        balanced = (reserve * ratio) // RATIO_SCALE
        excess = deposit - balanced
        # A vault is one asset holding all the weight. Only from V8 does the
        # puzzle charge the LP fee on the whole deposit there; a V7 vault keeps
        # the exact ratio it was created with, so it must stay free.
        vault = total_weight == weight and version >= VAULT_FEE_VERSION
        fee_base = deposit if vault else excess
        fee_share = total_weight if vault else total_weight - weight
        fee = (fee_base * fee_bps * fee_share) // (total_weight * WEIGHT_SCALE)
        effective.append(reserve + balanced + excess - fee)
    return effective


class DepositTooSmall(ValueError):
    """Deposit is positive but mints no LP, which the puzzle rejects outright."""


def require_positive_mint(mint: int, asset_count: int) -> int:
    """LP scales with the n-th root of the product ratio, so a small deposit on a
    wide pool can floor to zero. Refuse it here with something a user can act on
    rather than letting the puzzle fail with a bare clvm raise."""
    if mint <= 0:
        raise DepositTooSmall(
            f"This deposit is too small to mint any LP in a {asset_count}-asset pool. "
            "Increase the amount, or spread it across more of the pool's assets."
        )
    return mint


def weighted_product(amounts: Sequence[int], weights: Sequence[int]) -> int:
    """prod(amount_i ** weight_i), the quantity the pool holds constant."""
    product = 1
    for amount, weight in zip(amounts, weights):
        product *= amount ** weight
    return product


def invariant_lp_mint(
    old: Sequence[int],
    deposits: Sequence[int],
    total_lp: int,
    fee_bps: int,
    weights: Sequence[int] | None = None,
    version: int = VAULT_FEE_VERSION,
) -> int:
    """Largest mint the puzzle will accept for this deposit.

    The puzzle brackets it as the largest k where
    ``(total_lp + k)^K * prod(old_i^w_i) <= total_lp^K * prod(effective_i^w_i)``
    with K the sum of the weights, so solve for the same k by bisection rather
    than a float root.

    K is the SUM OF THE WEIGHTS, not the asset count. Those agree only when every
    weight is one; on an 80/20 pool the exponent is 5 rather than 2, and using
    the count produced a mint the puzzle rejected.
    """
    count = len(old)
    if count < MIN_ASSETS or any(reserve <= 0 for reserve in old):
        raise ValueError("invariant mint needs an initialized pool")
    if any(deposit < 0 for deposit in deposits):
        raise ValueError("deposits cannot be negative")
    if all(deposit == 0 for deposit in deposits):
        return 0

    units = normalised_weights(old, weights)
    total_weight = sum(units)
    old_product = weighted_product(old, units)
    target = (total_lp ** total_weight) * weighted_product(
        effective_amounts(old, deposits, fee_bps, units, version), units)

    low, high, best = 0, max(total_lp * 4, 1024), 0
    while ((total_lp + high) ** total_weight) * old_product <= target:
        high *= 2
    while low <= high:
        mid = (low + high) // 2
        if ((total_lp + mid) ** total_weight) * old_product <= target:
            best, low = mid, mid + 1
        else:
            high = mid - 1
    return best


def withdrawal_amounts(
    old: Sequence[int],
    burn: int,
    total_lp: int,
    fee_bps: int = 0,
) -> list[int]:
    """Pro-rata payout, floored per asset — mirror of exact_withdrawal.

    `fee_bps` is zero for every multi-asset pool, and for any vault before V8,
    which leaves this exactly proportional. Callers must pass a non-zero fee only
    for a V8 vault, where crossing between the LP and the single reserve is the
    pool's only trade and therefore carries its LP fee. Passing it for a V7 vault
    would quote a payout smaller than that puzzle actually releases.
    """
    if burn <= 0 or burn >= total_lp:
        raise ValueError("burn must be positive and leave the pool alive")
    return [
        (reserve * burn * (WEIGHT_SCALE - fee_bps)) // (total_lp * WEIGHT_SCALE)
        for reserve in old
    ]


def vault_fee_bps(reserve_count: int, version: int, fee_bps: int) -> int:
    """The LP fee a vault charges for crossing it. Zero for anything else.

    Single-asset pools existed from V7, but only V8 charges for the crossing.
    A V7 vault keeps the exact reserve-to-LP ratio it was created with.
    """
    return fee_bps if reserve_count == 1 and version >= 8 else 0


def swap_output(
    reserve_in: int,
    reserve_out: int,
    gross_input: int,
    fee_bps: int,
    weight_in: int = 1,
    weight_out: int = 1,
) -> int:
    """Mirror of exact_swap_output. Only the traded pair moves.

    The puzzle holds `reserve_in^w_in * reserve_out^w_out` constant and brackets
    the output exactly -- it accepts one figure and refuses every other -- so this
    has to reproduce that bracket rather than approximate it.

    With equal weights the invariant collapses to constant product and the closed
    form below is exact, which is why the weight-blind version was right for every
    pool anyone had actually traded. It is wrong the moment the weights differ: on
    an 80/20 pool it under-quotes by a factor of four, and the puzzle rejects the
    successor reserve the builder derives from it.
    """
    if reserve_in <= 0 or reserve_out <= 0 or gross_input <= 0:
        raise ValueError("swap reserves and input must be positive")
    if not 0 <= fee_bps <= 1_000:  # widest puzzle bound; creation enforces the per-version one
        raise ValueError("fee_bps outside V6 bounds")
    if weight_in < 1 or weight_out < 1:
        raise ValueError("weights are positive integer units")
    effective_input = (gross_input * (WEIGHT_SCALE - fee_bps)) // WEIGHT_SCALE
    if effective_input <= 0:
        raise ValueError("swap effective input rounds to zero")

    charged_in = reserve_in + effective_input
    if weight_in == weight_out:
        amount_out = (reserve_out * effective_input) // charged_in
    else:
        # Smallest surviving reserve that still holds the invariant; the puzzle
        # additionally requires one less to break it, which this is by
        # construction as the binary search converges on the boundary.
        floor = reserve_in ** weight_in * reserve_out ** weight_out
        low, high, best = 1, reserve_out - 1, None
        while low <= high:
            mid = (low + high) // 2
            if charged_in ** weight_in * mid ** weight_out >= floor:
                best, high = mid, mid - 1
            else:
                low = mid + 1
        if best is None:
            raise ValueError("swap output rounds to zero")
        amount_out = reserve_out - best
    if amount_out <= 0:
        raise ValueError("swap output rounds to zero")
    return amount_out


def solve_native_add_split(
    total_xch: int,
    reserves: Sequence[int],
    native_index: int,
    deposits: Sequence[int],
    total_lp: int,
    fee_bps: int,
    weights: Sequence[int] | None = None,
    version: int = VAULT_FEE_VERSION,
) -> tuple[int, int]:
    """Split one XCH settlement into native reserve funding plus LP backing.

    A native-XCH pool funds both from the same settlement coin, so the caller
    supplies a single total and the pool solves ``total = deposit + lp_mint``.
    Bisection works because raising the reserve deposit raises the mint too, so
    ``deposit + mint`` is monotonic in the deposit.
    """
    if total_xch <= 0 or total_lp <= 0 or any(reserve <= 0 for reserve in reserves):
        raise ValueError("native add split requires positive reserves, LP supply, and XCH")

    # LP scales with the n-th root of the product ratio, so on a wide pool a
    # small single-asset deposit can round to no LP at all. The puzzle requires
    # lp_delta > 0 on an add, so say so here instead of returning a zero mint.
    def required_for(native_deposit: int) -> tuple[int, int]:
        trial = list(deposits)
        trial[native_index] = native_deposit
        mint = invariant_lp_mint(reserves, trial, total_lp, fee_bps, weights, version)
        return native_deposit + mint, mint

    low, high, best = 0, total_xch, None
    while low <= high:
        mid = (low + high) // 2
        required, mint = required_for(mid)
        if required == total_xch:
            return mid, require_positive_mint(mint, len(reserves))
        if required < total_xch:
            best = (mid, mint)
            low = mid + 1
        else:
            high = mid - 1

    # Integer rounding can straddle the exact total; sweep the neighbourhood.
    start = max(0, (best[0] if best else 0) - 4)
    end = min(total_xch, (best[0] if best else total_xch) + 4)
    for native_deposit in range(start, end + 1):
        required, mint = required_for(native_deposit)
        if required == total_xch:
            return native_deposit, require_positive_mint(mint, len(reserves))
    raise ValueError("native add XCH does not split into reserve funding plus LP backing")
