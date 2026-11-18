#!/usr/bin/env python3
"""V19 creation through forge_stdin: DAO consent built inside the plan, rCAT reserves, the
V16 mirror cross-check, and wallet_coins finding layered rCAT coins. Offline.

1. DAO consent (DAO consent). The consent condition names the launcher id, which exists only
   inside `plan`, so the router hands `creator.dao_consent`:
     {coin, puzzle_reveal}   a coin at the DAO address; plan spends it back to itself with the
                             condition, and the wallet signs it with the rest;
     {from_creator_xch: true} the paying coin is at the DAO address; its conditions carry it.
   Both validate through consensus (`register` refuses a DAO-named pool without it); a DAO-named
   creation with no consent, a consent coin not at the DAO address, and from_creator_xch from a
   coin elsewhere are refused by name.
2. rCAT. `config.hidden_puzzle_hashes` reaches both pools in `plan`; a creator CAT of an rCAT
   asset takes H from the config, is spent through the layer's owner path; the bundle validates;
   the plan snapshot and the record patch carry H.
3. `config.from_pool`: a mirror whose config is not the V16 pool's market is refused.
4. wallet_coins.cat_coins_for_keys(hidden=...) finds CAT(asset, rev(H, owner)) coins.
Exit 0 all pass, 1 otherwise, 2 build absent.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key
from chia_rs import AugSchemeMPL, Coin, G2Element, PrivateKey, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin
import forge_v16_driver as drv16
import forge_v16_offer as v16
import forge_v19_create as create
import forge_v19_driver as drv
from _test_v19_create import CREATOR_PH, P2, fresh_registry
from forge_offer import ZERO_32

PLAIN = bytes32(b"\xa1" * 32)
# V19 (rCAT issuer binding): R is a real CHIP-0038 everything_with_singleton asset of ISSUER, and an
# rCAT creation carries the issuer's proof and attestation spend (creator.issuer_attestations).
ISSUER = drv.issuer_singleton(bytes32(b"\x5c" * 32))
NONCE = b"\x01"
R = drv.ews_asset_id(ISSUER[0], NONCE)
assert R > PLAIN, "asset order XCH < PLAIN < R"
H = bytes32(b"\x48" * 32)
TREASURY = bytes32(b"\x55" * 32)
DAO_P2 = puzzle_for_synthetic_public_key(AugSchemeMPL.key_gen(b"\x07" * 32).get_g1())
DAO_PH = DAO_P2.get_tree_hash()
FEE, G = 5_000_000, drv.MIN_GENESIS_LP
results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def run(payload):
    return forge_stdin.build(json.loads(json.dumps(payload)))


def coin_json(c: Coin) -> dict:
    return {"parent_coin_info": c.parent_coin_info.hex(), "puzzle_hash": c.puzzle_hash.hex(), "amount": int(c.amount)}


def registry_record(reg, slots) -> dict:
    return {"creation_fee": reg.creation_fee, "treasury_ph": reg.treasury_ph.hex(), "protocol_ph": reg.protocol_ph.hex(),
            "launcher_parent": reg.launcher_parent.hex(), "launcher_id": reg.launcher_id.hex(), "state": list(reg.state),
            "coin": coin_json(reg.coin),
            "lineage": {"parent_name": reg.lineage.parent_name.hex(),
                        "inner_puzzle_hash": None if reg.lineage.inner_puzzle_hash is None else reg.lineage.inner_puzzle_hash.hex(),
                        "amount": 1},
            "slots": slots}


def cat_coin(asset, amount, salt, hidden=None, inner=P2):
    under = drv.layered(hidden, inner)
    outer = construct_cat_puzzle(CAT_MOD, asset, under).get_tree_hash()
    grand = bytes32(bytes([salt]) * 32)
    coin = Coin(drv.coin_id(grand, outer, amount), outer, uint64(amount))
    return {"asset_id": asset.hex(), "coin": coin_json(coin), "inner_puzzle": bytes(inner).hex(),
            "lineage_proof": {"parent_name": grand.hex(), "inner_puzzle_hash": under.get_tree_hash().hex(), "amount": amount}}


def payload_for(assets, reserves, cats, xch_puzzle=P2, xch_salt=0x31, dao=None, dao_fee_bps=0, hiddens=None, extra_xch=9,
                xch_reserve=None):
    reg, slots = fresh_registry()
    xr = reserves[0] if xch_reserve is None else xch_reserve
    xch = Coin(bytes32(bytes([xch_salt]) * 32), xch_puzzle.get_tree_hash(), uint64(2 + xr + 1_000_000 + (G - 1) + FEE + extra_xch))
    cfg = {"asset_ids": [hx(a) for a in assets], "reserves": reserves, "weights": [1] * len(assets), "fee_bps": 30,
           "protocol_fee_bps": 5, "protocol_puzzle_hash": TREASURY.hex(), "total_lp": G, "name": "t", "symbol": "T"}
    if dao is not None:
        cfg["dao_puzzle_hash"], cfg["dao_fee_bps"] = dao.hex(), dao_fee_bps
    if hiddens is not None:
        cfg["hidden_puzzle_hashes"] = [h.hex() for h in hiddens]
    return reg, {"action": "prepare-create", "protocol_version": 20, "registry": registry_record(reg, slots), "config": cfg,
                 "creator": {"xch": {"coin": coin_json(xch), "puzzle_reveal": bytes(xch_puzzle).hex()}, "cats": cats},
                 "recipient_puzzle_hash": CREATOR_PH.hex(), "network_fee": FEE}


def attest(reg, payload, index):
    """creator.issuer_attestations for asset `index` of the payload's config: the issuer's proof and
    its singleton's spend sending the registry message for this creation's launcher."""
    from types import SimpleNamespace
    xch = payload["creator"]["xch"]["coin"]
    xch_coin = Coin(bytes32.fromhex(xch["parent_coin_info"]), bytes32.fromhex(xch["puzzle_hash"]), uint64(xch["amount"]))
    asset = bytes32.fromhex(payload["config"]["asset_ids"][index])
    hidden = bytes32.fromhex(payload["config"]["hidden_puzzle_hashes"][index])
    spend = drv.issuer_attest_spend(reg, SimpleNamespace(launcher_id=create.creation_launcher_id(xch_coin)), asset, hidden, ISSUER)
    payload["creator"]["issuer_attestations"] = [{"index": index, "launcher_id": ISSUER[0].hex(), "nonce": NONCE.hex(),
                                                  "inner_puzzle_hash": ISSUER[3].get_tree_hash().hex(),
                                                  "spend": spend.to_json_dict()}]
    return spend


def hx(a):
    return (ZERO_32 if a is None else a).hex()


def validates(out) -> tuple[bool, str, list]:
    from chia.types.coin_spend import CoinSpend
    spends = [CoinSpend.from_json_dict(cs) for cs in [*out["creator_spends"], *out.get("issuer_spends", []), *out["router_spends"]]]
    try:
        conds, adds = drv.validate(SpendBundle(spends, G2Element()))
        return True, f"cost {conds.cost:,}", adds
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:160], []


def refused(label, payload, needle=""):
    try:
        run(payload)
    except Exception as exc:  # noqa: BLE001
        return check(f"refused: {label}", needle in str(exc), str(exc)[:130])
    return check(f"refused: {label}", False, "ACCEPTED")


def consent_messages(out) -> int:
    """SEND_MESSAGE mode 23 conditions among the wallet-signed spends."""
    n = 0
    for cs in out["creator_spends"]:
        prog = Program.from_bytes(bytes.fromhex(cs["puzzle_reveal"].removeprefix("0x")))
        sol = Program.from_bytes(bytes.fromhex(cs["solution"].removeprefix("0x")))
        for c in prog.run(sol).as_iter():
            if c.first().atom is not None and c.first().as_int() == 66 and c.rest().first().as_int() == 0b010_111:
                n += 1
    return n


def main() -> int:
    if not (drv.v19_available() and drv.registry_available()):
        print("  [skip] V19 build outputs are absent")
        return 2

    print("1. DAO consent built inside the plan:")
    _, p = payload_for([None, PLAIN], [100_000_000, 50_000], [cat_coin(PLAIN, 60_000, 0x32)], dao=DAO_PH, dao_fee_bps=30)
    refused("a DAO-named creation without consent", p, "needs that DAO's consent")
    dao_coin = Coin(bytes32(b"\x3d" * 32), DAO_PH, uint64(1))
    with_coin = json.loads(json.dumps(p))
    with_coin["creator"]["dao_consent"] = {"coin": coin_json(dao_coin), "puzzle_reveal": bytes(DAO_P2).hex()}
    try:
        out = run(with_coin)
        ok, why, adds = validates(out)
        check("{coin, puzzle_reveal}: the bundle validates (register accepts the consent)", ok, why)
        check("  the consenting coin is spent among the wallet-signed spends, with the message",
              any(cs["coin"]["parent_coin_info"].removeprefix("0x") == dao_coin.parent_coin_info.hex() for cs in out["creator_spends"])
              and consent_messages(out) == 1)
        check("  and comes back whole to the DAO address", (DAO_PH, 1) in adds)
        check("  the pool names the DAO", out["pool"]["dao_puzzle_hash"] == DAO_PH.hex() and out["pool"]["dao_fee_bps"] == 30)
    except Exception as exc:  # noqa: BLE001
        check("{coin, puzzle_reveal}: the bundle validates (register accepts the consent)", False, f"{type(exc).__name__}: {str(exc)[:160]}")
    stranger = json.loads(json.dumps(with_coin))
    stranger["creator"]["dao_consent"]["coin"]["puzzle_hash"] = P2.get_tree_hash().hex()
    stranger["creator"]["dao_consent"]["puzzle_reveal"] = bytes(P2).hex()
    refused("a consent coin that is not at the DAO address", stranger, "not at the pool's DAO address")
    _, px = payload_for([None, PLAIN], [100_000_000, 50_000], [cat_coin(PLAIN, 60_000, 0x32)], xch_puzzle=DAO_P2, dao=DAO_PH,
                        dao_fee_bps=30)
    px["creator"]["dao_consent"] = {"from_creator_xch": True}
    try:
        out = run(px)
        ok, why, _ = validates(out)
        check("{from_creator_xch: true}: the bundle validates", ok, why)
        check("  the paying coin's own spend carries the message", consent_messages(out) == 1 and len(out["creator_spends"]) == 2)
    except Exception as exc:  # noqa: BLE001
        check("{from_creator_xch: true}: the bundle validates", False, f"{type(exc).__name__}: {str(exc)[:160]}")
    elsewhere = json.loads(json.dumps(p))
    elsewhere["creator"]["dao_consent"] = {"from_creator_xch": True}
    refused("from_creator_xch from a coin that is not at the DAO address", elsewhere, "not at the DAO address")
    _, plain = payload_for([None, PLAIN], [100_000_000, 50_000], [cat_coin(PLAIN, 60_000, 0x32)])
    plain["creator"]["dao_consent"] = {"from_creator_xch": True}
    refused("a consent for a pool naming no DAO", plain, "names no DAO")

    print("\n2. an rCAT reserve, H from the config only:")
    reg_r, pr = payload_for([None, PLAIN, R], [100_000_000, 50_000, 70_000],
                            [cat_coin(PLAIN, 60_000, 0x32), cat_coin(R, 90_000, 0x33, hidden=H)], hiddens=[ZERO_32, ZERO_32, H])
    attest(reg_r, pr, 2)
    try:
        out = run(pr)
        ok, why, adds = validates(out)
        check("the creation validates (the coin carries no H of its own; the config's is used)", ok, why)
        check("the plan snapshot returns hidden_puzzle_hashes", out["pool"]["hidden_puzzle_hashes"] == [ZERO_32.hex(), ZERO_32.hex(), H.hex()])
        r_ph = bytes32.fromhex(out["pool"]["reserves"][2]["coin"]["puzzle_hash"])
        r_inner = bytes32.fromhex(out["pool"]["reserve_inner_puzzle_hashes"][2])
        rev = drv.REVOCATION_LAYER.curry(drv.REVOCATION_LAYER_HASH, bytes(H), r_inner).get_tree_hash()
        check("the R reserve is born at CAT(R, rev(H, reserve inner))",
              (r_ph, 70_000) in adds and r_ph == construct_cat_puzzle(CAT_MOD, R, Program.to(rev)).get_tree_hash_precalc(rev))
        check("R's change returns to the creator, layered",
              (construct_cat_puzzle(CAT_MOD, R, drv.layered(H, P2)).get_tree_hash(), 20_000) in adds)
    except Exception as exc:  # noqa: BLE001
        check("the creation validates (the coin carries no H of its own; the config's is used)", False, f"{type(exc).__name__}: {str(exc)[:160]}")
    # the same through the plan, with create (signature not checked offline) and the record patch
    reg, slots = fresh_registry()
    cfg = create.CreationConfig([None, PLAIN, R], [100_000_000, 50_000, 70_000], [1, 1, 1], 30, 5, TREASURY, G,
                                hidden_puzzle_hashes=[ZERO_32, ZERO_32, H])
    xch = create.CreatorXch(Coin(bytes32(b"\x31" * 32), P2.get_tree_hash(), uint64(2 + 100_000_000 + 1_000_000 + (G - 1) + FEE)), P2)

    def creator(asset, amount, salt, hidden=ZERO_32):
        j = cat_coin(asset, amount, salt, hidden=hidden)
        c = Coin(bytes32.fromhex(j["coin"]["parent_coin_info"]), bytes32.fromhex(j["coin"]["puzzle_hash"]), uint64(amount))
        lin = LineageProof(bytes32.fromhex(j["lineage_proof"]["parent_name"]), bytes32.fromhex(j["lineage_proof"]["inner_puzzle_hash"]),
                           uint64(amount))
        return create.CreatorCat(asset, c, P2, lin, hidden=hidden)

    from types import SimpleNamespace
    issuer_spend = drv.issuer_attest_spend(reg, SimpleNamespace(launcher_id=create.creation_launcher_id(xch.coin)), R, H, ISSUER)
    plan = create.plan(reg, slots, cfg, xch, {PLAIN: creator(PLAIN, 60_000, 0x32), R: creator(R, 90_000, 0x33, H)}, CREATOR_PH, FEE,
                       issuer_attestations=(create.IssuerAttestation(R, ISSUER[0], NONCE, ISSUER[3].get_tree_hash(), issuer_spend),))
    check("both make_pool calls in plan carry H (shape and real pool agree)", plan.pool.hiddens == [ZERO_32, ZERO_32, H]
          and plan.pool.reserves[2].full_hash == construct_cat_puzzle(CAT_MOD, R, drv.layered(H, plan.pool.reserves[2].inner)).get_tree_hash())
    patch = create.record_patch(plan, {"coin": coin_json(reg.coin), "slots": slots, "launcher_id": reg.launcher_id.hex()})
    check("record_patch stores hidden_puzzle_hashes", patch["pool"]["hidden_puzzle_hashes"] == [ZERO_32.hex(), ZERO_32.hex(), H.hex()])
    unlayered = json.loads(json.dumps(pr))
    unlayered["creator"]["cats"][1] = cat_coin(R, 90_000, 0x33)          # a PLAIN coin of R for the (R, H) market
    refused("a plain R coin for the (R, H) reserve", unlayered, "is not CAT(asset, rev(H, inner))")

    print("\n3. config.from_pool: the mirror must be the V16 market:")
    v1 = drv16.make_pool([None, PLAIN], [10_000_000_000, 500_000], total_lp=2_000_000, leaves="forge", salt=0x61, fee_bps=30,
                         protocol_ph=TREASURY, last_height=10)
    _, pm = payload_for([None, PLAIN], [100_000_000, 50_000], [cat_coin(PLAIN, 60_000, 0x32)])
    pm["config"]["from_pool"] = v1.launcher_id.hex()
    pm["config"]["from_pool_snapshot"] = v16.pool_to_snapshot(v1)
    try:
        out = run(pm)
        check("a matching mirror plans", bool(out.get("pool")))
    except Exception as exc:  # noqa: BLE001
        check("a matching mirror plans", False, f"{type(exc).__name__}: {str(exc)[:140]}")
    wrong_fee = json.loads(json.dumps(pm))
    wrong_fee["config"]["fee_bps"] = 100
    refused("a mirror with another LP fee", wrong_fee, "LP fee")
    wrong_w = json.loads(json.dumps(pm))
    wrong_w["config"]["weights"] = [2, 1]
    refused("a mirror with other weights", wrong_w, "weights")
    wrong_id = json.loads(json.dumps(pm))
    wrong_id["config"]["from_pool"] = "ab" * 32
    refused("from_pool naming another pool than the snapshot", wrong_id, "not the pool from_pool names")
    no_record = json.loads(json.dumps(pm))
    no_record["config"].pop("from_pool_snapshot")
    no_record["v16_record_path"] = "does-not-exist.json"
    no_record["v17_record_path"] = "does-not-exist-either.json"   # V19 also reads the V17 record
    refused("from_pool with no V16 record to read it from", no_record, "no V16 record")

    print("\n3b. config.from_pool naming a V17 pool (V17 -> V19 mirror):")
    try:   # optional: the public repository ships V19 and not the frozen V17; this case skips there
        import forge_v17_driver as drv17
        import forge_v17_offer as v17
    except ImportError:
        drv17 = v17 = None
        print("   skipped (no V17 in this tree)")
    if drv17 is not None:
        v2 = drv17.make_pool([None, PLAIN], [10_000_000_000, 500_000], total_lp=2_000_000, leaves="forge", salt=0x62, fee_bps=30,
                             protocol_ph=TREASURY, last_height=10)
        _, pm17 = payload_for([None, PLAIN], [100_000_000, 50_000], [cat_coin(PLAIN, 60_000, 0x32)])
        pm17["config"]["from_pool"] = v2.launcher_id.hex()
        pm17["config"]["from_pool_snapshot"] = v17.pool_to_snapshot(v2)
        try:
            out = run(pm17)
            check("a matching mirror of a V17 pool plans", bool(out.get("pool")))
        except Exception as exc:  # noqa: BLE001
            check("a matching mirror of a V17 pool plans", False, f"{type(exc).__name__}: {str(exc)[:140]}")
        w17 = json.loads(json.dumps(pm17))
        w17["config"]["fee_bps"] = 100
        refused("a mirror of a V17 pool with another LP fee", w17, "V17 pool's")
        v2r = drv17.make_pool([None, PLAIN, R], [10_000_000_000, 500_000, 700_000], total_lp=2_000_000, leaves="forge", salt=0x63,
                              fee_bps=30, protocol_ph=TREASURY, last_height=10, hidden_puzzle_hashes=[ZERO_32, ZERO_32, H])
        _, plain_r = payload_for([None, PLAIN, R], [100_000_000, 50_000, 70_000],
                                 [cat_coin(PLAIN, 60_000, 0x32), cat_coin(R, 90_000, 0x33)])
        plain_r["config"]["from_pool"] = v2r.launcher_id.hex()
        plain_r["config"]["from_pool_snapshot"] = v17.pool_to_snapshot(v2r)
        refused("a plain mirror of an rCAT V17 pool (H must carry over)", plain_r, "hidden puzzle hashes")
    old_snap = json.loads(json.dumps(pm))
    old_snap["config"]["from_pool_snapshot"]["protocol_version"] = 16
    refused("from_pool_snapshot of another revision (not 17 or 18)", old_snap, "protocol-16")

    print("\n4. wallet_coins finds layered rCAT coins:")
    import wallet_coins as wc
    sk = PrivateKey.from_bytes(bytes([0x62]) * 32)
    inner = puzzle_for_synthetic_public_key(sk.get_g1())
    layered = construct_cat_puzzle(CAT_MOD, R, drv.layered(H, inner))
    grand = Coin(bytes32(b"\x01" * 32), layered.get_tree_hash(), uint64(500))
    held = Coin(grand.name(), layered.get_tree_hash(), uint64(500))

    def rec(coin, spent, height=0):
        return {"coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(), "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
                         "amount": int(coin.amount)}, "spent": spent, "spent_block_index": height, "confirmed_block_index": 1}

    class Node:
        def coin_records_by_puzzle_hashes(self, hashes):
            wanted = {bytes(h) for h in hashes}
            return [rec(held, False)] if bytes(held.puzzle_hash) in wanted else []

        def rpc(self, route, body):
            assert route == "get_coin_records_by_names", route
            return {"coin_records": [rec(grand, True, 7)]}

    found = wc.cat_coins_for_keys(Node(), [bytes(sk.get_g1()).hex()], [R.hex()], hidden={R.hex(): H.hex()})
    coins = found.get(R.hex()) or []
    check("the layered coin is found at CAT(R, rev(H, owner))", len(coins) == 1 and coins[0]["coin"]["puzzle_hash"] == held.puzzle_hash.hex())
    check("with the owner's inner puzzle and the config's H",
          bool(coins) and coins[0]["inner_puzzle"] == bytes(inner).hex() and coins[0]["hidden_puzzle_hash"] == H.hex())
    check("its lineage names the layered inner (the parent sat at the wallet's own layered address)",
          bool(coins) and coins[0]["lineage_proof"]["inner_puzzle_hash"] == drv.layered(H, inner).get_tree_hash().hex())
    plain_found = wc.cat_coins_for_keys(Node(), [bytes(sk.get_g1()).hex()], [R.hex()])
    check("without H the layered coin is not mistaken for a plain one", (plain_found.get(R.hex()) or []) == [])

    print(f"\n{sum(results)}/{len(results)} create-rCAT checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
