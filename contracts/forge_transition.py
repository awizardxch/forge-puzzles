#!/usr/bin/env python3
"""N-asset add / remove / swap bundles for Forge V6.

Separate from forge_offer.build_transition_v3, which is pinned to two assets
(`range(2)` loops and a two-way swap index) and has no imbalance fee. The proven
V4/V5 lane stays untouched; the two can be consolidated once V6 has real mileage.

All arithmetic comes from forge_math, which is verified against the compiled
puzzle to the integer — see `_test_v6_math.py`. Nothing here recomputes pool math
inline, because a second implementation is a second thing to drift.

NOTE: Not audited. Testnet only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend, make_spend
from chia.types.condition_opcodes import ConditionOpcode
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
from forge_math import (
    DepositTooSmall,
    WEIGHT_SCALE,
    vault_fee_bps,
    invariant_lp_mint,
    require_positive_mint,
    solve_native_add_split,
    swap_output,
    withdrawal_amounts,
)


@dataclass(frozen=True)
class TransitionV6Result:
    bundle: WalletSpendBundle
    pool: V3Pool
    lp_delta: int
    """Positive on add, negative on remove, zero on swap."""
    dev_fee_collected: int = 0
    """Mojos of the output asset actually paid to the dev recipient."""


def _assets(pool: V3Pool) -> list[bytes32]:
    return [bytes32(asset_id) for asset_id in pool.config[2]]


def _reserves(pool: V3Pool) -> list[int]:
    return [int(entry[2]) for entry in pool.state[0]]


def _settlement_key(asset_id: bytes32) -> bytes32 | None:
    """find_offer_settlements keys native XCH as None."""
    return None if asset_id == ZERO_32 else asset_id


def _reserve_puzzle_hash(asset_id: bytes32, reserve_inner: Program) -> bytes32:
    if asset_id == ZERO_32:
        return reserve_inner.get_tree_hash()
    return construct_cat_puzzle(CAT_MOD, asset_id, reserve_inner).get_tree_hash()


def _settlement_puzzle_hash(asset_id: bytes32) -> bytes32:
    if asset_id == ZERO_32:
        return OFFER_MOD_HASH
    return construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash()


def _plan_deltas(
    pool: V3Pool,
    offer: Offer,
    mode: int,
) -> tuple[list[int], int]:
    """Resolve the successor reserves and LP delta this Offer implies.

    Returns (successor_amounts, lp_delta). Everything the puzzle will re-derive
    is computed here from forge_math so the two cannot disagree.
    """
    assets = _assets(pool)
    old = _reserves(pool)
    total_lp = int(pool.state[1])
    fee_bps = int(pool.config[4])
    # Weight units, which the mint maths needs: the invariant is raised to their
    # SUM, and the imbalance fee is shared by weight, not by asset count.
    #
    # V7 introduced integer weight units. V6 stores basis points in the same slot
    # ([5000, 5000] for a pair) and its puzzle ignores them for the exponent, so
    # feeding those in would raise the invariant to the ten-thousandth power.
    version = int(pool.config[0])
    weights = [int(w) for w in pool.config[3]] if version >= 7 else None

    parsed = find_offer_settlements(offer, (*assets, pool.lp_asset_id))
    requested = requested_amounts(offer)

    offered = {
        asset_id: int(parsed[_settlement_key(asset_id)].coin.amount)
        for asset_id in assets
        if _settlement_key(asset_id) in parsed
    }

    if mode == MODE_SWAP:
        if len(offered) != 1:
            raise ValueError("a V6 swap Offer must provide exactly one pool asset")
        in_asset = next(iter(offered))
        in_index = assets.index(in_asset)

        wanted = set(requested) - {None}
        native_wanted = None in requested and ZERO_32 in assets
        if native_wanted:
            wanted.add(ZERO_32)
        if len(wanted) != 1:
            raise ValueError("a V6 swap Offer must request exactly one pool asset")
        out_asset = bytes32(next(iter(wanted)))
        if out_asset not in assets or out_asset == in_asset:
            raise ValueError("swap output must be a different asset in this pool")
        out_index = assets.index(out_asset)

        # The traded pair's weights: with unequal weights the curve is not
        # constant product, and the puzzle brackets the output exactly.
        units = ([int(w) for w in pool.config[3]] if int(pool.config[0]) >= 7
                 else [1] * len(assets))
        amount_out = swap_output(old[in_index], old[out_index], offered[in_asset],
                                 fee_bps, units[in_index], units[out_index])
        minimum = requested.get(_settlement_key(out_asset), 0)
        if minimum > amount_out:
            raise ValueError("swap output is below the Offer minimum")

        successor = list(old)
        successor[in_index] += offered[in_asset]
        successor[out_index] -= amount_out
        return successor, 0

    if mode == MODE_ADD:
        if not offered:
            raise ValueError("an add Offer must provide at least one reserve asset")
        deposits = [offered.get(asset_id, 0) for asset_id in assets]

        if ZERO_32 in assets:
            # A native pool funds the reserve deposit and the LP backing from the
            # same XCH settlement, so solve the split rather than trusting the
            # caller's division of it.
            native_index = assets.index(ZERO_32)
            native_total = deposits[native_index]
            if native_total <= 0:
                raise ValueError("a native-XCH V6 pool needs XCH in the add Offer to back the LP mint")
            deposits[native_index] = 0
            native_deposit, lp_delta = solve_native_add_split(
                native_total, old, native_index, deposits, total_lp, fee_bps, weights, version)
            deposits[native_index] = native_deposit
        else:
            lp_delta = require_positive_mint(
                invariant_lp_mint(old, deposits, total_lp, fee_bps, weights, version), len(assets))

        requested_lp = requested.get(pool.lp_asset_id, 0)
        if requested_lp != lp_delta:
            raise ValueError(
                f"add Offer requests {requested_lp} LP but the canonical mint is {lp_delta}")

        return [o + d for o, d in zip(old, deposits)], lp_delta

    if pool.lp_asset_id not in parsed:
        raise ValueError("a remove Offer must provide the LP CAT")
    offered_lp = int(parsed[pool.lp_asset_id].coin.amount)
    # The pool must outlive the burn, so the router caps it at total_lp - 1.
    burn = min(offered_lp, total_lp - 1)
    if burn <= 0:
        raise ValueError("invalid LP burn amount")

    fee_bps = vault_fee_bps(len(old), int(pool.config[0]), int(pool.config[4]))
    payouts = withdrawal_amounts(old, burn, total_lp, fee_bps)
    # A burn under the pool's claim granularity floors to nothing on every
    # reserve: the LP is destroyed and the reserves do not move. Nothing else
    # refuses it -- the puzzle's exact_withdrawal is satisfied by a zero payout,
    # and the Offer's minimum is satisfied by asking for zero -- so the holder
    # simply loses the coins.
    #
    # The threshold is the pool's LP-per-reserve ratio, which for a vault is the
    # ratio it opened at: a 100x vault pays nothing below 100 mojos of LP, and
    # its three CAT decimals are only usable down to 0.100. Reported with the
    # actual minimum, because "burn more" is not actionable without the number.
    if not any(payouts):
        smallest = min(
            -(-total_lp * WEIGHT_SCALE // (reserve * (WEIGHT_SCALE - fee_bps)))
            for reserve in old if reserve > 0)
        raise ValueError(
            f"burning {burn} LP mojos claims nothing from this pool and would "
            f"destroy the coins: it holds {total_lp} LP against reserves "
            f"{list(old)}, so the smallest burn that pays out anything is "
            f"{smallest} mojos")
    for index, asset_id in enumerate(assets):
        minimum = requested.get(_settlement_key(asset_id), 0)
        if minimum > payouts[index]:
            raise ValueError(f"remove output for asset {index} is below the Offer minimum")

    return [o - w for o, w in zip(old, payouts)], -burn


def build_transition(
    pool: V3Pool,
    offer: Offer,
    mode: int,
    dev_fee_puzzle_hash: bytes32 | None = None,
    dev_fee_bps: int = 0,
) -> TransitionV6Result:
    """Build an N-asset add, remove or swap bundle from a signed Offer.

    The dev fee is carved out of the shrinking reserve's surplus -- the gap
    between what the curve releases and the smaller amount the trader notarised,
    because the quote already netted the fee off their input. Without this the
    surplus is refunded to the trader and the fee is quoted but never taken.

    The fee is derived here from `dev_fee_bps` against the actual reserve
    decrease, never taken as an absolute figure from the caller, so a crafted
    request cannot understate it. It is capped at the surplus, so the trader is
    always paid at least the amount they notarised.

    Swaps only: adds and withdrawals carry no dev fee by design.
    """
    if mode not in (MODE_ADD, MODE_REMOVE, MODE_SWAP):
        raise ValueError(f"unsupported V6 mode: {mode}")
    version = int(pool.config[0])
    if version not in (6, 7, 8, 9, 10):
        raise ValueError("build_transition handles protocol version 6 through 10 pools")
    # V8 charges a protocol fee out of a swap's released amount and the reserve
    # puzzle pays it directly. The pool derives the same figure and refuses any
    # plan that disagrees, so this must mirror valid_protocol_fees exactly.
    protocol_fee_bps = int(pool.config[5]) if version >= 8 else 0
    protocol_puzzle_hash = bytes32(pool.config[6]) if version >= 8 else ZERO_32

    assets = _assets(pool)
    old = _reserves(pool)
    total_lp = int(pool.state[1])
    pool_coin_id = pool.pool.coin.name()
    reserve_inner = reserve_inner_puzzle(version, pool.launcher_id)
    pool_inner_hash = pool.pool.inner_puzzle.get_tree_hash()

    successor_amounts, lp_delta = _plan_deltas(pool, offer, mode)
    parsed = find_offer_settlements(offer, (*assets, pool.lp_asset_id))

    # On an add, the LP genesis coin is parented to the XCH settlement, so that
    # settlement has to create it. Compute its puzzle hash up front: the reserve
    # loop below owns the native settlement spend and must carry the payment.
    identity = Program.to(1)
    # The eve must be CREATED with the same inner it is later SPENT with in
    # _build_lp_leg: from V9 that is LP_MINT_INNER, so the eve's mint cannot be
    # faked; before V9 it is the plain identity puzzle.
    eve_inner = compiled_program("forge_lp_mint_inner_FORGE") if version >= 9 else identity
    lp_eve_puzzle_hash = (
        construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, eve_inner).get_tree_hash()
        if mode == MODE_ADD else None
    )
    native_settlement_spent = False

    collect_dev_fee = (
        mode == MODE_SWAP and dev_fee_puzzle_hash is not None and dev_fee_bps > 0
    )
    dev_fee_collected = 0

    plans: list[list[object]] = []
    next_reserves: dict[bytes32, ReserveCoin] = {}
    cat_spendables: dict[bytes32, list[SpendableCAT]] = {}
    native_spends: list[CoinSpend] = []

    def add_cat(asset_id: bytes32, spendable: SpendableCAT) -> None:
        cat_spendables.setdefault(asset_id, []).append(spendable)

    for index, asset_id in enumerate(assets):
        current = pool.reserves[asset_id]
        successor_amount = successor_amounts[index]
        is_native = asset_id == ZERO_32
        successor = Coin(
            current.coin.name(),
            _reserve_puzzle_hash(asset_id, reserve_inner),
            uint64(successor_amount),
        )

        settlement_coin: Coin | None
        settlement_lineage: LineageProof | None

        # On V8 a shrinking reserve pays the protocol recipient directly, so the
        # trader's settlement is the release minus that fee. Both the pool and
        # the reserve name this smaller coin, so it has to be sized before the
        # coin is built rather than patched into the plan afterwards.
        released_amount = current.coin.amount - successor_amount
        protocol_fee_owed = (
            released_amount * protocol_fee_bps // 10_000
            if version >= 8 and mode == MODE_SWAP and released_amount > 0
            else 0
        )

        if successor_amount > current.coin.amount:
            key = _settlement_key(asset_id)
            if key not in parsed:
                raise ValueError(f"reserve increase for asset {index} has no offered settlement coin")
            settlement = parsed[key]
            settlement_coin = settlement.coin
            settlement_lineage = None if is_native else settlement.lineage_proof
        elif successor_amount == current.coin.amount:
            # Untouched reserve: the puzzle wants a zero settlement id here.
            settlement_coin = None
            settlement_lineage = None
        else:
            settlement_coin = Coin(
                current.coin.name(),
                _settlement_puzzle_hash(asset_id),
                uint64(released_amount - protocol_fee_owed),
            )
            settlement_lineage = LineageProof(
                current.coin.parent_coin_info,
                current.inner_puzzle.get_tree_hash(),
                uint64(current.coin.amount),
            )

        plan = [
            asset_id,
            current.coin.name(),
            current.coin.amount,
            ZERO_32 if settlement_coin is None else settlement_coin.name(),
            successor.name(),
            successor.amount,
        ]
        if version >= 8:
            plan.extend([
                protocol_fee_owed,
                protocol_puzzle_hash if protocol_fee_owed else ZERO_32,
            ])
        plans.append(plan)

        reserve_solution = build_reserve_solution(
            version, pool.launcher_id, asset_id, reserve_inner.get_tree_hash(),
            pool_inner_hash, [mode, pool_coin_id, *plan])

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

        if settlement_coin is not None:
            groups: list[Program] = []
            if successor_amount < current.coin.amount:
                # A shrinking reserve pays the trader; surplus above their stated
                # minimum rides along under a router nonce.
                groups.extend(requested_solution(offer, _settlement_key(asset_id)))
                payments = offer.get_requested_payments().get(_settlement_key(asset_id), [])
                if payments:
                    minimum = sum(int(payment.amount) for payment in payments)
                    # The protocol fee has already left as its own coin, so the
                    # settlement only holds the trader's share. Sizing the
                    # surplus off the full release would try to pay out more
                    # than this coin contains.
                    settlement_value = released_amount - protocol_fee_owed
                    surplus = settlement_value - minimum
                    if surplus > 0 and collect_dev_fee:
                        # Never dip below the trader's notarised minimum: the fee
                        # can only ever come out of surplus. A short collection
                        # means quote and builder drifted, so report what was
                        # actually taken rather than silently claiming the full
                        # amount.
                        # Router fee comes out of what the trader would have
                        # received, after the protocol fee has been taken.
                        dev_fee_collected = min(
                            settlement_value * dev_fee_bps // 10_000, surplus)
                        if dev_fee_collected > 0:
                            groups.append(_settlement_group(
                                bytes32(tree_hash([settlement_coin.name(), b"forge-v6-dev-fee"])),
                                [(dev_fee_puzzle_hash, dev_fee_collected)],
                            ))
                            surplus -= dev_fee_collected
                    if surplus > 0:
                        groups.append(_settlement_group(
                            bytes32(tree_hash([settlement_coin.name(), b"forge-v6-surplus"])),
                            [(payments[0].puzzle_hash, surplus)],
                        ))
            if is_native and mode == MODE_ADD and lp_eve_puzzle_hash is not None:
                # One mojo seeds the LP CAT; the TAIL authorizes the rest.
                groups.append(_settlement_group(settlement_coin.name(), [(lp_eve_puzzle_hash, 1)]))
                native_settlement_spent = True
            groups.append(_settlement_group(settlement_coin.name(), []))

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

    if mode == MODE_ADD and not native_settlement_spent and lp_eve_puzzle_hash is not None:
        # All-CAT pool: the XCH settlement exists only to back the LP mint.
        if None not in parsed:
            raise ValueError("an add Offer must provide XCH to back the LP CAT mint")
        xch_settlement = parsed[None].coin
        native_spends.append(make_spend(
            xch_settlement,
            OFFER_MOD,
            Program.to([_settlement_group(xch_settlement.name(), [(lp_eve_puzzle_hash, 1)])]),
        ))

    new_total_lp = total_lp + lp_delta
    next_state: list[object] = [
        [[assets[index], plans[index][4], plans[index][5]] for index in range(len(assets))],
        new_total_lp,
    ]

    lp_spends, lp_action_coin_id, lp_parent_id = _build_lp_leg(
        pool, offer, parsed, mode, lp_delta, new_total_lp, next_state)

    # V9+ pools derive and bind the LP action coin id from its parent; earlier
    # puzzles ignore the trailing field. See pool_singleton_v9 for why.
    action = [mode, pool_coin_id, plans, lp_action_coin_id, lp_delta, lp_parent_id]
    pool_spend = make_spend(
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
    )

    cat_spends = [
        spend
        for spendables in cat_spendables.values()
        for spend in unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends
    ]

    next_inner = _pool_inner(pool.singleton, pool.config, next_state)
    next_puzzle = puzzle_for_singleton(pool.launcher_id, next_inner)

    bundle = aggregate_with_offer(offer, [pool_spend, *native_spends, *cat_spends, *lp_spends])
    return TransitionV6Result(
        bundle,
        V3Pool(
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
        ),
        lp_delta,
        dev_fee_collected,
    )


def _build_lp_leg(
    pool: V3Pool,
    offer: Offer,
    parsed: dict,
    mode: int,
    lp_delta: int,
    new_total_lp: int,
    next_state: Sequence[object],
) -> tuple[list[CoinSpend], bytes32, bytes32]:
    """Mint LP on an add, burn it on a remove, do nothing on a swap.

    Returns the LP spends, the LP action coin id, and that coin's parent id. From
    V9 the pool derives the action coin id from (parent, pinned LP-CAT puzzle
    hash, amount) and rejects anything else, so a reserve release can no longer
    happen without a real supply change -- see pool_singleton_v9. The action coin
    therefore wraps a fixed melt/mint inner rather than a plain identity puzzle.
    """
    if mode == MODE_SWAP:
        return [], ZERO_32, ZERO_32

    identity = Program.to(1)
    state_root = tree_hash(next_state)
    lp_binding = int(pool.config[0]) >= 9

    if mode == MODE_ADD:
        # LP CAT issuance is backed by real XCH value: the eve coin carries one
        # mojo and the TAIL authorizes the remaining supply. On V9 the eve wraps
        # LP_MINT_INNER, whose only outputs are the minted settlement coin and
        # the -113 mint, so the pool's acknowledgement cannot be faked.
        xch = parsed[None]
        eve_inner = compiled_program("forge_lp_mint_inner_FORGE") if lp_binding else identity
        lp_eve_outer = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, eve_inner)
        lp_eve = Coin(xch.coin.name(), lp_eve_outer.get_tree_hash(), uint64(1))
        lp_action = [pool.pool.coin.name(), lp_eve.name(), lp_delta, new_total_lp, state_root]
        lp_parent_id = xch.coin.name() if lp_binding else ZERO_32

        lp_settlement = Coin(
            lp_eve.name(),
            construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, OFFER_MOD).get_tree_hash(),
            uint64(lp_delta),
        )
        eve_solution = Program.to(
            [OFFER_MOD_HASH, lp_delta, pool.lp_tail, Program.to(lp_action)]
            if lp_binding
            else [
                [ConditionOpcode.CREATE_COIN, OFFER_MOD_HASH, lp_delta],
                [ConditionOpcode.CREATE_COIN, 0, -113, pool.lp_tail, Program.to(lp_action)],
            ]
        )
        bundle = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [
            SpendableCAT(
                lp_eve,
                pool.lp_asset_id,
                eve_inner,
                eve_solution,
                lineage_proof=LineageProof(),
                extra_delta=lp_delta - 1,
                limitations_program_reveal=pool.lp_tail,
                limitations_solution=Program.to(lp_action),
            ),
            SpendableCAT(
                lp_settlement,
                pool.lp_asset_id,
                OFFER_MOD,
                Program.to(requested_solution(offer, pool.lp_asset_id)),
                lineage_proof=LineageProof(lp_eve.parent_coin_info, eve_inner.get_tree_hash(), uint64(1)),
            ),
        ])
        return list(bundle.coin_spends), bytes32(lp_eve.name()), bytes32(lp_parent_id)

    offered_lp = parsed[pool.lp_asset_id]
    burn = -lp_delta
    change = int(offered_lp.coin.amount) - burn
    assert offered_lp.lineage_proof is not None

    if lp_binding:
        # The burned LP is split at the settlement into exactly a burn-sized coin
        # wrapping LP_MELT_INNER (whose sole behavior is to destroy its whole
        # amount) plus any change back to the owner. The pool binds the melt
        # coin's id, so the reserve release is conditional on the real burn.
        melt_inner = compiled_program("forge_lp_melt_inner_FORGE")
        melt_cat_hash = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, melt_inner).get_tree_hash()
        melt_coin = Coin(offered_lp.coin.name(), melt_cat_hash, uint64(burn))
        lp_action = [pool.pool.coin.name(), melt_coin.name(), lp_delta, new_total_lp, state_root]

        payments: list[tuple[bytes32, int]] = [(melt_inner.get_tree_hash(), burn)]
        if change > 0:
            reserve_payments = [
                payment
                for asset_id in _assets(pool)
                for payment in offer.get_requested_payments().get(_settlement_key(asset_id), [])
            ]
            if not reserve_payments:
                raise ValueError("remove Offer has no destination for residual LP change")
            payments.append((reserve_payments[0].puzzle_hash, change))
        route = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [
            SpendableCAT(
                offered_lp.coin,
                pool.lp_asset_id,
                OFFER_MOD,
                Program.to([_settlement_group(offered_lp.coin.name(), payments)]),
                lineage_proof=offered_lp.lineage_proof,
            ),
        ])
        burn_bundle = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [
            SpendableCAT(
                melt_coin,
                pool.lp_asset_id,
                melt_inner,
                Program.to([pool.lp_tail, Program.to(lp_action)]),
                lineage_proof=LineageProof(
                    offered_lp.coin.parent_coin_info,
                    OFFER_MOD_HASH,
                    uint64(offered_lp.coin.amount),
                ),
                extra_delta=lp_delta,
                limitations_program_reveal=pool.lp_tail,
                limitations_solution=Program.to(lp_action),
            ),
        ])
        return ([*route.coin_spends, *burn_bundle.coin_spends],
                bytes32(melt_coin.name()), offered_lp.coin.name())

    intermediate = Coin(
        offered_lp.coin.name(),
        construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, identity).get_tree_hash(),
        uint64(offered_lp.coin.amount),
    )
    lp_action = [pool.pool.coin.name(), intermediate.name(), lp_delta, new_total_lp, state_root]

    # The trader may have offered more LP than the pool can burn; the remainder
    # goes back to them rather than being stranded in the settlement coin.
    burn_conditions: list[list[object]] = [
        [ConditionOpcode.CREATE_COIN, 0, -113, pool.lp_tail, Program.to(lp_action)],
    ]
    if change > 0:
        reserve_payments = [
            payment
            for asset_id in _assets(pool)
            for payment in offer.get_requested_payments().get(_settlement_key(asset_id), [])
        ]
        if not reserve_payments:
            raise ValueError("remove Offer has no destination for residual LP change")
        burn_conditions.append(
            [ConditionOpcode.CREATE_COIN, reserve_payments[0].puzzle_hash, change])

    route = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [
        SpendableCAT(
            offered_lp.coin,
            pool.lp_asset_id,
            OFFER_MOD,
            # The CAT layer wraps this output, so name the inner hash here.
            Program.to([_settlement_group(
                offered_lp.coin.name(),
                [(identity.get_tree_hash(), int(offered_lp.coin.amount))],
            )]),
            lineage_proof=offered_lp.lineage_proof,
        ),
    ])
    burn_bundle = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [
        SpendableCAT(
            intermediate,
            pool.lp_asset_id,
            identity,
            Program.to(burn_conditions),
            lineage_proof=LineageProof(
                offered_lp.coin.parent_coin_info,
                OFFER_MOD_HASH,
                uint64(offered_lp.coin.amount),
            ),
            extra_delta=lp_delta,
            limitations_program_reveal=pool.lp_tail,
            limitations_solution=Program.to(lp_action),
        ),
    ])
    return [*route.coin_spends, *burn_bundle.coin_spends], bytes32(intermediate.name()), ZERO_32
