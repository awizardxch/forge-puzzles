#!/usr/bin/env python3
"""Nested zaps (forge_v16_route zap_out `through`, zap_in `pre_sale`) through forge_stdin,
keyless, in the simulator.

Owner, 2026-11-07: the LP of a pool holding only two LPs (⚒️🧙‍♂️✨❤️‍🔥🧙‍♂️: the Caster/XCH LP
and the ⚒️🧙‍♂️ LP) found no route to XCH, and ❤️ LOVE found no route into it. "yes build the
nested zaps":
  * out: the outer LP zapped out into the inner LP (its other reserve swapped into it on
    the outer pool), then every coin of the inner LP burned in the inner pool and zapped
    out into XCH or the inner pool's CAT -- two zap outs, one bundle;
  * in: a CAT neither pool holds sold whole for XCH on its own XCH pool first, then that
    XCH minted as offered XCH would be -- through the inner LP into the outer pool (two
    mints), or by the target's own join.

Every bundle must pass consensus validation, pay the trader exactly what the preview named,
and return successor snapshots that rebuild; one is signed by a real standard wallet and
passes chia_rs.validate_clvm_and_signature. Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import hashlib
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

T_C = bytes32(b"\xc3" * 32)
T_L = bytes32(b"\x1e" * 32)       # stands in for LOVE: held by neither pool of the zap
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


def make_sorted(spec, salt):
    # creation sorts a pool's assets (forge_v16_create.canonical); a fixture holding an LP does too
    spec = sorted(spec, key=lambda t: bytes(t[0]) if t[0] is not None else b"")
    return make([a for a, _, _ in spec], [r for _, r, _ in spec], [w for _, _, w in spec], salt=salt)


def settle(label, payload, asset_in, amount, asset_out, salt, bps=0):
    """The quote's offer -- the router's rate beside the entry, the exact release asked for --
    settled. Returns (stdin output, bundle, the release) or (None, None, 0)."""
    fee = amount * bps // 10_000
    payments = {asset_in: [(ROUTER_PH, fee)]} if fee else None
    charged = {**payload, "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": bps}}
    probe = fabricate_offer({asset_in: amount - fee}, {asset_out: 1}, salt=salt, payments=payments)
    try:
        want = int(run({**charged, "offer": probe.to_bech32(), "preview": True})["forge"]["releases"][hx(asset_out)])
        offer = fabricate_offer({asset_in: amount - fee}, {asset_out: want}, salt=salt, payments=payments)
        out = run({**charged, "offer": offer.to_bech32()})
    except Exception as exc:  # noqa: BLE001
        check(f"{label}: builder accepted", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return None, None, 0
    check(f"{label}: builder accepted", True)
    bundle = SpendBundle.from_json_dict(out["bundle"])
    check("  the trader is paid exactly the release the preview named", paid_to(bundle, TRADER_PH, asset_out) == want,
          f"{paid_to(bundle, TRADER_PH, asset_out)} vs {want}")
    check("  the bundle validates under consensus", bool(drv.validate(bundle)))
    rebuilt = all(v16.snapshot_to_pool(s).coin.puzzle_hash.hex() == s["pool_coin"]["puzzle_hash"] for s in out["pools"])
    check("  every successor snapshot rebuilds", rebuilt)
    if bps:
        check("  the router is paid once, in the entry asset, by the trader's own spend",
              paid_to(bundle, ROUTER_PH, asset_in) == fee and paid_to(bundle, ROUTER_PH, asset_out) == 0,
              f"{paid_to(bundle, ROUTER_PH, asset_in)} vs {fee}")
    return out, bundle, want


def wallet_built(label, payload, asset_in, amount, asset_out, salt, bps=300):
    """A wallet-built offer: the router's rate off the payout, once."""
    charged = {**payload, "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": bps}}
    probe = fabricate_offer({asset_in: amount}, {asset_out: 1}, salt=salt)
    try:
        pv = run({**charged, "offer": probe.to_bech32(), "preview": True, "probe": {"router_fee_mode": "surplus"}})
        release, fee = int(pv["forge"]["releases"][hx(asset_out)]), int(pv["forge"]["router_fee_out"])
        out = run({**charged, "offer": fabricate_offer({asset_in: amount}, {asset_out: release - fee}, salt=salt).to_bech32()})
        b = SpendBundle.from_json_dict(out["bundle"])
        check(f"wallet-built {label}: settles, the trader paid what they signed, the router its rate off the payout",
              fee == release * bps // 10_000 and paid_to(b, TRADER_PH, asset_out) == release - fee
              and paid_to(b, ROUTER_PH, asset_out) == fee and paid_to(b, ROUTER_PH, asset_in) == 0
              and out["forge"]["router_fee_side"] == "output-surplus",
              f"{paid_to(b, TRADER_PH, asset_out)} / {paid_to(b, ROUTER_PH, asset_out)} of {release}")
        greedy = fabricate_offer({asset_in: amount}, {asset_out: release - fee + 1}, salt=salt + 1)
        try:
            run({**charged, "offer": greedy.to_bech32()})
            check(f"refused: wallet-built {label} asking a mojo more", False, "accepted")
        except Exception as exc:  # noqa: BLE001
            check(f"refused: wallet-built {label} asking a mojo more", "quote again" in str(exc), str(exc)[:120])
    except Exception as exc:  # noqa: BLE001
        check(f"wallet-built {label}: settles", False, f"{type(exc).__name__}: {str(exc)[:200]}")


def refused(label, payload, asset_in, amount, asset_out, salt, needle):
    offer = fabricate_offer({asset_in: amount}, {asset_out: 1}, salt=salt)
    try:
        run({**payload, "offer": offer.to_bech32()})
    except Exception as exc:  # noqa: BLE001
        return check(f"refused: {label}", needle in str(exc), str(exc)[:160])
    return check(f"refused: {label}", False, "accepted")


def main() -> int:
    if not drv.v16_available():
        print("  [skip] V16 puzzles not built")
        return 2
    xa = make([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x71)                     # Caster/XCH-like
    bc = make([T_B, T_C], [800_000, 600_000], [1, 1], salt=0x72)                             # the ⚒️🧙‍♂️ pool, no XCH
    outer = make_sorted([(xa.lp_asset_id, 290_000, 3), (bc.lp_asset_id, 74_500, 1)], 0x73)   # holds only two LPs
    xl = make([None, T_L], [6_000_000_000, 90_000_000], [1, 1], salt=0x74)                   # LOVE/XCH-like
    base_out = {"action": "zap-out", "current_height": H, "pool": snap(outer), "throughPool": snap(xa)}

    print("nested zap out: the outer LP -> the XCH/A LP (B/C LP swapped into it) -> XCH")
    for burn, salt in ((20_000, 0x11), (3_337, 0x12), (150_001, 0x13)):
        out, bundle, want = settle(f"zap {burn} outer LP -> XCH through XCH/A", {**base_out, "assetOut": hx(None)},
                                   outer.lp_asset_id, burn, None, salt)
        if not out:
            continue
        slot = outer.asset_ids.index(xa.lp_asset_id)
        share = outer.state[0][slot] * burn // outer.state[1]
        swapped = sum(int(o) for _g, o in out["swapped"])
        check("  the inner remove burns every XCH/A LP mojo the outer legs released (its share + the swap's output)",
              int(out["inner_burned"]) == share + swapped, f"{out['inner_burned']} vs {share} + {swapped}")
        check("  no LP of either pool, and no A, leaves to the trader",
              all(paid_to(bundle, TRADER_PH, a) == 0 for a in (outer.lp_asset_id, xa.lp_asset_id, bc.lp_asset_id, T_A)))
        check("  two successors: the outer pool and XCH/A", len(out["pools"]) == 2)
        after = {s["launcher_id"]: s["state"] for s in out["pools"]}
        check("  both LP supplies fell by their burns",
              int(after[outer.launcher_id.hex()]["total_lp"]) == outer.state[1] - burn
              and int(after[xa.launcher_id.hex()]["total_lp"]) == xa.state[1] - int(out["inner_burned"]))
    settle("the same into A (the inner pool's CAT)", {**base_out, "assetOut": hx(T_A)}, outer.lp_asset_id, 20_000, T_A, 0x14)
    settle("the same at 300 bps, site-built", {**base_out, "assetOut": hx(None)}, outer.lp_asset_id, 20_000, None, 0x15, bps=300)
    wallet_built("nested zap out", {**base_out, "assetOut": hx(None)}, outer.lp_asset_id, 20_000, None, 0x16)

    print("sale first, then two mints: L sold on XCH/L, the XCH mints the XCH/A LP, deposited into the outer pool")
    base_in = {"action": "zap-in", "current_height": H, "pool": snap(outer), "viaPool": snap(xa), "preSalePool": snap(xl), "pools": []}
    for amount, salt in ((5_000_000, 0x21), (777_777, 0x22), (40_000_003, 0x23)):
        out, bundle, want = settle(f"zap {amount} L -> outer LP", base_in, T_L, amount, outer.lp_asset_id, salt)
        if not out:
            continue
        x = int(out["sale_outputs"][0])
        inner = out["inner_mints"][0]
        m1, inner_x = int(inner["minted"]), int(inner["deposits"][hx(None)])
        check("  the whole L is sold", int(out["sales"][0]) == amount, f"{out['sales']}")
        check("  the sale's XCH is the inner deposit + both backings, exactly",
              inner_x + m1 + int(out["minted"]) == x, f"{inner_x} + {m1} + {out['minted']} vs {x}")
        check("  the whole XCH/A mint is deposited, and the trader gets only the outer LP",
              int(out["deposits"][hx(xa.lp_asset_id)]) == m1 and paid_to(bundle, TRADER_PH, xa.lp_asset_id) == 0
              and paid_to(bundle, TRADER_PH, None) == 0 and int(out["minted"]) == want)
        check("  three successors: XCH/L, XCH/A, the outer pool", len(out["pools"]) == 3)
    settle("the same at 300 bps, site-built", base_in, T_L, 5_000_000, outer.lp_asset_id, 0x24, bps=300)
    wallet_built("sale-first zap through an LP", base_in, T_L, 5_000_000, outer.lp_asset_id, 0x25)

    print("sale first, then the target's own join: L sold on XCH/L, the XCH joins XCH/A")
    join = {"action": "zap-in", "current_height": H, "pool": snap(xa), "preSalePool": snap(xl), "pools": []}
    out, bundle, want = settle("zap 5,000,000 L -> XCH/A LP", join, T_L, 5_000_000, xa.lp_asset_id, 0x31)
    if out:
        x = int(out["sale_outputs"][0])
        check("  the sale's XCH is the deposit plus the backing", int(out["deposits"][hx(None)]) + int(out["minted"]) == x
              and int(out["deposits"][hx(T_A)]) == 0, f"{out['deposits']} + {out['minted']} vs {x}")
    wallet_built("sale-first join", join, T_L, 5_000_000, xa.lp_asset_id, 0x32)

    print("refusals")
    refused("a nested zap out through a pool whose LP the outer pool does not hold", {**base_out, "throughPool": snap(xl), "assetOut": hx(None)},
            outer.lp_asset_id, 20_000, None, 0x41, "does not hold the LP")
    refused("a nested zap out into an asset the inner pool lacks", {**base_out, "assetOut": hx(T_L)},
            outer.lp_asset_id, 20_000, T_L, 0x42, "does not hold")
    refused("a nested zap out through a vault-free CAT pool for the outer's own asset",
            {**base_out, "pool": snap(xa), "throughPool": snap(outer), "assetOut": hx(T_B)}, xa.lp_asset_id, 20_000, T_B, 0x43, "does not hold the LP")
    refused("a nested zap out through the outer pool itself", {**base_out, "throughPool": snap(outer), "assetOut": hx(None)},
            outer.lp_asset_id, 20_000, None, 0x49, "through another pool")
    refused("a sale first on the target itself", {**join, "preSalePool": snap(xa)}, T_A, 20_000, xa.lp_asset_id, 0x44, "does not mint in")
    refused("a sale first on the inner pool it mints through", {**base_in, "preSalePool": snap(xa)}, T_A, 20_000, outer.lp_asset_id, 0x4A,
            "does not mint in")
    refused("a sale first of a CAT the inner pool takes as it is", {**base_in, "preSalePool": snap(make([None, T_A], [9_000_000_000, 400_000], [1, 1], salt=0x75))},
            T_A, 20_000, outer.lp_asset_id, 0x4B, "takes this CAT as it is")
    refused("a sale first on a pool without XCH", {**base_in, "preSalePool": snap(bc)}, T_B, 20_000, outer.lp_asset_id, 0x45, "XCH pool holding it")
    refused("a sale first beside sale pools", {**base_in, "pools": [snap(xl)]}, T_L, 5_000_000, outer.lp_asset_id, 0x46, "no other pool")
    refused("a sale first into a pool without XCH and no LP to mint through", {**join, "pool": snap(bc)}, T_L, 5_000_000, bc.lp_asset_id, 0x47,
            "joins a pool holding XCH")
    refused("a sale first of XCH", base_in, None, 1_000_000_000, outer.lp_asset_id, 0x48, "sells a CAT")

    print("signed: a standard wallet offers the outer LP for A; the bundle passes validate_clvm_and_signature")
    signed_case(outer, xa)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} nested zap checks passed")
    return 1 if failed else 0


def signed_case(outer, xa) -> None:
    import _test_v16_dexie_split as ds
    t_sk, t_inner, t_ph = ds.wallet(0x37)
    burn = 20_000
    # the helper's wallet requests a CAT, so the signed zap ends in A, the inner pool's CAT
    probe = fabricate_offer({outer.lp_asset_id: burn}, {T_A: 1}, salt=0x91)
    want = int(v16r.zap_out(outer, T_A, probe, H, t_ph, preview=True, through=xa).details["releases"][hx(T_A)])
    trader = ds.std_offer(t_sk, t_inner, t_ph, outer.lp_asset_id, burn, T_A, want, 0xB7)
    try:
        result = v16r.zap_out(outer, T_A, trader, H, t_ph, through=xa)
    except Exception as exc:  # noqa: BLE001
        check("signed nested zap out: builder accepted", False, f"{type(exc).__name__}: {str(exc)[:160]}")
        return
    extra = {"AGG_SIG_ME_ADDITIONAL_DATA": ds.GENESIS}
    for name, op in (("AGG_SIG_PARENT_ADDITIONAL_DATA", 43), ("AGG_SIG_PUZZLE_ADDITIONAL_DATA", 44),
                     ("AGG_SIG_AMOUNT_ADDITIONAL_DATA", 45), ("AGG_SIG_PUZZLE_AMOUNT_ADDITIONAL_DATA", 46),
                     ("AGG_SIG_PARENT_AMOUNT_ADDITIONAL_DATA", 47), ("AGG_SIG_PARENT_PUZZLE_ADDITIONAL_DATA", 48)):
        extra[name] = hashlib.sha256(ds.GENESIS + bytes([op])).digest()
    constants = DEFAULT_CONSTANTS.replace(GENESIS_CHALLENGE=bytes32(ds.GENESIS), **{k: bytes32(v) for k, v in extra.items()})
    try:
        validate_clvm_and_signature(result.bundle, int(constants.MAX_BLOCK_COST_CLVM), constants, 10**8)
        ok, why = True, ""
    except Exception as exc:  # noqa: BLE001
        ok, why = False, f"{type(exc).__name__}: {exc}"[:160]
    check("the trader's signature over the nested zap out passes validate_clvm_and_signature", ok, why)
    check("the trader's standard wallet receives exactly the A it asked for", paid_to(result.bundle, t_ph, T_A) == want,
          f"{paid_to(result.bundle, t_ph, T_A)} vs {want}")


if __name__ == "__main__":
    sys.exit(main())
