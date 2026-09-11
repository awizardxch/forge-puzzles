#!/usr/bin/env python3
"""One-shot equilibrium settlement: one net swap per pool, as a flow.

The split lane packs pool-disjoint cycles, which forces overlapping cycles into
sequential offers — observed on testnet 2026-08-30, where the second offer
crossed a pool the first had already moved, in the opposite direction. In the
global optimum no pool is crossed both ways, so each pool needs exactly one net
swap. This builder settles that shape: a FLOW rather than branch cycles — a
pool's output may feed several consumers, and a pool may drink from several
producers, with every intermediate asset conserved exactly across the bundle.

How value moves
---------------
The same way the split lane's shared entry already works: a settlement coin is
authorization, not a pipe. A reserve names ONE settlement coin of its input
asset and asserts that coin's ``(id, [])`` announcement; the coin is spent once
and satisfies every asserter; the mojos balance through the CAT ring / bundle
value balance. So a merge needs no new machinery — two consumers simply name
the same producer's exit coin — and a split of one output across consumers is
the entry precedent applied one level down.

Server-side requote
-------------------
The caller sends per-leg inputs as intent. Amounts are re-derived here, in
topological order, against the pools' current snapshots: XCH entries are
rescaled to what the Offer actually put up (exactly as the split lane does),
and each intermediate consumer's input is its declared share of what its
producers actually released — so conservation holds in mojos, not merely in
intent, and the puzzle's +1 bracket is satisfied by construction.

Vault redemptions ride along with one constraint: the melt payment and the
announcement group must travel on one spend, so a vault leg must be fed
wholly by a single producer.

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
    _build_vault_redeem_leg,
    _build_vault_wrap_leg,
    _plan_amounts,
    _plan_legs,
    _pool_assets,
    _reserve_puzzle_hash,
    _settlement_puzzle_hash,
    extend_plan_for_protocol_fee,
    protocol_fee_for,
    vault_leg_kind,
    wrap_mint_eve_puzzle,
)


@dataclass(frozen=True)
class FlowLegSpec:
    """One pool's single net crossing: the pool, the pair, and the intent."""

    pool: V3Pool
    asset_in: bytes32
    asset_out: bytes32
    amount_in: int


def _sim_pool(base: V3Pool, amounts: dict[bytes32, int], total_lp: int) -> V3Pool:
    """A quoting stand-in for `base` at simulated reserve amounts.

    Coin ids in the synthetic state are dummies — everything the quoting path
    reads is the amounts, the LP supply, and the static config. Needed because
    a second crossing of the same pool prices against the FIRST crossing's
    successor state, not the snapshot the request carried.
    """
    state: list[object] = [
        [[bytes(asset), b"\x00" * 32, amounts[asset]] for asset in _pool_assets(base)],
        total_lp,
    ]
    return V3Pool(base.launcher_id, base.singleton, base.config, state,
                  base.pool, base.reserves, base.lp_asset_id, base.lp_tail)


@dataclass(frozen=True)
class FlowResult:
    bundle: WalletSpendBundle
    pools: list[V3Pool]
    leg_amounts: list[tuple[int, int]]
    """(amount_in, amount_out) per leg, in the input order."""
    total_out: int


def _toposort(specs: Sequence[FlowLegSpec], start_asset: bytes32) -> list[int]:
    """Order legs so every producer of a leg's input runs before it.

    The start asset is the source and the sink, so legs drinking it are ready
    at once. A stuck sort means the intermediates form a cycle, which has no
    execution order — the planner never emits one, so refusing is a contract
    check.

    A pool crossed more than once adds an ordering of its own: the second
    crossing spends the FIRST crossing's successor coin (an ephemeral chained
    advance), so same-pool legs must run in their given order even when a
    later one's input is ready sooner.
    """
    producers: dict[bytes32, list[int]] = {}
    for index, spec in enumerate(specs):
        if spec.asset_out != start_asset:
            producers.setdefault(spec.asset_out, []).append(index)

    same_pool_pred: dict[int, list[int]] = {}
    by_pool: dict[bytes32, list[int]] = {}
    for index, spec in enumerate(specs):
        preds = by_pool.setdefault(spec.pool.launcher_id, [])
        same_pool_pred[index] = list(preds)
        preds.append(index)

    order: list[int] = []
    done: set[int] = set()
    while len(order) < len(specs):
        progressed = False
        for index, spec in enumerate(specs):
            if index in done:
                continue
            if not all(p in done for p in same_pool_pred[index]):
                continue
            if spec.asset_in != start_asset:
                feeding = producers.get(spec.asset_in, [])
                if not feeding:
                    raise ValueError("flow leg consumes an asset nothing produces")
                if not all(p in done for p in feeding):
                    continue
            order.append(index)
            done.add(index)
            progressed = True
        if not progressed:
            raise ValueError("flow legs form a cycle among intermediate assets")
    return order


def _validate(specs: Sequence[FlowLegSpec], start_asset: bytes32) -> None:
    if len(specs) < 2:
        raise ValueError("a flow needs at least two legs")
    # A pool may be crossed more than once — the later crossing chains onto the
    # earlier one's successor coin inside the same bundle — but never on the
    # same ordered pair twice: identical crossings should have been netted into
    # one, and building them separately only pays the fee twice.
    crossings: dict[bytes32, int] = {}
    pairs: set[tuple[bytes32, bytes32, bytes32]] = set()
    for spec in specs:
        count = crossings.get(spec.pool.launcher_id, 0)
        if count >= 4:
            raise ValueError("a flow may cross one pool at most four times")
        crossings[spec.pool.launcher_id] = count + 1
        pair = (spec.pool.launcher_id, spec.asset_in, spec.asset_out)
        if pair in pairs:
            raise ValueError("a flow crosses each pool pair at most once; net same-pair crossings first")
        pairs.add(pair)
        if spec.amount_in <= 0:
            raise ValueError("every flow leg needs a positive input intent")
        assets = set(_pool_assets(spec.pool))
        if spec.asset_in in assets and spec.asset_out in assets:
            continue
        if vault_leg_kind(spec.pool, spec.asset_in, spec.asset_out) is not None:
            continue
        raise ValueError(
            f"pool {spec.pool.launcher_id.hex()[:16]} does not trade its assigned pair")
    if not any(spec.asset_in == start_asset for spec in specs):
        raise ValueError("a flow needs at least one entry leg in the start asset")
    if not any(spec.asset_out == start_asset for spec in specs):
        raise ValueError("a flow needs at least one exit leg in the start asset")
    # A wrap mints LP, and every CAT mojo is an XCH mojo: the mint's value can
    # only be carved out of a native entry. A CAT-entry flow has no free XCH
    # to back a mint with, so wraps are XCH-entry only.
    if start_asset != ZERO_32 and any(
        vault_leg_kind(spec.pool, spec.asset_in, spec.asset_out) == "vault-wrap"
        for spec in specs
    ):
        raise ValueError("a vault-wrap leg requires an XCH entry to fund its mint")


def build_flow_balance(
    specs: Sequence[FlowLegSpec],
    offer: Offer,
    start_asset: bytes32 = ZERO_32,
) -> FlowResult:
    """Settle one net swap per pool as a single atomic bundle.

    No router fee: the balancer is the platform trading with itself, and the
    surplus above the Offer's requested amount is paid back to the maker like
    any other surplus.
    """
    _validate(specs, start_asset)
    order = _toposort(specs, start_asset)

    every_asset = {asset for spec in specs for asset in _pool_assets(spec.pool)}
    parsed = find_offer_settlements(offer, tuple(every_asset))
    entry_key = None if start_asset == ZERO_32 else start_asset
    if entry_key not in parsed:
        raise ValueError("flow Offer does not provide the start asset")
    entry_settlement = parsed[entry_key]

    requested = requested_amounts(offer)
    if set(requested) - {entry_key}:
        raise ValueError("flow Offer requests an asset outside the flow's start asset")
    minimum_out = requested.get(entry_key, 0)
    if minimum_out <= 0:
        raise ValueError("flow Offer must request the start asset back")

    # ── rescale XCH entries to what the Offer actually put up ────────────────
    entry_indices = [i for i, spec in enumerate(specs) if spec.asset_in == start_asset]
    declared_entry = sum(specs[i].amount_in for i in entry_indices)
    actual_entry = int(entry_settlement.coin.amount)
    entry_amounts = {i: specs[i].amount_in for i in entry_indices}
    def requote(entry_budget: int):
        """Requote every leg in topological order against `entry_budget` XCH.

        available[asset] holds what upstream legs actually released and
        downstream legs have not yet drawn; the LAST consumer of an asset takes
        the exact remainder, so conservation holds to the mojo.
        """
        if declared_entry != entry_budget:
            scaled = {i: specs[i].amount_in * entry_budget // declared_entry for i in entry_indices}
            shortfall = entry_budget - sum(scaled.values())
            if shortfall != 0:
                largest = max(scaled, key=lambda i: scaled[i])
                scaled[largest] += shortfall
            if any(amount <= 0 for amount in scaled.values()):
                raise ValueError("the Offer's actual amount is too small to fund every entry leg")
            amounts = scaled
        else:
            amounts = {i: specs[i].amount_in for i in entry_indices}

        r_in: dict[int, int] = {}
        r_out: dict[int, int] = {}
        r_fee: dict[int, int] = {}
        r_kind: dict[int, str] = {}
        available: dict[bytes32, int] = {}
        consumers_left: dict[bytes32, int] = {}
        declared_share: dict[bytes32, int] = {}
        for spec in specs:
            if spec.asset_in != start_asset:
                consumers_left[spec.asset_in] = consumers_left.get(spec.asset_in, 0) + 1
                declared_share[spec.asset_in] = declared_share.get(spec.asset_in, 0) + spec.amount_in

        # Reserve state threaded per pool, so a chained second crossing quotes
        # against the first crossing's successor rather than the snapshot.
        sim_amounts: dict[bytes32, dict[bytes32, int]] = {}
        sim_lp: dict[bytes32, int] = {}

        for index in order:
            spec = specs[index]
            if spec.asset_in == start_asset:
                amount_in = amounts[index]
            else:
                pot = available.get(spec.asset_in, 0)
                remaining_consumers = consumers_left[spec.asset_in]
                if remaining_consumers == 1:
                    amount_in = pot
                else:
                    amount_in = pot * spec.amount_in // declared_share[spec.asset_in]
                if amount_in <= 0:
                    raise ValueError("a flow leg's input resolved to nothing")
                available[spec.asset_in] = pot - amount_in
                consumers_left[spec.asset_in] = remaining_consumers - 1

            lid = spec.pool.launcher_id
            if lid not in sim_amounts:
                sim_amounts[lid] = dict(_plan_amounts(spec.pool))
                sim_lp[lid] = int(spec.pool.state[1])
            quote_pool = _sim_pool(spec.pool, sim_amounts[lid], sim_lp[lid])
            leg = _plan_legs([quote_pool], [spec.asset_in, spec.asset_out], amount_in)[0]
            if leg.kind == "swap":
                sim_amounts[lid][spec.asset_in] += amount_in
                sim_amounts[lid][spec.asset_out] -= leg.amount_out
            elif leg.kind == "vault-redeem":
                sim_amounts[lid][spec.asset_out] -= leg.amount_out
                sim_lp[lid] -= amount_in
            else:  # vault-wrap
                sim_amounts[lid][spec.asset_in] += amount_in
                sim_lp[lid] += leg.amount_out
            r_in[index] = amount_in
            r_out[index] = leg.amount_out
            r_fee[index] = leg.protocol_fee
            r_kind[index] = leg.kind
            if spec.asset_out != start_asset:
                available[spec.asset_out] = available.get(spec.asset_out, 0) + (
                    leg.amount_out - leg.protocol_fee)

        for asset, leftover in available.items():
            if leftover != 0:
                raise ValueError(
                    f"intermediate {asset.hex()[:12]} does not conserve: {leftover} left over")
        return r_in, r_out, r_fee, r_kind, amounts

    # ── the wrap-backing bisection ───────────────────────────────────────────
    # Every LP mojo a wrap mints is an XCH mojo of bundle value: one mojo pays
    # the mint eve into existence and the rest rides its extra_delta. That
    # value must come out of the Offer's entry, so the entry legs deposit
    # `actual_entry - backing` and the mint consumes the remainder. The mint
    # shrinks as the backing grows (less enters the pools, less reaches the
    # vault), so the smallest sufficient backing is found by bisection --
    # naive iteration oscillates when the vault's scale rivals the entry.
    # Integer steps mean minted and backing need not meet exactly; the mojo or
    # two of slack between them is unallocated bundle value and lands as
    # network fee, which is the cheapest place for it.
    def backing_suffices(backing_try: int) -> bool:
        # A backing so large the starved pools cannot quote at all is
        # "sufficient" for the search's purposes -- the bisection then shrinks
        # toward the smallest backing that both quotes and covers its mint,
        # and the final requote surfaces the real error if none exists.
        try:
            r = requote(actual_entry - backing_try)
        except ValueError:
            return True
        minted = sum(r[1][i] for i, spec in enumerate(specs)
                     if r[3].get(i) == "vault-wrap")
        return minted <= backing_try

    backing = 0
    if any(vault_leg_kind(s.pool, s.asset_in, s.asset_out) == "vault-wrap" for s in specs):
        low, high = 0, actual_entry - len(entry_indices)
        if not backing_suffices(high):
            raise ValueError("the Offer's entry cannot fund the wrap's mint backing")
        while low < high:
            mid = (low + high) // 2
            if backing_suffices(mid):
                high = mid
            else:
                low = mid + 1
        backing = low

    resolved_in, resolved_out, resolved_fee, resolved_kind, entry_amounts = (
        requote(actual_entry - backing))

    total_out = sum(
        resolved_out[i] - resolved_fee[i]
        for i, spec in enumerate(specs) if spec.asset_out == start_asset
    )
    if total_out < minimum_out:
        # Say by how much. This is the error a stale quote produces -- the panel
        # priced the plan against a snapshot the chain has since moved past --
        # and without the two numbers it is indistinguishable from a builder
        # fault, which cost real time twice before the figures were printed.
        shortfall = minimum_out - total_out
        raise ValueError(
            f"flow output is below the Offer minimum: the route yields {total_out} "
            f"but the Offer asks {minimum_out}, short by {shortfall} "
            f"({shortfall * 10_000 // max(minimum_out, 1)} bps). The quote was "
            f"probably priced against reserves that have since moved.")

    # ── build the spends ─────────────────────────────────────────────────────
    cat_spendables: dict[bytes32, list[SpendableCAT]] = {}
    native_spends: list[CoinSpend] = []
    pool_spends: list[CoinSpend] = []
    successor_pools: list[V3Pool] = []
    start_exit_coins: list[tuple[Coin, LineageProof]] = []
    entry_spent = False

    def add_cat(asset_id: bytes32, spendable: SpendableCAT) -> None:
        cat_spendables.setdefault(asset_id, []).append(spendable)

    # Each intermediate asset gets ONE named source coin — the first producer's
    # exit coin. Every consumer asserts that coin's announcement; the mojos of
    # every producer balance through the ring regardless of which coin was
    # named. Exit coins that are not the named source are spent with an empty
    # group so the ring closes.
    source_coin: dict[bytes32, tuple[Coin, LineageProof]] = {}
    pending_exit: list[tuple[Coin, LineageProof, bytes32]] = []
    # A vault leg consumes its producer's coin directly (melt + announcement on
    # one spend), so that coin must be handed over unspent.
    vault_feeders: dict[bytes32, int] = {}
    for index, spec in enumerate(specs):
        if resolved_kind.get(index) == "vault-redeem":
            feeders = [i for i, s in enumerate(specs) if s.asset_out == spec.asset_in]
            if len(feeders) != 1:
                raise ValueError("a vault redemption must be fed by exactly one producer")
            if consumers_of := [i for i, s in enumerate(specs) if s.asset_in == spec.asset_in]:
                if len(consumers_of) != 1:
                    raise ValueError("a vault redemption may not share its input asset")
            vault_feeders[spec.asset_in] = feeders[0]

    # A pool crossed more than once chains: the second crossing spends the
    # first crossing's successor coin and reserves, all inside this bundle —
    # consensus allows spending a coin the same bundle creates, and the
    # successor carries correct lineage for it. `live_pool` tracks each
    # pool's newest incarnation as legs build.
    live_pool: dict[bytes32, V3Pool] = {}

    for index in order:
        spec = specs[index]
        pool = live_pool.get(spec.pool.launcher_id, spec.pool)
        amount_in = resolved_in[index]
        amount_out = resolved_out[index]
        kind = resolved_kind[index]
        reserve_inner = reserve_inner_puzzle(int(pool.config[0]), pool.launcher_id)
        pool_coin_id = pool.pool.coin.name()

        if kind == "vault-redeem":
            carried, carried_lineage = source_coin[spec.asset_in]
            leg = _plan_legs([pool], [spec.asset_in, spec.asset_out], amount_in)[0]
            exit_coin, exit_lineage, successor = _build_vault_redeem_leg(
                pool, leg, carried, carried_lineage, reserve_inner,
                pool_spends, native_spends, add_cat,
            )
            successor_pools.append(successor)
            live_pool[pool.launcher_id] = successor
            # Same four cases as a swap exit, and for the same reasons. This
            # branch used to carry only three: when a redemption was the FIRST
            # producer of an intermediate asset it became that asset's named
            # source but was never queued in `pending_exit`, so the bundle
            # created a settlement coin nothing ever spent and the asset's CAT
            # ring could not balance ("input and output amounts don't match").
            # It stayed hidden while every tested redemption paid out in the
            # START asset -- those exit through `start_exit_coins` and never
            # reach this path. A redemption feeding an intermediate is the
            # ordinary shape once a vault sits mid-route: buy the LP, redeem it
            # to t8, sell that t8 on somewhere else.
            if spec.asset_out == start_asset:
                start_exit_coins.append((exit_coin, exit_lineage))
            elif spec.asset_out in vault_feeders:
                # Handed to the next redemption unspent; consumed there.
                source_coin[spec.asset_out] = (exit_coin, exit_lineage)
            elif spec.asset_out not in source_coin:
                source_coin[spec.asset_out] = (exit_coin, exit_lineage)
                pending_exit.append((exit_coin, exit_lineage, spec.asset_out))
            else:
                pending_exit.append((exit_coin, exit_lineage, spec.asset_out))
            continue

        if kind == "vault-wrap":
            # The deposit coin is NOT handed over like a redemption's LP: the
            # vault's reserve merely names it, and it closes through
            # pending_exit like any consumed intermediate. What the wrap needs
            # uniquely is its mint eve, funded by one mojo from the entry
            # settlement (see the entry spend below) and backed by the entry
            # under-deposit the fixed point carved out.
            carried, _carried_lineage = source_coin[spec.asset_in]
            leg = _plan_legs([pool], [spec.asset_in, spec.asset_out], amount_in)[0]
            exit_coin, exit_lineage, successor = _build_vault_wrap_leg(
                pool, leg, carried, entry_settlement.coin.name(), reserve_inner,
                pool_spends, native_spends, add_cat,
            )
            successor_pools.append(successor)
            live_pool[pool.launcher_id] = successor
            if spec.asset_out == start_asset:
                start_exit_coins.append((exit_coin, exit_lineage))
            elif spec.asset_out in vault_feeders:
                source_coin[spec.asset_out] = (exit_coin, exit_lineage)
            elif spec.asset_out not in source_coin:
                source_coin[spec.asset_out] = (exit_coin, exit_lineage)
                pending_exit.append((exit_coin, exit_lineage, spec.asset_out))
            else:
                pending_exit.append((exit_coin, exit_lineage, spec.asset_out))
            continue

        if kind != "swap":
            raise ValueError(f"a flow cannot settle a {kind} leg")

        reserves_now = _plan_amounts(pool)
        successor_amounts = {asset_id: reserves_now[asset_id] for asset_id in _pool_assets(pool)}
        successor_amounts[spec.asset_in] = reserves_now[spec.asset_in] + amount_in
        successor_amounts[spec.asset_out] = reserves_now[spec.asset_out] - amount_out

        out_reserve = pool.reserves[spec.asset_out]
        exit_coin = Coin(
            out_reserve.coin.name(),
            _settlement_puzzle_hash(spec.asset_out),
            uint64(amount_out - resolved_fee[index]),
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
            is_native = asset_id == ZERO_32
            successor = Coin(
                current.coin.name(),
                _reserve_puzzle_hash(asset_id, reserve_inner),
                uint64(successor_amounts[asset_id]),
            )

            settlement_coin: Coin | None
            if asset_id == spec.asset_in:
                if spec.asset_in == start_asset:
                    settlement_coin = entry_settlement.coin
                else:
                    settlement_coin = source_coin[spec.asset_in][0]
            elif asset_id == spec.asset_out:
                settlement_coin = exit_coin
            else:
                settlement_coin = None

            recipient, fee = (
                protocol_fee_for(pool, amount_out)
                if asset_id == spec.asset_out else (ZERO_32, 0)
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

            reserve_sol = build_reserve_solution(
                int(pool.config[0]), pool.launcher_id, asset_id,
                reserve_inner.get_tree_hash(), pool.pool.inner_puzzle.get_tree_hash(),
                [MODE_SWAP, pool_coin_id, *plan])

            if is_native:
                native_spends.append(make_spend(current.coin, current.inner_puzzle, reserve_sol))
            else:
                add_cat(asset_id, SpendableCAT(
                    current.coin, asset_id, current.inner_puzzle, reserve_sol,
                    lineage_proof=current.lineage_proof,
                ))

            # The entry coin is named by every entry leg but spent once. Each
            # wrap leg's mint eve is paid its one mojo here -- the eve's
            # parent must be this very coin, since that is the id the wrap
            # builder derived and the V10 pool binds.
            if asset_id == spec.asset_in and spec.asset_in == start_asset and not entry_spent:
                entry_spent = True
                eve_payments = [
                    (wrap_mint_eve_puzzle(specs[i].pool).get_tree_hash(), 1)
                    for i in order if resolved_kind.get(i) == "vault-wrap"
                ]
                groups = [_settlement_group(entry_settlement.coin.name(), [])]
                if eve_payments:
                    groups.append(_settlement_group(
                        bytes32(Program.to([entry_settlement.coin.name(),
                                            b"forge-flow-wrap-eve"]).get_tree_hash()),
                        eve_payments,
                    ))
                if start_asset == ZERO_32:
                    native_spends.append(make_spend(entry_settlement.coin, OFFER_MOD, Program.to(groups)))
                else:
                    add_cat(start_asset, SpendableCAT(
                        entry_settlement.coin, start_asset, OFFER_MOD,
                        Program.to(groups), lineage_proof=entry_settlement.lineage_proof,
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

        if spec.asset_out == start_asset:
            start_exit_coins.append((exit_coin, exit_lineage))
        elif spec.asset_out in vault_feeders:
            # Handed to the redemption unspent; it will be consumed there.
            source_coin[spec.asset_out] = (exit_coin, exit_lineage)
        elif spec.asset_out not in source_coin:
            source_coin[spec.asset_out] = (exit_coin, exit_lineage)
            pending_exit.append((exit_coin, exit_lineage, spec.asset_out))
        else:
            pending_exit.append((exit_coin, exit_lineage, spec.asset_out))

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
        swap_successor = V3Pool(
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
        successor_pools.append(swap_successor)
        live_pool[pool.launcher_id] = swap_successor

    # ── spend the intermediate exit coins ────────────────────────────────────
    for coin, lineage, asset_id in pending_exit:
        groups = [_settlement_group(coin.name(), [])]
        if asset_id == ZERO_32:
            native_spends.append(make_spend(coin, OFFER_MOD, Program.to(groups)))
        else:
            add_cat(asset_id, SpendableCAT(
                coin, asset_id, OFFER_MOD, Program.to(groups), lineage_proof=lineage,
            ))

    # ── the XCH exits: first carries the requested payment and the surplus ───
    for position, (coin, lineage) in enumerate(start_exit_coins):
        groups: list[Program] = []
        if position == 0:
            groups.extend(requested_solution(offer, entry_key))
            surplus = total_out - minimum_out
            if surplus > 0:
                payments = offer.get_requested_payments()[entry_key]
                groups.append(_settlement_group(
                    bytes32(Program.to([coin.name(), b"forge-flow-surplus"]).get_tree_hash()),
                    [(payments[0].puzzle_hash, surplus)],
                ))
        groups.append(_settlement_group(coin.name(), []))
        if start_asset == ZERO_32:
            native_spends.append(make_spend(coin, OFFER_MOD, Program.to(groups)))
        else:
            add_cat(start_asset, SpendableCAT(
                coin, start_asset, OFFER_MOD, Program.to(groups), lineage_proof=lineage,
            ))

    cat_spends = [
        spend
        for spendables in cat_spendables.values()
        for spend in unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends
    ]

    bundle = aggregate_with_offer(offer, [*pool_spends, *native_spends, *cat_spends])
    # A chained pool produced one successor per crossing; only the LAST is the
    # pool's tip, and persisting an intermediate would wind the index back.
    final_by_launcher: dict[bytes32, V3Pool] = {p.launcher_id: p for p in successor_pools}
    final_pools = list(final_by_launcher.values())
    return FlowResult(
        bundle,
        final_pools,
        [(resolved_in[i], resolved_out[i]) for i in range(len(specs))],
        total_out,
    )
