from __future__ import annotations

import sys
from pathlib import Path

import pytest
from chia._tests.util.spend_sim import sim_and_client
from chia.types.blockchain_format.program import INFINITE_COST, Program
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia_rs.sized_bytes import bytes32

CONTRACTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTRACTS_DIR))

from forge_v3_spend import (
    ACS_PH,
    MODE_ADD,
    MODE_REMOVE,
    MODE_SWAP,
    apply_transition,
    build_funding_bundle,
    build_initialization_bundle,
    build_transition,
)


async def initialized_fixture(sim, client):
    await sim.farm_block(ACS_PH)
    records = await client.get_coin_records_by_puzzle_hash(ACS_PH, include_spent_coins=False)
    funding_coin = max((record.coin for record in records), key=lambda coin: coin.amount)
    funding_bundle, metadata = build_funding_bundle(funding_coin)
    status, error = await client.push_tx(funding_bundle)
    assert (status, error) == (MempoolInclusionStatus.SUCCESS, None)
    await sim.farm_block()

    initialization_bundle, fixture = build_initialization_bundle(metadata)
    status, error = await client.push_tx(initialization_bundle)
    assert (status, error) == (MempoolInclusionStatus.SUCCESS, None)
    await sim.farm_block()
    return fixture


async def assert_confirmed(client, previous_pool, previous_reserves, transition) -> None:
    removal_ids = {coin.name() for coin in transition.bundle.removals()}
    addition_ids = {coin.name() for coin in transition.bundle.additions()}
    assert previous_pool.name() in removal_ids
    assert transition.next_pool.name() in addition_ids
    previous_pool_record = await client.get_coin_record_by_name(previous_pool.name())
    assert previous_pool_record is not None and previous_pool_record.spent
    pool_record = await client.get_coin_record_by_name(transition.next_pool.name())
    assert pool_record is not None and not pool_record.spent
    for asset_id, reserve in transition.next_reserves.items():
        previous = previous_reserves[asset_id]
        assert previous.coin.name() in removal_ids
        assert reserve.coin.name() in addition_ids
        previous_record = await client.get_coin_record_by_name(previous.coin.name())
        assert previous_record is not None and previous_record.spent
        record = await client.get_coin_record_by_name(reserve.coin.name())
        assert record is not None and not record.spent
    expected_state = [
        [asset_id, reserve.coin.name(), reserve.coin.amount]
        for asset_id, reserve in transition.next_reserves.items()
    ]
    assert transition.next_state[0] == expected_state


def assert_spends_execute(bundle) -> None:
    for index, coin_spend in enumerate(bundle.coin_spends):
        try:
            puzzle = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
            solution = Program.from_bytes(bytes(coin_spend.solution))
            puzzle.run_with_cost(INFINITE_COST, solution)
        except Exception as error:
            pytest.fail(f"coin spend {index} ({coin_spend.coin.name().hex()}) failed CLVM: {error}")


@pytest.mark.anyio
async def legacy_v3_synthetic_cat2_sequential_lifecycle() -> None:
    async with sim_and_client() as (sim, client):
        fixture = await initialized_fixture(sim, client)

        previous_pool = fixture.pool_coin
        previous_reserves = fixture.reserves
        add = build_transition(fixture, MODE_ADD, (11_000, 22_000), 1_000, "add", (0, 0))
        assert_spends_execute(add.bundle)
        status, error = await client.push_tx(add.bundle)
        assert (status, error) == (MempoolInclusionStatus.SUCCESS, None)
        await sim.farm_block()
        await assert_confirmed(client, previous_pool, previous_reserves, add)
        assert add.lp_output is not None
        assert add.lp_output.coin.name() in {coin.name() for coin in add.bundle.additions()}
        assert (await client.get_coin_record_by_name(add.lp_output.coin.name())) is not None
        apply_transition(fixture, add)

        previous_pool = fixture.pool_coin
        previous_reserves = fixture.reserves
        swap = build_transition(fixture, MODE_SWAP, (12_000, 20_167), 0, "swap", (1, 1_834))
        status, error = await client.push_tx(swap.bundle)
        assert (status, error) == (MempoolInclusionStatus.SUCCESS, None)
        await sim.farm_block()
        await assert_confirmed(client, previous_pool, previous_reserves, swap)
        apply_transition(fixture, swap)

        previous_pool = fixture.pool_coin
        previous_reserves = fixture.reserves
        burned_lp_coin = fixture.initial_lp.coin
        remove = build_transition(fixture, MODE_REMOVE, (10_910, 18_334), -1_000, "remove", (1_091, 1_835))
        status, error = await client.push_tx(remove.bundle)
        assert (status, error) == (MempoolInclusionStatus.SUCCESS, None)
        await sim.farm_block()
        await assert_confirmed(client, previous_pool, previous_reserves, remove)
        assert burned_lp_coin.name() in {coin.name() for coin in remove.bundle.removals()}
        burned_record = await client.get_coin_record_by_name(burned_lp_coin.name())
        assert burned_record is not None and burned_record.spent
        assert remove.next_state[1] == 10_000


@pytest.mark.anyio
async def legacy_v3_synthetic_rejects_wrong_successor_and_lp_delta() -> None:
    async with sim_and_client() as (sim, client):
        fixture = await initialized_fixture(sim, client)
        wrong_successor = build_transition(
            fixture,
            MODE_ADD,
            (11_000, 22_000),
            1_000,
            "add",
            (0, 0),
            (bytes32(b"\xff" * 32), None),
        )
        status, error = await client.push_tx(wrong_successor.bundle)
        assert status == MempoolInclusionStatus.FAILED
        assert error is not None

        wrong_delta = build_transition(fixture, MODE_ADD, (11_000, 22_000), 1_001, "add", (0, 0))
        status, error = await client.push_tx(wrong_delta.bundle)
        assert status == MempoolInclusionStatus.FAILED
        assert error is not None


@pytest.mark.anyio
async def legacy_v3_synthetic_rejects_duplicate_lp_action_and_stale_pool_coin() -> None:
    async with sim_and_client() as (sim, client):
        fixture = await initialized_fixture(sim, client)
        add = build_transition(fixture, MODE_ADD, (11_000, 22_000), 1_000, "add", (0, 0))
        duplicated = type(add.bundle)([*add.bundle.coin_spends, add.bundle.coin_spends[-1]], add.bundle.aggregated_signature)
        status, error = await client.push_tx(duplicated)
        assert status == MempoolInclusionStatus.FAILED
        assert error is not None

        status, error = await client.push_tx(add.bundle)
        assert (status, error) == (MempoolInclusionStatus.SUCCESS, None)
        await sim.farm_block()
        status, error = await client.push_tx(add.bundle)
        assert status == MempoolInclusionStatus.FAILED
        assert error is not None