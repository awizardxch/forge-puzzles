#!/usr/bin/env python3
"""V9 closes the LP-authorization bug: a remove needs a real LP burn.

Through V8 the pool asserted a coin announcement from a free `lp_action_coin_id`,
so any attacker coin could stand in for the burn and drain reserves. V9 derives
that coin's id from its parent, the pinned LP-CAT(melt-inner) puzzle hash, and
the exact burn amount, and rejects anything else.

This drives the REAL compiled V9 pool inner against a synthetic-but-valid pool
state and checks:
  * the honest melt coin (derived id) is accepted;
  * an attacker-controlled coin is rejected;
  * even an LP CAT coin wrapping the WRONG inner is rejected (so the residual
    "own LP but skip the melt" attack is closed too).
"""
import sys
import hashlib

sys.path.insert(0, ".")

from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH

import forge_puzzles
from forge_offer import compiled_program, tree_hash, ZERO_32, MODE_REMOVE

CAT_MOD_HASH = CAT_MOD.get_tree_hash()


def amt_bytes(n: int) -> bytes:
    if n == 0:
        return b""
    length = (n.bit_length() + 8) // 8
    return n.to_bytes(length, "big")


def coin_id(parent: bytes32, puzzle_hash: bytes32, amount: int) -> bytes32:
    return bytes32(hashlib.sha256(bytes(parent) + bytes(puzzle_hash) + amt_bytes(amount)).digest())


def cat_ph(asset_id: bytes32, inner_hash: bytes32) -> bytes32:
    return construct_cat_puzzle(CAT_MOD, asset_id, Program.to(inner_hash)).get_tree_hash_precalc(inner_hash)


def settlement_ph(asset_id: bytes32) -> bytes32:
    if asset_id == ZERO_32:
        return OFFER_MOD_HASH
    return construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash()


def reserve_ph(asset_id: bytes32, reserve_inner_hash: bytes32) -> bytes32:
    if asset_id == ZERO_32:
        return reserve_inner_hash
    return cat_ph(asset_id, reserve_inner_hash)


def build_remove_action(config, state, burn, lp_action_coin_id, lp_parent_id):
    reserve_inner_hash = bytes32(config[8])
    total_lp = int(state[1])
    plans = []
    for asset_id_raw, cur_coin_id_raw, amount in state[0]:
        asset_id = bytes32(asset_id_raw)
        cur_coin_id = bytes32(cur_coin_id_raw)
        successor_amount = amount - amount * burn // total_lp
        released = amount - successor_amount
        succ_id = coin_id(cur_coin_id, reserve_ph(asset_id, reserve_inner_hash), successor_amount)
        settle_id = (coin_id(cur_coin_id, settlement_ph(asset_id), released)
                     if released > 0 else ZERO_32)
        plans.append([asset_id, cur_coin_id, amount, settle_id, succ_id,
                      successor_amount, 0, ZERO_32])
    return [MODE_REMOVE, bytes32(b"\x01" * 32), plans, lp_action_coin_id, -burn, lp_parent_id]


def run(config, state, action):
    singleton = [bytes32(b"\x0a" * 32), bytes32(b"\x0b" * 32), bytes32(b"\x0c" * 32)]
    inner = compiled_program("pool_singleton_FORGE").curry(singleton, config, state)
    return inner.run(Program.to([action]))


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    lp_asset_id = bytes32(bytes.fromhex("aa" * 32))
    reserve_inner_hash = bytes32(bytes.fromhex("bb" * 32))
    pool_mod_hash = bytes32(bytes.fromhex("cc" * 32))
    cat_b = bytes32(bytes.fromhex("dd" * 32))

    # Ascending asset order: native (zero) sorts first.
    config = [forge_puzzles.FORGE_VERSION, pool_mod_hash, [ZERO_32, cat_b], [1, 1], 30, 0, ZERO_32,
              lp_asset_id, reserve_inner_hash]
    reserves = [
        [ZERO_32, bytes32(b"\x21" * 32), 10_000_000_000_000],
        [cat_b, bytes32(b"\x22" * 32), 5_000_000],
    ]
    total_lp = 100_000
    state = [reserves, total_lp]
    burn = 40_000

    melt_inner_hash = compiled_program("forge_lp_melt_inner_FORGE").get_tree_hash()
    lp_parent = bytes32(b"\x31" * 32)
    honest_melt_ph = cat_ph(lp_asset_id, melt_inner_hash)
    honest_id = coin_id(lp_parent, honest_melt_ph, burn)

    results = []

    # 1. Honest: the derived melt-coin id is accepted.
    try:
        run(config, state, build_remove_action(config, state, burn, honest_id, lp_parent))
        results.append(check("honest melt coin (derived id) is accepted", True))
    except Exception as exc:
        results.append(check("honest melt coin (derived id) is accepted", False, str(exc)))

    # 2. Attack: a plain attacker coin is rejected.
    attacker_id = coin_id(bytes32(b"\xAA" * 32), bytes32(b"\xBB" * 32), 1)
    try:
        run(config, state, build_remove_action(config, state, burn, attacker_id, bytes32(b"\xAA" * 32)))
        results.append(check("a fabricated attacker coin is REJECTED", False,
                             "pool released reserves for no real burn"))
    except Exception:
        results.append(check("a fabricated attacker coin is REJECTED", True))

    # 3. Residual attack: a real LP CAT coin wrapping the WRONG inner (identity,
    #    which need not melt) is rejected -- only the pinned melt inner passes.
    identity_hash = Program.to(1).get_tree_hash()
    wrong_ph = cat_ph(lp_asset_id, identity_hash)
    wrong_id = coin_id(lp_parent, wrong_ph, burn)
    try:
        run(config, state, build_remove_action(config, state, burn, wrong_id, lp_parent))
        results.append(check("an LP coin with a non-melt inner is REJECTED", False,
                             "the residual keep-LP attack is open"))
    except Exception:
        results.append(check("an LP coin with a non-melt inner is REJECTED", True))

    # 4. Attack with the honest parent but wrong amount is rejected.
    wrong_amt_id = coin_id(lp_parent, honest_melt_ph, burn + 1)
    try:
        run(config, state, build_remove_action(config, state, burn, wrong_amt_id, lp_parent))
        results.append(check("a melt coin of the wrong amount is REJECTED", False))
    except Exception:
        results.append(check("a melt coin of the wrong amount is REJECTED", True))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} FORGE LP-binding checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
