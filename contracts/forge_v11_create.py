#!/usr/bin/env python3
"""Create and register a V11 pool from a creator's own coins, keylessly (phase 8.1).

The operator flow (scripts/deploy-v11-testnet.py create-pool) builds the creation
bundle with the operator's wallet. This is the same bundle split along the key
boundary so a **creator** can make a pool through the interface while the router
holds no key:

  creator side (signed by the creator's wallet, e.g. Sage `sign_coin_spends`):
    * one XCH coin spent to create the launcher (memos: name, symbol), the LP
      eve, the XCH reserve if the pool holds XCH, the fee settlement, and change;
      it also ASSERTS the announcement of the genesis LP payment to the creator,
      so a bundle that mints the genesis LP anywhere else fails the creator's
      own spend;
    * one CAT coin per CAT asset, spent to that asset's reserve puzzle hash
      (with change back to the creator);

  router side (no key):
    * the launcher spend (kv [total_lp]) creating the eve singleton;
    * the LP eve ring minting the genesis supply to OFFER_MOD, whose spend pays
      the creator under nonce = launcher id (the payment the creator asserted);
    * the fee settlement spend paying the treasury under nonce = launcher id;
    * the two neighbour slot spends and the registry's `register` action.

Why the launcher's parent is the creator's XCH coin: the launcher id, and from it
the LP asset id, is then known to the creator BEFORE signing, which is what lets
the creator bind the genesis LP to their own address without a guard puzzle.
The creator picks the coin, so the coin id is theirs to know.

No RPC, no signing. `plan()` returns the unsigned creator spends and the router
spends; `finalize()` accepts the creator's signature (and, optionally, the
creator spends as the wallet actually signed them) and produces the bundle.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import CoinSpend, make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import (  # noqa: E402
    CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER, SINGLETON_LAUNCHER_HASH  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH  # noqa: E402
from chia_rs import Coin, G2Element, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v11_driver as drv  # noqa: E402
from forge_offer import ZERO_32  # noqa: E402
from forge_v11_offer import OfferRejected, pool_to_snapshot  # noqa: E402

OFFER_PH = bytes32(OFFER_MOD_HASH)
CREATE_COIN = 51
ASSERT_PUZZLE_ANNOUNCEMENT = 63


@dataclass(frozen=True)
class CreatorXch:
    coin: Coin
    puzzle: Program                 # the creator's own p2 puzzle for this coin


@dataclass(frozen=True)
class CreatorCat:
    asset_id: bytes32
    coin: Coin
    inner: Program                  # the creator's inner p2 puzzle
    lineage: LineageProof


@dataclass(frozen=True)
class CreationConfig:
    asset_ids: list                 # None is XCH
    reserves: list                  # genesis reserves, same order
    weights: list
    fee_bps: int
    protocol_fee_bps: int
    protocol_ph: bytes32
    total_lp: int
    name: str = ""
    symbol: str = ""
    dao_ph: bytes32 = ZERO_32       # V11.1: the DAO recipient (immutable) and the opening rate
    dao_fee_bps: int = 0


@dataclass
class CreationPlan:
    pool: drv.V11Pool
    registry_after: drv.Registry
    key: bytes32
    left: tuple
    right: tuple
    recipient_ph: bytes32
    creator_spends: list            # unsigned CoinSpends the creator's wallet signs
    router_spends: list             # CoinSpends nobody signs
    network_fee: int
    details: dict[str, Any] = field(default_factory=dict)

    def unsigned_bundle(self) -> SpendBundle:
        return SpendBundle([*self.creator_spends, *self.router_spends], G2Element())


def canonical(config: CreationConfig) -> CreationConfig:
    """Assets ascending by id with XCH first, as the puzzle requires."""
    order = sorted(range(len(config.asset_ids)), key=lambda i: bytes(32) if config.asset_ids[i] is None else bytes(config.asset_ids[i]))
    return replace(config, asset_ids=[config.asset_ids[i] for i in order], reserves=[int(config.reserves[i]) for i in order],
                   weights=[int(config.weights[i]) for i in order])


def lp_payment_announcement(lp_asset_id: bytes32, launcher_id: bytes32, recipient_ph: bytes32, amount: int) -> bytes32:
    """The announcement the genesis LP settlement makes when it pays the creator:
    sha256(CAT(lp, OFFER_MOD) puzzle hash + tree_hash((launcher_id, [[recipient, amount, [recipient]]])))."""
    settle_ph = construct_cat_puzzle(CAT_MOD, lp_asset_id, OFFER_MOD).get_tree_hash()
    return bytes32(drv.hashlib.sha256(bytes(settle_ph) + bytes(Program.to((launcher_id, [[recipient_ph, amount, [recipient_ph]]])).get_tree_hash())).digest())


def plan(registry: drv.Registry, slots: dict, config: CreationConfig, xch: CreatorXch, cats: dict,
         recipient_ph: bytes32, network_fee: int) -> CreationPlan:
    """Build the creation bundle around the creator's coins.

    `slots` is the registry's live slot list: key hex -> {key, launcher_id, left, right,
    parent (coin json), parent_inner_hash}, as the registry driver records it.
    `cats` maps asset id -> CreatorCat. The XCH coin must cover: 2 mojos (launcher, eve),
    the XCH reserve, the creation fee, total_lp - 1 of LP backing, and the network fee.
    """
    config = canonical(config)
    n = len(config.asset_ids)
    if not (1 <= n <= 10) or len(config.reserves) != n or len(config.weights) != n:
        raise OfferRejected("a pool has 1 to 10 assets with one reserve and one weight each")
    if any(r <= 0 for r in config.reserves) or config.total_lp <= 0:
        raise OfferRejected("genesis reserves and total_lp must be positive")
    for asset in config.asset_ids:
        if asset is not None and asset not in cats:
            raise OfferRejected(f"the creator must provide a coin of {asset.hex()[:8]} for its reserve")
    xch_index = config.asset_ids.index(None) if None in config.asset_ids else None
    xch_reserve = config.reserves[xch_index] if xch_index is not None else 0
    fee = registry.creation_fee
    need = 2 + xch_reserve + fee + (config.total_lp - 1) + network_fee
    if int(xch.coin.amount) < need:
        raise OfferRejected(f"the creator's XCH coin holds {xch.coin.amount}, the creation needs {need} "
                            f"(launcher, eve, XCH reserve {xch_reserve}, fee {fee}, LP backing {config.total_lp - 1}, network fee {network_fee})")
    for asset, cat in cats.items():
        i = config.asset_ids.index(asset)
        if int(cat.coin.amount) < config.reserves[i]:
            raise OfferRejected(f"the creator's {asset.hex()[:8]} coin holds {cat.coin.amount}, the reserve needs {config.reserves[i]}")

    # the pool, with the creator's XCH coin as the launcher's parent
    shape = drv.make_pool(config.asset_ids, config.reserves, total_lp=config.total_lp, leaves="forge", weights=config.weights,
                          fee_bps=config.fee_bps, protocol_fee_bps=config.protocol_fee_bps, protocol_ph=config.protocol_ph,
                          dao_ph=config.dao_ph, dao_fee_bps=config.dao_fee_bps,
                          launcher_parent=xch.coin.name())
    reserve_coins = []
    for i, asset in enumerate(config.asset_ids):
        if asset is None:
            reserve_coins.append((Coin(xch.coin.name(), shape.reserves[i].inner_hash, uint64(config.reserves[i])), None))
        else:
            cat = cats[asset]
            reserve_coins.append((Coin(cat.coin.name(), shape.reserves[i].full_hash, uint64(config.reserves[i])),
                                  LineageProof(cat.coin.parent_coin_info, cat.inner.get_tree_hash(), cat.coin.amount)))
    pool = drv.make_pool(config.asset_ids, config.reserves, total_lp=config.total_lp, leaves="forge", weights=config.weights,
                         fee_bps=config.fee_bps, protocol_fee_bps=config.protocol_fee_bps, protocol_ph=config.protocol_ph,
                          dao_ph=config.dao_ph, dao_fee_bps=config.dao_fee_bps,
                         launcher_parent=xch.coin.name(), reserve_coins=reserve_coins)
    launcher_id = pool.launcher_id
    key = drv.pool_key(pool.config())
    if key.hex() in slots:
        raise OfferRejected("a pool with this configuration is already registered")
    keys = sorted(bytes32.fromhex(k) for k in slots)
    left_key = max((k for k in keys if k < key), default=None)
    right_key = min((k for k in keys if k > key), default=None)
    if left_key is None or right_key is None:
        raise OfferRejected("the registry's slot list has no bracket for this key (is it initialized?)")
    left_rec, right_rec = slots[left_key.hex()], slots[right_key.hex()]
    left = (left_key, bytes32.fromhex(left_rec["launcher_id"]), bytes32.fromhex(left_rec["left"]))
    right = (right_key, bytes32.fromhex(right_rec["launcher_id"]), bytes32.fromhex(right_rec["right"]))

    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
    memos = [m.encode("utf-8") for m in (config.name, config.symbol) if m]
    change = int(xch.coin.amount) - need
    conditions = [
        [CREATE_COIN, SINGLETON_LAUNCHER_HASH, 1, memos] if memos else [CREATE_COIN, SINGLETON_LAUNCHER_HASH, 1],
        [CREATE_COIN, eve_ph, 1],
    ]
    if xch_index is not None:
        conditions.append([CREATE_COIN, shape.reserves[xch_index].inner_hash, xch_reserve, [launcher_id]])
    conditions.append([CREATE_COIN, OFFER_PH, fee])
    if change > 0:
        conditions.append([CREATE_COIN, xch.puzzle.get_tree_hash(), change, [xch.puzzle.get_tree_hash()]])
    # The creator's bind: the genesis LP must be paid to recipient_ph, or this spend fails.
    conditions.append([ASSERT_PUZZLE_ANNOUNCEMENT, lp_payment_announcement(pool.lp_asset_id, launcher_id, recipient_ph, config.total_lp)])
    creator_spends = [make_spend(xch.coin, xch.puzzle, drv.p2_delegated_solution(conditions))]
    for asset, cat in cats.items():
        i = config.asset_ids.index(asset)
        conds = [[CREATE_COIN, shape.reserves[i].inner_hash, config.reserves[i], [launcher_id]]]
        if int(cat.coin.amount) > config.reserves[i]:
            conds.append([CREATE_COIN, cat.inner.get_tree_hash(), int(cat.coin.amount) - config.reserves[i], [cat.inner.get_tree_hash()]])
        creator_spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
            cat.coin, asset, cat.inner, drv.p2_delegated_solution(conds), lineage_proof=cat.lineage)]).coin_spends)

    # router side
    launcher = Coin(xch.coin.name(), SINGLETON_LAUNCHER_HASH, uint64(1))
    launcher_spend = make_spend(launcher, SINGLETON_LAUNCHER, Program.to([pool.coin.puzzle_hash, 1, [config.total_lp]]))
    eve = Coin(xch.coin.name(), eve_ph, uint64(1))
    eve_spends = drv.lp_eve_ring(pool, eve, OFFER_PH, config.total_lp, drv.genesis_action(pool))
    lp_settlement = Coin(eve.name(), construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, OFFER_MOD).get_tree_hash(), uint64(config.total_lp))
    lp_pay = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        lp_settlement, pool.lp_asset_id, OFFER_MOD,
        Program.to([(launcher_id, [[recipient_ph, config.total_lp, [recipient_ph]]])]),
        lineage_proof=LineageProof(eve.parent_coin_info, drv.LP_MINT_INNER.get_tree_hash(), eve.amount))]).coin_spends
    fee_coin = Coin(xch.coin.name(), OFFER_PH, uint64(fee))
    fee_spend = make_spend(fee_coin, OFFER_MOD, Program.to([[launcher_id, [registry.treasury_ph, fee, [registry.treasury_ph]]]]))
    slot_spends = []
    for rec, value in ((left_rec, drv.slot_value(left[0], left[1], left[2], right[0])),
                       (right_rec, drv.slot_value(right[0], right[1], left[0], right[2]))):
        parent = Coin(bytes32.fromhex(rec["parent"]["parent_coin_info"]), bytes32.fromhex(rec["parent"]["puzzle_hash"]),
                      uint64(int(rec["parent"]["amount"])))
        _, spend = drv.slot_spend(registry, value, parent, bytes32.fromhex(rec["parent_inner_hash"]))
        slot_spends.append(spend)
    reg_bundle, reg_state = drv.registry_spend(registry, "forge_registry_register", drv.register_solution(pool, left, right),
                                               extra_spends=[])
    router_spends = [launcher_spend, *eve_spends, *lp_pay, fee_spend, *slot_spends, *reg_bundle.coin_spends]
    registry_after = registry.advance([x.as_int() for x in reg_state.as_iter()])     # RegistryState is [initialized, pool_count]
    return CreationPlan(pool, registry_after, key, left, right, recipient_ph, creator_spends, router_spends, network_fee, {
        "launcher_id": launcher_id.hex(), "lp_asset_id": pool.lp_asset_id.hex(), "key": key.hex(),
        "need_xch": need, "change": change, "genesis_lp": config.total_lp, "name": config.name, "symbol": config.symbol,
    })


def finalize(plan_: CreationPlan, signature: G2Element, signed_creator_spends: list | None = None) -> SpendBundle:
    """The bundle: the creator's spends (as signed) plus the router's, under the creator's
    aggregate signature. If the wallet returned the spends it signed, they replace the
    planned ones -- but they must be the same spends, or the plan is void."""
    creator = signed_creator_spends if signed_creator_spends is not None else plan_.creator_spends
    planned = {(cs.coin.name(), bytes(cs.puzzle_reveal), bytes(cs.solution)) for cs in plan_.creator_spends}
    got = {(cs.coin.name(), bytes(cs.puzzle_reveal), bytes(cs.solution)) for cs in creator}
    if planned != got:
        raise OfferRejected("the signed creator spends differ from the planned ones; re-plan before signing")
    bundle = SpendBundle([*creator, *plan_.router_spends], signature)
    try:
        drv.validate(bundle)
    except drv.Rejected as exc:
        raise OfferRejected(f"creation bundle fails consensus validation: {exc}") from exc
    return bundle


def record_patch(plan_: CreationPlan, registry_record: dict[str, Any]) -> dict[str, Any]:
    """What the registry driver's record must learn once the bundle confirms: the pool
    record (as scripts/deploy-v11-testnet.py writes it) and the registry's new coin,
    state and slot list. Applied by `commit_record`."""
    pool = plan_.pool
    reg_after = plan_.registry_after
    key = plan_.key.hex()
    left_key, right_key = plan_.left[0].hex(), plan_.right[0].hex()
    reg_coin_json = {"parent_coin_info": registry_record["coin"]["parent_coin_info"], "puzzle_hash": registry_record["coin"]["puzzle_hash"],
                     "amount": int(registry_record["coin"]["amount"])}
    reg_inner = reg_after.lineage.inner_puzzle_hash.hex()
    slots = dict(registry_record["slots"])
    slots[key] = {"key": key, "launcher_id": pool.launcher_id.hex(), "left": left_key, "right": right_key,
                  "parent": reg_coin_json, "parent_inner_hash": reg_inner}
    slots[left_key] = {**slots[left_key], "right": key, "parent": reg_coin_json, "parent_inner_hash": reg_inner}
    slots[right_key] = {**slots[right_key], "left": key, "parent": reg_coin_json, "parent_inner_hash": reg_inner}
    st = pool.state
    return {
        "pool": {
            "label": plan_.details.get("symbol") or plan_.details["launcher_id"][:12],
            "emoji": plan_.details.get("name", ""), "symbol": plan_.details.get("symbol", ""),
            "key": key, "lp_recipient_ph": plan_.recipient_ph.hex(),
            "launcher_parent": pool.launcher_parent.hex(), "launcher_id": pool.launcher_id.hex(), "lp_asset_id": pool.lp_asset_id.hex(),
            "asset_ids": [None if a is None else a.hex() for a in pool.asset_ids], "weights": pool.weights,
            "fee_bps": pool.fee_bps, "protocol_fee_bps": pool.protocol_fee_bps, "protocol_ph": pool.protocol_ph.hex(),
            "state": [st[0], st[1], st[2], st[3], bytes(st[4]).hex()],
            "coin": {"parent_coin_info": pool.coin.parent_coin_info.hex(), "puzzle_hash": pool.coin.puzzle_hash.hex(), "amount": 1},
            "lineage": {"parent_name": pool.lineage.parent_name.hex(), "inner_puzzle_hash": None, "amount": 1},
            "reserves": [{"coin": {"parent_coin_info": r.coin.parent_coin_info.hex(), "puzzle_hash": r.coin.puzzle_hash.hex(), "amount": int(r.coin.amount)},
                          "lineage": None if r.lineage is None else {"parent_name": r.lineage.parent_name.hex(),
                                                                     "inner_puzzle_hash": r.lineage.inner_puzzle_hash.hex(), "amount": int(r.lineage.amount)}}
                         for r in pool.reserves],
            "history": [], "created_by": "keyless-create",
        },
        "registry": {
            "state": list(reg_after.state),
            "coin": {"parent_coin_info": reg_after.coin.parent_coin_info.hex(), "puzzle_hash": reg_after.coin.puzzle_hash.hex(), "amount": 1},
            "lineage": {"parent_name": reg_after.lineage.parent_name.hex(), "inner_puzzle_hash": reg_inner, "amount": 1},
            "slots": slots,
        },
    }


def commit_record(record_path: str, patch: dict[str, Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply a `record_patch` to the registry driver's record once the bundle confirmed."""
    import json
    path = Path(record_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    pool = dict(patch["pool"])
    if result:
        pool["created"] = result
        pool["history"] = [result]
    if any(p["launcher_id"] == pool["launcher_id"] for p in state["pools"]):
        return {"success": True, "committed": False, "reason": "already recorded"}
    state["pools"].append(pool)
    state["registry"].update(patch["registry"])
    state.setdefault("log", []).append({"step": "create-pool", "label": pool["label"], "emoji": pool["emoji"], "launcher_id": pool["launcher_id"],
                                        "lp_asset_id": pool["lp_asset_id"], "via": "keyless-create", **(result or {})})
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return {"success": True, "committed": True, "pools": len(state["pools"])}


def plan_json(plan_: CreationPlan) -> dict[str, Any]:
    """What an API returns to a creator: the spends to sign and the pool they will own."""
    return {
        "creator_spends": [cs.to_json_dict() for cs in plan_.creator_spends],
        "router_spends": [cs.to_json_dict() for cs in plan_.router_spends],
        "pool": pool_to_snapshot(plan_.pool),
        "registry": {"key": plan_.key.hex(), "left_key": plan_.left[0].hex(), "right_key": plan_.right[0].hex(),
                     "coin_after": {"parent_coin_info": plan_.registry_after.coin.parent_coin_info.hex(),
                                    "puzzle_hash": plan_.registry_after.coin.puzzle_hash.hex(), "amount": 1},
                     "state_after": plan_.registry_after.state},
        **plan_.details,
    }
