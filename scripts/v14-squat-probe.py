#!/usr/bin/env python3
"""Push the fourth review's R-1 construction at the LIVE V14 registry and let the node answer.

The V13 gap: a registration whose reserve parents name coins nobody creates was accepted,
and the slot it took could never be spent. Offline, V14 refuses it (12,
ASSERT_ANNOUNCE_CONSUMED_FAILED) and V13 accepts it (_test_v14_before_after.py). This
builds the same construction against the registry on testnet11 -- the funding coin pays
the fee and the genesis mint exactly as an honest create does, but creates NO reserve
launchers and spends none -- and pushes it. Nothing can move: the node either refuses it,
which is the claim, or accepts it, which would be a finding.

    python scripts/v14-squat-probe.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

spec = importlib.util.spec_from_file_location("deploy_v14", ROOT / "scripts" / "deploy-v14-testnet.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
drv = d.drv
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle  # noqa: E402
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER, SINGLETON_LAUNCHER_HASH  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402


def main() -> int:
    state = d.load()
    wallet = d.Wallet()
    reg = d.registry_from(state)
    # A market nobody has registered: TXCH/T6 at 31 bps, tiny "reserves" the squatter never funds.
    assets = [None, d.resolve_asset("T6", state)]
    reserves, weights, total_lp = [1_000_000, 100], [1, 1], 100
    fee = reg.creation_fee
    # Only the launcher, the eve, the fee and the LP backing are paid: the XCH "reserve" is not.
    funding, funding_puzzle = wallet.xch_coin(2 + fee + total_lp + d.TX_FEE)
    shape = drv.make_pool(assets, reserves, total_lp=total_lp, leaves="forge", weights=weights, fee_bps=31,
                          protocol_fee_bps=5, protocol_ph=wallet.puzzle_hash, launcher_parent=funding.name())
    # The V13 construction on V14 terms: grandparents that ARE real coins (the funding coin and
    # some wallet CAT coin), so the derived parents are well-formed -- but no launcher is ever
    # created or spent, so the reserves those parents would name never exist.
    cat, cat_inner, cat_lineage = wallet.cat_coin(assets[1], 1)
    xch_launcher = Coin(funding.name(), drv.RESERVE_LAUNCHER_HASH, uint64(reserves[0]))
    cat_launcher = Coin(cat.name(), drv.reserve_launcher_full_hash(assets[1]), uint64(reserves[1]))
    reserve_coins = [
        (Coin(xch_launcher.name(), shape.reserves[0].inner_hash, uint64(reserves[0])), None, funding.name()),
        (Coin(cat_launcher.name(), shape.reserves[1].full_hash, uint64(reserves[1])),
         d.LineageProof(cat.name(), drv.RESERVE_LAUNCHER_HASH, uint64(reserves[1])), cat.name(),
         d.LineageProof(cat.parent_coin_info, cat_inner.get_tree_hash(), cat.amount)),
    ]
    pool = drv.make_pool(assets, reserves, total_lp=total_lp, leaves="forge", weights=weights, fee_bps=31,
                         protocol_fee_bps=5, protocol_ph=wallet.puzzle_hash, launcher_parent=funding.name(),
                         reserve_coins=reserve_coins)
    key = drv.pool_key(pool.config())
    if key.hex() in state["registry"]["slots"]:
        raise SystemExit("that key is registered already; pick another config")
    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
    change = int(funding.amount) - 1 - 1 - fee - (total_lp - 1) - d.TX_FEE
    conditions = [[51, SINGLETON_LAUNCHER_HASH, 1, [b"squat probe"]], [51, eve_ph, 1],
                  [51, bytes32(OFFER_MOD_HASH), fee], [51, wallet.puzzle_hash, change, [wallet.puzzle_hash]]]
    funding_spend = make_spend(funding, funding_puzzle, drv.p2_delegated_solution(conditions))
    launcher = Coin(funding.name(), SINGLETON_LAUNCHER_HASH, uint64(1))
    eve = Coin(funding.name(), eve_ph, uint64(1))
    pool.extra["eve_coin_id"] = eve.name()
    launcher_spend = make_spend(launcher, SINGLETON_LAUNCHER, d.Program.to([pool.coin.puzzle_hash, 1, [total_lp, eve.name()]]))
    eve_spends = list(drv.lp_eve_ring(pool, eve, bytes32(OFFER_MOD_HASH), total_lp, drv.genesis_action(pool)))
    eve_spends.extend(drv.genesis_lp_settlement_spends(pool, eve, wallet.puzzle_hash, total_lp))
    fee_coin = Coin(funding.name(), bytes32(OFFER_MOD_HASH), uint64(fee))
    fee_spend = make_spend(fee_coin, OFFER_MOD, d.Program.to([[pool.launcher_id, [reg.treasury_ph, fee, [reg.treasury_ph]]]]))
    left_rec, right_rec = d.slot_bracket(state, key)
    left = (bytes32.fromhex(left_rec["key"]), bytes32.fromhex(left_rec["launcher_id"]), bytes32.fromhex(left_rec["left"]))
    right = (bytes32.fromhex(right_rec["key"]), bytes32.fromhex(right_rec["launcher_id"]), bytes32.fromhex(right_rec["right"]))
    slot_spends = []
    for rec, value in ((left_rec, drv.slot_value(left[0], left[1], left[2], right[0])),
                       (right_rec, drv.slot_value(right[0], right[1], left[0], right[2]))):
        slot_spends.append(drv.slot_spend(reg, value, d.coin_from_json(rec["parent"]), bytes32.fromhex(rec["parent_inner_hash"]))[1])
    # NO reserve launcher spends: the V13 construction.
    bundle, _ = drv.registry_spend(reg, "forge_registry_register", drv.register_solution(pool, left, right),
                                   extra_spends=[funding_spend, launcher_spend, *eve_spends, fee_spend, *slot_spends])
    print(f"squat probe: {len(bundle.coin_spends)} spends, reserves never created, key {key.hex()[:12]}")
    try:
        drv.validate(bundle)
        print("  offline validator: ACCEPTED (unexpected)")
    except drv.Rejected as exc:
        print(f"  offline validator: refused, {exc}")
    signed = wallet.sign(list(bundle.coin_spends))
    verdict = d.rpc("push_tx", {"spend_bundle": signed})
    print(f"  node push_tx: {json.dumps(verdict)}")
    ok = not verdict.get("success")
    print("R-1 on the live chain:", "the node REFUSES the unfunded registration" if ok else "ACCEPTED -- a finding")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
