#!/usr/bin/env python3
"""Atomic split swaps: one order settled across N parallel routes.

Past some size a single route's marginal rate decays below a second route's
opening rate, so splitting returns more than either route alone. Quoting proves
that; this settles it.

How branches share one Offer
----------------------------
The trader signs a single Offer, so there is exactly one entry settlement coin
and one requested payment. Both are shared:

* **Entry** — every branch's first reserve names the same settlement coin and
  asserts the same ``tree_hash((id, []))`` announcement. One spend satisfies all
  of them, and the value divides by ring / bundle balance in the proportions the
  reserves grow.
* **Exit** — each branch mints its own output settlement coin, all spent in the
  same ring, but only the first carries the trader's notarized payment, sized to
  the *combined* output. The others contribute value with an empty group, so the
  payment is funded once and never duplicated.

Branches must be pool-disjoint: two branches touching one pool would each price
against reserves the other also moves, so the quoted total could not be honoured.

NOTE: Not audited. Testnet only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.coin_spend import CoinSpend, make_spend
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD,
    construct_cat_puzzle,
    LineageProof,
    SpendableCAT,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton, solution_for_singleton
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH, Offer
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from forge_offer import (
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
from forge_multihop_swap import (
    IDENTITY, _build_vault_redeem_leg, _plan_and_spend_reserve, _successor_pool)
from forge_multihop_swap import (
    extend_plan_for_protocol_fee,
    protocol_fee_for,
    vault_leg_kind,
    _plan_amounts,
    _plan_legs,
    _pool_assets,
    _reserve_puzzle_hash,
    _settlement_puzzle_hash,
)


@dataclass(frozen=True)
class SplitBranchSpec:
    """One parallel branch: its own pool chain, asset path, and input share."""

    pools: Sequence[V3Pool]
    path: Sequence[bytes32]
    amount_in: int


@dataclass(frozen=True)
class SplitResult:
    bundle: WalletSpendBundle
    pools: list[V3Pool]
    branch_amounts: list[list[int]]
    total_out: int
    dev_fee_collected: int = 0
    """Mojos of the combined output paid to the router fee recipient."""


def _validate_branches(branches: Sequence[SplitBranchSpec]) -> tuple[bytes32, bytes32]:
    if len(branches) < 2:
        raise ValueError("split swap requires at least two branches")

    asset_in, asset_out = branches[0].path[0], branches[0].path[-1]
    seen_pools: set[bytes32] = set()

    for branch in branches:
        if len(branch.path) != len(branch.pools) + 1:
            raise ValueError("each branch path needs exactly one more asset than pools")
        if branch.path[0] != asset_in or branch.path[-1] != asset_out:
            raise ValueError("all split branches must share the same input and output asset")
        if branch.amount_in <= 0:
            raise ValueError("each split branch needs a positive input share")
        # A branch that closes on itself is a balancing cycle: out through one
        # pool and back through another that prices the asset differently. That
        # is the shape a combined autobalance takes -- several disjoint cycles
        # sharing one Offer, whose requested amount asserts the whole profit.
        # Interior assets must still be unique, or a branch could loop through
        # one asset repeatedly and price each pass against reserves an earlier
        # pass already moved.
        interior = branch.path[:-1] if len(branch.path) > 2 and branch.path[0] == branch.path[-1] else branch.path
        if len(set(interior)) != len(interior):
            raise ValueError("a branch path may not revisit an asset")

        for index, pool in enumerate(branch.pools):
            if pool.launcher_id in seen_pools:
                raise ValueError("split branches may not share a pool")
            seen_pools.add(pool.launcher_id)
            assets = set(_pool_assets(pool))
            if branch.path[index] in assets and branch.path[index + 1] in assets:
                continue
            # A single-asset pool never trades, so requiring both sides of the
            # hop to be reserves would reject the one crossing it does support:
            # its LP against its reserve. That is how a vault's liquidity is
            # reachable at all, so a branch is allowed to cross one.
            if vault_leg_kind(pool, branch.path[index], branch.path[index + 1]) is not None:
                continue
            raise ValueError(f"pool {pool.launcher_id.hex()[:16]} does not trade its assigned hop")

    return asset_in, asset_out



# The vault redemption is identical to the one a multi-hop performs -- burn the
# LP the previous leg released, pay the underlying into an exit coin -- so it is
# imported rather than repeated. It was duplicated here once, and when V9 pinned
# the melt puzzle only one of the copies was updated; a redemption that a split
# could not build but a multi-hop could is exactly the kind of drift a second
# implementation invites.
def build_split_swap(
    branches: Sequence[SplitBranchSpec],
    offer: Offer,
    dev_fee_puzzle_hash: bytes32 | None = None,
    dev_fee_bps: int = 0,
) -> SplitResult:
    """Settle one order across parallel branches in a single atomic bundle.

    The router fee is charged ONCE against the *combined* output, on the same
    exit coin that carries the trader's payment -- not per branch and not per
    hop. Branches are parallel, so their fees would not compound anyway, but
    charging each branch separately would still bill the trader for how finely
    we chose to split.
    """
    asset_in, asset_out = _validate_branches(branches)

    every_asset = {asset for branch in branches for pool in branch.pools for asset in _pool_assets(pool)}
    parsed = find_offer_settlements(offer, tuple(every_asset))

    entry_key = None if asset_in == ZERO_32 else asset_in
    if entry_key not in parsed:
        raise ValueError("split Offer does not provide the input asset")
    entry_settlement = parsed[entry_key]

    # The signed Offer is the source of truth for what was actually put up, not
    # the caller's precomputed shares -- a wallet's coin selection, a fee that
    # eats into the same coin, or the smallest reserve movement between quote
    # and signature can all leave the declared total a mojo or more off from
    # what actually landed in the settlement coin. Multihop already trusts the
    # offer this way (it reads amount_in straight off entry_settlement); split
    # only had a hint to work from because one coin funds several branches, so
    # here the hint is rescaled to match reality instead of being asserted.
    declared_total = sum(branch.amount_in for branch in branches)
    actual_total = int(entry_settlement.coin.amount)
    if declared_total <= 0:
        raise ValueError("split branch inputs must sum to a positive amount")
    if declared_total != actual_total:
        scaled = [(branch.amount_in * actual_total) // declared_total for branch in branches]
        shortfall = actual_total - sum(scaled)
        if shortfall != 0:
            largest = max(range(len(scaled)), key=lambda i: scaled[i])
            scaled[largest] += shortfall
        if any(amount <= 0 for amount in scaled):
            raise ValueError("the Offer's actual amount is too small to fund every split branch")
        branches = [
            SplitBranchSpec(branch.pools, branch.path, amount)
            for branch, amount in zip(branches, scaled)
        ]

    requested = requested_amounts(offer)
    requested_key = None if asset_out == ZERO_32 else asset_out
    if set(requested) - {requested_key}:
        raise ValueError("split Offer requests an asset outside the route output")
    dev_fee_collected = 0
    minimum_out = requested.get(requested_key, 0)
    if minimum_out <= 0:
        raise ValueError("split Offer must request the route output asset")

    branch_legs = [_plan_legs(b.pools, b.path, b.amount_in) for b in branches]
    # Net of each branch's V8 protocol fee: that share never reaches the exit.
    total_out = sum(legs[-1].amount_out - legs[-1].protocol_fee for legs in branch_legs)
    if total_out < minimum_out:
        raise ValueError("split route output is below the Offer minimum")

    cat_spendables: dict[bytes32, list[SpendableCAT]] = {}
    native_spends: list[CoinSpend] = []
    pool_spends: list[CoinSpend] = []
    successor_pools: list[V3Pool] = []
    exit_coins: list[tuple[Coin, LineageProof, bytes32]] = []
    entry_spent = False

    def add_cat(asset_id: bytes32, spendable: SpendableCAT) -> None:
        cat_spendables.setdefault(asset_id, []).append(spendable)

    for legs in branch_legs:
        carried: Coin | None = None
        carried_lineage: LineageProof | None = None

        for index, leg in enumerate(legs):
            pool = leg.pool
            reserve_inner = reserve_inner_puzzle(int(pool.config[0]), pool.launcher_id)
            pool_coin_id = pool.pool.coin.name()
            is_first, is_last = index == 0, index == len(legs) - 1

            if leg.kind == "vault-redeem":
                exit_coin, exit_lineage, successor = _build_vault_redeem_leg(
                    pool, leg, carried, carried_lineage, reserve_inner,
                    pool_spends, native_spends, add_cat,
                )
                successor_pools.append(successor)
                if is_last:
                    exit_coins.append((exit_coin, exit_lineage, leg.asset_out))
                else:
                    groups = [_settlement_group(exit_coin.name(), [])]
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
                raise ValueError(f"split branch cannot settle a {leg.kind} leg")

            reserves_now = _plan_amounts(pool)
            # A pool may hold more assets than this hop touches: V6 is N-asset,
            # so routing TXCH -> T6 through a TXCH/T6/T11 pool leaves T11 alone.
            # Every reserve still has to be named in the plan, so start from the
            # current balances and only move the two the hop actually trades.
            successor_amounts = {
                asset_id: reserves_now[asset_id] for asset_id in _pool_assets(pool)
            }
            successor_amounts[leg.asset_in] = reserves_now[leg.asset_in] + leg.amount_in
            successor_amounts[leg.asset_out] = reserves_now[leg.asset_out] - leg.amount_out

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
                    settlement_coin = entry_settlement.coin if is_first else carried
                    settlement_lineage = entry_settlement.lineage_proof if is_first else carried_lineage
                    assert settlement_coin is not None
                elif asset_id == leg.asset_out:
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
                    int(pool.config[0]), pool.launcher_id, asset_id,
                    reserve_inner.get_tree_hash(), pool.pool.inner_puzzle.get_tree_hash(),
                    [MODE_SWAP, pool_coin_id, *plan])

                if is_native:
                    native_spends.append(make_spend(current.coin, current.inner_puzzle, reserve_solution))
                else:
                    add_cat(asset_id, SpendableCAT(
                        current.coin,
                        asset_id,
                        current.inner_puzzle,
                        reserve_solution,
                        lineage_proof=current.lineage_proof,
                    ))

                # The entry coin is named by every branch but spent exactly once.
                if asset_id == leg.asset_in and is_first and not entry_spent:
                    entry_spent = True
                    groups = [_settlement_group(settlement_coin.name(), [])]
                    if is_native:
                        native_spends.append(make_spend(settlement_coin, OFFER_MOD, Program.to(groups)))
                    else:
                        assert settlement_lineage is not None
                        add_cat(asset_id, SpendableCAT(
                            settlement_coin, asset_id, OFFER_MOD,
                            Program.to(groups), lineage_proof=settlement_lineage,
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

            feeds_redemption = not is_last and legs[index + 1].kind == "vault-redeem"
            if is_last:
                # Deferred: one exit coin will carry the combined payment.
                exit_coins.append((exit_coin, exit_lineage, leg.asset_out))
            elif feeds_redemption:
                # Left unspent on purpose -- the redemption leg spends this coin,
                # because the melt payment and the announcement group the reserve
                # asserts must travel together on one spend.
                pass
            else:
                groups = [_settlement_group(exit_coin.name(), [])]
                if leg.asset_out == ZERO_32:
                    native_spends.append(make_spend(exit_coin, OFFER_MOD, Program.to(groups)))
                else:
                    add_cat(leg.asset_out, SpendableCAT(
                        exit_coin, leg.asset_out, OFFER_MOD,
                        Program.to(groups), lineage_proof=exit_lineage,
                    ))

            next_state: list[object] = [
                [[asset_id, plans[i][4], plans[i][5]] for i, asset_id in enumerate(_pool_assets(pool))],
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
            successor_pools.append(V3Pool(
                pool.launcher_id, pool.singleton, pool.config, next_state,
                PoolCoin(
                    Coin(pool_coin_id, next_puzzle.get_tree_hash(), uint64(1)),
                    next_inner,
                    pool.pool.coin.parent_coin_info,
                    pool.pool.inner_puzzle.get_tree_hash(),
                    pool.launcher_id,
                ),
                next_reserves, pool.lp_asset_id, pool.lp_tail,
            ))

            carried, carried_lineage = exit_coin, exit_lineage

    for position, (coin, lineage, asset_id) in enumerate(exit_coins):
        groups: list[Program] = []
        if position == 0:
            groups.extend(requested_solution(offer, requested_key))
            surplus = total_out - minimum_out
            if surplus > 0 and dev_fee_puzzle_hash is not None and dev_fee_bps > 0:
                # Derived from the combined output, capped at the surplus so the
                # trader is always paid at least their notarised minimum.
                dev_fee_collected = min(total_out * dev_fee_bps // 10_000, surplus)
                if dev_fee_collected > 0:
                    groups.append(_settlement_group(
                        bytes32(Program.to([coin.name(), b"forge-split-dev-fee"]).get_tree_hash()),
                        [(dev_fee_puzzle_hash, dev_fee_collected)],
                    ))
                    surplus -= dev_fee_collected
            if surplus > 0:
                payments = offer.get_requested_payments()[requested_key]
                groups.append(_settlement_group(
                    bytes32(Program.to([coin.name(), b"forge-split-surplus"]).get_tree_hash()),
                    [(payments[0].puzzle_hash, surplus)],
                ))
        groups.append(_settlement_group(coin.name(), []))

        if asset_id == ZERO_32:
            native_spends.append(make_spend(coin, OFFER_MOD, Program.to(groups)))
        else:
            add_cat(asset_id, SpendableCAT(
                coin, asset_id, OFFER_MOD, Program.to(groups), lineage_proof=lineage,
            ))

    cat_spends = [
        spend
        for spendables in cat_spendables.values()
        for spend in unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends
    ]

    bundle = aggregate_with_offer(offer, [*pool_spends, *native_spends, *cat_spends])
    return SplitResult(
        bundle,
        successor_pools,
        [[legs[0].amount_in, *(leg.amount_out for leg in legs)] for legs in branch_legs],
        total_out,
        dev_fee_collected,
    )
