#!/usr/bin/env python3
"""Routed deposits: swap the excess at market and add, in ONE bundle.

An uneven deposit is legal -- `invariant_lp_mint` reprices the pool and charges
the imbalance fee -- but that fee is priced to protect EXISTING LPs, and the
gap the repricing opens against the market is collected by the next arbitrage
bundle rather than held by the minted LP. Seen on chain twice: an unbalanced
add moved C2 4.5x off market (2026-08-30), and a 50/50 CAT add onto the
[4,1,1] D3 pool opened a 12x spread minutes after launch (2026-09-01), leaving
~1.5 TXCH of the depositor's own value on the table.

The alternative this lane settles: sell the excess through OTHER pools at
market -- which pays ordinary swap fees, not the imbalance fee -- and arrive at
the target with a reserve-proportional vector. The depositor keeps the
difference, and the pool's price never moves, so no arbitrage opens behind
them.

Shape
-----
This is not the flow lane. A flow is a cycle: one start asset in, the same
asset out, profit asserted by the Offer's requested amount. A deposit is a
funnel: SEVERAL assets in, LP out. So the entry is per-asset rather than
rescaled from one pot, and the exit is the mint.

    offer:   the deposit assets, uneven, plus XCH backing
      |
      +-- sale legs   excess -> deficit, through other pools (MODE_SWAP)
      |
      +-- MODE_ADD    every target reserve grows; the LP eve mints
      |
      v
    LP settlement -> the depositor, with any surplus over their minimum

Why the Offer is the safety
---------------------------
The depositor's Offer bounds everything: it puts up exactly the assets they
chose to deposit, and it requests a MINIMUM amount of LP. Amounts are re-derived
here against the pools' live snapshots -- intent decides the route, the chain
decides the numbers -- so a plan quoted against stale reserves cannot mint less
than the Offer demands without failing outright.

The XCH backing
---------------
Every CAT mojo is an XCH mojo: minting M mojos of LP consumes M mojos of XCH
bundle value (one for the eve coin, M-1 as its extra_delta). That value must
come out of the Offer, exactly as the single-pool add path already requires
("add Offer must provide XCH backing equal to LP mint plus native reserve
funding"). When the target holds a native XCH reserve the two draw on the same
settlement, and each mojo held back as backing is a mojo that does not deposit
-- so the mint shrinks as the backing grows and the smallest sufficient backing
is found by bisection, the same fixed point the vault-wrap lane solves.

NOTE: Not audited. Testnet only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend, make_spend
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD,
    LineageProof,
    SpendableCAT,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton, solution_for_singleton
from chia.wallet.trading.offer import OFFER_MOD, Offer
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from forge_offer import (
    MODE_SWAP,
    ZERO_32,
    PoolCoin,
    ReserveCoin,
    V3Pool,
    _pool_inner,
    _settlement_group,
    aggregate_with_offer,
    reserve_inner_puzzle,
    reserve_solution as build_reserve_solution,
    find_offer_settlements,
    requested_amounts,
    requested_solution,
)
from forge_multihop_swap import (
    _build_add_liquidity_leg,
    _plan_amounts,
    _plan_legs,
    _pool_assets,
    _reserve_puzzle_hash,
    _settlement_puzzle_hash,
    add_liquidity_mint,
    extend_plan_for_protocol_fee,
    lp_mint_eve_puzzle,
    protocol_fee_for,
    vault_leg_kind,
)
from forge_flow_balance import _sim_pool


@dataclass(frozen=True)
class DepositSale:
    """One excess asset sold into a deficit asset, through a chain of pools.

    `path` is the asset chain -- `[excess, ..., deficit]` -- and `pools` the
    pool crossed at each step, so a sale of length one is a direct swap and a
    longer one bridges through XCH like any routed trade. `amount_in` is how
    much of the excess to sell; it is intent, checked against what the Offer
    actually put up.
    """

    pools: Sequence[V3Pool]
    path: Sequence[bytes32]
    amount_in: int


@dataclass(frozen=True)
class RoutedDepositResult:
    bundle: WalletSpendBundle
    pools: list[V3Pool]
    """Successors: every pool a sale crossed, then the target."""
    target: V3Pool
    """The target's successor -- the pool the deposit landed in."""
    deposits: dict[bytes32, int]
    minted: int
    backing: int
    sale_outputs: list[int]
    leftover_xch: int


def _validate(target: V3Pool, sales: Sequence[DepositSale]) -> None:
    target_assets = set(_pool_assets(target))
    crossings: dict[bytes32, int] = {}
    pairs: set[tuple[bytes32, bytes32, bytes32]] = set()

    for sale in sales:
        if sale.amount_in <= 0:
            raise ValueError("every sale needs a positive input")
        if len(sale.pools) < 1 or len(sale.path) != len(sale.pools) + 1:
            raise ValueError("a sale's path must carry one more asset than pools")
        if sale.path[0] not in target_assets:
            raise ValueError("a sale must start from an asset the target pool holds")
        if sale.path[-1] not in target_assets:
            raise ValueError("a sale must end in an asset the target pool holds")
        if sale.path[0] == sale.path[-1]:
            raise ValueError("a sale must move value between two different assets")
        interior = list(sale.path)
        if len(set(interior)) != len(interior):
            raise ValueError("a sale may not revisit an asset")

        for index, pool in enumerate(sale.pools):
            if pool.launcher_id == target.launcher_id:
                # Crossing the target on the way in would price the swap
                # against reserves the add is about to move, and hand the
                # depositor a quote the add then invalidates.
                raise ValueError("a sale may not route through the pool being deposited into")
            count = crossings.get(pool.launcher_id, 0)
            if count >= 4:
                raise ValueError("a routed deposit may cross one pool at most four times")
            crossings[pool.launcher_id] = count + 1
            pair = (pool.launcher_id, sale.path[index], sale.path[index + 1])
            if pair in pairs:
                raise ValueError(
                    "a routed deposit crosses each pool pair at most once; net same-pair sales first")
            pairs.add(pair)

            assets = set(_pool_assets(pool))
            if sale.path[index] in assets and sale.path[index + 1] in assets:
                continue
            if vault_leg_kind(pool, sale.path[index], sale.path[index + 1]) is not None:
                raise ValueError("a routed deposit does not settle vault crossings")
            raise ValueError(
                f"pool {pool.launcher_id.hex()[:16]} does not trade its assigned sale leg")


def build_routed_deposit(
    target: V3Pool,
    sales: Sequence[DepositSale],
    offer: Offer,
) -> RoutedDepositResult:
    """Settle sales and a multi-asset add as one atomic bundle.

    No router fee: the sales exist to hand the depositor value the imbalance
    fee would otherwise have donated, so charging for them would take back part
    of what the lane exists to return.
    """
    _validate(target, sales)

    target_assets = _pool_assets(target)
    every_asset = set(target_assets) | {
        asset for sale in sales for asset in sale.path
    } | {asset for sale in sales for pool in sale.pools for asset in _pool_assets(pool)}
    parsed = find_offer_settlements(offer, tuple(every_asset))

    def key(asset: bytes32):
        return None if asset == ZERO_32 else asset

    # Minting is only possible against XCH the Offer put up, whether or not the
    # target holds a native reserve of its own.
    if None not in parsed:
        raise ValueError("a routed deposit Offer must provide the XCH that backs its LP mint")
    xch_settlement = parsed[None]
    offered_xch = int(xch_settlement.coin.amount)

    lp_asset = bytes32(target.lp_asset_id)
    requested = requested_amounts(offer)
    if set(requested) - {lp_asset}:
        raise ValueError("a routed deposit Offer requests only the pool's LP")
    minimum_lp = requested.get(lp_asset, 0)
    if minimum_lp <= 0:
        raise ValueError("a routed deposit Offer must request the pool's LP")

    offered = {
        asset: int(parsed[key(asset)].coin.amount)
        for asset in target_assets
        if key(asset) in parsed
    }
    if not offered:
        raise ValueError("a routed deposit Offer must provide at least one of the pool's assets")

    def resolve(backing: int):
        """Deposits, mint and per-sale outputs at this much XCH held back.

        `pot` is what each asset has available: what the Offer put up, less
        what the sales spend, plus what they produce. Whatever remains of a
        target asset deposits.
        """
        if backing < 0 or backing > offered_xch:
            raise ValueError("backing outside the Offer's XCH")
        pot: dict[bytes32, int] = dict(offered)
        if ZERO_32 in pot:
            pot[ZERO_32] = offered_xch - backing
        leftover = 0 if ZERO_32 in pot else offered_xch - backing

        sim_amounts: dict[bytes32, dict[bytes32, int]] = {}
        outputs: list[int] = []
        for sale in sales:
            source = sale.path[0]
            if pot.get(source, 0) < sale.amount_in:
                raise ValueError(
                    f"the Offer does not provide enough {source.hex()[:12]} to fund its sale")
            pot[source] -= sale.amount_in

            running = sale.amount_in
            for index, pool in enumerate(sale.pools):
                lid = pool.launcher_id
                if lid not in sim_amounts:
                    sim_amounts[lid] = dict(_plan_amounts(pool))
                quote_pool = _sim_pool(pool, sim_amounts[lid], int(pool.state[1]))
                leg = _plan_legs(
                    [quote_pool], [sale.path[index], sale.path[index + 1]], running)[0]
                sim_amounts[lid][sale.path[index]] += running
                sim_amounts[lid][sale.path[index + 1]] -= leg.amount_out
                running = leg.amount_out - leg.protocol_fee
                if running <= 0:
                    raise ValueError("a sale leg resolved to nothing")
            pot[sale.path[-1]] = pot.get(sale.path[-1], 0) + running
            outputs.append(running)

        deposits = {asset: pot.get(asset, 0) for asset in target_assets}
        if all(amount == 0 for amount in deposits.values()):
            raise ValueError("the routed deposit funds no reserve at all")
        return deposits, add_liquidity_mint(target, deposits), outputs, leftover

    # ── the backing fixed point ──────────────────────────────────────────────
    # Held-back XCH does not deposit, so on a pool with a native reserve the
    # mint shrinks as the backing grows: bisect for the smallest backing that
    # covers its own mint. A pool with no XCH reserve is the degenerate case --
    # the mint does not move at all and the search lands on the mint itself.
    # Integer steps leave a mojo or two of slack between backing and mint; that
    # is unallocated bundle value and lands as network fee, the cheapest place
    # for it.
    def backing_suffices(candidate: int) -> bool:
        try:
            _, minted, _, _ = resolve(candidate)
        except ValueError:
            # A backing so large the deposit cannot quote counts as sufficient:
            # the search then shrinks toward the smallest workable one, and the
            # final resolve surfaces the real error if none exists.
            return True
        return minted <= candidate

    low, high = 0, offered_xch
    if not backing_suffices(high):
        raise ValueError("the Offer's XCH cannot back the LP this deposit mints")
    while low < high:
        mid = (low + high) // 2
        if backing_suffices(mid):
            high = mid
        else:
            low = mid + 1
    backing = low

    deposits, minted, sale_outputs, leftover_xch = resolve(backing)
    if minted < minimum_lp:
        raise ValueError("the routed deposit mints less LP than the Offer requires")

    # ── build the spends ─────────────────────────────────────────────────────
    cat_spendables: dict[bytes32, list[SpendableCAT]] = {}
    native_spends: list[CoinSpend] = []
    pool_spends: list[CoinSpend] = []
    successor_pools: list[V3Pool] = []

    def add_cat(asset_id: bytes32, spendable: SpendableCAT) -> None:
        cat_spendables.setdefault(asset_id, []).append(spendable)

    # One named settlement coin per asset; every other coin of that asset is
    # spent carrying only its own empty group so the ring closes. A reserve
    # asserts the named coin's announcement, and the mojos balance through the
    # CAT ring regardless of which coin was named -- that is what lets an
    # entry settlement and a sale's output fund one reserve together.
    source_coin: dict[bytes32, tuple[Coin, LineageProof]] = {}
    pending_exit: list[tuple[Coin, LineageProof, bytes32]] = []
    for asset in target_assets:
        if key(asset) in parsed:
            settlement = parsed[key(asset)]
            source_coin[asset] = (settlement.coin, settlement.lineage_proof)

    # A pool crossed twice chains: the second crossing spends the first's
    # successor, created in this same bundle.
    live_pool: dict[bytes32, V3Pool] = {}

    for sale in sales:
        carried = source_coin[sale.path[0]][0]
        running = sale.amount_in
        for index, base in enumerate(sale.pools):
            pool = live_pool.get(base.launcher_id, base)
            asset_in, asset_out = sale.path[index], sale.path[index + 1]
            reserve_inner = reserve_inner_puzzle(int(pool.config[0]), pool.launcher_id)
            pool_coin_id = pool.pool.coin.name()

            leg = _plan_legs([pool], [asset_in, asset_out], running)[0]
            amount_out, fee = leg.amount_out, leg.protocol_fee

            reserves_now = _plan_amounts(pool)
            successor_amounts = dict(reserves_now)
            successor_amounts[asset_in] = reserves_now[asset_in] + running
            successor_amounts[asset_out] = reserves_now[asset_out] - amount_out

            out_reserve = pool.reserves[asset_out]
            exit_coin = Coin(
                out_reserve.coin.name(),
                _settlement_puzzle_hash(asset_out),
                uint64(amount_out - fee),
            )
            exit_lineage = LineageProof(
                out_reserve.coin.parent_coin_info,
                out_reserve.inner_puzzle.get_tree_hash(),
                uint64(out_reserve.coin.amount),
            )

            plans: list[list[object]] = []
            next_reserves: dict[bytes32, ReserveCoin] = {}
            for asset_id in _pool_assets(pool):
                current = pool.reserves[asset_id]
                successor = Coin(
                    current.coin.name(),
                    _reserve_puzzle_hash(asset_id, reserve_inner),
                    uint64(successor_amounts[asset_id]),
                )
                if asset_id == asset_in:
                    settlement_coin: Coin | None = carried
                elif asset_id == asset_out:
                    settlement_coin = exit_coin
                else:
                    settlement_coin = None

                recipient, plan_fee = (
                    protocol_fee_for(pool, amount_out)
                    if asset_id == asset_out else (ZERO_32, 0)
                )
                plan = extend_plan_for_protocol_fee(pool, [
                    asset_id,
                    current.coin.name(),
                    current.coin.amount,
                    ZERO_32 if settlement_coin is None else settlement_coin.name(),
                    successor.name(),
                    successor.amount,
                ], recipient, plan_fee)
                plans.append(plan)

                reserve_sol = build_reserve_solution(
                    int(pool.config[0]), pool.launcher_id, asset_id,
                    reserve_inner.get_tree_hash(), pool.pool.inner_puzzle.get_tree_hash(),
                    [MODE_SWAP, pool_coin_id, *plan])
                if asset_id == ZERO_32:
                    native_spends.append(
                        make_spend(current.coin, current.inner_puzzle, reserve_sol))
                else:
                    add_cat(asset_id, SpendableCAT(
                        current.coin, asset_id, current.inner_puzzle, reserve_sol,
                        lineage_proof=current.lineage_proof,
                    ))

                next_reserves[asset_id] = ReserveCoin(
                    asset_id, successor, reserve_inner,
                    LineageProof(
                        current.coin.parent_coin_info,
                        current.inner_puzzle.get_tree_hash(),
                        uint64(current.coin.amount),
                    ),
                )

            next_state: list[object] = [
                [[asset_id, plans[i][4], plans[i][5]]
                 for i, asset_id in enumerate(_pool_assets(pool))],
                int(pool.state[1]),
            ]
            pool_spends.append(make_spend(
                pool.pool.coin,
                pool.pool.puzzle,
                solution_for_singleton(
                    LineageProof(
                        pool.pool.lineage_parent_name,
                        pool.pool.parent_inner_puzzle_hash,
                        uint64(pool.pool.coin.amount),
                    ),
                    uint64(1),
                    Program.to([[MODE_SWAP, pool_coin_id, plans, ZERO_32, 0]]),
                ),
            ))

            next_inner = _pool_inner(pool.singleton, pool.config, next_state)
            next_puzzle = puzzle_for_singleton(pool.launcher_id, next_inner)
            successor = V3Pool(
                pool.launcher_id, pool.singleton, pool.config, next_state,
                PoolCoin(
                    Coin(pool_coin_id, next_puzzle.get_tree_hash(), uint64(1)),
                    next_inner,
                    pool.pool.coin.parent_coin_info,
                    pool.pool.inner_puzzle.get_tree_hash(),
                    pool.launcher_id,
                ),
                next_reserves, pool.lp_asset_id, pool.lp_tail,
            )
            successor_pools.append(successor)
            live_pool[pool.launcher_id] = successor

            # The sale's own output feeds the next leg directly; the last one
            # joins the pot for the asset it produced.
            if asset_out not in source_coin:
                source_coin[asset_out] = (exit_coin, exit_lineage)
            pending_exit.append((exit_coin, exit_lineage, asset_out))
            carried = exit_coin
            running = amount_out - fee

    # ── the add ──────────────────────────────────────────────────────────────
    target_inner = reserve_inner_puzzle(int(target.config[0]), target.launcher_id)
    lp_coin, lp_lineage, target_successor = _build_add_liquidity_leg(
        target,
        deposits,
        minted,
        {asset: source_coin[asset][0] for asset in target_assets if asset in source_coin},
        xch_settlement.coin.name(),
        target_inner,
        pool_spends,
        native_spends,
        add_cat,
    )
    successor_pools.append(target_successor)

    # ── the XCH settlement: it funds the mint eve and any payback ────────────
    # The eve's parent must be this coin: that is the id the wrap-and-add mint
    # derived and a V10 pool binds, so no other funder produces an eve the
    # pool will accept.
    eve_ph = lp_mint_eve_puzzle(target).get_tree_hash()
    xch_groups = [
        _settlement_group(xch_settlement.coin.name(), []),
        _settlement_group(
            bytes32(Program.to([xch_settlement.coin.name(),
                                b"forge-routed-deposit-eve"]).get_tree_hash()),
            [(eve_ph, 1)],
        ),
    ]
    if leftover_xch > 0:
        # The target holds no native reserve, so XCH beyond the backing has
        # nowhere to deposit. Hand it back rather than burn it as fee.
        payments = offer.get_requested_payments()[lp_asset]
        xch_groups.append(_settlement_group(
            bytes32(Program.to([xch_settlement.coin.name(),
                                b"forge-routed-deposit-change"]).get_tree_hash()),
            [(payments[0].puzzle_hash, leftover_xch)],
        ))
    native_spends.append(
        make_spend(xch_settlement.coin, OFFER_MOD, Program.to(xch_groups)))

    # ── the CAT entry settlements ────────────────────────────────────────────
    for asset in target_assets:
        if asset == ZERO_32 or key(asset) not in parsed:
            continue
        settlement = parsed[key(asset)]
        add_cat(asset, SpendableCAT(
            settlement.coin, asset, OFFER_MOD,
            Program.to([_settlement_group(settlement.coin.name(), [])]),
            lineage_proof=settlement.lineage_proof,
        ))

    # ── the sale exit coins ──────────────────────────────────────────────────
    for coin, lineage, asset_id in pending_exit:
        groups = [_settlement_group(coin.name(), [])]
        if asset_id == ZERO_32:
            native_spends.append(make_spend(coin, OFFER_MOD, Program.to(groups)))
        else:
            add_cat(asset_id, SpendableCAT(
                coin, asset_id, OFFER_MOD, Program.to(groups), lineage_proof=lineage,
            ))

    # ── the LP exit: the depositor's payment, plus anything above it ─────────
    lp_groups = list(requested_solution(offer, lp_asset))
    surplus = minted - minimum_lp
    if surplus > 0:
        payments = offer.get_requested_payments()[lp_asset]
        lp_groups.append(_settlement_group(
            bytes32(Program.to([lp_coin.name(), b"forge-routed-deposit-surplus"]).get_tree_hash()),
            [(payments[0].puzzle_hash, surplus)],
        ))
    lp_groups.append(_settlement_group(lp_coin.name(), []))
    add_cat(lp_asset, SpendableCAT(
        lp_coin, lp_asset, OFFER_MOD, Program.to(lp_groups), lineage_proof=lp_lineage,
    ))

    cat_spends = [
        spend
        for spendables in cat_spendables.values()
        for spend in unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends
    ]
    bundle = aggregate_with_offer(offer, [*pool_spends, *native_spends, *cat_spends])

    final_by_launcher: dict[bytes32, V3Pool] = {p.launcher_id: p for p in successor_pools}
    return RoutedDepositResult(
        bundle,
        list(final_by_launcher.values()),
        target_successor,
        deposits,
        minted,
        backing,
        sale_outputs,
        leftover_xch,
    )
