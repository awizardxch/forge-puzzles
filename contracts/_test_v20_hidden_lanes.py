#!/usr/bin/env python3
"""The V20 lanes carry the rCAT hidden puzzle hash (CHIP-0038, PoolConfig field 10), offline.

_test_v20_rcat.py proves the PUZZLES for a pool holding a revocable CAT R under H; this
suite proves the LANES a user actually touches carry H from the pool's CONFIG:
  * a snapshot round-trips `hidden_puzzle_hashes` (and an old snapshot without it is plain);
    a snapshot naming the wrong H does not rebuild;
  * creation: a creator funds the R reserve from their own layered coin, the reserve launcher
    is layered, the bundle validates, R's change comes back layered; the record patch carries
    H and the index rebuilds the pool from it; forge_stdin's prepare-create takes H from the
    config and the creator coin, and refuses a coin of the wrong market;
  * forge_stdin swap (into and out of R), add and remove settle against layered settlement
    coins at the config's H and pay R out layered; a plain-R offer is not found as R's input;
  * routes through such a pool are no longer gated in forge_stdin: the route composer threads H
    (forge_v20_route.route_hiddens); _test_v20_rcat_routes.py proves every route position.
Exit 0 all pass, 1 otherwise, 2 build absent.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats
from chia.wallet.conditions import CreateCoin
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.trading.offer import OFFER_MOD_HASH, Offer
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_math
import forge_stdin
import forge_v20_create as create
import forge_v20_driver as drv
import forge_v20_index as index
import forge_v20_offer as v20
from _test_v20_create import CREATOR_PH, P2, fresh_registry
from forge_offer import ZERO_32

IDENTITY = Program.to(1)
TRADER_PH = bytes32(b"\x77" * 32)
ROUTER_PH = bytes32(b"\x66" * 32)
PLAIN = bytes32(b"\xa1" * 32)
# V20 (rCAT issuer binding): R is a real CHIP-0038 everything_with_singleton asset of ISSUER, so a
# creation can carry the issuer's proof and attestation (forge_v20_create.IssuerAttestation).
ISSUER = drv.issuer_singleton(bytes32(b"\x5c" * 32))
NONCE = b"\x01"
R = drv.ews_asset_id(ISSUER[0], NONCE)
assert R > PLAIN, "asset order XCH < PLAIN < R"
H = bytes32(b"\x48" * 32)
HEIGHT = 4_700_000
results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def run(payload):
    return forge_stdin.build(json.loads(json.dumps(payload)))


def hiddens(pool) -> dict:
    return {a: h for a, h in zip(pool.asset_ids, pool.hiddens)}


def full_ph(asset, inner_ph, hidden=None) -> bytes32:
    """`inner_ph` as a coin of `asset` is created at: bare for XCH, CAT-wrapped, and under the
    revocation layer with `hidden` for an rCAT."""
    if asset is None:
        return inner_ph
    inner = inner_ph if drv.is_plain(hidden) else drv.REVOCATION_LAYER.curry(drv.REVOCATION_LAYER_HASH, bytes(hidden), inner_ph).get_tree_hash()
    return construct_cat_puzzle(CAT_MOD, asset, Program.to(inner)).get_tree_hash_precalc(inner)


def paid_to(bundle, ph, asset=None, hidden=None) -> int:
    target = full_ph(asset, ph, hidden)
    total = 0
    for cs in bundle.coin_spends:
        out = Program.from_bytes(bytes(cs.puzzle_reveal)).run(Program.from_bytes(bytes(cs.solution)))
        for c in out.as_iter():
            if c.first().atom is not None and c.first().as_int() == 51 and bytes32(c.rest().first().as_atom()) == target:
                total += c.rest().rest().first().as_int()
    return total


def fabricate(offered: dict, requested: dict, salt: int, hide: dict) -> Offer:
    """A trader offer whose coins of an rCAT are layered with `hide[asset]`: the trader's coin is
    CAT(asset, rev(H, (1))), spent through the layer's owner path, so the settlement it creates
    is CAT(asset, rev(H, OFFER_MOD)) -- the coin a wallet holding R makes. Requested payments'
    announcements are over the settlement puzzle hash at the same H."""
    coins = []
    for i, (asset, amount) in enumerate(offered.items()):
        if asset is None:
            coin = Coin(bytes32(bytes([salt, i]) * 16), IDENTITY.get_tree_hash(), uint64(amount))
        else:
            outer = construct_cat_puzzle(CAT_MOD, asset, drv.layered(hide.get(asset), IDENTITY)).get_tree_hash()
            grand = bytes32(bytes([salt, 0x10 + i]) * 16)
            coin = Coin(drv.coin_id(grand, outer, amount), outer, uint64(amount))
        coins.append((asset, coin))
    notarized = Offer.notarize_payments({a: [CreateCoin(TRADER_PH, uint64(v), [TRADER_PH])] for a, v in requested.items()},
                                        [c for _, c in coins])
    announcements = []
    for asset, group in notarized.items():
        settle = v20.settle_ph(asset, hide.get(asset))
        msg = Program.to((group[0].nonce, [p.as_condition_args() for p in group])).get_tree_hash()
        announcements.append([63, bytes32(drv.hashlib.sha256(bytes(settle) + bytes(msg)).digest())])
    spends, rings = [], {}
    for i, (asset, coin) in enumerate(coins):
        conditions = [[51, OFFER_MOD_HASH, int(offered[asset])], *announcements]
        if asset is None:
            spends.append(make_spend(coin, IDENTITY, Program.to(conditions)))
            continue
        h = hide.get(asset)
        grand = bytes32(bytes([salt, 0x10 + i]) * 16)
        rings.setdefault(asset, []).append(SpendableCAT(
            coin, asset, drv.layered(h, IDENTITY), drv.layered_solution(h, IDENTITY, conditions),
            lineage_proof=LineageProof(grand, drv.layered(h, IDENTITY).get_tree_hash(), coin.amount)))
    for ring in rings.values():
        spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, ring).coin_spends)
    driver = {a: PuzzleInfo({"type": "CAT", "tail": "0x" + a.hex()}) for a in requested if a is not None}
    return Offer(notarized, SpendBundle(spends, G2Element()), driver)


def rcat_pool(salt=0x41):
    return drv.make_pool([None, PLAIN, R], [10_000_000_000, 20_000_000, 30_000_000], total_lp=5_000_000, leaves="forge",
                         salt=salt, protocol_ph=bytes32(b"\x55" * 32), hidden_puzzle_hashes=[ZERO_32, ZERO_32, H],
                         last_height=HEIGHT - 10)


def creator_rcat(amount: int, salt: int) -> create.CreatorCat:
    outer = construct_cat_puzzle(CAT_MOD, R, drv.layered(H, P2)).get_tree_hash()
    grand = bytes32(bytes([salt]) * 32)
    coin = Coin(drv.coin_id(grand, outer, amount), outer, uint64(amount))
    return create.CreatorCat(R, coin, P2, LineageProof(grand, drv.layered(H, P2).get_tree_hash(), uint64(amount)), hidden=H)


def issuer_spend(reg, xch_coin, asset=None, hidden=None, issuer=ISSUER):
    """The issuer singleton's attestation spend for a creation paid from `xch_coin` (the pool
    launcher id follows from it) against the registry coin `reg.coin`."""
    from types import SimpleNamespace
    pool_ref = SimpleNamespace(launcher_id=create.creation_launcher_id(xch_coin))
    return drv.issuer_attest_spend(reg, pool_ref, asset or R, hidden or H, issuer)


def attestation(spend=None, nonce=NONCE, issuer=ISSUER) -> create.IssuerAttestation:
    return create.IssuerAttestation(R, issuer[0], nonce, issuer[3].get_tree_hash(), spend)


def creator_plain(asset, amount: int, salt: int) -> create.CreatorCat:
    outer = construct_cat_puzzle(CAT_MOD, asset, P2).get_tree_hash()
    grand = bytes32(bytes([salt]) * 32)
    coin = Coin(drv.coin_id(grand, outer, amount), outer, uint64(amount))
    return create.CreatorCat(asset, coin, P2, LineageProof(grand, P2.get_tree_hash(), uint64(amount)))


def main() -> int:
    if not (drv.v20_available() and drv.registry_available()):
        print("  [skip] V20 build outputs are absent")
        return 2

    print("snapshots carry hidden_puzzle_hashes:")
    pool = rcat_pool()
    snap = json.loads(json.dumps(v20.pool_to_snapshot(pool)))
    check("the snapshot names one H per asset", snap["hidden_puzzle_hashes"] == [ZERO_32.hex(), ZERO_32.hex(), H.hex()])
    back = v20.snapshot_to_pool(snap)
    check("it rebuilds the rCAT pool with that H", back.hiddens == [ZERO_32, ZERO_32, H] and back.coin == pool.coin)
    wrong = {**snap, "hidden_puzzle_hashes": [ZERO_32.hex(), ZERO_32.hex(), (b"\x49" * 32).hex()]}
    try:
        v20.snapshot_to_pool(wrong)
        check("a snapshot naming another H does not rebuild", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("a snapshot naming another H does not rebuild", True, str(exc)[:70])
    plain = drv.make_pool([None, PLAIN], [1_000_000_000, 1_000_000], leaves="forge", salt=0x42, last_height=HEIGHT - 10)
    old = {k: v for k, v in v20.pool_to_snapshot(plain).items() if k != "hidden_puzzle_hashes"}
    check("an old snapshot without the field is all plain", v20.snapshot_to_pool(old).hiddens == [ZERO_32, ZERO_32])

    print("\ncreation of XCH/PLAIN/R with the R reserve funded from the creator's layered coin:")
    reg, slots = fresh_registry()
    fee, G = 5_000_000, drv.MIN_GENESIS_LP
    cfg = create.CreationConfig([None, PLAIN, R], [100_000_000, 50_000, 70_000], [1, 1, 1], 30, 5, bytes32(b"\x55" * 32), G,
                                "rc", "RC", hidden_puzzle_hashes=[ZERO_32, ZERO_32, H])
    xch = create.CreatorXch(Coin(bytes32(b"\x31" * 32), P2.get_tree_hash(), uint64(2 + 100_000_000 + 1_000_000 + (G - 1) + fee + 9)), P2)
    cats = {PLAIN: creator_plain(PLAIN, 60_000, 0x32), R: creator_rcat(90_000, 0x33)}
    att = attestation(issuer_spend(reg, xch.coin))
    p = create.plan(reg, slots, cfg, xch, cats, CREATOR_PH, fee, issuer_attestations=(att,))
    bundle = create.finalize(p, G2Element())
    check("the issuer's attestation spend is in the bundle, apart from the creator's spends",
          att.spend in bundle.coin_spends and att.spend not in p.creator_spends and p.issuer_spends == [att.spend])
    try:
        conds, additions = drv.validate(bundle)
        check("the creation bundle validates", True, f"cost {conds.cost:,}")
    except Exception as exc:  # noqa: BLE001
        check("the creation bundle validates", False, str(exc)[:160])
        additions = []
    check("the pool's config carries H", p.pool.hiddens == [ZERO_32, ZERO_32, H])
    check("the R reserve is born at its layered full hash",
          (p.pool.reserves[2].full_hash, 70_000) in additions and p.pool.reserves[2].full_hash == full_ph(R, p.pool.reserves[2].inner_hash, H))
    check("the R reserve launcher is layered", any(c.puzzle_hash == drv.reserve_launcher_full_hash(R, H) for c, _ in []) or
          any(cs.coin.puzzle_hash == drv.reserve_launcher_full_hash(R, H) for cs in bundle.coin_spends))
    check("R's change comes back to the creator, layered", paid_to(bundle, P2.get_tree_hash(), R, H) == 20_000)
    check("PLAIN's change comes back plain", paid_to(bundle, P2.get_tree_hash(), PLAIN) == 10_000)
    patch = create.record_patch(p, {"coin": {"parent_coin_info": reg.coin.parent_coin_info.hex(), "puzzle_hash": reg.coin.puzzle_hash.hex(),
                                             "amount": 1}, "slots": slots, "launcher_id": reg.launcher_id.hex()})
    check("the record patch carries H", patch["pool"]["hidden_puzzle_hashes"] == [ZERO_32.hex(), ZERO_32.hex(), H.hex()])
    try:
        rebuilt = index.pool_from_record(json.loads(json.dumps(patch["pool"])))
        check("the index rebuilds the rCAT pool from its record", rebuilt.inner_hash == p.pool.inner_hash and rebuilt.hiddens[2] == H)
    except Exception as exc:  # noqa: BLE001
        check("the index rebuilds the rCAT pool from its record", False, str(exc)[:120])
    bad = {**cats, R: create.CreatorCat(R, cats[R].coin, P2, cats[R].lineage)}       # a plain coin record for the rCAT market
    try:
        create.plan(reg, slots, cfg, xch, bad, CREATOR_PH, fee, issuer_attestations=(att,))
        check("a creator coin of another market (plain R for (R, H)) is refused", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("a creator coin of another market (plain R for (R, H)) is refused", "hidden puzzle hash" in str(exc), str(exc)[:90])

    print("\nthe issuer binding through the plan:")

    def plan_refused(label, needle, **kw):
        try:
            create.plan(reg, slots, cfg, xch, cats, CREATOR_PH, fee, **kw)
            check(f"refused: {label}", False, "accepted")
        except create.OfferRejected as exc:
            check(f"refused: {label}", needle in str(exc), str(exc)[:110])
        except Exception as exc:  # noqa: BLE001 -- a raw raise is not a reason
            check(f"refused: {label}", False, f"raw {type(exc).__name__}: {str(exc)[:90]}")

    # Attestation is OPTIONAL (owner decision 2026-11-12): no attestation plans, finalizes and
    # validates with no issuer spend; the plan says the rCAT is unattested.
    try:
        un = create.plan(reg, slots, cfg, xch, cats, CREATOR_PH, fee)
        ub = create.finalize(un, G2Element())
        drv.validate(ub)
        un_patch = create.record_patch(un, {"coin": {"parent_coin_info": reg.coin.parent_coin_info.hex(),
                                                     "puzzle_hash": reg.coin.puzzle_hash.hex(), "amount": 1},
                                            "slots": slots, "launcher_id": reg.launcher_id.hex()})
        check("an rCAT creation with no issuer attestation: ACCEPTED, no issuer spend, rcat_assets attested false",
              un.issuer_spends == [] and un.issuer_needed == 0 and "issuer_attestations" not in un.details
              and un.details.get("rcat_assets") == [{"asset_id": R.hex(), "hidden_puzzle_hash": H.hex(), "attested": False}]
              and un_patch["pool"]["issuer_attested"] == [None, None, False])
    except Exception as exc:  # noqa: BLE001
        check("an rCAT creation with no issuer attestation: ACCEPTED, no issuer spend, rcat_assets attested false",
              False, f"{type(exc).__name__}: {str(exc)[:110]}")
    check("the attested creation: rcat_assets attested true, record issuer_attested [None, None, True]",
          p.details.get("rcat_assets") == [{"asset_id": R.hex(), "hidden_puzzle_hash": H.hex(), "attested": True}]
          and patch["pool"]["issuer_attested"] == [None, None, True])
    plan_refused("an issuer proof with the wrong TAIL nonce", "not the everything_with_singleton TAIL",
                 issuer_attestations=(attestation(issuer_spend(reg, xch.coin), nonce=b"\x02"),))
    other = drv.issuer_singleton(bytes32(b"\x5b" * 32))
    plan_refused("another singleton's attestation spend", "not a coin of the issuer singleton",
                 issuer_attestations=(create.IssuerAttestation(R, ISSUER[0], NONCE, ISSUER[3].get_tree_hash(),
                                                               issuer_spend(reg, xch.coin, issuer=other)),))
    plan_refused("the issuer's spend attesting another H", "does not send the attestation",
                 issuer_attestations=(attestation(issuer_spend(reg, xch.coin, hidden=bytes32(b"\x49" * 32))),))
    plan_refused("an attestation on a plain asset", "takes no issuer attestation",
                 issuer_attestations=(att, create.IssuerAttestation(PLAIN, ISSUER[0], NONCE, ISSUER[3].get_tree_hash())))
    pending = create.plan(reg, slots, cfg, xch, cats, CREATOR_PH, fee, issuer_attestations=(attestation(),))
    d = (pending.details.get("issuer_attestations") or [{}])[0]
    check("a proof without the issuer's spend plans, naming the condition the issuer must send",
          d.get("provided") is False and d.get("registry_coin_id") == reg.coin.name().hex()
          and d.get("message") == drv.rcat_issuer_message(R, H, pending.pool.launcher_id).hex() and pending.issuer_spends == [])
    try:
        create.finalize(pending, G2Element())
        check("refused: finalize with the issuer's spend still missing", False, "accepted")
    except create.OfferRejected as exc:
        check("refused: finalize with the issuer's spend still missing", "still needs the issuer" in str(exc), str(exc)[:110])

    print("\nforge_stdin prepare-create with an rCAT:")
    record = {"creation_fee": reg.creation_fee, "treasury_ph": reg.treasury_ph.hex(), "protocol_ph": reg.protocol_ph.hex(),
              "launcher_parent": reg.launcher_parent.hex(), "launcher_id": reg.launcher_id.hex(), "state": list(reg.state),
              "coin": {"parent_coin_info": reg.coin.parent_coin_info.hex(), "puzzle_hash": reg.coin.puzzle_hash.hex(), "amount": 1},
              "lineage": {"parent_name": reg.lineage.parent_name.hex(),
                          "inner_puzzle_hash": None if reg.lineage.inner_puzzle_hash is None else reg.lineage.inner_puzzle_hash.hex(),
                          "amount": 1},
              "slots": slots}

    def cat_json(c: create.CreatorCat, with_hidden: bool):
        out = {"asset_id": c.asset_id.hex(), "coin": {"parent_coin_info": c.coin.parent_coin_info.hex(),
                                                      "puzzle_hash": c.coin.puzzle_hash.hex(), "amount": int(c.coin.amount)},
               "inner_puzzle": bytes(c.inner).hex(),
               "lineage_proof": {"parent_name": c.lineage.parent_name.hex(), "inner_puzzle_hash": c.lineage.inner_puzzle_hash.hex(),
                                 "amount": int(c.lineage.amount)}}
        if with_hidden:
            out["hidden_puzzle_hash"] = H.hex()
        return out

    payload = {"action": "prepare-create", "protocol_version": 21, "registry": record,
               "config": {"asset_ids": [ZERO_32.hex(), PLAIN.hex(), R.hex()], "reserves": [100_000_000, 50_000, 70_000],
                          "weights": [1, 1, 1], "fee_bps": 30, "protocol_fee_bps": 5, "protocol_puzzle_hash": (b"\x55" * 32).hex(),
                          "total_lp": G, "name": "rc", "symbol": "RC",
                          "hidden_puzzle_hashes": [ZERO_32.hex(), ZERO_32.hex(), H.hex()]},
               "creator": {"xch": {"coin": {"parent_coin_info": xch.coin.parent_coin_info.hex(), "puzzle_hash": xch.coin.puzzle_hash.hex(),
                                            "amount": int(xch.coin.amount)}, "puzzle_reveal": bytes(P2).hex()},
                           "cats": [cat_json(cats[PLAIN], False), cat_json(cats[R], True)]},
               "recipient_puzzle_hash": CREATOR_PH.hex(), "network_fee": fee}
    unattested = json.loads(json.dumps(payload))
    payload["creator"]["issuer_attestations"] = [{"index": 2, "launcher_id": ISSUER[0].hex(), "nonce": NONCE.hex(),
                                                  "inner_puzzle_hash": ISSUER[3].get_tree_hash().hex(),
                                                  "spend": att.spend.to_json_dict()}]
    try:
        out = run(payload)
        check("prepare-create builds the rCAT pool", out["pool"]["hidden_puzzle_hashes"][2] == H.hex() and int(out["cost"]) > 0)
        check("  the issuer's spend comes back apart from the creator's (the issuer signs it)",
              len(out["issuer_spends"]) == 1 and all(cs["coin"] != out["issuer_spends"][0]["coin"] for cs in out["creator_spends"]))
    except Exception as exc:  # noqa: BLE001
        check("prepare-create builds the rCAT pool", False, f"{type(exc).__name__}: {str(exc)[:160]}")
    try:
        created = run({**json.loads(json.dumps(payload)), "action": "create", "aggregated_signature": bytes(G2Element()).hex()})
        b = SpendBundle.from_json_dict(created["bundle"])
        check("create: the bundle carries the issuer's spend and validates",
              att.spend in b.coin_spends and drv.validate(b) is not None)
    except Exception as exc:  # noqa: BLE001
        check("create: the bundle carries the issuer's spend and validates", False, f"{type(exc).__name__}: {str(exc)[:160]}")
    try:
        out = run(unattested)
        check("prepare-create of an rCAT with no issuer attestation: ACCEPTED, costed, not pending, attested false",
              int(out["cost"]) > 0 and "issuer_attestations_pending" not in out and "issuer_attestations" not in out
              and out["issuer_spends"] == [] and out.get("rcat_assets", [{}])[0].get("attested") is False)
        created_un = run({**json.loads(json.dumps(unattested)), "action": "create",
                          "aggregated_signature": bytes(G2Element()).hex()})
        check("create of an rCAT with no issuer attestation: the bundle validates",
              drv.validate(SpendBundle.from_json_dict(created_un["bundle"])) is not None)
    except Exception as exc:  # noqa: BLE001
        check("prepare-create/create of an rCAT with no issuer attestation: ACCEPTED", False, f"{type(exc).__name__}: {str(exc)[:110]}")
    by_id = json.loads(json.dumps(payload))
    att_json = by_id["creator"]["issuer_attestations"][0]
    att_json.pop("index"), att_json.pop("spend")
    att_json["asset_id"] = R.hex()
    try:
        out = run(by_id)
        check("prepare-create without the issuer's spend: pending, no cost, the condition to send",
              out.get("issuer_attestations_pending") is True and "cost" not in out
              and out["issuer_attestations"][0]["condition"][2] == drv.rcat_issuer_message(R, H, create.creation_launcher_id(xch.coin)).hex())
    except Exception as exc:  # noqa: BLE001
        check("prepare-create without the issuer's spend: pending, no cost, the condition to send", False, str(exc)[:120])
    try:
        run({**by_id, "action": "create", "aggregated_signature": bytes(G2Element()).hex()})
        check("refused: create with the issuer's spend missing", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("refused: create with the issuer's spend missing", "still needs the issuer" in str(exc), str(exc)[:110])
    no_h = json.loads(json.dumps(payload))
    no_h["creator"]["cats"][1].pop("hidden_puzzle_hash")
    try:
        out = run(no_h)
        check("a creator coin naming no H takes the config's", out["pool"]["hidden_puzzle_hashes"][2] == H.hex())
    except Exception as exc:  # noqa: BLE001
        check("a creator coin naming no H takes the config's", False, str(exc)[:90])
    other_h = json.loads(json.dumps(payload))
    other_h["creator"]["cats"][1]["hidden_puzzle_hash"] = (b"\x49" * 32).hex()
    try:
        run(other_h)
        check("a creator coin naming another H is refused", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("a creator coin naming another H is refused", True, str(exc)[:90])
    v16_lane = {**payload, "protocol_version": 17}
    try:
        run(v16_lane)
        check("the V16 lane refuses an rCAT creation", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("the V16 lane refuses an rCAT creation", "revocable" in str(exc), str(exc)[:90])

    print("\nforge_stdin swap / add / remove on the rCAT pool:")
    snap = v20.pool_to_snapshot(pool)
    hide = hiddens(pool)
    r, w = pool.state[0], pool.weights

    def payout_of(i_in, i_out, gross):
        honest = forge_math.swap_output(r[i_in], r[i_out], gross, pool.fee_bps, w[i_in], w[i_out])
        return honest - honest * pool.protocol_fee_bps // 10_000 - honest * pool.dao_fee_bps // 10_000

    for label, a_in, a_out, i_in, i_out, gross, salt in (("XCH -> R", None, R, 0, 2, 50_000_000, 0x51),
                                                        ("R -> XCH", R, None, 2, 0, 100_000, 0x52),
                                                        ("R -> PLAIN", R, PLAIN, 2, 1, 100_000, 0x53)):
        pay = payout_of(i_in, i_out, gross)
        offer = fabricate({a_in: gross}, {a_out: pay}, salt, hide)
        try:
            out = run({"action": "swap", "offer": offer.to_bech32(), "pool": snap, "current_height": HEIGHT,
                       "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}})
            b = SpendBundle.from_json_dict(out["bundle"])
            drv.validate(b)
            check(f"{label}: settles and validates", True)
            check(f"{label}: the trader is paid exactly, at the config's H for R",
                  paid_to(b, TRADER_PH, a_out, hide.get(a_out)) == pay and (a_out != R or paid_to(b, TRADER_PH, R) == 0), f"{pay}")
        except Exception as exc:  # noqa: BLE001
            check(f"{label}: settles and validates", False, f"{type(exc).__name__}: {str(exc)[:160]}")
    plain_r = fabricate({R: 100_000}, {None: payout_of(2, 0, 100_000)}, 0x54, {})
    try:
        run({"action": "swap", "offer": plain_r.to_bech32(), "pool": snap, "current_height": HEIGHT,
             "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}})
        check("an unlayered R coin is not this market's input", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("an unlayered R coin is not this market's input", "exactly one" in str(exc), str(exc)[:90])

    deposits = [x // 100 for x in r]
    honest = forge_math.invariant_lp_mint(r, deposits, pool.state[1], pool.fee_bps, w, version=20, **drv.add_fee_rates(pool))
    offered = {None: deposits[0] + honest, PLAIN: deposits[1], R: deposits[2]}
    try:
        out = run({"action": "add", "offer": fabricate(offered, {pool.lp_asset_id: honest}, 0x55, hide).to_bech32(), "pool": snap,
                   "current_height": HEIGHT, "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}})
        b = SpendBundle.from_json_dict(out["bundle"])
        drv.validate(b)
        check("add with a layered R deposit settles and validates", paid_to(b, TRADER_PH, pool.lp_asset_id) == honest)
        check("the add's successor keeps H", v20.snapshot_to_pool(out["pool"]).hiddens[2] == H)
    except Exception as exc:  # noqa: BLE001
        check("add with a layered R deposit settles and validates", False, f"{type(exc).__name__}: {str(exc)[:160]}")

    burn = 250_000
    vf = forge_math.vault_fee_bps(3, 10, pool.fee_bps)
    pays = forge_math.withdrawal_amounts(r, burn, pool.state[1], vf)
    try:
        out = run({"action": "remove", "offer": fabricate({pool.lp_asset_id: burn}, dict(zip(pool.asset_ids, pays)), 0x56, hide).to_bech32(),
                   "pool": snap, "current_height": HEIGHT, "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}})
        b = SpendBundle.from_json_dict(out["bundle"])
        drv.validate(b)
        check("remove settles and validates", True)
        check("the R share is paid layered, the plain share plain, XCH bare",
              paid_to(b, TRADER_PH, R, H) == pays[2] and paid_to(b, TRADER_PH, PLAIN) == pays[1] and paid_to(b, TRADER_PH) == pays[0])
    except Exception as exc:  # noqa: BLE001
        check("remove settles and validates", False, f"{type(exc).__name__}: {str(exc)[:160]}")

    print("\nroutes reach the composer (no rCAT gate; every route position is in _test_v20_rcat_routes.py):")
    try:
        run({"action": "multihop-swap", "offer": fabricate({None: 1_000_000}, {R: 1}, 0x57, hide).to_bech32(), "pools": [snap],
             "path": [ZERO_32.hex(), R.hex()], "current_height": HEIGHT})
        check("a one-pool multihop-swap is refused for its own reason, not an rCAT gate", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("a one-pool multihop-swap is refused for its own reason, not an rCAT gate",
              "revocable CAT" not in str(exc) and "two pools" in str(exc), str(exc)[:90])

    print(f"\n{sum(results)}/{len(results)} hidden-lane checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
