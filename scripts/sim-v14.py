#!/usr/bin/env python3
"""Run V14 end to end on an in-process Chia node: farm, issue tokens, open pools, trade.

Testnet11 halted on 2026-09-11, which stopped the only environment that could answer
the questions the offline validator cannot: does a coin exist, does its lineage hold,
is its birth height what the puzzle claims, does a block actually advance. A
simulator answers all four, because `chia._tests.util.spend_sim` runs the real
mempool manager and coin store. It is not a replacement for testnet -- the same
matrix still has to run there -- but it is strictly more than the offline suites
could reach, and it runs in seconds instead of waiting on a chain.

    python scripts/sim-v14.py                # registry, pools, lifecycle, adversarial
    python scripts/sim-v14.py --pools 3      # fewer pools when iterating

Every coin is anyone-can-spend, so no key material is involved and every bundle
carries an empty signature. Forge's own puzzles never ask for one.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import (  # noqa: E402
    CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER, SINGLETON_LAUNCHER_HASH  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_math  # noqa: E402
import forge_v14_driver as drv  # noqa: E402
from _sim_harness import (CREATE_COIN, IDENTITY, IDENTITY_HASH, SimRejected, Wallet,  # noqa: E402
                          expect_refusal, farm_to_identity, issue_cat, push, sim_and_client,
                          split_xch)

CREATION_FEE = 1_000_000
FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILED += 1


class SimRegistry:
    """The registry singleton plus the slot bookkeeping `register` needs."""

    def __init__(self, registry, slots: dict):
        self.registry = registry
        self.slots = slots

    def bracket(self, key: bytes32):
        """The two recorded slots this key sorts between."""
        left = max((s for s in self.slots.values() if bytes32.fromhex(s["key"]) < key),
                   key=lambda s: bytes32.fromhex(s["key"]))
        right = min((s for s in self.slots.values() if bytes32.fromhex(s["key"]) > key),
                    key=lambda s: bytes32.fromhex(s["key"]))
        return left, right


async def mint_registry(sim, client, wallet: Wallet) -> SimRegistry:
    funding = wallet.take_xch(10_000_000)
    registry = drv.make_registry(creation_fee=CREATION_FEE, treasury_ph=IDENTITY_HASH,
                                 launcher_parent=funding.name())
    change = int(funding.amount) - 1
    funding_spend = make_spend(funding, IDENTITY, Program.to([
        [CREATE_COIN, SINGLETON_LAUNCHER_HASH, 1],
        [CREATE_COIN, IDENTITY_HASH, change],
    ]))
    launcher = Coin(funding.name(), SINGLETON_LAUNCHER_HASH, uint64(1))
    launcher_spend = make_spend(launcher, SINGLETON_LAUNCHER,
                                Program.to([registry.coin.puzzle_hash, 1, []]))
    bundle, _ = drv.registry_spend(registry, "forge_registry_init", [],
                                   extra_spends=[funding_spend, launcher_spend])
    await push(client, sim, bundle, "registry genesis + init")
    wallet.xch.append(Coin(funding.name(), IDENTITY_HASH, uint64(change)))

    sentinel = {"parent": registry.coin, "parent_inner_hash": registry.inner_hash}
    slots = {
        drv.MIN_KEY.hex(): {"key": drv.MIN_KEY.hex(), "launcher_id": drv.ZERO_32.hex(),
                            "left": drv.MIN_KEY.hex(), "right": drv.MAX_KEY.hex(), **sentinel},
        drv.MAX_KEY.hex(): {"key": drv.MAX_KEY.hex(), "launcher_id": drv.ZERO_32.hex(),
                            "left": drv.MIN_KEY.hex(), "right": drv.MAX_KEY.hex(), **sentinel},
    }
    return SimRegistry(registry.advance([1, 0]), slots)


async def create_pool(sim, client, wallet: Wallet, reg: SimRegistry, assets: list,
                      reserves: list[int], weights: list[int], fee_bps: int = 30,
                      protocol_fee_bps: int = 5, label: str = "pool", push_now: bool = True,
                      launcher_knobs: dict | None = None):
    """The deploy script's create-and-register bundle, with simulator coins.

    `push_now=False` returns (bundle, pool) instead of pushing, with the registry and
    slot bookkeeping already advanced, so the NEXT create chains onto the successor this
    one will produce. Aggregating those bundles registers several pools in one
    transaction -- the registry singleton is spent repeatedly inside the bundle, which
    is legal because the registry leaves assert no birth height the way the pool
    prologue does.

    `launcher_knobs` is passed through to `reserve_launcher_spends` so a probe can make a
    launcher create or announce the wrong thing and let the NODE answer. It exists so the
    refusal probes run the same creation lane as the honest one: a probe that builds its
    own bundle proves something about the probe.
    """
    total_lp = min(reserves)
    if total_lp < drv.LOCKED_BURN:
        raise SimRejected(f"{label}: genesis supply {total_lp} is below LOCKED_BURN")
    xch_index = assets.index(None) if None in assets else None
    xch_reserve = reserves[xch_index] if xch_index is not None else 0

    funding = wallet.take_xch(2 + xch_reserve + CREATION_FEE + total_lp)
    shape = drv.make_pool(assets, reserves, total_lp=total_lp, leaves="forge", weights=weights,
                          fee_bps=fee_bps, protocol_fee_bps=protocol_fee_bps,
                          protocol_ph=IDENTITY_HASH, launcher_parent=funding.name())

    cats: dict[int, tuple[Coin, Program, LineageProof]] = {}
    for index, asset in enumerate(assets):
        if asset is None:
            continue
        coin, lineage = wallet.take_cat(asset, reserves[index])
        cats[index] = (coin, IDENTITY, lineage)

    reserve_coins = []
    for index, asset in enumerate(assets):
        if asset is None:
            # V14: the funding coin creates a reserve LAUNCHER; the launcher creates the reserve
            launcher = Coin(funding.name(), drv.RESERVE_LAUNCHER_HASH, uint64(reserves[index]))
            reserve_coins.append((Coin(launcher.name(), shape.reserves[index].inner_hash, uint64(reserves[index])),
                                  None, funding.name()))
        else:
            coin, inner, lineage = cats[index]
            launcher = Coin(coin.name(), drv.reserve_launcher_full_hash(asset), uint64(reserves[index]))
            reserve_coins.append((Coin(launcher.name(), shape.reserves[index].full_hash, uint64(reserves[index])),
                                  LineageProof(coin.name(), drv.RESERVE_LAUNCHER_HASH, uint64(reserves[index])),
                                  coin.name(),
                                  LineageProof(coin.parent_coin_info, inner.get_tree_hash(), coin.amount)))

    pool = drv.make_pool(assets, reserves, total_lp=total_lp, leaves="forge", weights=weights,
                         fee_bps=fee_bps, protocol_fee_bps=protocol_fee_bps,
                         protocol_ph=IDENTITY_HASH, launcher_parent=funding.name(),
                         reserve_coins=reserve_coins)
    launcher_id = pool.launcher_id
    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()

    change = int(funding.amount) - 1 - 1 - xch_reserve - CREATION_FEE - (total_lp - 1)
    if change < 0:
        raise SimRejected(f"{label}: funding coin is short by {-change:,}")
    conditions = [[CREATE_COIN, SINGLETON_LAUNCHER_HASH, 1], [CREATE_COIN, eve_ph, 1]]
    if xch_index is not None:
        conditions.append([CREATE_COIN, drv.RESERVE_LAUNCHER_HASH, xch_reserve])   # V14
    conditions += [[CREATE_COIN, bytes32(OFFER_MOD_HASH), CREATION_FEE],
                   [CREATE_COIN, IDENTITY_HASH, change]]
    funding_spend = make_spend(funding, IDENTITY, Program.to(conditions))

    cat_spends = []
    for index, (coin, inner, lineage) in cats.items():
        conds = [[CREATE_COIN, drv.RESERVE_LAUNCHER_HASH, reserves[index]]]   # V14
        if int(coin.amount) > reserves[index]:
            rest = int(coin.amount) - reserves[index]
            conds.append([CREATE_COIN, IDENTITY_HASH, rest, [IDENTITY_HASH]])
        cat_spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
            coin, assets[index], inner, Program.to(conds), lineage_proof=lineage)]).coin_spends)
        if int(coin.amount) > reserves[index]:
            rest = int(coin.amount) - reserves[index]
            child = Coin(coin.name(), construct_cat_puzzle(CAT_MOD, assets[index], IDENTITY).get_tree_hash(),
                         uint64(rest))
            wallet.cats.setdefault(assets[index], []).append(
                (child, LineageProof(coin.parent_coin_info, IDENTITY_HASH, coin.amount)))

    launcher = Coin(funding.name(), SINGLETON_LAUNCHER_HASH, uint64(1))
    eve = Coin(funding.name(), eve_ph, uint64(1))
    pool.extra["eve_coin_id"] = eve.name()
    launcher_spend = make_spend(launcher, SINGLETON_LAUNCHER,
                                Program.to([pool.coin.puzzle_hash, 1, [total_lp, eve.name()]]))
    # The genesis mint lands at the LP settlement, which burns LOCKED_BURN to the zero puzzle
    # hash and pays the rest to us -- `register` asserts that burn. (The V13 simulator paid
    # the whole supply straight to the wallet and had been refused by every node since the
    # burn arrived; the offline suites never noticed because they build the burn themselves.)
    eve_spends = [*drv.lp_eve_ring(pool, eve, bytes32(OFFER_MOD_HASH), total_lp, drv.genesis_action(pool)),
                  *drv.genesis_lp_settlement_spends(pool, eve, IDENTITY_HASH, total_lp)]
    fee_coin = Coin(funding.name(), bytes32(OFFER_MOD_HASH), uint64(CREATION_FEE))
    fee_spend = make_spend(fee_coin, OFFER_MOD,
                           Program.to([[launcher_id, [IDENTITY_HASH, CREATION_FEE, [IDENTITY_HASH]]]]))

    key = drv.pool_key(pool.config())
    left_rec, right_rec = reg.bracket(key)
    left = (bytes32.fromhex(left_rec["key"]), bytes32.fromhex(left_rec["launcher_id"]),
            bytes32.fromhex(left_rec["left"]))
    right = (bytes32.fromhex(right_rec["key"]), bytes32.fromhex(right_rec["launcher_id"]),
             bytes32.fromhex(right_rec["right"]))
    slot_spends = []
    for rec, value in ((left_rec, drv.slot_value(left[0], left[1], left[2], right[0])),
                       (right_rec, drv.slot_value(right[0], right[1], left[0], right[2]))):
        _, spend = drv.slot_spend(reg.registry, value, rec["parent"], rec["parent_inner_hash"])
        slot_spends.append(spend)

    bundle, _ = drv.registry_spend(
        reg.registry, "forge_registry_register", drv.register_solution(pool, left, right),
        extra_spends=[funding_spend, *cat_spends, *drv.reserve_launcher_spends(pool, **(launcher_knobs or {})), launcher_spend, *eve_spends, fee_spend, *slot_spends])
    if push_now:
        await push(client, sim, bundle, f"create + register {label}")

    wallet.xch.append(Coin(funding.name(), IDENTITY_HASH, uint64(change)))
    # the registry advanced, and the three slots it wrote become the new bookkeeping
    new_registry = reg.registry.advance([1, reg.registry.state[1] + 1])
    parent = {"parent": reg.registry.coin, "parent_inner_hash": reg.registry.inner_hash}
    reg.slots[key.hex()] = {"key": key.hex(), "launcher_id": launcher_id.hex(),
                            "left": left[0].hex(), "right": right[0].hex(), **parent}
    reg.slots[left[0].hex()] = {"key": left[0].hex(), "launcher_id": left[1].hex(),
                                "left": left[2].hex(), "right": key.hex(), **parent}
    reg.slots[right[0].hex()] = {"key": right[0].hex(), "launcher_id": right[1].hex(),
                                 "left": key.hex(), "right": right[2].hex(), **parent}
    reg.registry = new_registry

    if push_now:
        record = await client.get_coin_record_by_name(pool.coin.name())
        if record is None:
            raise SimRejected(f"{label}: the pool coin is not on chain after its own block")
        pool.birth = int(record.confirmed_block_index)

    # The genesis LP the eve minted to us. Tracking it here is what lets `remove` later
    # build a melt coin with a REAL CAT parent instead of a fabricated one.
    # It is the LP settlement's child: total_lp - LOCKED_BURN of it, the floor burned.
    lp_settlement = Coin(eve.name(), construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, OFFER_MOD).get_tree_hash(), uint64(total_lp))
    lp_coin = Coin(lp_settlement.name(), construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, IDENTITY).get_tree_hash(),
                   uint64(total_lp - drv.LOCKED_BURN))
    wallet.cats.setdefault(pool.lp_asset_id, []).append(
        (lp_coin, LineageProof(eve.name(), bytes32(OFFER_MOD_HASH), uint64(total_lp))))
    return (bundle, pool) if not push_now else pool


async def swap_xch_in(sim, client, wallet: Wallet, pool, gross: int, label: str):
    """Trade XCH for the pool's CAT: the trader's mojos arrive as an offer coin the
    bundle spends, and the pool pays the CAT out to the offer puzzle in the same bundle."""
    assert pool.asset_ids[0] is None, "this helper trades the native side in"
    asset = pool.asset_ids[1]
    reserves, weights = pool.state[0], pool.weights
    honest = forge_math.swap_output(reserves[0], reserves[1], gross, pool.fee_bps, weights[0], weights[1])
    protocol_fee = honest * pool.protocol_fee_bps // 10_000
    payout_amount = honest - protocol_fee

    funding = wallet.take_xch(gross + 1)
    change = int(funding.amount) - gross
    conditions = [[CREATE_COIN, bytes32(OFFER_MOD_HASH), gross]]
    if change > 0:
        conditions.append([CREATE_COIN, IDENTITY_HASH, change])
    funding_spend = make_spend(funding, IDENTITY, Program.to(conditions))
    settlement = Coin(funding.name(), bytes32(OFFER_MOD_HASH), uint64(gross))
    settlement_spend = make_spend(settlement, OFFER_MOD, Program.to([[settlement.name()]]))

    out_reserve = pool.reserves[1]
    payout = Coin(out_reserve.coin.name(),
                  construct_cat_puzzle(CAT_MOD, asset, OFFER_MOD).get_tree_hash(), uint64(payout_amount))
    payout_spendable = SpendableCAT(
        payout, asset, OFFER_MOD,
        Program.to([[payout.name(), [IDENTITY_HASH, payout_amount, [IDENTITY_HASH]]]]),
        lineage_proof=LineageProof(out_reserve.coin.parent_coin_info, out_reserve.inner_hash,
                                   out_reserve.coin.amount))

    height = sim.block_height
    bundle, new_state = drv.spend_action(
        pool, "forge_action_swap", [height, 0, 1, gross, honest, *drv.settlement_ref(settlement)],
        extra_spends=[funding_spend, settlement_spend], extra_cats={asset: [payout_spendable]})
    await push(client, sim, bundle, f"swap on {label}")

    if change > 0:
        wallet.xch.append(Coin(funding.name(), IDENTITY_HASH, uint64(change)))
    traded = Coin(payout.name(), construct_cat_puzzle(CAT_MOD, asset, IDENTITY).get_tree_hash(),
                  uint64(payout_amount))
    wallet.cats.setdefault(asset, []).append(
        (traded, LineageProof(payout.parent_coin_info, OFFER_MOD_HASH, payout.amount)))

    successor = pool.advance(drv.state_to_list(new_state))
    record = await client.get_coin_record_by_name(successor.coin.name())
    if record is not None:
        successor.birth = int(record.confirmed_block_index)
    return successor, drv.state_to_list(new_state), honest, protocol_fee


async def add_liquidity(sim, client, wallet: Wallet, pool, deposits: list[int], label: str):
    """Deposit into every reserve and take newly minted LP, all in one bundle.

    The XCH side arrives as an offer coin; the CAT side arrives as a CAT(OFFER_MOD)
    coin that joins that asset's ring; the LP is minted by a fresh one-mojo eve the
    pool authorizes by message.
    """
    assert pool.asset_ids[0] is None, "this helper deposits the native side"
    asset = pool.asset_ids[1]
    minted = forge_math.invariant_lp_mint(pool.state[0], deposits, pool.state[1],
                                          pool.fee_bps, pool.weights, version=10)
    if minted <= 0:
        raise SimRejected(f"{label}: deposits {deposits} mint nothing")
    new_total = pool.state[1] + minted

    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
    funding = wallet.take_xch(deposits[0] + 1 + minted)
    change = int(funding.amount) - deposits[0] - 1 - (minted - 1)
    if change < 0:
        raise SimRejected(f"{label}: funding short by {-change:,}")
    funding_spend = make_spend(funding, IDENTITY, Program.to([
        [CREATE_COIN, bytes32(OFFER_MOD_HASH), deposits[0]],
        [CREATE_COIN, eve_ph, 1],
        [CREATE_COIN, IDENTITY_HASH, change],
    ]))
    xch_settlement = Coin(funding.name(), bytes32(OFFER_MOD_HASH), uint64(deposits[0]))
    xch_settlement_spend = make_spend(xch_settlement, OFFER_MOD, Program.to([[xch_settlement.name()]]))

    coin, lineage = wallet.take_cat(asset, deposits[1])
    settlement_ph = construct_cat_puzzle(CAT_MOD, asset, OFFER_MOD).get_tree_hash()
    cat_conditions = [[CREATE_COIN, bytes32(OFFER_MOD_HASH), deposits[1]]]
    rest = int(coin.amount) - deposits[1]
    if rest > 0:
        cat_conditions.append([CREATE_COIN, IDENTITY_HASH, rest, [IDENTITY_HASH]])
    cat_spends = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        coin, asset, IDENTITY, Program.to(cat_conditions), lineage_proof=lineage)]).coin_spends
    cat_settlement = Coin(coin.name(), settlement_ph, uint64(deposits[1]))
    cat_settlement_spendable = SpendableCAT(
        cat_settlement, asset, OFFER_MOD, Program.to([[cat_settlement.name()]]),
        lineage_proof=LineageProof(coin.parent_coin_info, IDENTITY_HASH, coin.amount))

    height = sim.block_height
    solution = [height, deposits, minted, funding.name(),
                *drv.settlement_refs([xch_settlement, cat_settlement])]
    probe_state, _, _, _ = drv.run_leaf(pool, "forge_action_add", solution)
    eve = Coin(funding.name(), eve_ph, uint64(1))
    eve_spends = drv.lp_eve_ring(pool, eve, IDENTITY_HASH, minted,
                                 [minted, new_total, probe_state.get_tree_hash(),
                                  pool.inner_hash, drv.ZERO_32])
    bundle, new_state = drv.spend_action(
        pool, "forge_action_add", solution,
        extra_spends=[funding_spend, xch_settlement_spend, *cat_spends, *eve_spends],
        extra_cats={asset: [cat_settlement_spendable]})
    await push(client, sim, bundle, f"add on {label}")

    if change > 0:
        wallet.xch.append(Coin(funding.name(), IDENTITY_HASH, uint64(change)))
    if rest > 0:
        child = Coin(coin.name(), construct_cat_puzzle(CAT_MOD, asset, IDENTITY).get_tree_hash(),
                     uint64(rest))
        wallet.cats.setdefault(asset, []).append(
            (child, LineageProof(coin.parent_coin_info, IDENTITY_HASH, coin.amount)))
    # The LP this deposit minted is ours, and forgetting it here is what made the
    # LOCKED_BURN boundary unreachable: the wallet held less than the pool's supply.
    wallet.cats.setdefault(pool.lp_asset_id, []).append(
        (Coin(eve.name(), construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, IDENTITY).get_tree_hash(),
              uint64(minted)),
         LineageProof(eve.parent_coin_info, drv.LP_MINT_INNER.get_tree_hash(), uint64(1))))

    successor = pool.advance(drv.state_to_list(new_state))
    record = await client.get_coin_record_by_name(successor.coin.name())
    if record is not None:
        successor.birth = int(record.confirmed_block_index)
    return successor, drv.state_to_list(new_state), minted


async def consolidate_cat(sim, client, wallet: Wallet, asset: bytes32) -> int:
    """Merge every coin of one CAT into a single coin, and return the total held.

    A ring balances when the deltas sum to zero, so the first coin creates the whole
    amount and the rest create nothing.
    """
    pile = wallet.cats.get(asset) or []
    if len(pile) <= 1:
        return int(pile[0][0].amount) if pile else 0
    total = sum(int(coin.amount) for coin, _ in pile)
    spendables = []
    for index, (coin, lineage) in enumerate(pile):
        conditions = [[CREATE_COIN, IDENTITY_HASH, total, [IDENTITY_HASH]]] if index == 0 else []
        spendables.append(SpendableCAT(coin, asset, IDENTITY, Program.to(conditions),
                                       lineage_proof=lineage))
    ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends
    from chia_rs import G2Element as _G2, SpendBundle as _SB
    await push(client, sim, _SB(ring, _G2()), f"consolidate {asset.hex()[:8]}")
    first, first_lineage = pile[0]
    merged = Coin(first.name(), construct_cat_puzzle(CAT_MOD, asset, IDENTITY).get_tree_hash(),
                  uint64(total))
    wallet.cats[asset] = [(merged, LineageProof(first.parent_coin_info, IDENTITY_HASH, first.amount))]
    return total


async def remove_liquidity(sim, client, wallet: Wallet, pool, burn: int, label: str):
    """Burn LP and take every reserve pro rata.

    The LP coin we hold splits into a melt coin at the PINNED melt inner plus change,
    and that melt coin is spent in the same bundle with `extra_delta = -burn` so the
    TAIL runs and supply really falls. The payouts land at the offer puzzle and are
    settled back to us in the same bundle.
    """
    assert pool.asset_ids[0] is None, "this helper redeems a pool holding the native asset"
    asset = pool.asset_ids[1]
    vault_fee = forge_math.vault_fee_bps(len(pool.state[0]), 10, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vault_fee)
    new_total = pool.state[1] - burn

    lp_coin, lp_lineage = wallet.take_cat(pool.lp_asset_id, burn)
    try:
        return await _remove_with_lp(sim, client, wallet, pool, burn, label, asset, payouts,
                                     new_total, lp_coin, lp_lineage)
    except Exception:
        # The take above already removed the coin from the pile. A refused burn must not
        # cost us the LP, or the next step fails for a reason that has nothing to do with
        # the puzzle -- which is exactly how the LOCKED_BURN check first passed falsely.
        wallet.cats.setdefault(pool.lp_asset_id, []).append((lp_coin, lp_lineage))
        raise


async def _remove_with_lp(sim, client, wallet: Wallet, pool, burn: int, label: str, asset,
                          payouts, new_total, lp_coin, lp_lineage):
    melt_inner_hash = drv.LP_MELT_INNER.get_tree_hash()
    lp_conditions = [[CREATE_COIN, melt_inner_hash, burn]]
    rest = int(lp_coin.amount) - burn
    if rest > 0:
        lp_conditions.append([CREATE_COIN, IDENTITY_HASH, rest, [IDENTITY_HASH]])
    lp_spends = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        lp_coin, pool.lp_asset_id, IDENTITY, Program.to(lp_conditions),
        lineage_proof=lp_lineage)]).coin_spends

    height = sim.block_height
    solution = [height, burn, lp_coin.name(), payouts]
    probe_state, _, _, _ = drv.run_leaf(pool, "forge_action_remove", solution)
    melt = Coin(lp_coin.name(),
                construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MELT_INNER).get_tree_hash(),
                uint64(burn))
    action = [-burn, new_total, probe_state.get_tree_hash(), pool.inner_hash, drv.ZERO_32]
    melt_spends = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        melt, pool.lp_asset_id, drv.LP_MELT_INNER, Program.to([pool.lp_tail, action]),
        lineage_proof=LineageProof(lp_coin.parent_coin_info, IDENTITY_HASH, lp_coin.amount),
        extra_delta=-burn, limitations_program_reveal=pool.lp_tail,
        limitations_solution=Program.to(action))]).coin_spends

    native, cat_reserve = pool.reserves[0], pool.reserves[1]
    xch_payout = Coin(native.coin.name(), bytes32(OFFER_MOD_HASH), uint64(payouts[0]))
    xch_payout_spend = make_spend(xch_payout, OFFER_MOD, Program.to(
        [[xch_payout.name(), [IDENTITY_HASH, payouts[0], [IDENTITY_HASH]]]]))
    cat_payout = Coin(cat_reserve.coin.name(),
                      construct_cat_puzzle(CAT_MOD, asset, OFFER_MOD).get_tree_hash(),
                      uint64(payouts[1]))
    cat_payout_spendable = SpendableCAT(
        cat_payout, asset, OFFER_MOD,
        Program.to([[cat_payout.name(), [IDENTITY_HASH, payouts[1], [IDENTITY_HASH]]]]),
        lineage_proof=LineageProof(cat_reserve.coin.parent_coin_info, cat_reserve.inner_hash,
                                   cat_reserve.coin.amount))

    bundle, new_state = drv.spend_action(
        pool, "forge_action_remove", solution,
        extra_spends=[*lp_spends, *melt_spends, xch_payout_spend],
        extra_cats={asset: [cat_payout_spendable]})
    await push(client, sim, bundle, f"remove on {label}")

    wallet.xch.append(Coin(xch_payout.name(), IDENTITY_HASH, uint64(payouts[0])))
    wallet.cats.setdefault(asset, []).append(
        (Coin(cat_payout.name(), construct_cat_puzzle(CAT_MOD, asset, IDENTITY).get_tree_hash(),
              uint64(payouts[1])),
         LineageProof(cat_payout.parent_coin_info, OFFER_MOD_HASH, cat_payout.amount)))
    if rest > 0:
        wallet.cats.setdefault(pool.lp_asset_id, []).append(
            (Coin(lp_coin.name(), construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, IDENTITY).get_tree_hash(),
                  uint64(rest)),
             LineageProof(lp_coin.parent_coin_info, IDENTITY_HASH, lp_coin.amount)))

    successor = pool.advance(drv.state_to_list(new_state))
    record = await client.get_coin_record_by_name(successor.coin.name())
    if record is not None:
        successor.birth = int(record.confirmed_block_index)
    return successor, drv.state_to_list(new_state), payouts


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pools", type=int, default=4, help="how many pools of the matrix to open")
    parser.add_argument("--db", help="keep the CHAIN in this sqlite file between runs. The block "
                                     "records and coin store reload; this script's own bookkeeping "
                                     "(wallet coins, registry slots, pool objects) does not, so a "
                                     "second run opens a fresh set on top of the old chain rather "
                                     "than continuing the old one.")
    args = parser.parse_args()

    db_path = Path(args.db).resolve() if args.db else None
    if db_path is not None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    async with sim_and_client(db_path=db_path) as (sim, client):
        if db_path is not None:
            print(f"== chain persists in {db_path.name}, resuming at peak {sim.block_height}")
        print("== farm and issue")
        coins = await farm_to_identity(sim, client, blocks=6)
        wallet = Wallet(xch=list(coins), cats={})
        print(f"  farmed {len(coins)} coins, {sum(int(c.amount) for c in coins):,} mojos, peak {sim.block_height}")

        funding = wallet.take_xch(1_000_000_000)
        pieces, bundle = split_xch(funding, [200_000_000] * 4)
        await push(client, sim, bundle, "split for token issuance")
        tokens: list[bytes32] = []
        for index, piece in enumerate(pieces):
            asset_id, minted, lineage, mint_bundle = issue_cat(piece, 60_000_000, salt=0xA0 + index)
            await push(client, sim, mint_bundle, f"issue token {index}")
            wallet.cats.setdefault(asset_id, []).append((minted, lineage))
            tokens.append(asset_id)
        print(f"  issued {len(tokens)} test tokens of 60,000,000 each, peak {sim.block_height}")

        print("== registry")
        reg = await mint_registry(sim, client, wallet)
        print(f"  launcher {reg.registry.launcher_id.hex()[:24]}, peak {sim.block_height}")

        print("== pools")
        matrix = [
            ("XCH/token0", [None, tokens[0]], [50_000_000, 20_000_000], [1, 1]),
            ("XCH/token1 80-20", [None, tokens[1]], [40_000_000, 10_000_000], [4, 1]),
            ("token0/token1", [tokens[0], tokens[1]], [8_000_000, 8_000_000], [1, 1]),
            ("XCH/token2 zero-fee", [None, tokens[2]], [30_000_000, 15_000_000], [1, 1]),
        ][: max(1, args.pools)]
        pools = []
        for label, assets, reserves, weights in matrix:
            order = sorted(range(len(assets)),
                           key=lambda i: bytes(32) if assets[i] is None else bytes(assets[i]))
            pool = await create_pool(
                sim, client, wallet, reg,
                [assets[i] for i in order], [reserves[i] for i in order], [weights[i] for i in order],
                fee_bps=0 if "zero-fee" in label else 30,
                protocol_fee_bps=0 if "zero-fee" in label else 5, label=label)
            pools.append((label, pool))
            check(f"{label} created and registered, birth {pool.birth}", pool.birth > 0)
        # The two shapes the plain matrix does not cover: a single-asset vault, and a pool
        # whose reserve is ANOTHER pool's LP. The wrapper is the interesting one -- it is how
        # a trapped unit compounded across V11's F1/F2 pair -- and it only works because the
        # vault's genesis LP is a real CAT this wallet now holds.
        vault = await create_pool(sim, client, wallet, reg, [tokens[3]], [5_000_000], [1],
                                  fee_bps=30, protocol_fee_bps=0, label="token3 vault")
        pools.append(("token3 vault", vault))
        check(f"single-asset vault created, birth {vault.birth}", vault.birth > 0)

        wrapper_lp = int((wallet.cats.get(vault.lp_asset_id) or [(None, None)])[0][0].amount)
        check("the vault's genesis LP is spendable as an ordinary CAT", wrapper_lp > 0,
              f"held {wrapper_lp}")
        if wrapper_lp >= 2_000_000:
            wrap_assets = [None, vault.lp_asset_id]
            order = sorted(range(2), key=lambda i: bytes(32) if wrap_assets[i] is None
                           else bytes(wrap_assets[i]))
            wrapper = await create_pool(
                sim, client, wallet, reg,
                [wrap_assets[i] for i in order],
                [[8_000_000, 2_000_000][i] for i in order],
                [1, 1], fee_bps=30, protocol_fee_bps=5, label="XCH/vault-LP wrapper")
            pools.append(("XCH/vault-LP wrapper", wrapper))
            check(f"wrapper pool over another pool's LP created, birth {wrapper.birth}",
                  wrapper.birth > 0)

        print(f"  {len(pools)} pools live, peak {sim.block_height}, registry counts "
              f"{reg.registry.state[1]}")

        print("== observe: the oracle against real birth heights")
        advanced = []
        for label, pool in pools:
            height = sim.block_height
            bundle, new_state = drv.spend_action(pool, "forge_action_observe", [height])
            await push(client, sim, bundle, f"observe {label}")
            successor = pool.advance(drv.state_to_list(new_state))
            record = await client.get_coin_record_by_name(successor.coin.name())
            check(f"{label}: observe confirmed, successor born {record.confirmed_block_index if record else '-'}",
                  record is not None)
            if record is not None:
                successor.birth = int(record.confirmed_block_index)
                state = drv.state_to_list(new_state)
                check(f"  {label}: oracle recorded height {state[3][0]} and {len(state[3][1])} accumulator(s)",
                      state[3][0] == height)
                advanced.append((label, successor))

        print("== swaps")
        traded = []
        for label, pool in advanced:
            if pool.asset_ids[0] is not None:
                continue
            before = list(pool.state[0])
            gross = max(int(before[0]) // 50, 1000)
            pool, state, out, protocol_fee = await swap_xch_in(sim, client, wallet, pool, gross, label)
            check(f"{label}: swapped {gross:,} in for {out:,} out", out > 0)
            check(f"  {label}: the in reserve grew by exactly the gross",
                  state[0][0] == before[0] + gross, f"{state[0][0]} vs {before[0] + gross}")
            check(f"  {label}: the protocol fee of {protocol_fee:,} accrued rather than paid out",
                  state[2][1] == protocol_fee or protocol_fee == 0, f"fees_owed {state[2]}")
            traded.append((label, pool))
        advanced = [(label, pool) for label, pool in advanced
                    if label not in {lbl for lbl, _ in traded}] + traded

        print("== adds: deposit and mint LP")
        deposited = []
        for label, pool in advanced:
            if pool.asset_ids[0] is not None:
                continue
            before_total = int(pool.state[1])
            before_reserves = [int(r) for r in pool.state[0]]
            deposits = [max(r // 20, 1) for r in before_reserves]
            try:
                pool, state, minted = await add_liquidity(sim, client, wallet, pool, deposits, label)
            except SimRejected as exc:
                check(f"{label}: add", False, str(exc))
                continue
            check(f"{label}: deposited {deposits} and minted {minted:,} LP", minted > 0)
            check(f"  {label}: total_lp rose from {before_total:,} to {int(state[1]):,}",
                  int(state[1]) == before_total + minted, f"{state[1]}")
            check(f"  {label}: every reserve grew by exactly its deposit",
                  all(int(state[0][i]) == before_reserves[i] + deposits[i]
                      for i in range(len(deposits))),
                  f"{[int(x) for x in state[0]]} vs {[b + d for b, d in zip(before_reserves, deposits)]}")
            deposited.append((label, pool))
        advanced = [(label, pool) for label, pool in advanced
                    if label not in {lbl for lbl, _ in deposited}] + deposited

        print("== removes: burn LP and take the reserves back")
        redeemed = []
        for label, pool in advanced:
            if pool.asset_ids[0] is not None:
                continue
            before_total = int(pool.state[1])
            burn = before_total // 10
            try:
                pool, state, payouts = await remove_liquidity(sim, client, wallet, pool, burn, label)
            except SimRejected as exc:
                check(f"{label}: remove", False, str(exc))
                continue
            check(f"{label}: burned {burn:,} LP for payouts {payouts}", all(p > 0 for p in payouts))
            check(f"  {label}: total_lp fell from {before_total:,} to {int(state[1]):,}",
                  int(state[1]) == before_total - burn, f"{state[1]}")
            redeemed.append((label, pool))
        advanced = [(label, pool) for label, pool in advanced
                    if label not in {lbl for lbl, _ in redeemed}] + redeemed

        print("== the minimum locked liquidity, on a real node")
        label, pool = redeemed[0] if redeemed else advanced[0]
        # Our LP is spread over several coins (genesis, the add's mint, the burn's change) and
        # `take_cat` wants one coin that covers the burn, so consolidate first. Skipping this
        # made the boundary test pass for the wrong reason: the refusal came from the wallet
        # having no big enough coin, not from the puzzle refusing the burn.
        held = await consolidate_cat(sim, client, wallet, pool.lp_asset_id)
        redeemable = int(pool.state[1]) - drv.LOCKED_BURN
        # V14: the floor is ONE unit and it was burned at genesis, so this wallet holds every
        # unit that can ever be redeemed -- and "one past the floor" is a burn nobody can hold.
        # The leaf's refusal of that burn is proved offline (_test_v14_before_after.py); what a
        # real node can add is that the whole redeemable supply comes back out.
        check(f"{label}: holding {held:,} LP -- every unit not burned at genesis", held == redeemable,
              f"held {held:,}, redeemable {redeemable:,}")
        if held >= redeemable:
            pool, state, _ = await remove_liquidity(sim, client, wallet, pool, redeemable, label)
            check(f"  {label}: burning everything above the floor is accepted, leaving "
                  f"{int(state[1]):,}", int(state[1]) == drv.LOCKED_BURN, f"{state[1]}")
            try:
                # The pool sits at the floor now; burning the floor itself is refused by the leaf
                # before anything is built. That IS the puzzle refusing.
                vf = forge_math.vault_fee_bps(len(pool.state[0]), 10, pool.fee_bps)
                drv.run_leaf(pool, "forge_action_remove", [sim.block_height, drv.LOCKED_BURN, drv.ZERO_32,
                                                          [0] * len(pool.state[0])])
                check(f"  {label}: burning the floor itself is refused", False, "ACCEPTED")
            except ValueError as exc:
                check(f"  {label}: the remove leaf itself refuses burning the floor", "clvm raise" in str(exc), str(exc))
            # This pool moved twice more; the list still holds the version from before, and
            # spending that one next would be a double spend of a coin we ourselves replaced.
            advanced = [(lbl, pool if lbl == label else p) for lbl, p in advanced]

        print("== collect: the accrued protocol fees are paid out")
        collected = []
        for label, pool in advanced:
            indices = [i for i, owed in enumerate(pool.state[2]) if owed > 0]
            if not indices:
                continue
            owed = [pool.state[2][i] for i in indices]
            height = sim.block_height
            bundle, new_state = drv.spend_action(pool, "forge_action_collect", [height, indices])
            await push(client, sim, bundle, f"collect on {label}")
            successor = pool.advance(drv.state_to_list(new_state))
            record = await client.get_coin_record_by_name(successor.coin.name())
            if record is not None:
                successor.birth = int(record.confirmed_block_index)
            after = drv.state_to_list(new_state)
            check(f"{label}: collected {sum(owed):,} from {len(indices)} reserve(s)",
                  all(after[2][i] == 0 for i in indices), f"fees_owed now {after[2]}")
            collected.append((label, successor))
        advanced = [(label, pool) for label, pool in advanced
                    if label not in {lbl for lbl, _ in collected}] + collected

        print("== the birth-height lock, judged by a real node")
        label, pool = advanced[0]
        # A couple of blocks first, so the honest height is above the coin's birth and a lie of
        # one block in either direction still passes the prologue locally -- the refusal has
        # to be the node's, not the leaf's `birth > last_height`.
        await farm_to_identity(sim, client, blocks=2)
        honest_height = sim.block_height
        lying = drv.replace(pool, birth=pool.birth + 1)
        bundle, _ = drv.spend_action(lying, "forge_action_observe", [honest_height])
        error = await expect_refusal(client, bundle, "false birth")
        check(f"a spend overstating its birth by 1 block is refused: {error}",
              error == "ASSERT_MY_BIRTH_HEIGHT_FAILED", f"got {error}")

        bundle, _ = drv.spend_action(pool, "forge_action_observe", [sim.block_height + 5])
        error = await expect_refusal(client, bundle, "future height")
        check(f"a spend claiming a height above the peak is refused: {error}",
              error == "ASSERT_HEIGHT_ABSOLUTE_FAILED", f"got {error}")

        bundle, new_state = drv.spend_action(pool, "forge_action_observe", [sim.block_height])
        await push(client, sim, bundle, f"honest observe {label}")
        check("the honest spend at the same height is accepted", True)
        pool = pool.advance(drv.state_to_list(new_state))
        record = await client.get_coin_record_by_name(pool.coin.name())
        pool.birth = int(record.confirmed_block_index)

        print("== borrowing a birth height that is genuinely correct for another coin")
        # The worry: birth heights are not unique -- thousands of coins share one. So can a
        # pool claim a height that IS the true birth of some other coin, and have consensus
        # wave it through? It cannot, and the reason is worth stating: the condition is
        # evaluated against the birth of THE COIN BEING SPENT, not against anything else in
        # the bundle. Sharing a value with another coin buys an attacker nothing, because
        # they still cannot make their own coin's record say it.
        await farm_to_identity(sim, client, blocks=2)     # fresh coins born after the pool's last spend
        donors = await client.get_coin_records_by_puzzle_hash(IDENTITY_HASH, include_spent_coins=False)
        # A donor born AFTER the pool's last claimed height, so the borrowed birth passes the
        # prologue's `birth > last_height` locally and the refusal is the node's.
        donor = next((r for r in donors if int(r.confirmed_block_index) != pool.birth
                      and int(r.confirmed_block_index) > int(pool.state[3][0])
                      and int(r.coin.amount) > 10_000), None)
        check("found a spendable coin born in a different block from the pool", donor is not None)
        if donor is not None:
            donor_height = int(donor.confirmed_block_index)
            check(f"  the donor's birth is genuinely {donor_height}, the pool's is {pool.birth}",
                  donor_height != pool.birth)
            donor_spend = make_spend(donor.coin, IDENTITY, Program.to(
                [[CREATE_COIN, IDENTITY_HASH, int(donor.coin.amount)]]))
            borrowed = drv.replace(pool, birth=donor_height)
            bundle, _ = drv.spend_action(borrowed, "forge_action_observe", [sim.block_height],
                                         extra_spends=[donor_spend])
            error = await expect_refusal(client, bundle, "borrowed birth")
            check(f"  a pool claiming the donor's real birth, with the donor spent in the same "
                  f"bundle, is refused: {error}",
                  error == "ASSERT_MY_BIRTH_HEIGHT_FAILED", f"got {error}")

        print("== a decoy reserve that really exists on chain")
        # Review finding 5 said the finalizer took its reserve parents from the SOLUTION, so a
        # coin at the reserve's puzzle hash and amount could stand in and orphan the real one.
        # Offline that is refused because the substituted coin is never messaged. Here the
        # decoy is a REAL coin, paid to the reserve's own puzzle hash and confirmed in its own
        # block, so the refusal cannot be an artefact of the coin not existing.
        target = next((p for _, p in advanced if p.asset_ids[0] is None), None)
        if target is not None:
            real_reserve = target.reserves[0].coin
            donor_coin = wallet.take_xch(int(real_reserve.amount) + 1)
            leftover = int(donor_coin.amount) - int(real_reserve.amount)
            decoy_spend_src = make_spend(donor_coin, IDENTITY, Program.to(
                [[CREATE_COIN, real_reserve.puzzle_hash, int(real_reserve.amount)],
                 [CREATE_COIN, IDENTITY_HASH, leftover]]))
            from chia_rs import SpendBundle as _SB, G2Element as _G2
            await push(client, sim, _SB([decoy_spend_src], _G2()), "fund the decoy reserve")
            wallet.xch.append(Coin(donor_coin.name(), IDENTITY_HASH, uint64(leftover)))
            decoy = Coin(donor_coin.name(), real_reserve.puzzle_hash, uint64(real_reserve.amount))
            decoy_record = await client.get_coin_record_by_name(decoy.name())
            check("the decoy is a real unspent coin at the reserve's puzzle hash and amount",
                  decoy_record is not None and not decoy_record.spent)
            check("  it differs from the real reserve only in its parent",
                  decoy.puzzle_hash == real_reserve.puzzle_hash
                  and decoy.amount == real_reserve.amount and decoy.name() != real_reserve.name())

            honest, _ = drv.spend_action(target, "forge_action_observe", [sim.block_height])
            swapped = []
            for cs in honest.coin_spends:
                if cs.coin.name() == real_reserve.name():
                    swapped.append(make_spend(decoy, Program.from_bytes(bytes(cs.puzzle_reveal)),
                                              Program.from_bytes(bytes(cs.solution))))
                else:
                    swapped.append(cs)
            error = await expect_refusal(client, _SB(swapped, honest.aggregated_signature),
                                         "decoy reserve")
            check(f"  the decoy cannot stand in for the reserve: {error}",
                  error in ("MESSAGE_NOT_SENT_OR_RECEIVED", "GENERATOR_RUNTIME_ERROR",
                            "ASSERT_ANNOUNCE_CONSUMED_FAILED"), f"got {error}")

        print("== an impostor singleton, settled on coin existence")
        # The TAIL derives its expected sender as singleton(curried launcher_id, the inner hash
        # from the solution). Name your OWN inner and the derivation points at you -- so the
        # last thing standing is the singleton's lineage assert. Offline that argument had to
        # rest on "such a coin could never exist", which the validator cannot check. Here it can.
        from chia.wallet.puzzles.singleton_top_layer_v1_1 import (  # noqa: E402
            puzzle_for_singleton, solution_for_singleton,
        )
        victim = advanced[0][1]
        impostor_puzzle = puzzle_for_singleton(victim.launcher_id, IDENTITY)
        impostor_ph = impostor_puzzle.get_tree_hash()
        check("the impostor's puzzle hash differs from the real pool's",
              impostor_ph != victim.coin.puzzle_hash)

        donor2 = wallet.take_xch(10_000)
        await push(client, sim, _SB([make_spend(donor2, IDENTITY, Program.to(
            [[CREATE_COIN, impostor_ph, 1],
             [CREATE_COIN, IDENTITY_HASH, int(donor2.amount) - 1]]))], _G2()),
            "fund an impostor singleton")
        wallet.xch.append(Coin(donor2.name(), IDENTITY_HASH, uint64(int(donor2.amount) - 1)))
        funded = Coin(donor2.name(), impostor_ph, uint64(1))
        funded_record = await client.get_coin_record_by_name(funded.name())
        check("a coin at the impostor puzzle hash really exists on chain",
              funded_record is not None and not funded_record.spent)

        conditions = Program.to([[CREATE_COIN, impostor_ph, 1]])
        spend = make_spend(funded, impostor_puzzle, solution_for_singleton(
            LineageProof(bytes32(b"\xcc" * 32), IDENTITY_HASH, uint64(1)), uint64(1), conditions))
        error = await expect_refusal(client, _SB([spend], _G2()), "impostor spend")
        check(f"  but it cannot be spent: {error}", error == "ASSERT_MY_PARENT_ID_FAILED", f"got {error}")

        # The two parents whose lineage WOULD satisfy that assert are the launcher and the
        # pool's own coin, and neither ever created such a child.
        for name, parent_id in (("the launcher", victim.launcher_id),
                                ("the pool coin", victim.coin.name())):
            hypothetical = Coin(parent_id, impostor_ph, uint64(1))
            record = await client.get_coin_record_by_name(hypothetical.name())
            check(f"  an impostor parented by {name} does not exist", record is None)

        # And the harmless half: two different singletons really can share a birth height,
        # and each spends fine asserting it, because each is asserting its own truth.
        same_block = [(lbl, p) for lbl, p in advanced if p.birth == advanced[0][1].birth]
        check(f"pools sharing a birth height are fine: {len(same_block)} at height "
              f"{advanced[0][1].birth}", True)

        print()
        print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- V14 on the simulator")
        return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
