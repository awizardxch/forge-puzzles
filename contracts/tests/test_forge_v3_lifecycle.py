from __future__ import annotations

import sys
import unittest
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTRACTS_DIR))

from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia_rs.sized_bytes import bytes32

from forge_v3 import (
    ASSET_A,
    LP_ACTION_COIN_ID,
    MODE_ADD,
    MODE_REMOVE,
    MODE_SWAP,
    POOL_COIN_ID,
    RESERVE_INNER_PUZZLE,
    RESERVE_INNER_PUZZLE_HASH,
    opcode_conditions,
    plans_for,
    pool_args,
    reserve_cat_puzzle_hash,
    require_coin_context,
    run_contract,
    sha256,
)


class ForgeV3LifecycleTests(unittest.TestCase):
    def test_reserve_outer_hash_matches_canonical_cat_puzzle(self) -> None:
        expected = construct_cat_puzzle(
            CAT_MOD,
            bytes32(ASSET_A),
            RESERVE_INNER_PUZZLE,
        ).get_tree_hash()
        self.assertEqual(reserve_cat_puzzle_hash(ASSET_A), bytes(expected))

    def assert_reserve_handshakes(self, mode: int) -> None:
        _, pool_conditions = run_contract("pool_singleton_v3", pool_args(mode))
        pool_messages = opcode_conditions(pool_conditions, ConditionOpcode.CREATE_COIN_ANNOUNCEMENT)
        pool_assertions = {
            condition[1]
            for condition in opcode_conditions(pool_conditions, ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT)
        }
        _, plans, _ = plans_for(mode)
        for index, plan in enumerate(plans):
            _, reserve_conditions = run_contract(
                "forge_reserve_v3",
                [
                    __import__("forge_v3").LAUNCHER_ID,
                    plan.asset_id,
                    RESERVE_INNER_PUZZLE_HASH,
                    plan.reserve_program(mode),
                ],
            )
            require_coin_context(
                reserve_conditions,
                actual_coin_id=plan.current_coin_id,
                actual_amount=plan.current_amount,
            )
            reserve_pool_assertion = opcode_conditions(
                reserve_conditions,
                ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT,
            )[0][1]
            self.assertEqual(reserve_pool_assertion, sha256(POOL_COIN_ID, pool_messages[index][1]))
            reserve_ack = opcode_conditions(
                reserve_conditions,
                ConditionOpcode.CREATE_COIN_ANNOUNCEMENT,
            )[0][1]
            self.assertIn(sha256(plan.current_coin_id, reserve_ack), pool_assertions)

    def test_add_generates_exact_pool_reserve_and_lp_conditions(self) -> None:
        cost, conditions = run_contract("pool_singleton_v3", pool_args(MODE_ADD))
        self.assertGreater(cost, 0)
        self.assertEqual(len(opcode_conditions(conditions, ConditionOpcode.CREATE_COIN)), 1)
        self.assertEqual(len(opcode_conditions(conditions, ConditionOpcode.CREATE_COIN_ANNOUNCEMENT)), 3)
        self.assertEqual(len(opcode_conditions(conditions, ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT)), 3)
        require_coin_context(conditions, actual_coin_id=POOL_COIN_ID, actual_amount=1)

        _, tail_conditions = run_contract("forge_lp_cat_tail_v3", __import__("forge_v3").lp_tail_args(MODE_ADD))
        require_coin_context(tail_conditions, actual_coin_id=LP_ACTION_COIN_ID, actual_amount=1)
        self.assertEqual(len(opcode_conditions(tail_conditions, ConditionOpcode.CREATE_COIN_ANNOUNCEMENT)), 1)
        self.assert_reserve_handshakes(MODE_ADD)

    def test_swap_generates_two_reserve_handshakes_and_no_lp_handshake(self) -> None:
        _, conditions = run_contract("pool_singleton_v3", pool_args(MODE_SWAP))
        self.assertEqual(len(opcode_conditions(conditions, ConditionOpcode.CREATE_COIN_ANNOUNCEMENT)), 2)
        self.assertEqual(len(opcode_conditions(conditions, ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT)), 2)
        self.assert_reserve_handshakes(MODE_SWAP)

    def test_remove_generates_exact_pool_reserve_and_lp_conditions(self) -> None:
        _, conditions = run_contract("pool_singleton_v3", pool_args(MODE_REMOVE))
        self.assertEqual(len(opcode_conditions(conditions, ConditionOpcode.CREATE_COIN_ANNOUNCEMENT)), 3)
        _, tail_conditions = run_contract("forge_lp_cat_tail_v3", __import__("forge_v3").lp_tail_args(MODE_REMOVE))
        require_coin_context(tail_conditions, actual_coin_id=LP_ACTION_COIN_ID, actual_amount=1)
        self.assert_reserve_handshakes(MODE_REMOVE)


if __name__ == "__main__":
    unittest.main()