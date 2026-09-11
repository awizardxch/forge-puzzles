from __future__ import annotations

import sys
import unittest
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTRACTS_DIR))

from forge_v3 import (
    ASSET_A,
    ASSET_B,
    LP_ACTION_COIN_ID,
    MODE_ADD,
    POOL_COIN_ID,
    ReservePlan,
    config_program,
    launch_guard_args,
    launch_intent,
    lp_tail_args,
    plans_for,
    pool_args,
    require_coin_context,
    require_unique_spends,
    run_contract,
    tree_hash,
)


class ForgeV3AdversarialTests(unittest.TestCase):
    def assert_clvm_rejects(self, contract: str, args: object) -> None:
        with self.assertRaises(Exception):
            run_contract(contract, args)

    def test_rejects_reserve_redirection(self) -> None:
        _, plans, _ = plans_for(MODE_ADD)
        redirected = ReservePlan(
            plans[0].asset_id,
            plans[0].current_coin_id,
            plans[0].current_amount,
            plans[0].settlement_coin_id,
            plans[0].successor_amount,
            bytes.fromhex("ff" * 32),
        )
        self.assert_clvm_rejects("pool_singleton_v3", pool_args(MODE_ADD, plans=[redirected, plans[1]]))

    def test_rejects_duplicate_lp_action_reuse(self) -> None:
        _, first = run_contract("forge_lp_cat_tail_v3", lp_tail_args(MODE_ADD))
        _, second = run_contract("forge_lp_cat_tail_v3", lp_tail_args(MODE_ADD))
        require_coin_context(first, actual_coin_id=LP_ACTION_COIN_ID, actual_amount=1)
        require_coin_context(second, actual_coin_id=LP_ACTION_COIN_ID, actual_amount=1)
        with self.assertRaisesRegex(ValueError, "duplicate coin spend"):
            require_unique_spends([LP_ACTION_COIN_ID, LP_ACTION_COIN_ID])

    def test_rejects_wrong_lp_delta_from_cat_truths(self) -> None:
        self.assert_clvm_rejects(
            "forge_lp_cat_tail_v3",
            lp_tail_args(MODE_ADD, expected_delta=1000, cat_delta=1001),
        )

    def test_rejects_stale_pool_coin_context(self) -> None:
        stale_id = bytes.fromhex("de" * 32)
        _, conditions = run_contract("pool_singleton_v3", pool_args(MODE_ADD, pool_coin_id=stale_id))
        with self.assertRaisesRegex(ValueError, "ASSERT_MY_COIN_ID"):
            require_coin_context(conditions, actual_coin_id=POOL_COIN_ID, actual_amount=1)

    def test_rejects_swap_fee_bypass_and_under_output(self) -> None:
        _, plans, _ = plans_for(__import__("forge_v3").MODE_SWAP)
        fee_bypass = ReservePlan(
            plans[1].asset_id,
            plans[1].current_coin_id,
            plans[1].current_amount,
            plans[1].settlement_coin_id,
            18_181,
        )
        self.assert_clvm_rejects(
            "pool_singleton_v3",
            pool_args(__import__("forge_v3").MODE_SWAP, plans=[plans[0], fee_bypass]),
        )

        under_output = ReservePlan(
            plans[1].asset_id,
            plans[1].current_coin_id,
            plans[1].current_amount,
            plans[1].settlement_coin_id,
            18_188,
        )
        self.assert_clvm_rejects(
            "pool_singleton_v3",
            pool_args(__import__("forge_v3").MODE_SWAP, plans=[plans[0], under_output]),
        )

    def test_rejects_malformed_lists_and_config(self) -> None:
        bad_configs = [
            config_program(asset_ids=[ASSET_A]),
            config_program(asset_ids=[ASSET_A, ASSET_A]),
            config_program(asset_ids=[ASSET_B, ASSET_A]),
            config_program(weights=[5000]),
            config_program(weights=[4000, 6000]),
            config_program(fee_bps=1001),
        ]
        for bad_config in bad_configs:
            with self.subTest(config=bad_config):
                self.assert_clvm_rejects("pool_singleton_v3", pool_args(MODE_ADD, config=bad_config))

        reserves, plans, _ = plans_for(MODE_ADD)
        self.assert_clvm_rejects("pool_singleton_v3", pool_args(MODE_ADD, plans=plans[:1]))
        self.assert_clvm_rejects("pool_singleton_v3", pool_args(MODE_ADD, reserves=reserves[:1]))

    def test_rejects_tampered_launch_intent(self) -> None:
        signed_intent = launch_intent()
        committed_hash = tree_hash(signed_intent)
        tampered_intent = launch_intent(fee_bps=31)
        self.assert_clvm_rejects(
            "forge_launch_guard_v3",
            launch_guard_args(tampered_intent, committed_hash),
        )

    def test_launch_guard_accepts_untampered_intent(self) -> None:
        intent = launch_intent()
        cost, conditions = run_contract("forge_launch_guard_v3", launch_guard_args(intent))
        self.assertGreater(cost, 0)
        self.assertEqual(len(conditions), 7)


if __name__ == "__main__":
    unittest.main()