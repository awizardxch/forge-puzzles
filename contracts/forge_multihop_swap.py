#!/usr/bin/env python3
"""Atomic multi-hop Forge swaps.

A single-pool swap (forge_offer.build_transition_v3, MODE_SWAP) pays its
output through the *user's* notarized payments, so it cannot express an
intermediate leg: on a TXCH -> T6 -> T11 route nobody has requested T6, and the
single-pool builder raises "reserve withdrawal has no requested payment".

This module chains N pool spends into one bundle instead. It deliberately lives
apart from forge_offer so the proven single-hop lane is not disturbed; the two
can be consolidated once multi-hop has real testnet mileage.

How the hand-off works (no contract changes required)
-----------------------------------------------------
forge_reserve_v{n} already creates an ephemeral ``CAT(asset, OFFER_MOD)`` coin
when a reserve shrinks, and asserts that coin announces ``tree_hash((id, []))``
— an *empty* payment group keyed by its own coin id. It places no other
constraint on that coin, and the same assertion is emitted when a reserve grows.

So one intermediate coin can satisfy both sides at once:

    hop i   reserve shrinks -> creates intermediate, asserts its empty group
    hop i+1 reserve grows   -> asserts that same empty group

The intermediate is spent exactly once, carrying only that empty group, so it
creates no coins and its value flows through the shared per-asset CAT ring into
the next pool's reserve successor.

Security properties
-------------------
* Intermediates are strictly *ephemeral* — created and spent inside this one
  bundle. settlement_payments has no authorization of its own, so an OFFER_MOD
  coin left unspent on-chain could be swept by anyone; never split a route
  across transactions.
* The trader is protected by their own offer: the final payment is announcement
  bound, so receiving less than requested invalidates their own spend and the
  whole bundle fails.
* Each pool is protected by its own singleton and reserve puzzles.
* Nobody custodies anything and the builder holds no keys.

NOTE: Not audited. Testnet only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.coin_spend import CoinSpend, make_spend
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD,
    LineageProof,
    SpendableCAT,
    construct_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton, solution_for_singleton
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH, Offer
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from forge_math import invariant_lp_mint, swap_output, WEIGHT_SCALE, vault_fee_bps
from forge_offer import (
    MODE_ADD,
    MODE_REMOVE,
    MODE_SWAP,
    ZERO_32,
    PoolCoin,
    ReserveCoin,
    V3Pool,
    _pool_inner,
    _settlement_group,
    aggregate_with_offer,
    compiled_program,
    reserve_inner_puzzle,
    reserve_solution as build_reserve_solution,
    find_offer_settlements,
    requested_amounts,
    requested_solution,
    tree_hash,
)


@dataclass(frozen=True)
class MultiHopResult:
    bundle: WalletSpendBundle
    pools: list[V3Pool]
    amounts: list[int]
    """Running amounts along the path: [amount_in, hop1_out, ..., final_out]."""
    dev_fee_collected: int = 0
    """Mojos of the final output paid to the router fee recipient."""


@dataclass
class _Leg:
    """One pool traversal: asset_in -> asset_out through a single pool.

    `kind` says how the pool is being crossed, because not every crossing is a
    trade. A single-asset pool cannot swap at all -- there is no second reserve
    to move -- but its LP is an ordinary CAT, so it can be entered and left by
    minting or melting that LP against the one reserve. Those are MODE_ADD and
    MODE_REMOVE spends, not MODE_SWAP, and they settle differently.
    """

    pool: V3Pool
    asset_in: bytes32
    asset_out: bytes32
    amount_in: int
    amount_out: int
    kind: str = "swap"
    protocol_fee: int = 0
    """V8 only: the share of `amount_out` the reserve pays the fee recipient.

    `amount_out` stays GROSS -- it is what the reserve releases, so it is what
    the successor balance is derived from. The trader, and the next leg, receive
    `amount_out - protocol_fee`.
    """


def _settlement_puzzle_hash(asset_id: bytes32) -> bytes32:
    if asset_id == ZERO_32:
        return OFFER_MOD_HASH
    return construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash()


def _reserve_puzzle_hash(asset_id: bytes32, reserve_inner: Program) -> bytes32:
    if asset_id == ZERO_32:
        return reserve_inner.get_tree_hash()
    return construct_cat_puzzle(CAT_MOD, asset_id, reserve_inner).get_tree_hash()


def _pool_assets(pool: V3Pool) -> tuple[bytes32, ...]:
    return tuple(bytes32(asset_id) for asset_id in pool.config[2])


def _plan_amounts(pool: V3Pool) -> dict[bytes32, int]:
    return {bytes32(reserve[0]): int(reserve[2]) for reserve in pool.state[0]}


def _validate_path(pools: Sequence[V3Pool], path: Sequence[bytes32]) -> None:
    if len(pools) < 2:
        raise ValueError("multi-hop swap requires at least two pools")
    if len(path) != len(pools) + 1:
        raise ValueError("path must contain exactly one more asset than pools")
    # A closing cycle is the one legal repeat: start and end on the same asset,
    # which is what a balancing trade is -- offer TXCH, cross two pools that
    # disagree on a price, come back to TXCH with more than you started. The
    # Offer's own requested amount is what makes that safe: it asserts the
    # profit, so a bundle that fails to produce it cannot settle at all.
    #
    # Intermediates must still be unique, or a route could loop through the same
    # asset repeatedly and price each pass against reserves an earlier pass moved.
    is_cycle = len(path) > 2 and path[0] == path[-1]
    interior = path[:-1] if is_cycle else path
    if len(set(interior)) != len(interior):
        raise ValueError("multi-hop path may not revisit an asset")

    seen: set[bytes32] = set()
    for index, pool in enumerate(pools):
        if pool.launcher_id in seen:
            raise ValueError("multi-hop route may not reuse a pool")
        seen.add(pool.launcher_id)

        assets = set(_pool_assets(pool))
        if path[index] in assets and path[index + 1] in assets:
            continue
        # A single-asset pool never trades, so requiring both sides of the hop to
        # be reserves would reject the one crossing it supports: its LP against
        # its reserve. That crossing is how a route reaches an asset held only
        # inside a vault.
        if vault_leg_kind(pool, path[index], path[index + 1]) is not None:
            continue
        raise ValueError(f"pool {pool.launcher_id.hex()[:16]} does not trade hop {index}")


def vault_leg_kind(pool: V3Pool, asset_in: bytes32, asset_out: bytes32) -> str | None:
    """Classify a hop across a single-asset pool, or None if it is not one.

    A vault hop crosses the pool's LP against its one reserve. The rate is the
    reserve-to-supply ratio, which is fixed for the life of the pool: a vault
    earns no fee, and deposits and withdrawals move reserve and supply together.
    It is not necessarily 1:1 -- that ratio is chosen at creation.
    """
    assets = _pool_assets(pool)
    if len(assets) != 1:
        return None
    reserve_asset = assets[0]
    lp_asset = bytes32(pool.lp_asset_id)
    if asset_in == lp_asset and asset_out == reserve_asset:
        return "vault-redeem"
    if asset_in == reserve_asset and asset_out == lp_asset:
        return "vault-wrap"
    return None



def protocol_fee_for(pool: V3Pool, released_amount: int) -> tuple[bytes32, int]:
    """What a V8 pool owes on a leg that releases `released_amount`.

    Mirrors `valid_protocol_fees` in pool_singleton_v8: the fee is a share of
    what the reserve actually releases, never of the trader's input, so it is
    derived from the curve's output. Pre-V8 pools owe nothing and carry no such
    fields in their plan at all.
    """
    if int(pool.config[0]) < 8 or released_amount <= 0:
        return ZERO_32, 0
    fee = released_amount * int(pool.config[5]) // 10_000
    return (bytes32(pool.config[6]) if fee > 0 else ZERO_32), fee


def extend_plan_for_protocol_fee(pool: V3Pool, plan: list, recipient: bytes32, fee: int) -> list:
    """Append the two V8 plan fields. A V7 plan must stay six wide."""
    if int(pool.config[0]) >= 8:
        plan.extend([fee, recipient])
    return plan


# Shared with the vault-route and split builders. They live here because both
# of those modules already import from this one; putting them the other way
# round would make the imports circular.
IDENTITY = Program.to(1)


def _plan_and_spend_reserve(
    pool: V3Pool,
    mode: int,
    asset_id: bytes32,
    successor_amount: int,
    settlement_coin: Coin | None,
    reserve_inner: Program,
    spends: list[CoinSpend],
    add_cat,
    protocol_fee: int = 0,
    protocol_recipient: bytes32 = ZERO_32,
) -> tuple[list[object], ReserveCoin]:
    """One reserve's plan plus its spend, shared by both legs."""
    current = pool.reserves[asset_id]
    successor = Coin(
        current.coin.name(),
        _reserve_puzzle_hash(asset_id, reserve_inner),
        uint64(successor_amount),
    )
    plan: list[object] = [
        asset_id,
        current.coin.name(),
        current.coin.amount,
        ZERO_32 if settlement_coin is None else settlement_coin.name(),
        successor.name(),
        successor.amount,
    ]
    if int(pool.config[0]) >= 8:
        # V8 carries the protocol fee in the plan. A redemption is not a swap, so
        # `valid_protocol_fees` charges nothing for it and callers leave the fee
        # at zero -- but the two fields must still be present, or the solution
        # does not match the shape the V8 reserve destructures.
        plan.extend([protocol_fee, protocol_recipient if protocol_fee else ZERO_32])

    solution = build_reserve_solution(
        int(pool.config[0]),
        pool.launcher_id,
        asset_id,
        reserve_inner.get_tree_hash(),
        pool.pool.inner_puzzle.get_tree_hash(),
        [mode, pool.pool.coin.name(), *plan],
    )
    if asset_id == ZERO_32:
        spends.append(make_spend(current.coin, current.inner_puzzle, solution))
    else:
        add_cat(asset_id, SpendableCAT(
            current.coin, asset_id, current.inner_puzzle, solution,
            lineage_proof=current.lineage_proof,
        ))

    return plan, ReserveCoin(
        asset_id,
        successor,
        reserve_inner,
        LineageProof(
            current.coin.parent_coin_info,
            current.inner_puzzle.get_tree_hash(),
            uint64(current.coin.amount),
        ),
    )

def _successor_pool(pool: V3Pool, next_state: list[object], next_reserves: dict) -> V3Pool:
    pool_coin_id = pool.pool.coin.name()
    next_inner = _pool_inner(pool.singleton, pool.config, next_state)
    next_puzzle = puzzle_for_singleton(pool.launcher_id, next_inner)
    return V3Pool(
        pool.launcher_id,
        pool.singleton,
        pool.config,
        next_state,
        PoolCoin(
            Coin(pool_coin_id, next_puzzle.get_tree_hash(), uint64(1)),
            next_inner,
            pool.pool.coin.parent_coin_info,
            pool.pool.inner_puzzle.get_tree_hash(),
            pool.launcher_id,
        ),
        next_reserves,
        pool.lp_asset_id,
        pool.lp_tail,
    )

def _plan_legs(pools: Sequence[V3Pool], path: Sequence[bytes32], amount_in: int) -> list[_Leg]:
    legs: list[_Leg] = []
    running = amount_in
    for index, pool in enumerate(pools):
        asset_in, asset_out = path[index], path[index + 1]
        reserves = _plan_amounts(pool)

        kind = vault_leg_kind(pool, asset_in, asset_out)
        if kind is not None:
            reserve_amount = reserves[_pool_assets(pool)[0]]
            supply = int(pool.state[1])
            if reserve_amount <= 0 or supply <= 0:
                raise ValueError("vault leg needs a funded pool")
            # V8 vaults charge their LP fee for the crossing; earlier ones are
            # an exact conversion at the ratio they were created with.
            fee = vault_fee_bps(1, int(pool.config[0]), int(pool.config[4]))
            if kind == "vault-redeem":
                amount_out = (
                    reserve_amount * running * (WEIGHT_SCALE - fee)
                    // (supply * WEIGHT_SCALE))
            else:
                # The mint must EQUAL the canonical figure or the pool refuses
                # the add. The closed form `d*(S-f)/S` floors the fee inside
                # the product and lands a mojo under the puzzle's
                # charge-fee-then-floor bracket, so quote through the exact
                # mirror instead.
                amount_out = invariant_lp_mint(
                    [reserve_amount], [running], supply,
                    fee, weights=None, version=int(pool.config[0]),
                )
            if amount_out <= 0:
                raise ValueError("vault leg rounds to a zero output")
            legs.append(_Leg(pool, asset_in, asset_out, running, amount_out, kind))
            running = amount_out
            continue

        # The V3-era closed form was weight-blind. That is
        # exact only when the traded pair's weights are equal, so a route through
        # a weighted pool has to use the bracketing form the puzzle enforces.
        assets = _pool_assets(pool)
        units = ([int(w) for w in pool.config[3]] if int(pool.config[0]) >= 7
                 else [1] * len(assets))
        amount_out = swap_output(
            reserves[asset_in],
            reserves[asset_out],
            running,
            int(pool.config[4]),
            units[assets.index(asset_in)],
            units[assets.index(asset_out)],
        )
        _, fee = protocol_fee_for(pool, amount_out)
        legs.append(_Leg(pool, asset_in, asset_out, running, amount_out, "swap", fee))
        # The reserve releases `amount_out` and pays `fee` straight to the
        # recipient, so what continues down the route is what is left.
        running = amount_out - fee
        if running <= 0:
            raise ValueError("protocol fee consumes the whole leg output")
    return legs



def _build_vault_redeem_leg(
    vault: V3Pool,
    leg: _Leg,
    lp_coin,
    lp_lineage,
    reserve_inner: Program,
    pool_spends: list[CoinSpend],
    native_spends: list[CoinSpend],
    add_cat,
):
    """Burn LP into a vault's reserve as one hop of a chained route.

    This is what makes a balancing cycle atomic. The largest price gaps sit
    behind a vault -- an asset cheap inside one, dear in a pool that trades it
    directly -- and closing that needs a route which swaps in, redeems, and sells
    on. Without this the two halves had to be separate transactions, leaving the
    asset held between them at whatever price the second half found.

    The LP arrives as the previous hop's exit settlement and is spent HERE, with
    the melt payment and the announcement group its reserve asserts travelling
    together on one spend. The hop that feeds a redemption must therefore leave
    its exit coin alone.

    Returns the exit coin carrying the underlying, for the next hop to consume.
    """
    if lp_coin is None or lp_lineage is None:
        raise ValueError("a vault redemption needs the LP released by the previous hop")

    underlying = leg.asset_out
    lp_asset = bytes32(vault.lp_asset_id)
    burn, redeemed = leg.amount_in, leg.amount_out
    if burn >= int(vault.state[1]):
        raise ValueError("a vault redemption may not burn the entire LP supply")

    out_reserve = vault.reserves[underlying]
    exit_coin = Coin(
        out_reserve.coin.name(),
        _settlement_puzzle_hash(underlying),
        uint64(redeemed),
    )
    exit_lineage = LineageProof(
        out_reserve.coin.parent_coin_info,
        out_reserve.inner_puzzle.get_tree_hash(),
        uint64(out_reserve.coin.amount),
    )

    plan, reserve = _plan_and_spend_reserve(
        vault, MODE_REMOVE, underlying,
        _plan_amounts(vault)[underlying] - redeemed, exit_coin,
        reserve_inner, native_spends, add_cat,
    )

    new_total_lp = int(vault.state[1]) - burn
    next_state: list[object] = [[[underlying, plan[4], plan[5]]], new_total_lp]

    # The LP arrives wrapped in the settlement puzzle, which the TAIL cannot
    # melt, so it is routed through an intermediate coin that CAN be melted and
    # that coin is named as the pool's LP action.
    #
    # From V9 that intermediate is not a bare identity puzzle: the pool derives
    # the action coin's id from a pinned melt inner, so only a coin whose sole
    # behavior is the -113 melt can satisfy it. Using identity here made every
    # vault redemption unbuildable against a V9+ pool -- and because the vault
    # suites all run against live V7 pools, nothing noticed.
    version = int(vault.config[0])
    lp_binding = version >= 9
    melt_inner = compiled_program("forge_lp_melt_inner_FORGE") if lp_binding else IDENTITY
    intermediate = Coin(
        lp_coin.name(),
        construct_cat_puzzle(CAT_MOD, lp_asset, melt_inner).get_tree_hash(),
        uint64(burn),
    )

    lp_action = [
        vault.pool.coin.name(),
        intermediate.name(),
        -burn,
        new_total_lp,
        tree_hash(next_state),
    ]

    # V10 pool actions carry the LP action coin's parent so the pool can derive
    # and bind that coin's id.
    pool_action: list[object] = [MODE_REMOVE, vault.pool.coin.name(), [plan],
                                 intermediate.name(), -burn]
    if version >= 10:
        pool_action.append(lp_coin.name())
    pool_spends.append(make_spend(
        vault.pool.coin,
        vault.pool.puzzle,
        solution_for_singleton(
            LineageProof(
                vault.pool.lineage_parent_name,
                vault.pool.parent_inner_puzzle_hash,
                uint64(vault.pool.coin.amount),
            ),
            uint64(1),
            Program.to([pool_action]),
        ),
    ))

    add_cat(lp_asset, SpendableCAT(
        lp_coin, lp_asset, OFFER_MOD,
        Program.to([
            _settlement_group(lp_coin.name(), [(melt_inner.get_tree_hash(), burn)]),
            _settlement_group(lp_coin.name(), []),
        ]),
        lineage_proof=lp_lineage,
    ))
    add_cat(lp_asset, SpendableCAT(
        intermediate, lp_asset, melt_inner,
        # The melt inner takes (lp_tail lp_action) and emits the -113 itself;
        # the identity puzzle had to be handed the whole condition.
        Program.to([vault.lp_tail, Program.to(lp_action)]) if lp_binding
        else Program.to([[ConditionOpcode.CREATE_COIN, 0, -113, vault.lp_tail,
                          Program.to(lp_action)]]),
        lineage_proof=LineageProof(
            lp_coin.parent_coin_info, OFFER_MOD_HASH, uint64(lp_coin.amount)),
        extra_delta=-burn,
        limitations_program_reveal=vault.lp_tail,
        limitations_solution=Program.to(lp_action),
    ))

    return exit_coin, exit_lineage, _successor_pool(vault, next_state, {underlying: reserve})


def lp_mint_eve_puzzle(pool: V3Pool) -> Program:
    """The full CAT puzzle of an LP mint eve.

    From V9 the eve must run LP_MINT_INNER -- a fixed puzzle whose only outputs
    are the minted settlement coin and the -113 mint -- because the pool derives
    and binds the eve's id from its parent, this puzzle hash, and amount 1.
    Pre-V9 pools accept a plain identity inner carrying the same two outputs.
    Exposed so the lane that funds the eve (the caller pays it one mojo out of
    the Offer's entry settlement) derives the same address the pool binds.

    Every MODE_ADD mints through this shape, whether the add is a vault wrap or
    an ordinary multi-asset deposit -- the eve depends only on the pool's LP
    asset and revision, not on how many reserves grew.
    """
    inner = (compiled_program("forge_lp_mint_inner_FORGE")
             if int(pool.config[0]) >= 9 else IDENTITY)
    return construct_cat_puzzle(CAT_MOD, bytes32(pool.lp_asset_id), inner)


# The wrap lane named this first; adds of every shape share it.
wrap_mint_eve_puzzle = lp_mint_eve_puzzle


def _build_lp_mint(
    pool: V3Pool,
    minted: int,
    plans: Sequence[Sequence[object]],
    next_state: list[object],
    eve_parent_id: bytes32,
    pool_spends: list[CoinSpend],
    add_cat,
) -> tuple[Coin, LineageProof]:
    """The mint half of any MODE_ADD: the pool spend and its LP eve.

    Shared by the vault wrap and the multi-asset deposit, which differ only in
    how many reserves they advanced to get here. The pool binds the eve's id
    from its parent, this puzzle hash and amount 1 (V9+), so the caller must
    pay the eve its one mojo out of a coin whose id it passed as
    `eve_parent_id` -- any other funder derives a different eve and the pool
    refuses the add.

    Every CAT mojo is an XCH mojo, so the mint consumes `minted` XCH of bundle
    value: one mojo for the eve coin and the rest as its extra_delta. Leaving
    that much XCH unspent elsewhere is the caller's job.
    """
    if minted <= 0:
        raise ValueError("a MODE_ADD must mint a positive amount of LP")

    version = int(pool.config[0])
    lp_asset = bytes32(pool.lp_asset_id)
    lp_binding = version >= 9
    eve_inner = compiled_program("forge_lp_mint_inner_FORGE") if lp_binding else IDENTITY
    eve_outer = construct_cat_puzzle(CAT_MOD, lp_asset, eve_inner)
    lp_eve = Coin(eve_parent_id, eve_outer.get_tree_hash(), uint64(1))

    new_total_lp = int(next_state[1])
    lp_action = [
        pool.pool.coin.name(),
        lp_eve.name(),
        minted,
        new_total_lp,
        tree_hash(next_state),
    ]

    pool_action: list[object] = [MODE_ADD, pool.pool.coin.name(), list(plans),
                                 lp_eve.name(), minted]
    if version >= 10:
        pool_action.append(eve_parent_id)
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
            Program.to([pool_action]),
        ),
    ))

    eve_solution = Program.to(
        [OFFER_MOD_HASH, minted, pool.lp_tail, lp_action]
        if lp_binding
        else [
            [ConditionOpcode.CREATE_COIN, OFFER_MOD_HASH, minted],
            [ConditionOpcode.CREATE_COIN, 0, -113, pool.lp_tail, lp_action],
        ]
    )
    add_cat(lp_asset, SpendableCAT(
        lp_eve, lp_asset, eve_inner, eve_solution,
        lineage_proof=LineageProof(),
        extra_delta=minted - 1,
        limitations_program_reveal=pool.lp_tail,
        limitations_solution=Program.to(lp_action),
    ))

    exit_coin = Coin(lp_eve.name(), _settlement_puzzle_hash(lp_asset), uint64(minted))
    exit_lineage = LineageProof(
        lp_eve.parent_coin_info, eve_inner.get_tree_hash(), uint64(1))
    return exit_coin, exit_lineage


def add_liquidity_mint(pool: V3Pool, deposits: Mapping[bytes32, int]) -> int:
    """The canonical mint for a multi-asset deposit -- the puzzle's own bracket.

    The figure `pool_singleton_FORGE` accepts as the largest legal mint, so a
    builder quoting anything else is refused. Weight- and version-aware: K is
    the sum of the weights, and a V8+ vault charges its fee on the whole
    deposit rather than on the imbalanced share.
    """
    assets = _pool_assets(pool)
    reserves = _plan_amounts(pool)
    return invariant_lp_mint(
        [reserves[asset] for asset in assets],
        [int(deposits.get(asset, 0)) for asset in assets],
        int(pool.state[1]),
        int(pool.config[4]),
        weights=([int(w) for w in pool.config[3]] if int(pool.config[0]) >= 7 else None),
        version=int(pool.config[0]),
    )


def _build_add_liquidity_leg(
    pool: V3Pool,
    deposits: Mapping[bytes32, int],
    minted: int,
    settlements: Mapping[bytes32, Coin],
    eve_parent_id: bytes32,
    reserve_inner: Program,
    pool_spends: list[CoinSpend],
    native_spends: list[CoinSpend],
    add_cat,
):
    """Deposit into SEVERAL reserves at once and mint the LP, as one hop.

    The generalization of `_build_vault_wrap_leg` from a vault's single reserve
    to a whole pool. A vault wrap is the degenerate case of this: one asset
    holding all the weight.

    The pool does not require a deposit to be balanced -- `invariant_lp_mint`
    reprices and charges the imbalance fee on the excess -- so the uneven
    deposit a user types is legal on its own. What it is not is *cheap*: the
    imbalance is priced to protect existing LPs, and the gap it opens against
    the market is collected by the next arbitrage bundle rather than held by
    the minted LP (seen on chain twice, C2 at 4.5x and D3 at 12x). The routed
    deposit lane exists to hand that value back to the depositor instead, by
    swapping the excess at market in the SAME bundle and arriving here with a
    vector that pays no imbalance fee at all.

    `deposits` is per-asset and may be zero for an untouched reserve, which
    still gets a plan naming no settlement and a byte-identical successor --
    the same frozen-reserve shape a pairwise MODE_SWAP uses for the assets it
    does not trade. `settlements` names, per growing asset, the settlement coin
    that reserve asserts; the mojos balance through the CAT ring regardless of
    which coin was named, so several producers of one asset need only agree on
    a single name.

    Returns the minted LP settlement coin, its lineage, and the successor pool.
    """
    assets = _pool_assets(pool)
    reserves_now = _plan_amounts(pool)
    if any(int(amount) < 0 for amount in deposits.values()):
        raise ValueError("a deposit cannot be negative")
    if set(deposits) - set(assets):
        raise ValueError("a deposit names an asset the pool does not hold")
    if all(int(deposits.get(asset, 0)) == 0 for asset in assets):
        raise ValueError("a deposit must fund at least one reserve")

    plans: list[list[object]] = []
    next_reserves: dict[bytes32, ReserveCoin] = {}
    for asset in assets:
        deposit = int(deposits.get(asset, 0))
        settlement = settlements.get(asset) if deposit > 0 else None
        if deposit > 0 and settlement is None:
            raise ValueError(
                f"reserve {asset.hex()[:12]} grows but names no settlement coin")
        plan, reserve = _plan_and_spend_reserve(
            pool, MODE_ADD, asset,
            reserves_now[asset] + deposit, settlement,
            reserve_inner, native_spends, add_cat,
        )
        plans.append(plan)
        next_reserves[asset] = reserve

    next_state: list[object] = [
        [[asset, plans[index][4], plans[index][5]] for index, asset in enumerate(assets)],
        int(pool.state[1]) + minted,
    ]

    exit_coin, exit_lineage = _build_lp_mint(
        pool, minted, plans, next_state, eve_parent_id, pool_spends, add_cat)
    return exit_coin, exit_lineage, _successor_pool(pool, next_state, next_reserves)


def _build_vault_wrap_leg(
    vault: V3Pool,
    leg: _Leg,
    deposit_coin: Coin,
    eve_parent_id: bytes32,
    reserve_inner: Program,
    pool_spends: list[CoinSpend],
    native_spends: list[CoinSpend],
    add_cat,
):
    """Mint a vault's LP against a deposit as one hop of a chained route.

    The mirror of `_build_vault_redeem_leg`, and the missing half of a vault
    gap: redeeming settles when the vault quotes DEAR relative to its pairs,
    but when the underlying is cheap in the pools and dear THROUGH the vault,
    extraction runs the other way -- deposit the underlying (MODE_ADD), mint
    the LP, sell it on. Without this the route existed only as a quote
    (roadmap 2d; three unsettleable signed offers on 2026-08-31).

    Mechanics compose two proven pieces. The deposit side is an ordinary
    reserve advance: the vault's one reserve grows by the deposit and names the
    carried settlement coin, exactly like a swap leg's input. The mint side is
    the add lane's eve construction: a one-mojo eve whose parent the pool binds
    (V9+ via LP_MINT_INNER, V10 additionally carrying the parent id in the
    action), minting `leg.amount_out` LP -- net of the V8+ crossing fee, which
    the puzzle's canonical mint charges and `_plan_legs` mirrors -- into a
    settlement coin the next hop consumes like any exit.

    Every CAT mojo is an XCH mojo, so the mint consumes `leg.amount_out` XCH
    of bundle value: one mojo paid to the eve by the caller (who must fund it
    from the Offer's entry settlement) and the rest as the eve's extra_delta.
    The caller is responsible for leaving that much of the entry XCH
    undeposited -- see the backing fixed point in the flow builder.

    Returns the minted LP settlement coin for the next hop to consume.
    """
    if deposit_coin is None:
        raise ValueError("a vault wrap needs the deposit released by the previous hop")

    underlying = leg.asset_in
    lp_asset = bytes32(vault.lp_asset_id)
    deposit, minted = leg.amount_in, leg.amount_out
    if minted <= 0:
        raise ValueError("a vault wrap must mint a positive amount of LP")

    plan, reserve = _plan_and_spend_reserve(
        vault, MODE_ADD, underlying,
        _plan_amounts(vault)[underlying] + deposit, deposit_coin,
        reserve_inner, native_spends, add_cat,
    )

    next_state: list[object] = [[[underlying, plan[4], plan[5]]],
                                int(vault.state[1]) + minted]

    # The mint half is shared with the multi-asset deposit lane: a wrap is that
    # add with one reserve, so the two must not drift.
    exit_coin, exit_lineage = _build_lp_mint(
        vault, minted, [plan], next_state, eve_parent_id, pool_spends, add_cat)

    return exit_coin, exit_lineage, _successor_pool(vault, next_state, {underlying: reserve})


def _exit_payout_groups(
    offer: Offer,
    requested_key,
    exit_coin: Coin,
    payable: int,
    minimum_out: int,
    dev_fee_puzzle_hash: bytes32 | None,
    dev_fee_bps: int,
) -> tuple[list[Program], int]:
    """The payment groups the route's FINAL exit settlement carries.

    Identical whether the last leg is a swap or a vault redemption, and shared
    so it stays that way: the trader's own notarised payment first, then the
    router fee carved out of the surplus, then whatever surplus remains, paid
    back to the trader. `payable` is what actually reaches this settlement --
    net of any protocol fee the reserve pays out directly, which never enters
    here and so must never be paid out of here.

    Returns the groups and the router fee actually collected.
    """
    groups: list[Program] = list(requested_solution(offer, requested_key))
    surplus = payable - minimum_out
    collected = 0
    if surplus > 0 and dev_fee_puzzle_hash is not None and dev_fee_bps > 0:
        # Derived here from the actual output, never taken as an absolute figure
        # from the caller, and capped at the surplus so the trader is always paid
        # at least the amount they notarised.
        collected = min(payable * dev_fee_bps // 10_000, surplus)
        if collected > 0:
            groups.append(_settlement_group(
                bytes32(Program.to([exit_coin.name(), b"forge-multihop-dev-fee"]).get_tree_hash()),
                [(dev_fee_puzzle_hash, collected)],
            ))
            surplus -= collected
    if surplus > 0:
        payments = offer.get_requested_payments()[requested_key]
        groups.append(_settlement_group(
            bytes32(Program.to([exit_coin.name(), b"forge-multihop-surplus"]).get_tree_hash()),
            [(payments[0].puzzle_hash, surplus)],
        ))
    return groups, collected


def build_multihop_swap(
    pools: Sequence[V3Pool],
    path: Sequence[bytes32],
    offer: Offer,
    dev_fee_puzzle_hash: bytes32 | None = None,
    dev_fee_bps: int = 0,
) -> MultiHopResult:
    """Chain `pools` along `path` into one atomic bundle settling `offer`.

    The router fee is charged ONCE on the final output, never per hop: each pool
    already takes its own LP fee inside the curve, and billing the router fee per
    hop would charge the trader for our routing choice. Intermediate hops are
    never skimmed -- they stay strictly ephemeral, which is what makes the route
    atomic.

    Taken from the exit surplus -- the gap between what the curve releases and
    the smaller amount the trader notarised -- so the invariant is untouched and
    the trader is always paid at least their stated minimum.
    """
    _validate_path(pools, path)

    asset_in, asset_out = path[0], path[-1]
    every_asset = {asset for pool in pools for asset in _pool_assets(pool)}
    parsed = find_offer_settlements(offer, tuple(every_asset))

    settlement_key = None if asset_in == ZERO_32 else asset_in
    if settlement_key not in parsed:
        raise ValueError("multi-hop Offer does not provide the input asset")
    entry_settlement = parsed[settlement_key]

    # Only the endpoints may appear in the Offer: an intermediate the trader can
    # claim would let a leg settle on its own, breaking atomicity.
    requested = requested_amounts(offer)
    requested_key = None if asset_out == ZERO_32 else asset_out
    unexpected = set(requested) - {requested_key}
    if unexpected:
        raise ValueError("multi-hop Offer requests an asset outside the route output")

    legs = _plan_legs(pools, path, int(entry_settlement.coin.amount))
    # What the trader is actually paid: the last reserve releases `amount_out`
    # and hands `protocol_fee` straight to the V8 recipient, so only the
    # remainder reaches the exit settlement.
    final_out = legs[-1].amount_out - legs[-1].protocol_fee
    dev_fee_collected = 0
    minimum_out = requested.get(requested_key, 0)
    if minimum_out <= 0:
        raise ValueError("multi-hop Offer must request the route output asset")
    if final_out < minimum_out:
        raise ValueError("multi-hop route output is below the Offer minimum")

    # Per-asset CAT rings; native XCH legs settle through bundle value balance.
    cat_spendables: dict[bytes32, list[SpendableCAT]] = {}
    native_spends: list[CoinSpend] = []
    pool_spends: list[CoinSpend] = []
    successor_pools: list[V3Pool] = []

    def add_cat(asset_id: bytes32, spendable: SpendableCAT) -> None:
        cat_spendables.setdefault(asset_id, []).append(spendable)

    # The coin handed from the previous hop; None on the first hop, where the
    # trader's own Offer settlement supplies the input.
    carried: Coin | None = None
    carried_lineage: LineageProof | None = None

    for index, leg in enumerate(legs):
        pool = leg.pool
        protocol_version = int(pool.config[0])
        reserve_inner = reserve_inner_puzzle(protocol_version, pool.launcher_id)
        pool_coin_id = pool.pool.coin.name()
        is_first, is_last = index == 0, index == len(legs) - 1

        if leg.kind == "vault-redeem":
            if is_first:
                # A redemption consumes LP the route has not acquired yet: the
                # coin it burns is the previous hop's exit settlement, so there
                # has to BE a previous hop.
                raise ValueError("a route may not open with a vault redemption")
            exit_coin, exit_lineage, successor = _build_vault_redeem_leg(
                pool, leg, carried, carried_lineage, reserve_inner,
                pool_spends, native_spends, add_cat,
            )
            successor_pools.append(successor)

            # Every hop spends the coin it releases, so the next one can consume
            # it. Skipping this leaves a coin created by the vault's reserve that
            # nothing spends, and the CAT ring cannot balance around it.
            #
            # When the redemption is LAST there is no next hop, and that coin is
            # the trader's payout instead -- the shape a vault-crossing CYCLE
            # ends on: buy the vault's LP where it trades below its redemption
            # ratio, burn it at the vault, come back holding more of what you
            # started with. A vault charges no protocol fee, so the whole
            # redemption is payable.
            groups: list[Program] = []
            if is_last:
                groups, dev_fee_collected = _exit_payout_groups(
                    offer, requested_key, exit_coin, leg.amount_out,
                    minimum_out, dev_fee_puzzle_hash, dev_fee_bps,
                )
            groups.append(_settlement_group(exit_coin.name(), []))
            if leg.asset_out == ZERO_32:
                native_spends.append(make_spend(exit_coin, OFFER_MOD, Program.to(groups)))
            else:
                add_cat(leg.asset_out, SpendableCAT(
                    exit_coin, leg.asset_out, OFFER_MOD,
                    Program.to(groups), lineage_proof=exit_lineage,
                ))

            carried, carried_lineage = exit_coin, exit_lineage
            continue

        if leg.kind != "swap":
            raise ValueError(f"multi-hop cannot settle a {leg.kind} leg")

        feeds_redemption = not is_last and legs[index + 1].kind == "vault-redeem"

        # A pool may hold more assets than this hop touches: V6 is N-asset, so
        # routing TXCH -> T6 through a TXCH/T6/T11 pool leaves T11 alone. Every
        # reserve still has to be named in the plan, so start from the current
        # balances and only move the two the hop actually trades.
        reserves_now = _plan_amounts(pool)
        successor_amounts = {asset_id: reserves_now[asset_id] for asset_id in _pool_assets(pool)}
        successor_amounts[leg.asset_in] = reserves_now[leg.asset_in] + leg.amount_in
        successor_amounts[leg.asset_out] = reserves_now[leg.asset_out] - leg.amount_out

        # The coin this hop hands to the next one (or to the trader when last).
        out_reserve = pool.reserves[leg.asset_out]
        exit_coin = Coin(
            out_reserve.coin.name(),
            _settlement_puzzle_hash(leg.asset_out),
            uint64(leg.amount_out - leg.protocol_fee),
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
            successor_amount = successor_amounts[asset_id]
            is_native = asset_id == ZERO_32
            successor = Coin(
                current.coin.name(),
                _reserve_puzzle_hash(asset_id, reserve_inner),
                uint64(successor_amount),
            )

            settlement_coin: Coin | None
            settlement_lineage: LineageProof | None
            if asset_id == leg.asset_in:
                # Growing side: consumes the carried coin (or the Offer's).
                settlement_coin = entry_settlement.coin if is_first else carried
                settlement_lineage = entry_settlement.lineage_proof if is_first else carried_lineage
                assert settlement_coin is not None
            elif asset_id == leg.asset_out:
                # Shrinking side: mints the coin the next hop will consume.
                settlement_coin = exit_coin
                settlement_lineage = exit_lineage
            else:
                # Untouched reserve: the puzzle requires a zero settlement id
                # here, and nothing may be paid out of it.
                settlement_coin = None
                settlement_lineage = None

            recipient, fee = (
                protocol_fee_for(pool, leg.amount_out)
                if asset_id == leg.asset_out else (ZERO_32, 0)
            )
            plan = extend_plan_for_protocol_fee(pool, [
                asset_id,
                current.coin.name(),
                current.coin.amount,
                ZERO_32 if settlement_coin is None else settlement_coin.name(),
                successor.name(),
                successor.amount,
            ], recipient, fee)
            plans.append(plan)

            reserve_solution = build_reserve_solution(
                protocol_version, pool.launcher_id, asset_id,
                reserve_inner.get_tree_hash(), pool.pool.inner_puzzle.get_tree_hash(),
                [MODE_SWAP, pool_coin_id, *plan])

            # Every settlement coin carries the empty group its reserve asserts.
            # The final hop additionally pays the trader; intermediates never do.
            groups: list[Program] = []
            if asset_id == leg.asset_out and is_last:
                # Net of the V8 protocol fee: that share is paid by the reserve
                # directly to the recipient and never enters this settlement.
                groups, dev_fee_collected = _exit_payout_groups(
                    offer, requested_key, settlement_coin,
                    leg.amount_out - leg.protocol_fee, minimum_out,
                    dev_fee_puzzle_hash, dev_fee_bps,
                )
            if settlement_coin is not None:
                groups.append(_settlement_group(settlement_coin.name(), []))

            if is_native:
                native_spends.append(make_spend(current.coin, current.inner_puzzle, reserve_solution))
                # An intermediate is spent once, by the hop that consumes it --
                # and a redemption consumes its input itself, carrying the melt
                # payment, so the hop before one must leave its exit alone.
                if (settlement_coin is not None
                        and not (asset_id == leg.asset_in and not is_first)
                        and not (asset_id == leg.asset_out and feeds_redemption)):
                    native_spends.append(make_spend(settlement_coin, OFFER_MOD, Program.to(groups)))
            else:
                add_cat(asset_id, SpendableCAT(
                    current.coin,
                    asset_id,
                    current.inner_puzzle,
                    reserve_solution,
                    lineage_proof=current.lineage_proof,
                ))
                if (settlement_coin is not None
                        and not (asset_id == leg.asset_in and not is_first)
                        and not (asset_id == leg.asset_out and feeds_redemption)):
                    assert settlement_lineage is not None
                    add_cat(asset_id, SpendableCAT(
                        settlement_coin,
                        asset_id,
                        OFFER_MOD,
                        Program.to(groups),
                        lineage_proof=settlement_lineage,
                    ))

            next_reserves[asset_id] = ReserveCoin(
                asset_id,
                successor,
                reserve_inner,
                LineageProof(
                    current.coin.parent_coin_info,
                    current.inner_puzzle.get_tree_hash(),
                    uint64(current.coin.amount),
                ),
            )

        next_state: list[object] = [
            [[asset_id, plans[i][4], plans[i][5]] for i, asset_id in enumerate(_pool_assets(pool))],
            int(pool.state[1]),
        ]
        action = [MODE_SWAP, pool_coin_id, plans, ZERO_32, 0]
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
                Program.to([action]),
            ),
        ))

        next_inner = _pool_inner(pool.singleton, pool.config, next_state)
        next_puzzle = puzzle_for_singleton(pool.launcher_id, next_inner)
        successor_pools.append(V3Pool(
            pool.launcher_id,
            pool.singleton,
            pool.config,
            next_state,
            PoolCoin(
                Coin(pool_coin_id, next_puzzle.get_tree_hash(), uint64(1)),
                next_inner,
                pool.pool.coin.parent_coin_info,
                pool.pool.inner_puzzle.get_tree_hash(),
                pool.launcher_id,
            ),
            next_reserves,
            pool.lp_asset_id,
            pool.lp_tail,
        ))

        carried, carried_lineage = exit_coin, exit_lineage

    cat_spends = [
        spend
        for spendables in cat_spendables.values()
        for spend in unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends
    ]

    bundle = aggregate_with_offer(offer, [*pool_spends, *native_spends, *cat_spends])
    return MultiHopResult(
        bundle,
        successor_pools,
        [legs[0].amount_in, *(leg.amount_out for leg in legs)],
        dev_fee_collected,
    )
