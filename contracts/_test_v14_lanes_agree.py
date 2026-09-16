#!/usr/bin/env python3
"""The deploy lane and the website lane build the same registration (spec 1.6, S2's lesson).

S2 -- the unburned floor -- existed because the deploy script and the website lane did
different things and only one was checked. V14 adds a launcher per reserve to both lanes.
This builds ONE pool from ONE set of creator coins through both:

  * the deploy lane: `make_pool` with real reserve coins parented by launchers, then
    `reserve_launcher_spends` and `register_solution` -- what deploy-v14-testnet.py does;
  * the website lane: `forge_v14_create.plan`, what the browser's create flow POSTs.

and requires the launcher spends' announcements, the register solutions and the pool
coins to be byte-identical. Then it validates both bundles.

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import hashlib
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER, SINGLETON_LAUNCHER_HASH
from chia_rs import AugSchemeMPL
from chia.types.coin_spend import make_spend
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_v14_create as create
import forge_v14_driver as drv
from forge_offer import ZERO_32

results = []
IDENTITY = Program.to(1)
# the creator's coins sit behind the standard p2 puzzle (signatures are not checked offline),
# because both lanes build the delegated-puzzle solution a real wallet signs
P2 = puzzle_for_synthetic_public_key(AugSchemeMPL.key_gen(bytes(32)).get_g1())
T_A = bytes32(b"\xa1" * 32)
CREATOR_PH = bytes32(b"\x77" * 32)
CREATE_COIN, CREATE_COIN_ANN = 51, 60


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def fresh_registry():
    reg0 = drv.make_registry(salt=0x21, creation_fee=1_000_000, treasury_ph=bytes32(b"\x55" * 32))
    _bundle, new_state = drv.registry_spend(reg0, "forge_registry_init", [])
    reg1 = reg0.advance([x.as_int() for x in new_state.as_iter()])
    parent = {"parent_coin_info": reg0.coin.parent_coin_info.hex(), "puzzle_hash": reg0.coin.puzzle_hash.hex(), "amount": 1}
    slots = {
        drv.MIN_KEY.hex(): {"key": drv.MIN_KEY.hex(), "launcher_id": ZERO_32.hex(), "left": drv.MIN_KEY.hex(), "right": drv.MAX_KEY.hex(),
                            "parent": parent, "parent_inner_hash": reg0.inner_hash.hex()},
        drv.MAX_KEY.hex(): {"key": drv.MAX_KEY.hex(), "launcher_id": ZERO_32.hex(), "left": drv.MIN_KEY.hex(), "right": drv.MAX_KEY.hex(),
                            "parent": parent, "parent_inner_hash": reg0.inner_hash.hex()},
    }
    return reg0, reg1, slots


def coin_announcements(coin_spends) -> set:
    """Every reserve-launcher COIN announcement id (sha256(coin id + message)) the spends emit,
    run locally. The CAT layer adds its own ring announcements (0xcb-prefixed) around a CAT
    launcher; only the launcher's message, which starts with the V14 tag, is compared."""
    out = set()
    for cs in coin_spends:
        puzzle = Program.from_bytes(bytes(cs.puzzle_reveal))
        for c in puzzle.run(Program.from_bytes(bytes(cs.solution))).as_iter():
            if c.first().atom is not None and c.first().as_int() == CREATE_COIN_ANN:
                message = bytes(c.rest().first().as_atom())
                if message.startswith(b"forge-reserve-v14"):
                    out.add(hashlib.sha256(bytes(cs.coin.name()) + message).digest())
    return out


def main() -> int:
    if not (drv.v14_available() and drv.registry_available()):
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py")
        return 2
    reg0, reg, slots = fresh_registry()
    fee = 5_000_000
    assets, reserves, weights, total_lp = [None, T_A], [100_000_000, 50_000], [1, 1], 50_000
    xch_coin = Coin(bytes32(b"\x31" * 32), P2.get_tree_hash(), uint64(2 + 100_000_000 + 1_000_000 + 49_999 + fee + 777))
    cat_outer = construct_cat_puzzle(CAT_MOD, T_A, P2).get_tree_hash()
    cat_grand = bytes32(b"\x32" * 32)
    cat_coin = Coin(drv.coin_id(cat_grand, cat_outer, 60_000), cat_outer, uint64(60_000))
    cat_lineage = LineageProof(cat_grand, P2.get_tree_hash(), uint64(60_000))

    print("the website lane (forge_v14_create.plan):")
    cfg = create.CreationConfig(assets, reserves, weights, 30, 5, bytes32(b"\x55" * 32), total_lp, "TXCH 🍕", "TXCH/A")
    plan = create.plan(reg, slots, cfg, create.CreatorXch(xch_coin, P2), {T_A: create.CreatorCat(T_A, cat_coin, P2, cat_lineage)},
                       CREATOR_PH, fee)
    web_bundle = create.finalize(plan, G2Element())
    check("the plan validates through consensus", drv.validate(web_bundle) is not None)
    web_launchers = [cs for cs in web_bundle.coin_spends
                     if cs.coin.puzzle_hash in (drv.reserve_launcher_full_hash(None), drv.reserve_launcher_full_hash(T_A))]
    check("  it spends one reserve launcher per asset", len(web_launchers) == 2)

    print("the deploy lane (make_pool with real coins + reserve_launcher_spends + register_solution):")
    shape = drv.make_pool(assets, reserves, total_lp=total_lp, leaves="forge", weights=weights, fee_bps=30, protocol_fee_bps=5,
                          protocol_ph=bytes32(b"\x55" * 32), launcher_parent=xch_coin.name())
    xch_launcher = Coin(xch_coin.name(), drv.RESERVE_LAUNCHER_HASH, uint64(reserves[0]))
    cat_launcher = Coin(cat_coin.name(), drv.reserve_launcher_full_hash(T_A), uint64(reserves[1]))
    reserve_coins = [
        (Coin(xch_launcher.name(), shape.reserves[0].inner_hash, uint64(reserves[0])), None, xch_coin.name()),
        (Coin(cat_launcher.name(), shape.reserves[1].full_hash, uint64(reserves[1])),
         LineageProof(cat_coin.name(), drv.RESERVE_LAUNCHER_HASH, uint64(reserves[1])), cat_coin.name(),
         LineageProof(cat_coin.parent_coin_info, P2.get_tree_hash(), cat_coin.amount)),
    ]
    pool = drv.make_pool(assets, reserves, total_lp=total_lp, leaves="forge", weights=weights, fee_bps=30, protocol_fee_bps=5,
                         protocol_ph=bytes32(b"\x55" * 32), launcher_parent=xch_coin.name(), reserve_coins=reserve_coins)
    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
    eve = Coin(xch_coin.name(), eve_ph, uint64(1))
    pool.extra["eve_coin_id"] = eve.name()
    deploy_launchers = drv.reserve_launcher_spends(pool)
    left = (drv.MIN_KEY, ZERO_32, drv.MIN_KEY)
    right = (drv.MAX_KEY, ZERO_32, drv.MAX_KEY)
    solution = drv.register_solution(pool, left, right)

    print("the two lanes agree:")
    check("the pool coin is the same coin", pool.coin == plan.pool.coin)
    check("the reserve coins are the same coins", [r.coin for r in pool.reserves] == [r.coin for r in plan.pool.reserves])
    check("the launcher spends' COIN announcements are byte-identical",
          coin_announcements(deploy_launchers) == coin_announcements(web_launchers) and len(coin_announcements(deploy_launchers)) == 2)
    check("the launcher coins themselves are the same coins",
          sorted(cs.coin.name() for cs in deploy_launchers) == sorted(cs.coin.name() for cs in web_launchers))
    web_reg = next(cs for cs in plan.router_spends if cs.coin == reg.coin)
    web_solution = list(Program.from_bytes(bytes(web_reg.solution)).as_iter())[2]
    web_leaf_solution = list(list(web_solution.as_iter())[2].as_iter())[0]
    check("the register solutions are byte-identical (grandparents included)",
          bytes(web_leaf_solution) == bytes(Program.to(solution)))
    check("  the grandparents are the creator's own coins", solution[5] == [xch_coin.name(), cat_coin.name()])

    # And the deploy lane's bundle validates too, assembled the way the deploy script does it.
    funding_conditions = [[CREATE_COIN, SINGLETON_LAUNCHER_HASH, 1], [CREATE_COIN, eve_ph, 1],
                          [CREATE_COIN, drv.RESERVE_LAUNCHER_HASH, reserves[0]], [CREATE_COIN, bytes32(OFFER_MOD_HASH), reg.creation_fee],
                          [CREATE_COIN, P2.get_tree_hash(), int(xch_coin.amount) - 2 - reserves[0] - reg.creation_fee - (total_lp - 1) - fee]]
    spends = [make_spend(xch_coin, P2, drv.p2_delegated_solution(funding_conditions))]
    spends.extend(drv.unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [drv.SpendableCAT(
        cat_coin, T_A, P2, drv.p2_delegated_solution([[CREATE_COIN, drv.RESERVE_LAUNCHER_HASH, reserves[1]],
                                                      [CREATE_COIN, P2.get_tree_hash(), 10_000, [P2.get_tree_hash()]]]),
        lineage_proof=cat_lineage)]).coin_spends)
    spends.extend(deploy_launchers)
    launcher = Coin(xch_coin.name(), SINGLETON_LAUNCHER_HASH, uint64(1))
    spends.append(make_spend(launcher, SINGLETON_LAUNCHER, Program.to([pool.coin.puzzle_hash, 1, [total_lp, eve.name()]])))
    spends.extend(drv.lp_eve_ring(pool, eve, bytes32(OFFER_MOD_HASH), total_lp, drv.genesis_action(pool)))
    spends.extend(drv.genesis_lp_settlement_spends(pool, eve, CREATOR_PH, total_lp))
    fee_coin = Coin(xch_coin.name(), bytes32(OFFER_MOD_HASH), uint64(reg.creation_fee))
    spends.append(make_spend(fee_coin, OFFER_MOD, Program.to([[pool.launcher_id, [reg.treasury_ph, reg.creation_fee, [reg.treasury_ph]]]])))
    for rec, value in ((slots[drv.MIN_KEY.hex()], drv.slot_value(left[0], left[1], left[2], right[0])),
                       (slots[drv.MAX_KEY.hex()], drv.slot_value(right[0], right[1], left[0], right[2]))):
        spends.append(drv.slot_spend(reg, value, reg0.coin, reg0.inner_hash)[1])
    bundle, _ = drv.registry_spend(reg, "forge_registry_register", solution, extra_spends=spends)
    try:
        conds, additions = drv.validate(bundle)
        check("the deploy lane's bundle validates through consensus", True, f"cost {conds.cost:,}")
        check("  and creates the pool coin at the registry's recomputed hash", (pool.coin.puzzle_hash, 1) in additions)
    except drv.Rejected as exc:
        check("the deploy lane's bundle validates through consensus", False, str(exc)[:80])

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} lanes-agree checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
