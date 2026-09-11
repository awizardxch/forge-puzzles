#!/usr/bin/env python3
"""The V11 route lanes through forge_stdin, keyless, in the simulator.

Fabricated wallet offers (see _test_v11_offer_lane) drive multi-hop, split,
flow, vault-route and routed-deposit bundles across pools built by the driver.
Every bundle must pass consensus validation, pay the trader exactly their
request, pay the surplus to the router, and return successor snapshots that
rebuild. Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs import SpendBundle
from chia_rs.sized_bytes import bytes32

import forge_math
import forge_stdin
import forge_v11_driver as drv
import forge_v11_offer as v11
import forge_v11_route as v11r  # noqa: E402
from _test_v11_offer_lane import ROUTER_PH, T_A, T_B, TRADER_PH, H, fabricate_offer, make, paid_to
from forge_offer import ZERO_32

T_C = bytes32(b"\xc3" * 32)
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def run(payload):
    return forge_stdin.build(json.loads(json.dumps(payload)))


def snap(pool):
    return v11.pool_to_snapshot(pool)


def hx(asset):
    return (ZERO_32 if asset is None else asset).hex()


def swap_out(pool, i_in, i_out, gross, state=None):
    r = (state or pool.state)[0]
    honest = forge_math.swap_output(r[i_in], r[i_out], gross, pool.fee_bps, pool.weights[i_in], pool.weights[i_out])
    return honest - honest * pool.protocol_fee_bps // 10_000


def attempt(label, payload, expect_ok=True):
    try:
        out = run(payload)
    except Exception as exc:
        check(f"{label}: builder accepted", not expect_ok, f"{type(exc).__name__}: {str(exc)[:110]}")
        return None
    if not check(f"{label}: builder accepted", expect_ok):
        return None
    for s in out["pools"]:
        check("  successor snapshot rebuilds", v11.snapshot_to_pool(s).coin.puzzle_hash.hex() == s["pool_coin"]["puzzle_hash"])
    return out


def main() -> int:
    if not drv.v11_available():
        print("  [skip] V11 puzzles not built"); return 2
    xa = make([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x51)          # XCH/A
    ab = make([T_A, T_B], [800_000, 600_000], [1, 1], salt=0x52)                   # A/B
    xb = make([None, T_B], [8_000_000_000, 700_000], [1, 1], salt=0x53)           # XCH/B
    xa2 = make([None, T_A], [30_000_000_000, 1_400_000], [1, 1], salt=0x54)       # a second XCH/A
    bc = make([T_B, T_C], [500_000, 900_000], [1, 1], salt=0x55)                   # B/C
    vault = make([T_A], [2_000_000], [1], salt=0x56)                               # vault of A
    xlp = make([None, vault.lp_asset_id], [5_000_000_000, 400_000], [1, 1], salt=0x57)   # XCH / vault LP
    triple = make([None, T_A, T_B], [20_000_000_000, 400_000, 300_000], [2, 1, 1], salt=0x58)
    fee = {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}

    print("multi-hop XCH -> A -> B (two pools)")
    gross = 100_000_000
    o1 = swap_out(xa, 0, 1, gross)
    o2 = swap_out(ab, 0, 1, o1)
    want = o2 * 99 // 100
    offer = fabricate_offer({None: gross}, {T_B: want}, salt=0x61)
    out = attempt("2-hop", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap(xa), snap(ab)],
                            "path": [hx(None), hx(T_A), hx(T_B)], "current_height": H, "dev_fee": fee})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  amounts follow the chain", [int(a) for a in out["amounts"]] == [gross, o1, o2], f"{out['amounts']}")
        check("  trader is paid exactly the request", paid_to(bundle, TRADER_PH, T_B) == want)
        check("  router gets the surplus", paid_to(bundle, ROUTER_PH, T_B) == o2 - want and int(out["dev_fee_collected"]) == o2 - want)
        check("  the intermediate A never reaches anyone", paid_to(bundle, TRADER_PH, T_A) == 0 and paid_to(bundle, ROUTER_PH, T_A) == 0)
        check("  two successors, in input order", [s["launcher_id"] for s in out["pools"]] == [xa.launcher_id.hex(), ab.launcher_id.hex()])
    attempt("2-hop, greedy trader", {"action": "multihop-swap", "offer": fabricate_offer({None: gross}, {T_B: o2 + 1}, salt=0x62).to_bech32(),
                                     "pools": [snap(xa), snap(ab)], "path": [hx(None), hx(T_A), hx(T_B)], "current_height": H, "dev_fee": fee},
            expect_ok=False)

    print("multi-hop XCH -> A -> B -> C (three pools, CAT bridges)")
    o3 = swap_out(bc, 0, 1, o2)
    offer = fabricate_offer({None: gross}, {T_C: o3 * 99 // 100}, salt=0x63)
    out = attempt("3-hop", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap(xa), snap(ab), snap(bc)],
                            "path": [hx(None), hx(T_A), hx(T_B), hx(T_C)], "current_height": H, "dev_fee": fee})
    if out:
        check("  amounts follow the chain", [int(a) for a in out["amounts"]] == [gross, o1, o2, o3])

    print("split XCH -> A across two pools")
    g1, g2 = 60_000_000, 40_000_000
    s1, s2 = swap_out(xa, 0, 1, g1), swap_out(xa2, 0, 1, g2)
    want = (s1 + s2) * 99 // 100
    offer = fabricate_offer({None: g1 + g2}, {T_A: want}, salt=0x64)
    out = attempt("split", {"action": "split-swap", "offer": offer.to_bech32(), "current_height": H, "dev_fee": fee,
                            "branches": [{"pools": [snap(xa)], "path": [hx(None), hx(T_A)], "amountIn": g1},
                                         {"pools": [snap(xa2)], "path": [hx(None), hx(T_A)], "amountIn": g2}]})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  branch amounts are the declared shares of the offered XCH", [[int(a) for a in b] for b in out["branch_amounts"]] == [[g1, s1], [g2, s2]])
        check("  total_out is the sum", int(out["total_out"]) == s1 + s2)
        check("  trader is paid exactly the request, once", paid_to(bundle, TRADER_PH, T_A) == want)
        check("  router gets the merged surplus", paid_to(bundle, ROUTER_PH, T_A) == s1 + s2 - want)
    # an equal split would create two identical entry children; the composer nudges the
    # shares apart by one mojo so the coins differ and the total still matches the offer
    ge = 50_000_000
    se = swap_out(xa, 0, 1, ge + 1) + swap_out(xa2, 0, 1, ge - 1)
    offer_eq = fabricate_offer({None: 2 * ge}, {T_A: se * 99 // 100}, salt=0x6A)
    out = attempt("equal split", {"action": "split-swap", "offer": offer_eq.to_bech32(), "current_height": H, "dev_fee": fee,
                                  "branches": [{"pools": [snap(xa)], "path": [hx(None), hx(T_A)], "amountIn": ge},
                                               {"pools": [snap(xa2)], "path": [hx(None), hx(T_A)], "amountIn": ge}]})
    if out:
        check("  equal shares were nudged apart by a mojo, total preserved",
              [int(b[0]) for b in out["branch_amounts"]] == [ge + 1, ge - 1], f"{out['branch_amounts']}")
    attempt("split sharing a pool", {"action": "split-swap", "offer": offer.to_bech32(), "current_height": H,
                                     "branches": [{"pools": [snap(xa)], "path": [hx(None), hx(T_A)], "amountIn": g1},
                                                  {"pools": [snap(xa)], "path": [hx(None), hx(T_A)], "amountIn": g2}]}, expect_ok=False)

    print("split with a CAT entry (A -> B directly and A -> XCH -> B)")
    ga1, ga2 = 30_000, 20_000
    b1 = swap_out(ab, 0, 1, ga1)
    x2 = swap_out(xa, 1, 0, ga2)
    b2 = swap_out(xb, 0, 1, x2)
    offer = fabricate_offer({T_A: ga1 + ga2}, {T_B: (b1 + b2) * 99 // 100}, salt=0x65)
    out = attempt("CAT split", {"action": "split-swap", "offer": offer.to_bech32(), "current_height": H, "dev_fee": fee,
                                "branches": [{"pools": [snap(ab)], "path": [hx(T_A), hx(T_B)], "amountIn": ga1},
                                             {"pools": [snap(xa), snap(xb)], "path": [hx(T_A), hx(None), hx(T_B)], "amountIn": ga2}]})
    if out:
        check("  branch amounts", [[int(a) for a in b] for b in out["branch_amounts"]] == [[ga1, b1], [ga2, x2, b2]])

    print("vault route: XCH -> vault LP on the LP pool, redeemed in the vault")
    gross = 50_000_000
    lp_out = swap_out(xlp, 0, 1, gross)
    vf = forge_math.vault_fee_bps(1, 10, vault.fee_bps)
    redeemed = forge_math.withdrawal_amounts(vault.state[0], lp_out, vault.state[1], vf)[0]
    offer = fabricate_offer({None: gross}, {T_A: redeemed * 99 // 100}, salt=0x66)
    out = attempt("vault route", {"action": "vault-route", "offer": offer.to_bech32(), "current_height": H, "dev_fee": fee,
                                  "swapPool": snap(xlp), "assetIn": hx(None), "vault": snap(vault)})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  swap_out and redeemed as quoted", int(out["swap_out"]) == lp_out and int(out["redeemed"]) == redeemed, f"{out['swap_out']} LP -> {out['redeemed']}")
        check("  trader is paid exactly the request", paid_to(bundle, TRADER_PH, T_A) == redeemed * 99 // 100)
        check("  the vault's LP supply fell by the burn", int(out["pools"][1]["state"]["total_lp"]) == vault.state[1] - lp_out)

    print("wrap: XCH -> A on xa, the A deposited into the vault mid-route, its LP sold for XCH on xlp")
    offered_x = 300_000_000
    gross_w, backing = v11r.wrap_backing([xa, vault, xlp], [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None)], offered_x)
    a_out, lp_mint, x_back = v11r.simulate_chain([xa, vault, xlp], [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None)], gross_w)
    check("  the fixed point splits the offered XCH into entry and backing", gross_w + backing == offered_x and backing == lp_mint,
          f"gross {gross_w} backing {backing} mint {lp_mint}")
    want_w = x_back * 99 // 100
    offer = fabricate_offer({None: offered_x}, {None: want_w}, salt=0x6b)
    out = attempt("wrap route", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap(xa), snap(vault), snap(xlp)],
                                 "path": [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None)], "current_height": H, "dev_fee": fee})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  amounts follow the chain: entry, A, LP minted, XCH back", [int(a) for a in out["amounts"]] == [gross_w, a_out, lp_mint, x_back], f"{out['amounts']}")
        check("  the wrap reports its deposit, mint and backing",
              int(out["wrap"]["deposit"]) == a_out and int(out["wrap"]["minted"]) == lp_mint and int(out["wrap"]["leftover_xch"]) == 0, f"{out['wrap']}")
        check("  trader is paid exactly the request in XCH", paid_to(bundle, TRADER_PH, None) == want_w)
        check("  router gets the XCH surplus", paid_to(bundle, ROUTER_PH, None) == x_back - want_w)
        check("  no LP leaves the route", paid_to(bundle, TRADER_PH, vault.lp_asset_id) == 0 and paid_to(bundle, ROUTER_PH, vault.lp_asset_id) == 0)
        check("  successors in path order", [s["launcher_id"] for s in out["pools"]] == [xa.launcher_id.hex(), vault.launcher_id.hex(), xlp.launcher_id.hex()])
        vault_after = out["pools"][1]["state"]
        check("  the vault holds the deposit and minted the LP",
              [int(r) for r in vault_after["reserves"]] == [vault.state[0][0] + a_out] and int(vault_after["total_lp"]) == vault.state[1] + lp_mint,
              f"{vault_after['reserves']} lp {vault_after['total_lp']}")
        xlp_after = out["pools"][2]["state"]
        x0, l0 = xlp.state[0]
        gross_x = forge_math.swap_output(l0, x0, lp_mint, xlp.fee_bps, xlp.weights[1], xlp.weights[0])
        check("  the LP pool took the minted LP and released the XCH", [int(r) for r in xlp_after["reserves"]] == [x0 - gross_x, l0 + lp_mint], f"{xlp_after['reserves']}")
    attempt("wrapping the wrong asset into the vault", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap(xb), snap(vault), snap(xlp)],
                                                        "path": [hx(None), hx(T_B), hx(vault.lp_asset_id), hx(None)], "current_height": H, "dev_fee": fee}, expect_ok=False)
    attempt("a wrap with no XCH to back it", {"action": "multihop-swap", "offer": fabricate_offer({T_A: 50_000}, {None: 1}, salt=0x6c).to_bech32(),
                                              "pools": [snap(vault), snap(xlp)], "path": [hx(T_A), hx(vault.lp_asset_id), hx(None)], "current_height": H, "dev_fee": fee}, expect_ok=False)
    print("wrap as the entry: the trader offers A plus the backing, and is paid XCH")
    dep_a = 50_000
    lp_mint2, x_back2 = v11r.simulate_chain([vault, xlp], [hx(T_A), hx(vault.lp_asset_id), hx(None)], dep_a)
    offer = fabricate_offer({T_A: dep_a, None: lp_mint2 + 777}, {None: x_back2 * 99 // 100}, salt=0x6d)
    out = attempt("wrap entry", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap(vault), snap(xlp)],
                                 "path": [hx(T_A), hx(vault.lp_asset_id), hx(None)], "current_height": H, "dev_fee": fee})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  the whole offered A is wrapped", int(out["wrap"]["deposit"]) == dep_a and int(out["wrap"]["minted"]) == lp_mint2)
        check("  XCH beyond the backing comes back with the output", int(out["wrap"]["leftover_xch"]) == 777
              and paid_to(bundle, TRADER_PH, None) == x_back2 * 99 // 100 and paid_to(bundle, ROUTER_PH, None) == x_back2 + 777 - x_back2 * 99 // 100)

    print("revisiting route: XCH -> A on xa, wrapped in the vault, the LP sold for XCH on xlp, that XCH buying A again on xa2")
    rpools = [xa, vault, xlp, xa2]
    rpath = [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None), hx(T_A)]
    offered_r = 400_000_000
    gross_r, backing_r = v11r.wrap_backing(rpools, rpath, offered_r)
    outs_r = v11r.simulate_chain(rpools, rpath, gross_r)
    want_r = outs_r[-1] * 99 // 100
    offer = fabricate_offer({None: offered_r}, {T_A: want_r}, salt=0x6e)
    out = attempt("revisiting route", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap(p) for p in rpools],
                                       "path": rpath, "current_height": H, "dev_fee": fee})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  amounts follow the chain through both passes", [int(a) for a in out["amounts"]] == [gross_r, *outs_r], f"{out['amounts']}")
        check("  trader is paid the A of the second pass", paid_to(bundle, TRADER_PH, T_A) == want_r)
        check("  router gets the A surplus", paid_to(bundle, ROUTER_PH, T_A) == outs_r[-1] - want_r)
        check("  the XCH released mid-route reached xa2, not the trader", paid_to(bundle, TRADER_PH, None) == 0 and paid_to(bundle, ROUTER_PH, None) == 0)
        check("  four successors in path order", [s["launcher_id"] for s in out["pools"]] == [p.launcher_id.hex() for p in rpools])
        xa2_after = out["pools"][3]["state"]
        check("  xa2 took the released XCH", int(xa2_after["reserves"][0]) == xa2.state[0][0] + outs_r[2], f"{xa2_after['reserves']}")
    attempt("a chain leg fed the wrong asset", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap(xa), snap(bc)],
                                                 "path": [hx(None), hx(T_A), hx(T_C)], "current_height": H, "dev_fee": fee}, expect_ok=False)

    print("flow: a triangle XCH -> A -> B -> XCH with the entry rescaled to the offer")
    gross = 200_000_000
    fa = swap_out(xa, 0, 1, gross)
    fb = swap_out(ab, 0, 1, fa)
    fx = swap_out(xb, 1, 0, fb)
    offer = fabricate_offer({None: gross}, {None: fx * 99 // 100 if fx < gross else gross // 2}, salt=0x67)
    out = attempt("triangle flow", {"action": "flow-balance", "offer": offer.to_bech32(), "current_height": H, "startAsset": hx(None),
                                    "legs": [{"pool": snap(xa), "assetIn": hx(None), "assetOut": hx(T_A), "amountIn": gross // 2},
                                             {"pool": snap(ab), "assetIn": hx(T_A), "assetOut": hx(T_B), "amountIn": 1},
                                             {"pool": snap(xb), "assetIn": hx(T_B), "assetOut": hx(None), "amountIn": 1}]})
    if out:
        check("  legs re-derived in order", [[int(a), int(b)] for a, b in out["leg_amounts"]] == [[gross, fa], [fa, fb], [fb, fx]], f"{out['leg_amounts']}")
        check("  total_out is the XCH released", int(out["total_out"]) == fx)

    print("flow: a merge and a pool crossed twice (two actions in one spend)")
    # XCH splits into A (via xa) and B (via xb); A -> B on ab; both B streams merge into xb back to XCH,
    # so xb is crossed twice (XCH->B, then B->XCH on its post-swap state).
    gx = 300_000_000
    d1, d2 = 2, 1
    ga, gb = gx * d1 // 3, gx * d2 // 3
    a_out = swap_out(xa, 0, 1, ga)
    b_from_xb = swap_out(xb, 0, 1, gb)
    b_from_ab = swap_out(ab, 0, 1, a_out)
    offer = fabricate_offer({None: gx}, {None: 1}, salt=0x68)
    out = attempt("merge flow", {"action": "flow-balance", "offer": offer.to_bech32(), "current_height": H, "startAsset": hx(None),
                                 "legs": [{"pool": snap(xa), "assetIn": hx(None), "assetOut": hx(T_A), "amountIn": d1},
                                          {"pool": snap(xb), "assetIn": hx(None), "assetOut": hx(T_B), "amountIn": d2},
                                          {"pool": snap(ab), "assetIn": hx(T_A), "assetOut": hx(T_B), "amountIn": 1},
                                          {"pool": snap(xb), "assetIn": hx(T_B), "assetOut": hx(None), "amountIn": 1}]})
    if out:
        legs = [[int(a), int(b)] for a, b in out["leg_amounts"]]
        check("  entry legs got their declared shares", legs[0][0] == ga and legs[1][0] == gb, f"{legs}")
        check("  the merged leg drank both B streams", legs[3][0] == b_from_xb + b_from_ab)
        check("  one successor per distinct pool", len(out["pools"]) == 3)
        xb_after = next(s for s in out["pools"] if s["launcher_id"] == xb.launcher_id.hex())
        # first crossing: XCH in (gb), B out (honest1); second: B in (legs[3][0]), XCH out (honest2) priced on the first's state
        x0, b0 = xb.state[0]
        honest1 = forge_math.swap_output(x0, b0, gb, xb.fee_bps, xb.weights[0], xb.weights[1])
        honest2 = forge_math.swap_output(b0 - honest1, x0 + gb, legs[3][0], xb.fee_bps, xb.weights[1], xb.weights[0])
        check("  the twice-crossed pool's reserves reflect both actions",
              [int(x) for x in xb_after["state"]["reserves"]] == [x0 + gb - honest2, b0 - honest1 + legs[3][0]],
              f"{xb_after['state']['reserves']}")
    attempt("flow repeating a pair", {"action": "flow-balance", "offer": offer.to_bech32(), "current_height": H, "startAsset": hx(None),
                                      "legs": [{"pool": snap(xa), "assetIn": hx(None), "assetOut": hx(T_A), "amountIn": 1},
                                               {"pool": snap(xa), "assetIn": hx(None), "assetOut": hx(T_A), "amountIn": 1}]}, expect_ok=False)

    print("routed deposit: uneven XCH + A into the triple, selling part of the A into B first")
    dep_x, dep_a, sell_a = 2_000_000_000, 80_000, 30_000
    b_bought = swap_out(ab, 0, 1, sell_a)
    deposits = [0, dep_a - sell_a, b_bought]
    # the XCH deposit is what is left of the offered XCH after the backing
    mint = forge_math.invariant_lp_mint(triple.state[0], [dep_x, *deposits[1:]], triple.state[1], triple.fee_bps, triple.weights, version=10)
    offer = fabricate_offer({None: dep_x + mint, T_A: dep_a}, {triple.lp_asset_id: mint * 95 // 100}, salt=0x69)
    out = attempt("routed deposit", {"action": "routed-deposit", "offer": offer.to_bech32(), "current_height": H, "pool": snap(triple),
                                     "sales": [{"pools": [snap(ab)], "path": [hx(T_A), hx(T_B)], "amountIn": sell_a}], "dev_fee": fee})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        minted = int(out["minted"])
        check("  the sale bought B for the deposit", [int(a) for a in out["sale_outputs"]] == [b_bought])
        check("  deposits: the unsold A, the bought B, and XCH net of backing",
              int(out["deposits"][hx(T_A)]) == dep_a - sell_a and int(out["deposits"][hx(T_B)]) == b_bought
              and int(out["deposits"][hx(None)]) + minted == dep_x + mint, f"{out['deposits']} minted {minted}")
        check("  trader receives exactly the requested LP", paid_to(bundle, TRADER_PH, triple.lp_asset_id) == mint * 95 // 100)
        check("  router gets the LP surplus", paid_to(bundle, ROUTER_PH, triple.lp_asset_id) == minted - mint * 95 // 100)
        check("  successors: the sale pool then the target", [s["launcher_id"] for s in out["pools"]] == [ab.launcher_id.hex(), triple.launcher_id.hex()])
        check("  target snapshot grew total_lp by the mint", int(out["target"]["state"]["total_lp"]) == triple.state[1] + minted)

    print("zap: XCH only into xa, part of it swapped into A inside the same pool, then the add")
    zap_x, zap_swap = 3_000_000_000, 1_000_000_000
    x0, a0 = xa.state[0]
    gross_a = forge_math.swap_output(x0, a0, zap_swap, xa.fee_bps, xa.weights[0], xa.weights[1])
    a_out = gross_a - gross_a * xa.protocol_fee_bps // 10_000
    post = [x0 + zap_swap, a0 - gross_a]
    est = forge_math.invariant_lp_mint(post, [zap_x - zap_swap, a_out], xa.state[1], xa.fee_bps, xa.weights, version=10)
    offer = fabricate_offer({None: zap_x + est}, {xa.lp_asset_id: est * 95 // 100}, salt=0x6a)
    out = attempt("zap through the target", {"action": "routed-deposit", "offer": offer.to_bech32(), "current_height": H, "pool": snap(xa),
                                            "sales": [{"pools": [snap(xa)], "path": [hx(None), hx(T_A)], "amountIn": zap_swap}], "dev_fee": fee})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        minted = int(out["minted"])
        check("  one pool, one successor", [s["launcher_id"] for s in out["pools"]] == [xa.launcher_id.hex()])
        check("  the swap bought A at the pool's own rate", [int(a) for a in out["sale_outputs"]] == [a_out])
        check("  deposits: the bought A and the XCH left after the swap and the backing",
              int(out["deposits"][hx(T_A)]) == a_out and int(out["deposits"][hx(None)]) + minted == zap_x + est - zap_swap,
              f"{out['deposits']} minted {minted}")
        check("  the mint is priced on the post-swap state", minted == forge_math.invariant_lp_mint(
            post, [zap_x + est - zap_swap - minted, a_out], xa.state[1], xa.fee_bps, xa.weights, version=10), f"{minted} vs {est}")
        check("  trader receives exactly the requested LP", paid_to(bundle, TRADER_PH, xa.lp_asset_id) == est * 95 // 100)
        check("  router gets the LP surplus", paid_to(bundle, ROUTER_PH, xa.lp_asset_id) == minted - est * 95 // 100)
        after = out["target"]["state"]
        check("  reserves reflect swap then add", [int(r) for r in after["reserves"]] == [post[0] + int(out["deposits"][hx(None)]), post[1] + a_out],
              f"{after['reserves']}")
        check("  the protocol fee on the swap is owed", [int(f) for f in after["fees_owed"]] == [0, gross_a - a_out], f"{after['fees_owed']}")
        check("  total_lp grew by the mint", int(after["total_lp"]) == xa.state[1] + minted)

    print("lane guards")
    v10_like = dict(snap(xa)); v10_like["protocol_version"] = 10
    attempt("a route mixing revisions", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap(xa), v10_like],
                                         "path": [hx(None), hx(T_A), hx(T_B)], "current_height": H}, expect_ok=False)
    attempt("swapping through a vault", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap(xlp), snap(vault)],
                                         "path": [hx(None), hx(vault.lp_asset_id), hx(T_A)], "current_height": H}, expect_ok=False)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} V11 route-lane checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
