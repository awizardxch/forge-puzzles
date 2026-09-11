#!/usr/bin/env python3
"""Routes that cross a vault: a swap chained into an LP redemption.

A single-asset pool cannot trade -- there is no second reserve to move -- so the
router excludes it from the swap graph. But its LP token is an ordinary CAT that
can be paired in another pool, and burning that LP returns the underlying. Put
those together and the vault becomes reachable:

    TXCH --swap--> vaultLP --redeem--> t8

The redemption is not a curve. A vault can never swap, so no fee ever accrues to
it, and both deposits and withdrawals move reserve and LP proportionally. Its
ratio is therefore fixed at creation forever, and the leg is an exact conversion
with no price impact.

What makes the composition safe is the same property multi-hop relies on: the LP
coin the swap releases is created and spent inside one bundle, so it is never
claimable on its own and either the whole route settles or none of it does.

The swap pool may hold any number of assets. Only the vault has to be
single-asset, because a wider pool's withdrawal returns every reserve pro rata
and so cannot be one leg of a linear route.

Testnet research only; unaudited.
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
    find_offer_settlements,
    requested_amounts,
    requested_solution,
    tree_hash,
)
from forge_multihop_swap import (
    IDENTITY,
    _plan_and_spend_reserve,
    _successor_pool,
    protocol_fee_for,
)
from forge_math import swap_output, vault_fee_bps, withdrawal_amounts
from forge_transition import (
    _assets,
    _reserve_puzzle_hash,
    _reserves,
    _settlement_key,
    _settlement_puzzle_hash,
)



@dataclass(frozen=True)
class VaultRouteResult:
    bundle: WalletSpendBundle
    pools: list[V3Pool]
    """Successor states, swap pool first then the vault."""
    swap_out: int
    """LP released by the swap, which is exactly what the vault burns."""
    redeemed: int
    """Underlying paid to the trader."""


def vault_redeem_rate(vault: V3Pool) -> tuple[int, int]:
    """Reserve and LP supply. Their ratio is fixed for the life of the vault."""
    reserves = _reserves(vault)
    if len(reserves) != 1:
        raise ValueError("a vault leg needs a single-asset pool")
    return reserves[0], int(vault.state[1])


def quote_swap_then_redeem(
    swap_pool: V3Pool,
    asset_in: bytes32,
    vault: V3Pool,
    amount_in: int,
) -> tuple[int, int]:
    """(LP from the swap, underlying from the redemption)."""
    lp_asset = bytes32(vault.lp_asset_id)
    assets = _assets(swap_pool)
    if lp_asset not in assets:
        raise ValueError("the swap pool does not hold this vault's LP asset")
    if asset_in not in assets or asset_in == lp_asset:
        raise ValueError("swap input must be a different asset in the swap pool")

    released = swap_released(swap_pool, asset_in, lp_asset, amount_in)
    # On V8 the reserve pays part of what it releases straight to the fee
    # recipient, so only the remainder reaches the vault to be burned.
    _, fee = protocol_fee_for(swap_pool, released)
    lp_out = released - fee
    if lp_out <= 0:
        raise ValueError("protocol fee consumes the whole swap output")
    reserve, supply = vault_redeem_rate(vault)
    return lp_out, withdrawal_amounts(
        [reserve], lp_out, supply,
        vault_fee_bps(1, int(vault.config[0]), int(vault.config[4])))[0]


def swap_released(pool: V3Pool, asset_in: bytes32, asset_out: bytes32, amount_in: int) -> int:
    """Gross amount the reserve releases, before any protocol fee is carved."""
    assets = _assets(pool)
    reserves = _reserves(pool)
    i, j = assets.index(asset_in), assets.index(asset_out)
    # Weight units from V7 on; a V6 pool stores basis points here and its puzzle
    # ignores them, so it is equally weighted.
    units = ([int(w) for w in pool.config[3]] if int(pool.config[0]) >= 7
             else [1] * len(assets))
    return swap_output(
        reserves[i], reserves[j], amount_in, int(pool.config[4]), units[i], units[j],
    )


def build_swap_then_redeem(
    swap_pool: V3Pool,
    asset_in: bytes32,
    vault: V3Pool,
    offer: Offer,
) -> VaultRouteResult:
    """Swap into a vault's LP, then burn that LP for the underlying.

    The Offer provides `asset_in` and requests the vault's reserve asset. Every
    coin between those two ends is created and spent here.
    """
    lp_asset = bytes32(vault.lp_asset_id)
    vault_assets = _assets(vault)
    if len(vault_assets) != 1:
        raise ValueError("a vault leg needs a single-asset pool")
    underlying = vault_assets[0]

    swap_assets = _assets(swap_pool)
    if lp_asset not in swap_assets:
        raise ValueError("the swap pool does not hold this vault's LP asset")
    if asset_in not in swap_assets or asset_in == lp_asset:
        raise ValueError("swap input must be a different asset in the swap pool")
    if swap_pool.launcher_id == vault.launcher_id:
        raise ValueError("a route may not use one pool for both legs")

    entry_key = _settlement_key(asset_in)
    parsed = find_offer_settlements(offer, (asset_in, lp_asset, underlying))
    if entry_key not in parsed:
        raise ValueError("the Offer does not provide the route's input asset")
    entry = parsed[entry_key]
    amount_in = int(entry.coin.amount)

    requested = requested_amounts(offer)
    exit_key = _settlement_key(underlying)
    if set(requested) - {exit_key}:
        raise ValueError("the Offer requests an asset outside the route output")
    minimum_out = requested.get(exit_key, 0)
    if minimum_out <= 0:
        raise ValueError("the Offer must request the route's output asset")

    lp_out, redeemed = quote_swap_then_redeem(swap_pool, asset_in, vault, amount_in)
    if redeemed < minimum_out:
        raise ValueError("route output is below the Offer minimum")

    spends: list[CoinSpend] = []
    cat_spendables: dict[bytes32, list[SpendableCAT]] = {}

    def add_cat(asset_id: bytes32, spendable: SpendableCAT) -> None:
        cat_spendables.setdefault(asset_id, []).append(spendable)

    # ── Leg one: the swap, releasing the vault's LP as an ephemeral coin ─────
    released_lp = swap_released(swap_pool, asset_in, lp_asset, amount_in)
    lp_coin, lp_lineage, swap_successor = _build_swap_leg(
        swap_pool, asset_in, lp_asset, amount_in, released_lp, entry, spends, add_cat)

    # ── Leg two: burn that LP at the vault, paying the trader the underlying ──
    vault_successor = _build_redeem_leg(
        vault, underlying, lp_coin, lp_lineage, lp_out, redeemed, minimum_out,
        offer, spends, add_cat)

    cat_spends = [
        spend
        for spendables in cat_spendables.values()
        for spend in unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends
    ]
    bundle = aggregate_with_offer(offer, [*spends, *cat_spends])
    return VaultRouteResult(bundle, [swap_successor, vault_successor], lp_out, redeemed)


def _build_swap_leg(
    pool: V3Pool,
    asset_in: bytes32,
    asset_out: bytes32,
    amount_in: int,
    amount_out: int,
    entry,
    spends: list[CoinSpend],
    add_cat,
) -> tuple[Coin, LineageProof, V3Pool]:
    """Standard pairwise swap; its exit coin is handed to the next leg."""
    version = int(pool.config[0])
    reserve_inner = reserve_inner_puzzle(version, pool.launcher_id)
    assets = _assets(pool)
    old = _reserves(pool)

    out_reserve = pool.reserves[asset_out]
    # `amount_out` is gross: the successor balance is derived from what the
    # reserve releases, while the coin that travels onward is what is left after
    # the fee the reserve pays directly to the recipient.
    _, leg_fee = protocol_fee_for(pool, amount_out)
    exit_coin = Coin(
        out_reserve.coin.name(),
        _settlement_puzzle_hash(asset_out),
        uint64(amount_out - leg_fee),
    )
    exit_lineage = LineageProof(
        out_reserve.coin.parent_coin_info,
        out_reserve.inner_puzzle.get_tree_hash(),
        uint64(out_reserve.coin.amount),
    )

    plans: list[list[object]] = []
    next_reserves: dict[bytes32, ReserveCoin] = {}
    for index, asset_id in enumerate(assets):
        if asset_id == asset_in:
            successor_amount = old[index] + amount_in
            settlement = entry.coin
        elif asset_id == asset_out:
            successor_amount = old[index] - amount_out
            settlement = exit_coin
        else:
            successor_amount = old[index]
            settlement = None

        # Only the reserve that releases owes a protocol fee, and only on V8.
        recipient, fee = (
            protocol_fee_for(pool, amount_out) if asset_id == asset_out
            else (ZERO_32, 0)
        )
        plan, reserve = _plan_and_spend_reserve(
            pool, MODE_SWAP, asset_id, successor_amount, settlement,
            reserve_inner, spends, add_cat, fee, recipient)
        plans.append(plan)
        next_reserves[asset_id] = reserve

        if settlement is None:
            continue
        # The entry settlement is spent once, by the leg that consumes it. The
        # exit settlement is spent by the NEXT leg, so it is not spent here.
        if asset_id == asset_in:
            groups = [_settlement_group(settlement.name(), [])]
            if asset_id == ZERO_32:
                spends.append(make_spend(settlement, OFFER_MOD, Program.to(groups)))
            else:
                assert entry.lineage_proof is not None
                add_cat(asset_id, SpendableCAT(
                    settlement, asset_id, OFFER_MOD, Program.to(groups),
                    lineage_proof=entry.lineage_proof,
                ))

    next_state: list[object] = [
        [[assets[i], plans[i][4], plans[i][5]] for i in range(len(assets))],
        int(pool.state[1]),
    ]
    spends.append(make_spend(
        pool.pool.coin,
        pool.pool.puzzle,
        solution_for_singleton(
            LineageProof(
                pool.pool.lineage_parent_name,
                pool.pool.parent_inner_puzzle_hash,
                uint64(pool.pool.coin.amount),
            ),
            uint64(1),
            Program.to([[MODE_SWAP, pool.pool.coin.name(), plans, ZERO_32, 0]]),
        ),
    ))
    return exit_coin, exit_lineage, _successor_pool(pool, next_state, next_reserves)


def _build_redeem_leg(
    vault: V3Pool,
    underlying: bytes32,
    lp_coin: Coin,
    lp_lineage: LineageProof,
    burn: int,
    redeemed: int,
    minimum_out: int,
    offer: Offer,
    spends: list[CoinSpend],
    add_cat,
) -> V3Pool:
    """Burn the LP the swap released and pay the trader the underlying."""
    version = int(vault.config[0])
    reserve_inner = reserve_inner_puzzle(version, vault.launcher_id)
    lp_asset = bytes32(vault.lp_asset_id)
    reserve_amount = _reserves(vault)[0]

    payout_coin = Coin(
        vault.reserves[underlying].coin.name(),
        _settlement_puzzle_hash(underlying),
        uint64(redeemed),
    )
    plan, reserve = _plan_and_spend_reserve(
        vault, MODE_REMOVE, underlying, reserve_amount - redeemed, payout_coin,
        reserve_inner, spends, add_cat)

    # The reserve pays the trader their notarised amount, and any surplus above
    # it rides along under a router nonce rather than being stranded.
    groups = list(requested_solution(offer, _settlement_key(underlying)))
    surplus = redeemed - minimum_out
    if surplus > 0:
        payments = offer.get_requested_payments().get(_settlement_key(underlying), [])
        if not payments:
            raise ValueError("no destination for the route's surplus output")
        groups.append(_settlement_group(
            bytes32(tree_hash([payout_coin.name(), b"forge-vault-surplus"])),
            [(payments[0].puzzle_hash, surplus)],
        ))
    groups.append(_settlement_group(payout_coin.name(), []))

    payout_lineage = LineageProof(
        vault.reserves[underlying].coin.parent_coin_info,
        vault.reserves[underlying].inner_puzzle.get_tree_hash(),
        uint64(vault.reserves[underlying].coin.amount),
    )
    if underlying == ZERO_32:
        spends.append(make_spend(payout_coin, OFFER_MOD, Program.to(groups)))
    else:
        add_cat(underlying, SpendableCAT(
            payout_coin, underlying, OFFER_MOD, Program.to(groups),
            lineage_proof=payout_lineage,
        ))

    new_total_lp = int(vault.state[1]) - burn
    next_state: list[object] = [[[underlying, plan[4], plan[5]]], new_total_lp]

    # Melt the LP the swap released. It arrives as a settlement coin, so route it
    # through an intermediate the TAIL can burn, and name THAT coin as the pool's
    # LP action -- naming the settlement instead leaves the pool asserting an
    # announcement nothing emits.
    #
    # From V9 the intermediate must wrap the pinned melt inner rather than a bare
    # identity puzzle: the pool derives the action coin's id from that puzzle
    # hash, so an identity-wrapped coin simply does not match. From V10 the pool
    # action also carries the LP coin's parent, which is what it derives the id
    # against.
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
    pool_action: list[object] = [MODE_REMOVE, vault.pool.coin.name(), [plan],
                                 intermediate.name(), -burn]
    if version >= 10:
        pool_action.append(lp_coin.name())

    spends.append(make_spend(
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
    # Two groups, not one. The payment moves the LP into a coin the TAIL can
    # melt; the empty group satisfies the puzzle announcement the swap pool's
    # reserve asserts about its own exit settlement. A normal remove never needs
    # the second one, because there the LP arrives from the Offer rather than
    # from a reserve that is watching for it.
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
        Program.to([vault.lp_tail, Program.to(lp_action)]) if lp_binding
        else Program.to([[ConditionOpcode.CREATE_COIN, 0, -113, vault.lp_tail,
                          Program.to(lp_action)]]),
        lineage_proof=LineageProof(
            lp_coin.parent_coin_info, OFFER_MOD_HASH, uint64(lp_coin.amount)),
        extra_delta=-burn,
        limitations_program_reveal=vault.lp_tail,
        limitations_solution=Program.to(lp_action),
    ))

    return _successor_pool(vault, next_state, {underlying: reserve})
