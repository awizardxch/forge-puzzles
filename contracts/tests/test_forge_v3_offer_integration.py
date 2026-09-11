from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest
from chia._tests.util.spend_sim import sim_and_client
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.program import INFINITE_COST
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD,
    LineageProof,
    SpendableCAT,
    construct_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.conditions import CreateCoin
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.trading.offer import OFFER_MOD, Offer
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia.util.hash import std_hash
from chia_rs import AugSchemeMPL, G2Element, PrivateKey, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

CONTRACTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTRACTS_DIR))

from forge_v3_offer import (
    LaunchConfig,
    LaunchIntent,
    MODE_ADD,
    MODE_REMOVE,
    MODE_SWAP,
    build_create_v3,
    build_transition_v3,
    compiled_program,
    finalize_create_offer,
    find_offer_settlements,
    invariant_lp_mint_v3,
    prepare_create_v3,
    solve_native_add_deposit,
    singleton_struct,
    ZERO_32,
)
from forge_v3_stdin import build as stdin_build
from forge_v3_stdin import _pool as pool_from_snapshot
from forge_create_pool_v5 import deploy as deploy_v5
ACS = Program.to(1)
ACS_PH = ACS.get_tree_hash()


def test_v5_native_add_split_matches_mixed_reserve_invariant() -> None:
    native_deposit, lp_delta = solve_native_add_deposit(
        total_xch=2_000,
        native_reserve=20_000,
        other_reserve=20_000,
        other_deposit=2_000,
        total_lp=20_000,
    )
    assert native_deposit == 674
    assert lp_delta == 1_326
    assert native_deposit + lp_delta == 2_000
    assert invariant_lp_mint_v3((20_000, 20_000), (20_674, 22_000), 20_000) == lp_delta


def quoted_tail(marker: bytes) -> Program:
    return Program.to((1, [])).curry(marker)


async def _push_and_farm(sim, client, bundle: WalletSpendBundle) -> None:
    created: set[bytes32] = set()
    asserted: dict[bytes32, list[tuple[int, str]]] = {}
    for index, coin_spend in enumerate(bundle.coin_spends):
        try:
            Program.from_bytes(bytes(coin_spend.puzzle_reveal)).run_with_cost(
                INFINITE_COST,
                Program.from_bytes(bytes(coin_spend.solution)),
            )
        except Exception as error:
            pytest.fail(f"coin spend {index} ({coin_spend.coin.name().hex()}, puzzle={coin_spend.coin.puzzle_hash.hex()}) failed CLVM: {error}")
        conditions = conditions_dict_for_solution(
            coin_spend.puzzle_reveal,
            coin_spend.solution,
            11_000_000_000,
        )
        for condition in conditions.get(ConditionOpcode.CREATE_COIN_ANNOUNCEMENT, []):
            created.add(std_hash(coin_spend.coin.name() + condition.vars[0]))
        for condition in conditions.get(ConditionOpcode.CREATE_PUZZLE_ANNOUNCEMENT, []):
            created.add(std_hash(coin_spend.coin.puzzle_hash + condition.vars[0]))
        for opcode in (
            ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT,
            ConditionOpcode.ASSERT_PUZZLE_ANNOUNCEMENT,
        ):
            for condition in conditions.get(opcode, []):
                announcement_id = bytes32(condition.vars[0])
                asserted.setdefault(announcement_id, []).append((index, opcode.name))
    missing = set(asserted) - created
    assert not missing, f"missing announcement ids: {[(item.hex(), asserted[item]) for item in missing]}"
    status, error = await client.push_tx(bundle)
    if status != MempoolInclusionStatus.SUCCESS:
        addition_ids = {coin.name() for coin in bundle.additions()}
        unresolved = []
        for coin in bundle.removals():
            if coin.name() not in addition_ids and await client.get_coin_record_by_name(coin.name()) is None:
                unresolved.append(coin.name().hex())
        pytest.fail(f"SpendSim rejected bundle: {error}; unresolved removals: {unresolved}")
    await sim.farm_block()


async def _source_coins(sim, client) -> tuple[Coin, dict[bytes32, tuple[Coin, Program, LineageProof]]]:
    await sim.farm_block(ACS_PH)
    records = await client.get_coin_records_by_puzzle_hash(ACS_PH, include_spent_coins=False)
    funding = max((record.coin for record in records), key=lambda coin: coin.amount)
    tails = sorted((tail.get_tree_hash(), tail) for tail in (quoted_tail(b"offer-a"), quoted_tail(b"offer-b")))
    conditions: list[list[object]] = [[ConditionOpcode.CREATE_COIN, ACS_PH, 20_003]]
    eve_coins: list[Coin] = []
    for asset_id, _tail in tails:
        amount = 20_000
        outer_hash = construct_cat_puzzle(CAT_MOD, asset_id, ACS).get_tree_hash()
        conditions.append([ConditionOpcode.CREATE_COIN, outer_hash, amount])
        eve_coins.append(Coin(funding.name(), outer_hash, uint64(amount)))
    await _push_and_farm(
        sim,
        client,
        WalletSpendBundle([make_spend(funding, ACS, Program.to(conditions))], G2Element()),
    )

    source_cats: dict[bytes32, tuple[Coin, Program, LineageProof]] = {}
    initialization_bundles: list[WalletSpendBundle] = []
    for (asset_id, tail), eve in zip(tails, eve_coins):
        spendable = SpendableCAT(
            eve,
            asset_id,
            ACS,
            Program.to([
                [ConditionOpcode.CREATE_COIN, ACS_PH, eve.amount],
                [ConditionOpcode.CREATE_COIN, 0, -113, tail, []],
            ]),
            lineage_proof=LineageProof(),
            limitations_program_reveal=tail,
        )
        initialization_bundles.append(
            unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable])
        )
        source = Coin(eve.name(), construct_cat_puzzle(CAT_MOD, asset_id, ACS).get_tree_hash(), eve.amount)
        source_cats[asset_id] = (
            source,
            tail,
            LineageProof(eve.parent_coin_info, ACS_PH, eve.amount),
        )
    await _push_and_farm(
        sim,
        client,
        WalletSpendBundle.aggregate(initialization_bundles),
    )
    xch_source = Coin(funding.name(), ACS_PH, uint64(20_003))
    return xch_source, source_cats


def _real_create_offer(
    xch_source: Coin,
    source_cats: dict[bytes32, tuple[Coin, Program, LineageProof]],
    signer_key: PrivateKey,
) -> tuple[Offer, LaunchIntent, Offer]:
    asset_ids = tuple(sorted(source_cats))
    xch_settlement = Coin(xch_source.name(), OFFER_MOD.get_tree_hash(), uint64(20_003))
    cat_settlement_ids = tuple(
        Coin(
            source_cats[asset_id][0].name(),
            construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash(),
            source_cats[asset_id][0].amount,
        ).name()
        for asset_id in asset_ids
    )
    launcher_id = Coin(
        xch_settlement.name(),
        __import__("chia.wallet.puzzles.singleton_top_layer_v1_1", fromlist=["SINGLETON_LAUNCHER_HASH"])
        .SINGLETON_LAUNCHER_HASH,
        uint64(1),
    ).name()
    offered_coins = [xch_source, *(source_cats[asset_id][0] for asset_id in asset_ids)]
    notarized = Offer.notarize_payments(
        {None: [CreateCoin(ACS_PH, uint64(1), [ACS_PH])]},
        offered_coins,
    )
    drivers: dict[bytes32, PuzzleInfo] = {}
    announcements = Offer.calculate_announcements(notarized, drivers)
    xch_conditions: list[object] = [
        [ConditionOpcode.CREATE_COIN, OFFER_MOD.get_tree_hash(), 20_003],
        *(announcement.to_program().as_python() for announcement in announcements),
    ]
    xch_spend = make_spend(xch_source, ACS, Program.to(xch_conditions))

    cat_bundles: list[WalletSpendBundle] = []
    for asset_id in asset_ids:
        source, _tail, lineage = source_cats[asset_id]
        cat_spendable = SpendableCAT(
            source,
            asset_id,
            ACS,
            Program.to([[ConditionOpcode.CREATE_COIN, OFFER_MOD.get_tree_hash(), source.amount]]),
            lineage_proof=lineage,
        )
        cat_bundles.append(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [cat_spendable]))

    raw_offer = Offer(
        notarized,
        WalletSpendBundle([
            xch_spend,
            *(spend for cat_bundle in cat_bundles for spend in cat_bundle.coin_spends),
        ], G2Element()),
        drivers,
    )
    pre_offer = Offer.from_spend_bundle(raw_offer.to_spend_bundle())
    preparation = prepare_create_v3(
        pre_offer,
        LaunchConfig(
            asset_ids,
            (5_000, 5_000),
            (20_000, 20_000),
            30,
            ACS_PH,
            20_000,
            1_000,
            bytes32(b"\xef" * 32),
        ),
        signer_key.get_g1(),
        current_height=10,
    )
    guard_message = (
        bytes(preparation.intent.commitment)
        + bytes(preparation.guard_coin.name())
        + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
    )
    guard_signature = AugSchemeMPL.sign(signer_key, guard_message)
    finalized = finalize_create_offer(preparation, guard_signature)
    portable = Offer.from_bech32(finalized.to_bech32())
    return portable, preparation.intent, pre_offer


async def _issue_sources(
    sim,
    client,
    tails: dict[bytes32, Program],
    cat_amounts: dict[bytes32, int],
    xch_amount: int = 0,
) -> tuple[Coin | None, dict[bytes32, tuple[Coin, Program, LineageProof]]]:
    await sim.farm_block(ACS_PH)
    records = await client.get_coin_records_by_puzzle_hash(ACS_PH, include_spent_coins=False)
    funding = max((record.coin for record in records), key=lambda coin: coin.amount)
    conditions: list[list[object]] = []
    if xch_amount:
        conditions.append([ConditionOpcode.CREATE_COIN, ACS_PH, xch_amount])
    eves: dict[bytes32, Coin] = {}
    for asset_id, amount in cat_amounts.items():
        outer_hash = construct_cat_puzzle(CAT_MOD, asset_id, ACS).get_tree_hash()
        conditions.append([ConditionOpcode.CREATE_COIN, outer_hash, amount])
        eves[asset_id] = Coin(funding.name(), outer_hash, uint64(amount))
    await _push_and_farm(
        sim,
        client,
        WalletSpendBundle([make_spend(funding, ACS, Program.to(conditions))], G2Element()),
    )

    sources: dict[bytes32, tuple[Coin, Program, LineageProof]] = {}
    for asset_id, eve in eves.items():
        tail = tails[asset_id]
        bundle = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
            eve,
            asset_id,
            ACS,
            Program.to([
                [ConditionOpcode.CREATE_COIN, ACS_PH, eve.amount],
                [ConditionOpcode.CREATE_COIN, 0, -113, tail, []],
            ]),
            lineage_proof=LineageProof(),
            limitations_program_reveal=tail,
        )])
        await _push_and_farm(sim, client, bundle)
        sources[asset_id] = (
            Coin(eve.name(), construct_cat_puzzle(CAT_MOD, asset_id, ACS).get_tree_hash(), eve.amount),
            tail,
            LineageProof(eve.parent_coin_info, ACS_PH, eve.amount),
        )
    xch = Coin(funding.name(), ACS_PH, uint64(xch_amount)) if xch_amount else None
    return xch, sources


def _operation_offer(
    source_xch: Coin | None,
    source_cats: dict[bytes32, tuple[Coin, Program, LineageProof]],
    requested: dict[bytes32, int],
) -> Offer:
    offered_coins = ([source_xch] if source_xch is not None else []) + [
        source[0] for source in source_cats.values()
    ]
    drivers = {
        asset_id: PuzzleInfo({"type": "CAT", "tail": "0x" + asset_id.hex()})
        for asset_id in requested
        if asset_id is not None
    }
    notarized = Offer.notarize_payments(
        {
            asset_id: [CreateCoin(ACS_PH, uint64(amount), [ACS_PH])]
            for asset_id, amount in requested.items()
        },
        offered_coins,
    )
    announcement_conditions = [
        announcement.to_program().as_python()
        for announcement in Offer.calculate_announcements(notarized, drivers)
    ]
    spends = []
    assertions_consumed = False
    if source_xch is not None:
        spends.append(make_spend(
            source_xch,
            ACS,
            Program.to([
                [ConditionOpcode.CREATE_COIN, OFFER_MOD.get_tree_hash(), source_xch.amount],
                *announcement_conditions,
            ]),
        ))
        assertions_consumed = True
    for asset_id, (source, _tail, lineage) in source_cats.items():
        conditions: list[object] = [
            [ConditionOpcode.CREATE_COIN, OFFER_MOD.get_tree_hash(), source.amount],
        ]
        if not assertions_consumed:
            conditions.extend(announcement_conditions)
            assertions_consumed = True
        bundle = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
            source,
            asset_id,
            ACS,
            Program.to(conditions),
            lineage_proof=lineage,
        )])
        spends.extend(bundle.coin_spends)
    raw = Offer(notarized, WalletSpendBundle(spends, G2Element()), drivers)
    return Offer.from_spend_bundle(raw.to_spend_bundle())


@pytest.mark.anyio
async def test_v3_create_consumes_real_offer_and_uses_standard_singleton_launcher() -> None:
    async with sim_and_client() as (sim, client):
        xch_source, source_cats = await _source_coins(sim, client)
        signer_key = AugSchemeMPL.key_gen(b"forge-v3-offer-guard" + bytes(12))
        offer, intent, pre_offer = _real_create_offer(xch_source, source_cats, signer_key)
        with pytest.raises(ValueError, match="unsigned/unfinalized"):
            build_create_v3(pre_offer, intent, signer_key.get_g1(), current_height=10)
        settlements = find_offer_settlements(offer, intent.asset_ids)
        assert settlements[None].coin.name() == intent.xch_settlement_coin_id
        assert tuple(settlements[asset].coin.name() for asset in intent.asset_ids) == intent.cat_settlement_coin_ids

        result = build_create_v3(offer, intent, signer_key.get_g1(), current_height=10)
        assert result.pool.singleton == singleton_struct(result.pool.launcher_id)
        await _push_and_farm(sim, client, result.bundle)

        launcher_record = await client.get_coin_record_by_name(result.pool.launcher_id)
        assert launcher_record is not None and launcher_record.spent
        guard_record = await client.get_coin_record_by_name(result.guard_coin.name())
        assert guard_record is not None and guard_record.spent
        pool_record = await client.get_coin_record_by_name(result.pool.pool.coin.name())
        assert pool_record is not None and not pool_record.spent
        for reserve in result.pool.reserves.values():
            record = await client.get_coin_record_by_name(reserve.coin.name())
            assert record is not None and not record.spent


@pytest.mark.anyio
async def test_v3_stdin_prepare_and_finalize_create_round_trip() -> None:
    async with sim_and_client() as (sim, client):
        xch_source, source_cats = await _source_coins(sim, client)
        signer_key = AugSchemeMPL.key_gen(b"forge-v3-stdin-guard" + bytes(12))
        _finalized, _intent, pre_offer = _real_create_offer(xch_source, source_cats, signer_key)
        asset_ids = tuple(sorted(source_cats))
        config = {
            "asset_ids": [asset_id.hex() for asset_id in asset_ids],
            "weights": [5_000, 5_000],
            "bootstrap_amounts": [20_000, 20_000],
            "fee_bps": 30,
            "lp_recipient": ACS_PH.hex(),
            "initial_lp": 20_000,
            "expiry_height": 1_000,
            "salt": bytes32(b"\xef" * 32).hex(),
        }
        payload = {
            "action": "prepare-create",
            "offer": pre_offer.to_bech32(),
            "config": config,
            "signer_public_key": bytes(signer_key.get_g1()).hex(),
            "current_height": 10,
        }
        prepared_json = stdin_build(payload)
        assert prepared_json["signing_request"]["partialSign"] is True
        assert prepared_json["signing_request"]["coinSpends"] == [prepared_json["guard_coin_spend"]]

        preparation = prepare_create_v3(
            pre_offer,
            LaunchConfig(
                asset_ids,
                (5_000, 5_000),
                (20_000, 20_000),
                30,
                ACS_PH,
                20_000,
                1_000,
                bytes32(b"\xef" * 32),
            ),
            signer_key.get_g1(),
            current_height=10,
        )
        guard_message = (
            bytes(preparation.intent.commitment)
            + bytes(preparation.guard_coin.name())
            + DEFAULT_CONSTANTS.AGG_SIG_ME_ADDITIONAL_DATA
        )
        finalized_json = stdin_build({
            **payload,
            "action": "finalize-create",
            "guard_signature": bytes(AugSchemeMPL.sign(signer_key, guard_message)).hex(),
        })
        portable = Offer.from_bech32(finalized_json["offer"])
        result = build_create_v3(portable, preparation.intent, signer_key.get_g1(), current_height=10)
        await _push_and_farm(sim, client, result.bundle)


@pytest.mark.anyio
async def test_v3_real_offers_execute_create_add_swap_remove_sequentially() -> None:
    async with sim_and_client() as (sim, client):
        xch_source, create_sources = await _source_coins(sim, client)
        signer_key = AugSchemeMPL.key_gen(b"forge-v3-offer-guard" + bytes(12))
        create_offer, intent, _pre_offer = _real_create_offer(xch_source, create_sources, signer_key)
        created = build_create_v3(create_offer, intent, signer_key.get_g1(), current_height=10)
        await _push_and_farm(sim, client, created.bundle)
        pool = created.pool
        reserve_tails = {asset_id: source[1] for asset_id, source in create_sources.items()}

        add_xch, add_sources = await _issue_sources(
            sim,
            client,
            reserve_tails,
            {asset_id: 2_000 for asset_id in intent.asset_ids},
            xch_amount=2_000,
        )
        assert add_xch is not None
        add_offer = _operation_offer(add_xch, add_sources, {pool.lp_asset_id: 2_000})
        added = build_transition_v3(pool, add_offer, MODE_ADD)
        await _push_and_farm(sim, client, added.bundle)
        assert added.lp_output_coin is not None and added.lp_output_lineage is not None
        pool = added.pool

        _unused_xch, swap_sources = await _issue_sources(
            sim,
            client,
            reserve_tails,
            {intent.asset_ids[0]: 1_000},
        )
        swap_offer = _operation_offer(None, swap_sources, {intent.asset_ids[1]: 900})
        swapped = build_transition_v3(pool, swap_offer, MODE_SWAP)
        await _push_and_farm(sim, client, swapped.bundle)
        assert swapped.pool.state[0][1][2] < pool.state[0][1][2]
        pool = swapped.pool

        lp_source = {
            pool.lp_asset_id: (
                added.lp_output_coin,
                pool.lp_tail,
                added.lp_output_lineage,
            )
        }
        withdrawals = {
            asset_id: int(pool.state[0][index][2]) * 2_000 // int(pool.state[1]) - 1
            for index, asset_id in enumerate(intent.asset_ids)
        }
        remove_offer = _operation_offer(None, lp_source, withdrawals)
        removed = build_transition_v3(pool, remove_offer, MODE_REMOVE)
        await _push_and_farm(sim, client, removed.bundle)
        assert removed.pool.state[1] == 20_000


@pytest.mark.anyio
async def test_v5_native_cat_create_add_swap_remove_lifecycle() -> None:
    async with sim_and_client() as (sim, client):
        await sim.farm_block(ACS_PH)
        records = await client.get_coin_records_by_puzzle_hash(ACS_PH, include_spent_coins=False)
        funding = max((record.coin for record in records), key=lambda coin: coin.amount)
        cat_tail = quoted_tail(b"v5-native-cat")
        cat_asset = cat_tail.get_tree_hash()
        xch_source, cat_sources = await _issue_sources(
            sim,
            client,
            {cat_asset: cat_tail},
            {cat_asset: 20_000},
            xch_amount=40_002,
        )
        assert xch_source is not None
        create_offer = _operation_offer(xch_source, cat_sources, {})
        created_json = deploy_v5({
            "offer": create_offer.to_bech32(),
            "execution": {
                "assetIds": [ZERO_32.hex(), cat_asset.hex()],
                "weights": [5_000, 5_000],
                "bootstrapAmounts": [20_000, 20_000],
                "swapFeeBps": 30,
                "lpRecipientPuzzleHash": ACS_PH.hex(),
            },
            "dry_run": True,
        })
        assert created_json["protocol_version"] == 5
        created_bundle = SpendBundle.from_json_dict(created_json["bundle"])
        await _push_and_farm(
            sim,
            client,
            WalletSpendBundle(created_bundle.coin_spends, created_bundle.aggregated_signature),
        )
        pool = pool_from_snapshot(created_json["v3PoolSnapshot"])

        add_xch, add_sources = await _issue_sources(
            sim,
            client,
            {cat_asset: cat_tail},
            {cat_asset: 2_000},
            xch_amount=2_000,
        )
        assert add_xch is not None
        add_offer = _operation_offer(add_xch, add_sources, {pool.lp_asset_id: 1_326})
        added = build_transition_v3(pool, add_offer, MODE_ADD)
        await _push_and_farm(sim, client, added.bundle)
        assert tuple(int(reserve[2]) for reserve in added.pool.state[0]) == (20_674, 22_000)

        swap_xch, _ = await _issue_sources(sim, client, {}, {}, xch_amount=1_000)
        assert swap_xch is not None
        swap_offer = _operation_offer(swap_xch, {}, {cat_asset: 950})
        swapped = build_transition_v3(added.pool, swap_offer, MODE_SWAP)
        await _push_and_farm(sim, client, swapped.bundle)
        assert int(swapped.pool.state[0][0][2]) > int(added.pool.state[0][0][2])

        assert added.lp_output_coin is not None and added.lp_output_lineage is not None
        lp_source = {pool.lp_asset_id: (added.lp_output_coin, added.pool.lp_tail, added.lp_output_lineage)}
        withdrawals = {
                None: int(swapped.pool.state[0][0][2]) * 1_326 // int(swapped.pool.state[1]) - 1,
            cat_asset: int(swapped.pool.state[0][1][2]) * 1_326 // int(swapped.pool.state[1]) - 1,
        }
        remove_offer = _operation_offer(None, lp_source, withdrawals)
        removed = build_transition_v3(swapped.pool, remove_offer, MODE_REMOVE)
        await _push_and_farm(sim, client, removed.bundle)
        assert removed.pool.state[1] == int(swapped.pool.state[1]) - 1_326


@pytest.mark.anyio
async def test_v3_single_asset_add_mints_invariant_lp_and_removes_pro_rata() -> None:
    async with sim_and_client() as (sim, client):
        xch_source, create_sources = await _source_coins(sim, client)
        signer_key = AugSchemeMPL.key_gen(b"forge-v3-single-join" + bytes(12))
        create_offer, intent, _pre_offer = _real_create_offer(xch_source, create_sources, signer_key)
        created = build_create_v3(create_offer, intent, signer_key.get_g1(), current_height=10)
        await _push_and_farm(sim, client, created.bundle)

        deposit_asset = intent.asset_ids[0]
        lp_mint = invariant_lp_mint_v3((20_000, 20_000), (22_000, 20_000), 20_000)
        assert lp_mint == 976
        reserve_tails = {asset_id: source[1] for asset_id, source in create_sources.items()}
        add_xch, add_sources = await _issue_sources(
            sim,
            client,
            reserve_tails,
            {deposit_asset: 2_000},
            xch_amount=lp_mint,
        )
        assert add_xch is not None
        add_offer = _operation_offer(add_xch, add_sources, {created.pool.lp_asset_id: lp_mint})
        added = build_transition_v3(created.pool, add_offer, MODE_ADD)
        await _push_and_farm(sim, client, added.bundle)
        assert tuple(int(reserve[2]) for reserve in added.pool.state[0]) == (22_000, 20_000)
        assert int(added.pool.state[1]) == 20_976

        assert added.lp_output_coin is not None and added.lp_output_lineage is not None
        burn = lp_mint
        lp_source = {
            added.pool.lp_asset_id: (
                added.lp_output_coin,
                added.pool.lp_tail,
                added.lp_output_lineage,
            )
        }
        expected = {
            asset_id: int(added.pool.state[0][index][2]) * burn // int(added.pool.state[1])
            for index, asset_id in enumerate(intent.asset_ids)
        }
        assert all(amount > 0 for amount in expected.values())
        remove_offer = _operation_offer(None, lp_source, expected)
        removed = build_transition_v3(added.pool, remove_offer, MODE_REMOVE)
        await _push_and_farm(sim, client, removed.bundle)
        for index, asset_id in enumerate(intent.asset_ids):
            assert int(added.pool.state[0][index][2]) - int(removed.pool.state[0][index][2]) == expected[asset_id]


@pytest.mark.anyio
async def test_v3_real_offers_reject_tampering_wrong_assets_minimums_replay_and_duplicate_mint() -> None:
    async with sim_and_client() as (sim, client):
        xch_source, create_sources = await _source_coins(sim, client)
        signer_key = AugSchemeMPL.key_gen(b"forge-v3-offer-guard" + bytes(12))
        create_offer, intent, _pre_offer = _real_create_offer(xch_source, create_sources, signer_key)

        with pytest.raises(ValueError, match="launch guard"):
            build_create_v3(
                create_offer,
                replace(intent, fee_bps=31),
                signer_key.get_g1(),
                current_height=10,
            )

        created = build_create_v3(create_offer, intent, signer_key.get_g1(), current_height=10)
        await _push_and_farm(sim, client, created.bundle)
        pool = created.pool
        reserve_tails = {asset_id: source[1] for asset_id, source in create_sources.items()}

        wrong_tail = quoted_tail(b"wrong-offer-asset")
        wrong_asset = wrong_tail.get_tree_hash()
        _unused_xch, wrong_sources = await _issue_sources(
            sim,
            client,
            {wrong_asset: wrong_tail},
            {wrong_asset: 1_000},
        )
        wrong_offer = _operation_offer(None, wrong_sources, {pool.lp_asset_id: 1})
        with pytest.raises(ValueError, match="at least one reserve asset"):
            build_transition_v3(pool, wrong_offer, MODE_ADD)

        _unused_xch, swap_sources = await _issue_sources(
            sim,
            client,
            reserve_tails,
            {intent.asset_ids[0]: 1_000},
        )
        excessive_minimum = _operation_offer(None, swap_sources, {intent.asset_ids[1]: 5_000})
        with pytest.raises(ValueError, match="below Offer minimum"):
            build_transition_v3(pool, excessive_minimum, MODE_SWAP)

        add_xch, add_sources = await _issue_sources(
            sim,
            client,
            reserve_tails,
            {asset_id: 2_000 for asset_id in intent.asset_ids},
            xch_amount=2_000,
        )
        assert add_xch is not None
        add_offer = _operation_offer(add_xch, add_sources, {pool.lp_asset_id: 2_000})
        added = build_transition_v3(pool, add_offer, MODE_ADD)
        duplicated_mint = WalletSpendBundle(
            [*added.bundle.coin_spends, added.bundle.coin_spends[-2]],
            added.bundle.aggregated_signature,
        )
        status, error = await client.push_tx(duplicated_mint)
        assert status == MempoolInclusionStatus.FAILED and error is not None

        await _push_and_farm(sim, client, added.bundle)
        status, error = await client.push_tx(added.bundle)
        assert status == MempoolInclusionStatus.FAILED and error is not None