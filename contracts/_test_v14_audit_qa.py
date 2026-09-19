"""Focused local-node rechecks of the 2026-09-19 audit baseline.

Written by the GPT-6 Astra (Copilot) QA pass in the runbook's simulator role, against the
public clone; ported here because the monorepo is the source of truth and the sync would
otherwise overwrite it. Three probes, seven cases:

  * each reserve launcher omitted, mis-targeted, mis-hinted or short by a mojo, refused by
    a real node with the exact consensus code, and the unchanged honest registration then
    confirmed -- the reserves-proved suite's claims, judged by a coin store;
  * L-3 reproduced: two zero-rate pools differing only in an inert DAO recipient register
    under two keys;
  * L-5 narrowed: a zero NUMERATOR reserve is accepted by `observe` and records spot zero;
    a zero DENOMINATOR reserve divides by zero. Only the second is the brick the audit
    described.

pytest-style, run directly: the __main__ block hands the file to pytest and returns its
exit code, so the repository's script convention (0 pass, 1 fail, 2 skip) holds.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.util.errors import Err
from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forge_v14_driver as driver
from _sim_harness import Wallet, farm_to_identity, issue_cat, push, sim_and_client


def load_simulator():
    path = Path(__file__).resolve().parent.parent / "scripts" / "sim-v14.py"
    spec = importlib.util.spec_from_file_location("v14_audit_simulator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("omit_xch", Err.ASSERT_ANNOUNCE_CONSUMED_FAILED),
        ("omit_cat", Err.ASSERT_ANNOUNCE_CONSUMED_FAILED),
        ("wrong_puzzle", Err.ASSERT_ANNOUNCE_CONSUMED_FAILED),
        ("wrong_hint", Err.ASSERT_ANNOUNCE_CONSUMED_FAILED),
        ("short_amount", Err.ASSERT_MY_AMOUNT_FAILED),
    ],
)
def test_reserve_launcher_exact_consensus_refusal(variant, expected):
    async def scenario():
        simulator = load_simulator()
        async with sim_and_client() as (sim, client):
            wallet = Wallet(await farm_to_identity(sim, client, blocks=4), {})
            funding = wallet.take_xch(60_000_001)
            asset, token, lineage, issuance = issue_cat(funding, 60_000_000, salt=0xDA)
            await push(client, sim, issuance, "QA token issuance")
            wallet.cats[asset] = [(token, lineage)]
            registry = await simulator.mint_registry(sim, client, wallet)
            honest, pool = await simulator.create_pool(
                sim, client, wallet, registry, [None, asset],
                [10_000_000, 20_000_000], [4, 1], push_now=False,
            )
            conditions, additions = driver.validate(honest)
            coin_ids = [spend.coin.name() for spend in honest.coin_spends]
            assert len(coin_ids) == len(set(coin_ids))
            assert (pool.coin.puzzle_hash, 1) in additions
            assert conditions.cost > 0

            reserve = pool.reserves[1 if variant == "omit_cat" else 0]
            target_id = reserve.coin.parent_coin_info
            matched = [spend for spend in honest.coin_spends if spend.coin.name() == target_id]
            assert len(matched) == 1
            original = matched[0]
            replacement = None
            if not variant.startswith("omit_"):
                created = bytes32(b"\x98" * 32) if variant == "wrong_puzzle" else reserve.inner_hash
                hint = bytes32(b"\x99" * 32) if variant == "wrong_hint" else pool.launcher_id
                amount = int(reserve.coin.amount) - (1 if variant == "short_amount" else 0)
                replacement = make_spend(
                    original.coin, driver.RESERVE_LAUNCHER,
                    Program.to([created, amount, hint]),
                )
            spends = [spend for spend in honest.coin_spends if spend.coin.name() != target_id]
            if replacement is not None:
                spends.append(replacement)
            attacked = SpendBundle(spends, G2Element())
            peak = sim.block_height
            status, error = await client.push_tx(attacked)
            assert status == MempoolInclusionStatus.FAILED
            assert error == expected, (variant, status, error)
            assert sim.block_height == peak
            assert await client.get_coin_record_by_name(pool.coin.name()) is None

            await push(client, sim, honest, "QA unchanged honest registration")
            for reserve in pool.reserves:
                record = await client.get_coin_record_by_name(reserve.coin.name())
                assert record is not None and not record.spent
                assert record.coin == reserve.coin
            record = await client.get_coin_record_by_name(pool.coin.name())
            assert record is not None and not record.spent
            print(f"{variant}: {error.name}; unchanged honest registration confirmed")

    asyncio.run(scenario())


def test_zero_rate_dao_recipient_creates_another_registry_key():
    from _test_v14_registry import registration

    asset = bytes32(b"\xd0" * 32)
    first = driver.make_pool(
        [None, asset], [10_000_000, 20_000_000], total_lp=5_000_000,
        leaves="forge", salt=0x50,
    )
    second = driver.make_pool(
        [None, asset], [10_000_000, 20_000_000], total_lp=5_000_000,
        leaves="forge", salt=0x51, dao_ph=bytes32(b"\x42" * 32), dao_fee_bps=0,
    )
    first_key = driver.pool_key(first.config())
    second_key = driver.pool_key(second.config())
    assert first_key != second_key
    initial = driver.make_registry(salt=0x21)
    driver.validate(driver.registry_spend(initial, "forge_registry_init", [])[0])
    registry = initial.advance([1, 0])
    slots = {key: (initial.coin, initial.inner_hash) for key in (driver.MIN_KEY, driver.MAX_KEY)}
    left = (driver.MIN_KEY, driver.ZERO_32, driver.MIN_KEY)
    right = (driver.MAX_KEY, driver.ZERO_32, driver.MAX_KEY)
    driver.validate(registration(registry, first, left, right, slots)[0])
    slots = {
        key: (registry.coin, registry.inner_hash)
        for key in (driver.MIN_KEY, first_key, driver.MAX_KEY)
    }
    if second_key < first_key:
        right = (first_key, first.launcher_id, driver.MAX_KEY)
    else:
        left = (first_key, first.launcher_id, driver.MIN_KEY)
    bundle, state = registration(registry.advance([1, 1]), second, left, right, slots)
    driver.validate(bundle)
    assert [item.as_int() for item in state.as_iter()] == [1, 2]
    print("L-3: honest pool and zero-rate DAO twin both register offline; count=2")


def test_zero_reserve_prologue_depends_on_asset_position():
    pool = driver.make_pool(
        [None, bytes32(b"\xd0" * 32)], [10_000_000, 20_000_000],
        total_lp=5_000_000, leaves="forge",
    )
    driver.run_leaf(pool, "forge_action_observe", [6_999_990])
    state = list(pool.state)
    state[0] = [0, 20_000_000]
    result = driver.run_leaf(pool, "forge_action_observe", [6_999_990], state=state)
    assert driver.state_to_list(result[0])[3][2] == [0]
    state[0] = [10_000_000, 0]
    with pytest.raises(ValueError, match="div with 0|divmod|zero") as rejection:
        driver.run_leaf(pool, "forge_action_observe", [6_999_990], state=state)
    print(f"L-5: zero numerator accepted; zero denominator refused: {rejection.value}")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))