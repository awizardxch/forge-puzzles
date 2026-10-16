#!/usr/bin/env python3
"""The LP-to-LP zap (forge_v16_route.zap_lp) through forge_stdin, keyless, in the simulator.

Owner, 2026-10-15: paying the ⚒️🌱💗 LP (LOVE/SPROUT, CAT/CAT) for the ⚒️🍫🌮XCH LP showed "No
routes found", although "these LPs ... do have a route through XCH and other asset routes".
One bundle: the source LP removed, its reserves sold (into XCH, or straight into an asset
the target holds) or deposited as they are, the XCH raised spent on the target's assets
when the target holds none, and the target's LP minted with the backing EXACTLY covered.

Every bundle must pass consensus validation, pay the trader exactly what they asked, and
return successor snapshots that rebuild; one is signed by real standard wallets and passes
chia_rs.validate_clvm_and_signature. Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia_rs import SpendBundle, validate_clvm_and_signature
from chia_rs.sized_bytes import bytes32

import forge_stdin
import forge_v16_driver as drv
import forge_v16_offer as v16
import forge_v16_route as v16r
from _test_v16_offer_lane import ROUTER_PH, T_A, T_B, TRADER_PH, H, fabricate_offer, make, paid_to
from forge_offer import ZERO_32
from forge_v16_offer import OfferRejected

T_C = bytes32(b"\xc3" * 32)
T_D = bytes32(b"\xd4" * 32)
results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def run(payload):
    return forge_stdin.build(json.loads(json.dumps(payload)))


def snap(pool):
    return v16.pool_to_snapshot(pool)


def hx(asset):
    return (ZERO_32 if asset is None else asset).hex()


def preview(payload, lp_in, burn, lp_out, salt, mode=None):
    probe = fabricate_offer({lp_in: burn}, {lp_out: 1}, salt=salt)
    return run({**payload, "offer": probe.to_bech32(), "preview": True, **({"probe": {"router_fee_mode": mode}} if mode else {})})


def settle(label, payload, lp_in, burn, lp_out, salt, bps=0):
    """The quote's offer for this zap -- the router's rate paid beside the entry, the exact
    release asked for -- settled. Returns (stdin output, bundle) or (None, None)."""
    fee = burn * bps // 10_000
    payments = {lp_in: [(ROUTER_PH, fee)]} if fee else None
    charged = {**payload, "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": bps}}
    probe = fabricate_offer({lp_in: burn - fee}, {lp_out: 1}, salt=salt, payments=payments)
    try:
        want = int(run({**charged, "offer": probe.to_bech32(), "preview": True})["forge"]["releases"][hx(lp_out)])
        offer = fabricate_offer({lp_in: burn - fee}, {lp_out: want}, salt=salt, payments=payments)
        out = run({**charged, "offer": offer.to_bech32()})
    except Exception as exc:  # noqa: BLE001
        check(f"{label}: builder accepted", False, f"{type(exc).__name__}: {str(exc)[:160]}")
        return None, None
    check(f"{label}: builder accepted", True)
    bundle = SpendBundle.from_json_dict(out["bundle"])
    check("  the trader is paid exactly the mint the preview named", paid_to(bundle, TRADER_PH, lp_out) == want == int(out["minted"]),
          f"{paid_to(bundle, TRADER_PH, lp_out)} vs {want}")
    check("  nothing of the source LP or the removed assets leaves the bundle",
          paid_to(bundle, TRADER_PH, lp_in) == 0 and paid_to(bundle, TRADER_PH, None) == 0)
    check("  the bundle validates under consensus", bool(drv.validate(bundle)))
    for s in out["pools"]:
        if v16.snapshot_to_pool(s).coin.puzzle_hash.hex() != s["pool_coin"]["puzzle_hash"]:
            check("  a successor snapshot does not rebuild", False, s["launcher_id"][:8])
    return out, bundle


def refused(label, payload, lp_in, burn, lp_out, salt, needle=""):
    offer = fabricate_offer({lp_in: burn}, {lp_out: 1}, salt=salt)
    try:
        run({**payload, "offer": offer.to_bech32()})
    except Exception as exc:  # noqa: BLE001
        return check(f"refused: {label}", needle in str(exc), str(exc)[:140])
    return check(f"refused: {label}", False, "accepted")


def main() -> int:
    if not drv.v16_available():
        print("  [skip] V16 puzzles not built")
        return 2
    xa = make([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x51)          # XCH/A
    ab = make([T_A, T_B], [800_000, 600_000], [1, 1], salt=0x52)                   # A/B
    xb = make([None, T_B], [8_000_000_000, 700_000], [1, 1], salt=0x53)           # XCH/B
    bc = make([T_B, T_C], [500_000, 900_000], [1, 1], salt=0x55)                   # B/C
    xc = make([None, T_C], [9_000_000_000, 300_000], [1, 4], salt=0x5F)           # XCH/C, weighted like 🍫🌮
    triple = make([None, T_A, T_B], [20_000_000_000, 400_000, 300_000], [2, 1, 1], salt=0x58)
    base = {"action": "zap-lp", "current_height": H}

    print("LP(A/B) -> LP(XCH/C): both CATs sold for XCH, the XCH joins the target (the owner's shape)")
    thru = {**base, "sourcePool": snap(ab), "pool": snap(xc),
            "sales": [{"pool": snap(xa), "assetIn": hx(T_A), "assetOut": hx(None)},
                      {"pool": snap(xb), "assetIn": hx(T_B), "assetOut": hx(None)}]}
    for burn, salt in ((20_000, 0x11), (3_333, 0x12), (123_457, 0x13)):
        out, bundle = settle(f"zap {burn} A/B LP -> XCH/C LP", thru, ab.lp_asset_id, burn, xc.lp_asset_id, salt)
        if not out:
            continue
        removed = [int(a) for a in out["removed"]]
        check("  the remove is proportional", removed == [ab.state[0][0] * burn // ab.state[1], ab.state[0][1] * burn // ab.state[1]],
              f"{removed}")
        check("  each sale sells the whole removed coin", [int(a) for a in out["sales"]] == removed, f"{out['sales']}")
        x_raised = sum(int(a) for a in out["sale_outputs"])
        check("  every XCH mojo raised is the deposit plus the backing",
              int(out["deposits"][hx(None)]) + int(out["minted"]) == x_raised and int(out["deposits"][hx(T_C)]) == 0,
              f"{out['deposits']} + {out['minted']} vs {x_raised}")
        check("  four successors: source, both sale pools, target", len(out["pools"]) == 4)
        src_after = next(s for s in out["pools"] if s["launcher_id"] == ab.launcher_id.hex())["state"]
        check("  the source's LP supply fell by the burn", int(src_after["total_lp"]) == ab.state[1] - burn)
    out, bundle = settle("the same at 300 bps", thru, ab.lp_asset_id, 20_000, xc.lp_asset_id, 0x14, bps=300)
    if out:
        check("  the router is paid ONCE, in the source LP, by the trader's own spend, and no target LP",
              paid_to(bundle, ROUTER_PH, ab.lp_asset_id) == 20_000 * 300 // 10_000 and paid_to(bundle, ROUTER_PH, xc.lp_asset_id) == 0,
              f"{paid_to(bundle, ROUTER_PH, ab.lp_asset_id)}")
        check("  the remove burns the net entry", int(out["burned"]) == 20_000 - 600, out["burned"])

    print("LP(A/B) -> LP(XCH/B): B is shared and deposited as it is, only A is sold")
    shared = {**base, "sourcePool": snap(ab), "pool": snap(xb), "sales": [{"pool": snap(xa), "assetIn": hx(T_A), "assetOut": hx(None)}]}
    out, _ = settle("zap A/B LP -> XCH/B LP", shared, ab.lp_asset_id, 20_000, xb.lp_asset_id, 0x15)
    if out:
        check("  the removed B is deposited whole, no sale for it", int(out["deposits"][hx(T_B)]) == int(out["removed"][1])
              and len(out["sales"]) == 1, f"{out['deposits']}")
    print("LP(A/B) -> LP(XCH/C): B sold straight into C on B/C (the other-asset path), A for XCH, both deposited")
    other = {**base, "sourcePool": snap(ab), "pool": snap(xc),
             "sales": [{"pool": snap(xa), "assetIn": hx(T_A), "assetOut": hx(None)},
                       {"pool": snap(bc), "assetIn": hx(T_B), "assetOut": hx(T_C)}]}
    out, _ = settle("zap A/B LP -> XCH/C LP, B into C", other, ab.lp_asset_id, 20_000, xc.lp_asset_id, 0x17)
    if out:
        check("  the C bought is deposited, the XCH covers the rest of the deposit and the backing",
              int(out["deposits"][hx(T_C)]) == int(out["sale_outputs"][1])
              and int(out["deposits"][hx(None)]) + int(out["minted"]) == int(out["sale_outputs"][0]), f"{out['deposits']}")
    print("LP(A/B) -> LP(XCH/A/B): one sale straight into a target asset is allowed too (A -> B on another pool)")
    into = {**base, "sourcePool": snap(ab), "pool": snap(triple),
            "sales": [{"pool": snap(xa), "assetIn": hx(T_A), "assetOut": hx(None)}]}
    settle("zap A/B LP -> XCH/A/B LP, A sold for XCH", into, ab.lp_asset_id, 20_000, triple.lp_asset_id, 0x16)

    print("LP(XCH/A) -> LP(B/C): a target without XCH, the XCH raised buys into it, the rest exactly the backing")
    one = {**base, "sourcePool": snap(xa), "pool": snap(bc),
           "sales": [{"pool": snap(ab), "assetIn": hx(T_A), "assetOut": hx(T_B)}], "buys": [{"pool": snap(xb), "asset": hx(T_B)}]}
    two = {**base, "sourcePool": snap(xa), "pool": snap(bc),
           "sales": [{"pool": snap(xa), "assetIn": hx(T_A), "assetOut": hx(None)}],
           "buys": [{"pool": snap(xb), "asset": hx(T_B)}, {"pool": snap(xc), "asset": hx(T_C)}]}
    for label, payload in (("A sold into B on A/B, one buy of B", one), ("A sold for XCH on the source itself, both assets bought", two)):
        for burn, salt in ((20_000, 0x21), (7_777, 0x22)):
            out, _ = settle(f"zap {burn} XCH/A LP -> B/C LP, {label}", payload, xa.lp_asset_id, burn, bc.lp_asset_id, salt + len(label))
            if not out:
                continue
            x_raised = int(out["removed"][0]) + sum(int(a) for a, s in zip(out["sale_outputs"], payload["sales"]) if s["assetOut"] == hx(None))
            check("  sum(buys) + mint == the XCH raised, exactly", sum(int(y) for y in out["buys"]) + int(out["minted"]) == x_raised,
                  f"{out['buys']} + {out['minted']} vs {x_raised}")
    print("the same pool crossed twice: A sold for XCH on XCH/A/B, then XCH buys B on that pool's new state")
    twice = {**base, "sourcePool": snap(xa), "pool": snap(bc),
             "sales": [{"pool": snap(triple), "assetIn": hx(T_A), "assetOut": hx(None)}], "buys": [{"pool": snap(triple), "asset": hx(T_B)}]}
    out, _ = settle("zap XCH/A LP -> B/C LP through XCH/A/B twice", twice, xa.lp_asset_id, 20_000, bc.lp_asset_id, 0x31)
    if out:
        check("  three successors (the twice-crossed pool once)", len(out["pools"]) == 3)

    print("LP(XCH/C) -> LP(A/B), C valuable (2,000 units against 10 XCH): one mojo of C or XCH buys nothing, and the search moves on")
    xd = make([None, T_D], [10_000_000_000, 2_000], [1, 1], salt=0x5A)            # a valuable CAT, like Caster
    ad = make([T_A, T_D], [500_000, 1_500], [1, 1], salt=0x5B)
    valuable = {**base, "sourcePool": snap(xd), "pool": snap(ad),
                "sales": [{"pool": snap(xd), "assetIn": hx(T_D), "assetOut": hx(None)}],
                "buys": [{"pool": snap(xa), "asset": hx(T_A)}, {"pool": snap(xd), "asset": hx(T_D)}]}
    settle("zap XCH/D LP -> A/D LP (D valuable)", valuable, xd.lp_asset_id, 50_000, ad.lp_asset_id, 0x41)
    settle("zap XCH/D LP -> A/D LP buying only D", {**valuable, "buys": [{"pool": snap(xd), "asset": hx(T_D)}]},
           xd.lp_asset_id, 50_000, ad.lp_asset_id, 0x42)

    print("the owner's pools, shaped from mainnet (2026-10-15): ⚒️🌱💗 LP -> ⚒️🍫🌮XCH LP through XCH")
    love = bytes32.fromhex("70010d83542594dd44314efbae75d82b3d9ae7d946921ed981a6cd08f0549e50")
    sprout = bytes32.fromhex("ab558b1b841365a24d1ff2264c55982e55664a8b6e45bc107446b7e667bb463b")
    taco = bytes32.fromhex("8df67763ad273f4a08f8f19f8a172d80b38ad940f32fe20b0b2ed3d665edf575")

    def live(assets, reserves, lp, weights, fee, dao, salt):
        return drv.make_pool(assets, reserves, total_lp=lp, weights=weights, fee_bps=fee, protocol_fee_bps=5,
                             protocol_ph=bytes32(b"\x55" * 32), salt=salt, leaves="forge", last_height=H - 10,
                             dao_fee_bps=dao, dao_ph=bytes32(b"\x66" * 32))
    love_x = live([None, love], [15_171_374_384_384, 37_385_982], 70_372_019, [1, 1], 100, 99, 0x61)
    sprout_x = live([None, sprout], [11_121_575_228_864, 859_938_086], 1_259_187_606, [1, 1], 100, 100, 0x62)
    ls = live([love, sprout], [138_936_190, 4_391_315_120], 144_501_670, [1, 1], 30, 30, 0x63)
    choco = live([None, taco], [3_659_397_037_822, 7_845], 8_785, [1, 4], 100, 100, 0x64)
    owner = {**base, "sourcePool": snap(ls), "pool": snap(choco),
             "sales": [{"pool": snap(love_x), "assetIn": hx(love), "assetOut": hx(None)},
                       {"pool": snap(sprout_x), "assetIn": hx(sprout), "assetOut": hx(None)}]}
    for burn in (1_000_000, 10_000):
        pv = preview(owner, ls.lp_asset_id, burn, choco.lp_asset_id, 0x65)
        print(f"    preview {burn} LP mojos: minted {pv['minted']}, sales {pv['sales']} -> {pv['sale_outputs']}, deposits {pv['deposits']}")
    settle("zap 1,000,000 ⚒️🌱💗 LP mojos -> ⚒️🍫🌮XCH LP", owner, ls.lp_asset_id, 1_000_000, choco.lp_asset_id, 0x66)
    try:
        run({**owner, "offer": fabricate_offer({ls.lp_asset_id: 1_000}, {choco.lp_asset_id: 1}, salt=0x67).to_bech32(), "preview": True})
        check("1 ⚒️🌱💗 LP (1,000 mojos) mints no ⚒️🍫🌮XCH LP: refused", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("1 ⚒️🌱💗 LP (1,000 mojos) mints no ⚒️🍫🌮XCH LP: refused", "mints no LP" in str(exc), str(exc)[:120])

    print("wallet-built: the router's rate off the target-LP payout, once")
    probe = preview({**thru, "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 300}}, ab.lp_asset_id, 20_000, xc.lp_asset_id, 0x71, mode="surplus")
    release, fee = int(probe["forge"]["releases"][hx(xc.lp_asset_id)]), int(probe["forge"]["router_fee_out"])
    check("  the preview names the fee: the rate on the release", fee == release * 300 // 10_000, f"{fee} of {release}")
    offer = fabricate_offer({ab.lp_asset_id: 20_000}, {xc.lp_asset_id: release - fee}, salt=0x71)
    try:
        out = run({**thru, "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 300}, "offer": offer.to_bech32()})
        b = SpendBundle.from_json_dict(out["bundle"])
        check("wallet-built LP zap: settles, the trader paid what they signed, the router its fee in the target LP, nothing of the entry",
              paid_to(b, TRADER_PH, xc.lp_asset_id) == release - fee and paid_to(b, ROUTER_PH, xc.lp_asset_id) == fee
              and paid_to(b, ROUTER_PH, ab.lp_asset_id) == 0 and out["forge"]["router_fee_side"] == "output-surplus",
              f"{paid_to(b, TRADER_PH, xc.lp_asset_id)} / {paid_to(b, ROUTER_PH, xc.lp_asset_id)}")
    except Exception as exc:  # noqa: BLE001
        check("wallet-built LP zap: settles", False, f"{type(exc).__name__}: {str(exc)[:140]}")
    greedy = fabricate_offer({ab.lp_asset_id: 20_000}, {xc.lp_asset_id: release - fee + 1}, salt=0x72)
    try:
        run({**thru, "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 300}, "offer": greedy.to_bech32()})
        check("refused: wallet-built asking a mojo more than the release less the fee", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("refused: wallet-built asking a mojo more than the release less the fee", "quote again" in str(exc), str(exc)[:120])

    print("refusals")
    want = int(preview(thru, ab.lp_asset_id, 20_000, xc.lp_asset_id, 0x81)["forge"]["releases"][hx(xc.lp_asset_id)])
    try:
        run({**thru, "offer": fabricate_offer({ab.lp_asset_id: 20_000}, {xc.lp_asset_id: want + 1}, salt=0x81).to_bech32()})
        check("refused: asking one target-LP mojo more than the mint", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("refused: asking one target-LP mojo more than the mint", "quote again" in str(exc), str(exc)[:120])
    refused("a removed CAT with nowhere to go (B neither sold nor held by the target)",
            {**thru, "sales": thru["sales"][:1]}, ab.lp_asset_id, 20_000, xc.lp_asset_id, 0x82, "neither deposited nor sold")
    refused("an offer of XCH instead of the source LP", thru, None, 1_000_000_000, xc.lp_asset_id, 0x83, "source pool's LP alone")
    refused("a target holding XCH with buys", {**thru, "buys": [{"pool": snap(xb), "asset": hx(T_B)}]},
            ab.lp_asset_id, 20_000, xc.lp_asset_id, 0x84, "buys on no other pool")
    refused("a target without XCH and no buy", {**one, "buys": []}, xa.lp_asset_id, 20_000, bc.lp_asset_id, 0x85, "one or two")
    refused("a sale ending in an asset the target does not hold", {**thru, "sales": [{"pool": snap(ab), "assetIn": hx(T_A), "assetOut": hx(T_B)},
                                                                                     thru["sales"][1]]},
            ab.lp_asset_id, 20_000, xc.lp_asset_id, 0x86, "XCH or in an asset the target holds")
    refused("the same pool as source and target", {**thru, "pool": snap(ab)}, ab.lp_asset_id, 20_000, ab.lp_asset_id, 0x87, "")
    refused("selling one reserve twice", {**thru, "sales": [thru["sales"][0], {**thru["sales"][0], "pool": snap(triple)}, thru["sales"][1]]},
            ab.lp_asset_id, 20_000, xc.lp_asset_id, 0x88, "once")
    # Exactness: where one unit of the bought asset mints thousands of LP mojos (a target with
    # a fine LP, here 10^10 over a small pool), `y + mint(y)` jumps at every unit bought, so for
    # some amounts no purchase leaves exactly the backing. The lane says so -- "change the
    # amount by a mojo" -- rather than leave XCH over (a second payout) or short the backing.
    fine = make([T_B, T_C], [500_000, 900_000], [1, 1], salt=0x5C, total_lp=10**10)
    fine_one = {**one, "pool": snap(fine)}
    sized = {b: _sizing(xa, fine, ab, xb, b) for b in range(20_000, 20_040)}
    refusing = [b for b, why in sized.items() if "change the amount by a mojo" in why]
    settling = [b for b, why in sized.items() if why == "ok"]
    check("a fine-LP target has amounts where no purchase leaves exactly the backing", bool(refusing), f"{len(refusing)} of 40 refused")
    if refusing:
        refused(f"the lane refuses such an amount ({refusing[0]}) with 'change the amount by a mojo'", fine_one, xa.lp_asset_id, refusing[0],
                fine.lp_asset_id, 0x8A, "change the amount by a mojo")
    check("  and some amount nearby is exact", bool(settling), f"{settling}")
    if settling:
        out, _ = settle(f"  that amount ({settling[0]}) settles, the backing exact", fine_one, xa.lp_asset_id, settling[0], fine.lp_asset_id, 0x8B)
        if out:
            check("  sum(buys) + mint == the XCH removed", sum(int(y) for y in out["buys"]) + int(out["minted"]) == int(out["removed"][0]))

    print("signed: a standard wallet offers the source LP; the bundle passes validate_clvm_and_signature")
    signed_case(ab, xc, xa, xb)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} LP-to-LP zap checks passed")
    return 1 if failed else 0


def _sizing(xa, target, ab, xb, burn) -> str:
    """The lane's sizing alone (no composer run): "ok", or why it refuses this burn."""
    try:
        res, pots, _p, _o = v16r.zap_lp_exit(xa, burn, [(ab, T_A, T_B)])
        v16r.zap_lp_buys(target, res, pots, [(xb, T_B)])
        return "ok"
    except OfferRejected as exc:
        return str(exc)


def signed_case(ab, xc, xa, xb) -> None:
    import _test_v16_dexie_split as ds
    t_sk, t_inner, t_ph = ds.wallet(0x31)
    burn = 20_000
    sales = [(xa, T_A, None), (xb, T_B, None)]
    probe = fabricate_offer({ab.lp_asset_id: burn}, {xc.lp_asset_id: 1}, salt=0x91)
    want = int(v16r.zap_lp(ab, xc, sales, [], probe, H, t_ph, preview=True).details["releases"][hx(xc.lp_asset_id)])
    trader = ds.std_offer(t_sk, t_inner, t_ph, ab.lp_asset_id, burn, xc.lp_asset_id, want, 0xB3)
    try:
        result = v16r.zap_lp(ab, xc, sales, [], trader, H, t_ph)
    except Exception as exc:  # noqa: BLE001
        check("signed LP zap: builder accepted", False, f"{type(exc).__name__}: {str(exc)[:140]}")
        return
    extra = {"AGG_SIG_ME_ADDITIONAL_DATA": ds.GENESIS}
    import hashlib
    for name, op in (("AGG_SIG_PARENT_ADDITIONAL_DATA", 43), ("AGG_SIG_PUZZLE_ADDITIONAL_DATA", 44),
                     ("AGG_SIG_AMOUNT_ADDITIONAL_DATA", 45), ("AGG_SIG_PUZZLE_AMOUNT_ADDITIONAL_DATA", 46),
                     ("AGG_SIG_PARENT_AMOUNT_ADDITIONAL_DATA", 47), ("AGG_SIG_PARENT_PUZZLE_ADDITIONAL_DATA", 48)):
        extra[name] = hashlib.sha256(ds.GENESIS + bytes([op])).digest()
    constants = DEFAULT_CONSTANTS.replace(GENESIS_CHALLENGE=bytes32(ds.GENESIS), **{k: bytes32(v) for k, v in extra.items()})
    try:
        validate_clvm_and_signature(result.bundle, int(constants.MAX_BLOCK_COST_CLVM), constants, 10**8)
        ok, why = True, ""
    except Exception as exc:  # noqa: BLE001
        ok, why = False, f"{type(exc).__name__}: {exc}"[:140]
    check("the trader's signature over the LP zap passes validate_clvm_and_signature", ok, why)
    check("the trader's standard wallet receives exactly the target LP it asked for", paid_to(result.bundle, t_ph, xc.lp_asset_id) == want,
          f"{paid_to(result.bundle, t_ph, xc.lp_asset_id)} vs {want}")


if __name__ == "__main__":
    sys.exit(main())
