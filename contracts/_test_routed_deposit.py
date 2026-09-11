#!/usr/bin/env python3
"""Routed deposits: sell the excess at market and add, in one bundle.

Pinned to the incident that motivated the lane. On 2026-09-01, minutes after
the [4,1,1] D3 pool launched, a 50/50 CAT deposit landed on it with no TXCH at
all. The puzzle accepted it and minted 5,587 LP -- while repricing the pool 12x
and leaving roughly 1.5 TXCH of the depositor's own value sitting on the
balancer as someone else's arbitrage.

The same coins, routed through the market first, mint materially more LP and
move the pool's price not at all. This suite proves that the routed bundle
settles as ONE spend: the sale legs, the multi-asset MODE_ADD and the LP mint
in a single Offer, with the mint equal to the puzzle's own canonical figure.
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
from forge_multihop_swap import _plan_amounts, _pool_assets, add_liquidity_mint
from forge_routed_deposit import DepositSale, build_routed_deposit

results = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    results.append(ok)
    return ok


def reserve_of(pool, asset) -> int:
    for asset_id, _coin_id, amount in pool.state[0]:
        if bytes32(asset_id) == asset:
            return int(amount)
    raise AssertionError("no such reserve")


def deposit_offer(backing: int, cats: dict, lp_asset: bytes32, minimum_lp: int, salt: int):
    """The depositor's Offer: their uneven assets plus the mint's XCH backing."""
    spends = list(xch_maker_spend(backing, salt))
    for index, (asset, amount) in enumerate(sorted(cats.items())):
        spends.extend(cat_maker_spend(asset, amount, salt + index + 1))
    offer = make_offer(
        spends, {lp_asset: [NotarizedPayment(USER_PH, uint64(minimum_lp), [], NONCE)]})
    return offer, spends


def main() -> int:
    # ── the D3 shape: a weighted three-asset pool, deposited 50/50 in CATs ────
    # TXCH carries two thirds of the value at 4u, so a CAT-only deposit is as
    # far off ratio as the incident's was.
    assets = sorted([ZERO_32, X, Z])
    bootstrap = {ZERO_32: 2_000_000_000_000, X: 4_452, Z: 5_101}
    target = make_pool(assets, bootstrap, 0xD3, weights=[4, 1, 1])

    # The market the sales price against: two deep XCH pairs, one per CAT.
    market_x = make_pool(sorted([ZERO_32, X]), {ZERO_32: 8_219_215_969_811, X: 73_196}, 0xA1)
    market_z = make_pool(sorted([ZERO_32, Z]), {ZERO_32: 3_768_059_677_663, Z: 38_975}, 0xA2)

    lp_asset = bytes32(target.lp_asset_id)
    deposit_cats = {X: 50_000, Z: 50_000}
    backing = 20_000

    print("the incident deposit, direct: 50,000 X + 50,000 Z into a [4,1,1] pool")
    direct_offer, direct_spends = deposit_offer(backing, deposit_cats, lp_asset, 1, 0xE1)
    direct = build_routed_deposit(target, [], direct_offer)
    print(f"    mints {direct.minted:,} LP   deposits "
          f"{ {a.hex()[:4]: v for a, v in direct.deposits.items()} }")
    check("the direct add builds with no sales at all", direct.minted > 0)
    problems, checks = audit(direct.bundle, external_of([target], direct_spends))
    check(f"the direct add audits clean ({checks} assertions, "
          f"{len(direct.bundle.coin_spends)} spends)", not problems)
    for problem in problems:
        print(f"         - {problem}")

    # ── the same coins, routed ───────────────────────────────────────────────
    # Sell most of each CAT into XCH at market, so what reaches the pool is
    # close to its own reserve ratio and pays almost no imbalance fee.
    print()
    print("the same coins, routed: sell the excess into XCH at market first")
    # Sized so what remains lands near the pool's own ratio: the deposit is
    # worth ~3.5x the pool, so each CAT keeps ~3.5 times its reserve and sells
    # the rest. The TypeScript planner bisects for this; here it is arithmetic.
    sales = [
        DepositSale([market_x], [X, ZERO_32], 34_400),
        DepositSale([market_z], [Z, ZERO_32], 32_200),
    ]
    routed_offer, routed_spends = deposit_offer(backing, deposit_cats, lp_asset, 1, 0xE5)
    routed = build_routed_deposit(target, sales, routed_offer)
    print(f"    sales {[f'{v:,}' for v in routed.sale_outputs]} XCH-mojos"
          f"   backing {routed.backing:,}")
    print(f"    mints {routed.minted:,} LP   deposits "
          f"{ {a.hex()[:4]: f'{v:,}' for a, v in routed.deposits.items()} }")

    check("the routed deposit mints MORE LP than the direct one",
          routed.minted > direct.minted,
          f"{routed.minted:,} vs {direct.minted:,}")

    problems, checks = audit(
        routed.bundle, external_of([target, market_x, market_z], routed_spends))
    check(f"the routed deposit audits clean ({checks} assertions, "
          f"{len(routed.bundle.coin_spends)} spends)", not problems)
    for problem in problems:
        print(f"         - {problem}")

    check("it settles as ONE bundle: sales and the add together",
          len(routed.pools) == 3,
          f"{len(routed.pools)} pools advanced")

    # The mint is the puzzle's own bracket for the deposit that actually
    # landed -- not an estimate, and not the plan's guess.
    check("the mint equals the canonical figure for the resolved deposit",
          routed.minted == add_liquidity_mint(target, routed.deposits))

    check("the whole mint reaches the depositor",
          paid_to(routed.bundle, lp_asset, USER_PH) == routed.minted,
          f"{paid_to(routed.bundle, lp_asset, USER_PH):,} == {routed.minted:,}")

    # Every reserve grew by exactly what the resolve said it would.
    grew_right = all(
        reserve_of(routed.target, asset) == bootstrap[asset] + routed.deposits[asset]
        for asset in _pool_assets(target)
    )
    check("every target reserve grew by its resolved deposit", grew_right,
          " ".join(f"{a.hex()[:4]}={reserve_of(routed.target, a):,}"
                   for a in _pool_assets(target)))

    check("the LP supply grew by the mint",
          int(routed.target.state[1]) == int(target.state[1]) + routed.minted)

    # The point of the lane: what lands is near the pool's own ratio, so it
    # barely moves the price. The direct add moves it enormously.
    def off_ratio(deposits) -> float:
        shares = [deposits[a] / bootstrap[a] for a in _pool_assets(target) if deposits[a] > 0]
        return max(shares) / min(shares) if len(shares) > 1 and min(shares) > 0 else float("inf")

    routed_off, direct_off = off_ratio(routed.deposits), off_ratio(direct.deposits)
    print(f"    off-ratio spread: routed {routed_off:.2f}x   direct {direct_off:.2f}x")
    check("the routed deposit lands far closer to the pool's ratio",
          routed_off < direct_off / 4,
          f"{routed_off:.2f}x vs {direct_off:.2f}x")

    # ── the Offer's minimum is the depositor's safety ────────────────────────
    print()
    print("contract refusals:")
    greedy_offer, _ = deposit_offer(
        backing, deposit_cats, lp_asset, routed.minted + 1, 0xE9)
    try:
        build_routed_deposit(target, sales, greedy_offer)
        check("an Offer asking more LP than the deposit mints is refused", False)
    except ValueError as exc:
        check("an Offer asking more LP than the deposit mints is refused", True,
              str(exc)[:60])

    # A sale through the target would price against reserves the add moves.
    try:
        build_routed_deposit(
            target, [DepositSale([target], [X, ZERO_32], 1_000)], routed_offer)
        check("a sale routed through the target pool is refused", False)
    except ValueError as exc:
        check("a sale routed through the target pool is refused", True, str(exc)[:60])

    # Without XCH there is nothing to back the mint with.
    spends = list(cat_maker_spend(X, 50_000, 0xF1))
    bare = make_offer(spends, {lp_asset: [NotarizedPayment(USER_PH, uint64(1), [], NONCE)]})
    try:
        build_routed_deposit(target, [], bare)
        check("an Offer with no XCH backing is refused", False)
    except ValueError as exc:
        check("an Offer with no XCH backing is refused", True, str(exc)[:60])

    # A sale bigger than the Offer put up cannot be funded.
    try:
        build_routed_deposit(
            target, [DepositSale([market_x], [X, ZERO_32], 60_000)], routed_offer)
        check("a sale larger than the Offer's own coins is refused", False)
    except ValueError as exc:
        check("a sale larger than the Offer's own coins is refused", True, str(exc)[:60])

    # ── a CAT-only target: the backing has nowhere to deposit ────────────────
    # The pool holds no native reserve, so XCH beyond the mint is handed back
    # rather than burned.
    print()
    print("a target with no native reserve returns the unused backing:")
    cat_target = make_pool(sorted([X, Z]), {X: 20_000, Z: 20_000}, 0xC7)
    cat_lp = bytes32(cat_target.lp_asset_id)
    cat_offer, cat_spends = deposit_offer(9_000, {X: 6_000, Z: 2_000}, cat_lp, 1, 0xF5)
    cat_result = build_routed_deposit(cat_target, [], cat_offer)
    print(f"    mints {cat_result.minted:,} LP   backing {cat_result.backing:,}"
          f"   returned {cat_result.leftover_xch:,}")
    check("the mint is backed and the rest returned",
          cat_result.leftover_xch == 9_000 - cat_result.backing
          and cat_result.backing >= cat_result.minted,
          f"backing {cat_result.backing:,} >= mint {cat_result.minted:,}")
    problems, checks = audit(cat_result.bundle, external_of([cat_target], cat_spends))
    check(f"the CAT-only routed deposit audits clean ({checks} assertions)", not problems)
    for problem in problems:
        print(f"         - {problem}")

    print()
    print(f"{sum(results)}/{len(results)} routed-deposit checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
