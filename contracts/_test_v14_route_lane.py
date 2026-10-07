#!/usr/bin/env python3
"""The V11 route lanes through forge_stdin, keyless, in the simulator.

Fabricated wallet offers (see _test_v14_offer_lane) drive multi-hop, split,
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
import forge_v14_driver as drv
import forge_v14_offer as v14
import forge_v14_route as v14r  # noqa: E402
from _test_v14_offer_lane import ROUTER_PH, T_A, T_B, TRADER_PH, H, fabricate_offer, make, paid_to
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
    return v14.pool_to_snapshot(pool)


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
        check("  successor snapshot rebuilds", v14.snapshot_to_pool(s).coin.puzzle_hash.hex() == s["pool_coin"]["puzzle_hash"])
    return out


def exact_offer(payload: dict, offered: dict, out_asset, salt: int, bps: int = 0, fee_asset="auto"):
    """The offer a quote produces for this route (F3, 2026-10-07): the router's rate paid by
    the trader's own spend beside the entry settlement, and EXACTLY what the route releases
    asked for -- read from the composer's own preview of the same payload. Returns the
    offer and the amount it asks for."""
    offered = dict(offered)
    payments = None
    if bps:
        asset = next(iter(offered)) if fee_asset == "auto" else fee_asset
        fee = offered[asset] * bps // 10_000
        offered[asset] -= fee
        payments = {asset: [(ROUTER_PH, fee)]}
    probe = fabricate_offer(offered, {out_asset: 1}, salt=salt, payments=payments)
    preview = run({**payload, "offer": probe.to_bech32(), "preview": True})
    want = int(preview["forge"]["releases"][hx(out_asset)])
    return fabricate_offer(offered, {out_asset: want}, salt=salt, payments=payments), want


def main() -> int:
    if not drv.v14_available():
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
    hop2 = {"action": "multihop-swap", "pools": [snap(xa), snap(ab)], "path": [hx(None), hx(T_A), hx(T_B)], "current_height": H, "dev_fee": fee}
    offer, want = exact_offer(hop2, {None: gross}, T_B, salt=0x61)
    check("  the composer's preview releases what the chain computes", want == o2, f"{want} vs {o2}")
    out = attempt("2-hop", {**hop2, "offer": offer.to_bech32()})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  amounts follow the chain", [int(a) for a in out["amounts"]] == [gross, o1, o2], f"{out['amounts']}")
        # The router is paid on the ENTRY (nothing, at 0 bps), so the trader receives every
        # mojo the last pool released: exactly what the offer asked for, nothing left over.
        check("  the whole output reaches the trader", paid_to(bundle, TRADER_PH, T_B) == o2, f"{o2}")
        check("  the router takes nothing out of the output", paid_to(bundle, ROUTER_PH, T_B) == 0)
        check("  the intermediate A never reaches anyone", paid_to(bundle, TRADER_PH, T_A) == 0 and paid_to(bundle, ROUTER_PH, T_A) == 0)

    # The block above runs the router at 0 bps, which cannot tell a fee that is collected
    # correctly from a fee that is never collected at all. These run it at a real rate.
    print("multi-hop with a real router rate: the fee comes off the ENTRY, once")
    for bps in (30, 300, 1000):
        charged = {"puzzle_hash": ROUTER_PH.hex(), "bps": bps}
        entry_fee = gross * bps // 10_000
        net_in = gross - entry_fee
        n1 = swap_out(xa, 0, 1, net_in)
        n2 = swap_out(ab, 0, 1, n1)
        # the trader's own spend pays the fee beside the entry settlement, which holds the net
        offer_n, asked = exact_offer({**hop2, "dev_fee": charged}, {None: gross}, T_B, salt=0x70 + (bps % 100), bps=bps)
        check(f"  {bps} bps: the preview prices the route on the net entry", asked == n2, f"{asked} vs {n2}")
        out_n = attempt(f"2-hop at {bps} bps", {**hop2, "dev_fee": charged, "offer": offer_n.to_bech32()})
        if not out_n:
            continue
        b = SpendBundle.from_json_dict(out_n["bundle"])
        check(f"  {bps} bps: the router is paid in the ENTRY asset by the trader's own spend, not out of the output",
              paid_to(b, ROUTER_PH, None) == entry_fee and paid_to(b, ROUTER_PH, T_B) == 0,
              f"{paid_to(b, ROUTER_PH, None)} vs {entry_fee}")
        check(f"  {bps} bps: the route is priced on the NET input",
              [int(a) for a in out_n["amounts"]] == [net_in, n1, n2], f"{out_n['amounts']}")
        check(f"  {bps} bps: the trader keeps the whole output", paid_to(b, TRADER_PH, T_B) == n2)
        check(f"  {bps} bps: the response reports what was actually taken",
              int(out_n["dev_fee_collected"]) == entry_fee, out_n["dev_fee_collected"])
        check("  two successors, in input order", [s["launcher_id"] for s in out["pools"]] == [xa.launcher_id.hex(), ab.launcher_id.hex()])
    attempt("2-hop, greedy trader", {**hop2, "offer": fabricate_offer({None: gross}, {T_B: o2 + 1}, salt=0x62).to_bech32()}, expect_ok=False)
    attempt("2-hop, a stale quote (one mojo under)", {**hop2, "offer": fabricate_offer({None: gross}, {T_B: o2 - 1}, salt=0x62).to_bech32()},
            expect_ok=False)

    print("multi-hop XCH -> A -> B -> C (three pools, CAT bridges)")
    o3 = swap_out(bc, 0, 1, o2)
    hop3 = {"action": "multihop-swap", "pools": [snap(xa), snap(ab), snap(bc)], "path": [hx(None), hx(T_A), hx(T_B), hx(T_C)],
            "current_height": H, "dev_fee": fee}
    offer, want = exact_offer(hop3, {None: gross}, T_C, salt=0x63)
    check("  the preview follows the chain", want == o3)
    out = attempt("3-hop", {**hop3, "offer": offer.to_bech32()})
    if out:
        check("  amounts follow the chain", [int(a) for a in out["amounts"]] == [gross, o1, o2, o3])

    print("split XCH -> A across two pools")
    g1, g2 = 60_000_000, 40_000_000
    s1, s2 = swap_out(xa, 0, 1, g1), swap_out(xa2, 0, 1, g2)
    split2 = {"action": "split-swap", "current_height": H, "dev_fee": fee,
              "branches": [{"pools": [snap(xa)], "path": [hx(None), hx(T_A)], "amountIn": g1},
                           {"pools": [snap(xa2)], "path": [hx(None), hx(T_A)], "amountIn": g2}]}
    offer, want = exact_offer(split2, {None: g1 + g2}, T_A, salt=0x64)
    check("  the preview sums the branches", want == s1 + s2)
    out = attempt("split", {**split2, "offer": offer.to_bech32()})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  branch amounts are the declared shares of the offered XCH", [[int(a) for a in b] for b in out["branch_amounts"]] == [[g1, s1], [g2, s2]])
        check("  total_out is the sum", int(out["total_out"]) == s1 + s2)
        check("  both branches' output reaches the trader, exactly", paid_to(bundle, TRADER_PH, T_A) == s1 + s2)
        check("  the router takes nothing out of the output", paid_to(bundle, ROUTER_PH, T_A) == 0)
    # an equal split would create two identical entry children; the composer nudges the
    # shares apart by one mojo so the coins differ and the total still matches the offer
    ge = 50_000_000
    se = swap_out(xa, 0, 1, ge + 1) + swap_out(xa2, 0, 1, ge - 1)
    spliteq = {"action": "split-swap", "current_height": H, "dev_fee": fee,
               "branches": [{"pools": [snap(xa)], "path": [hx(None), hx(T_A)], "amountIn": ge},
                            {"pools": [snap(xa2)], "path": [hx(None), hx(T_A)], "amountIn": ge}]}
    offer_eq, want_eq = exact_offer(spliteq, {None: 2 * ge}, T_A, salt=0x6A)
    check("  the preview prices the nudged shares", want_eq == se)
    out = attempt("equal split", {**spliteq, "offer": offer_eq.to_bech32()})
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
    splitc = {"action": "split-swap", "current_height": H, "dev_fee": fee,
              "branches": [{"pools": [snap(ab)], "path": [hx(T_A), hx(T_B)], "amountIn": ga1},
                           {"pools": [snap(xa), snap(xb)], "path": [hx(T_A), hx(None), hx(T_B)], "amountIn": ga2}]}
    offer, want = exact_offer(splitc, {T_A: ga1 + ga2}, T_B, salt=0x65)
    check("  the preview sums both branches", want == b1 + b2)
    out = attempt("CAT split", {**splitc, "offer": offer.to_bech32()})
    if out:
        check("  branch amounts", [[int(a) for a in b] for b in out["branch_amounts"]] == [[ga1, b1], [ga2, x2, b2]])

    print("vault route: XCH -> vault LP on the LP pool, redeemed in the vault")
    gross = 50_000_000
    lp_out = swap_out(xlp, 0, 1, gross)
    vf = forge_math.vault_fee_bps(1, 10, vault.fee_bps)
    redeemed = forge_math.withdrawal_amounts(vault.state[0], lp_out, vault.state[1], vf)[0]
    vroute = {"action": "vault-route", "current_height": H, "dev_fee": fee, "swapPool": snap(xlp), "assetIn": hx(None), "vault": snap(vault)}
    offer, want = exact_offer(vroute, {None: gross}, T_A, salt=0x66)
    check("  the preview is the redeemed amount", want == redeemed)
    out = attempt("vault route", {**vroute, "offer": offer.to_bech32()})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  swap_out and redeemed as quoted", int(out["swap_out"]) == lp_out and int(out["redeemed"]) == redeemed, f"{out['swap_out']} LP -> {out['redeemed']}")
        check("  the redeemed amount reaches the trader in full", paid_to(bundle, TRADER_PH, T_A) == redeemed)
        check("  the vault's LP supply fell by the burn", int(out["pools"][1]["state"]["total_lp"]) == vault.state[1] - lp_out)

    print("wrap: XCH -> A on xa, the A deposited into the vault mid-route, its LP sold for XCH on xlp")
    offered_x = 300_000_000
    gross_w, backing = v14r.wrap_backing([xa, vault, xlp], [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None)], offered_x)
    a_out, lp_mint, x_back = v14r.simulate_chain([xa, vault, xlp], [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None)], gross_w)
    check("  the fixed point splits the offered XCH into entry and backing", gross_w + backing == offered_x and backing == lp_mint,
          f"gross {gross_w} backing {backing} mint {lp_mint}")
    wroute = {"action": "multihop-swap", "pools": [snap(xa), snap(vault), snap(xlp)],
              "path": [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None)], "current_height": H, "dev_fee": fee}
    offer, want_w = exact_offer(wroute, {None: offered_x}, None, salt=0x6b)
    check("  the preview is the XCH the LP pool releases", want_w == x_back, f"{want_w} vs {x_back}")
    out = attempt("wrap route", {**wroute, "offer": offer.to_bech32()})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  amounts follow the chain: entry, A, LP minted, XCH back", [int(a) for a in out["amounts"]] == [gross_w, a_out, lp_mint, x_back], f"{out['amounts']}")
        check("  the wrap reports its deposit, mint and backing",
              int(out["wrap"]["deposit"]) == a_out and int(out["wrap"]["minted"]) == lp_mint and int(out["wrap"]["leftover_xch"]) == 0, f"{out['wrap']}")
        check("  the whole XCH output reaches the trader", paid_to(bundle, TRADER_PH, None) == x_back)
        check("  the router takes nothing out of the output", paid_to(bundle, ROUTER_PH, None) == 0)
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

    # The same wrap route WITH the router charging. The fee is paid by the trader's own spend
    # beside the entry settlement (F3, 2026-10-07), so the settlement holds the net and the
    # split between the entry and the wrap's backing is taken on exactly that: nothing is
    # carved from the hub any more. (Until then the split had to be fee-aware, found on
    # 2026-09-16 when a 30,000,000 offer at 300 bps came up 56 mojos short.)
    print("wrap route with the router charging: the fee is paid beside the entry, the split is on the net")
    charged_w = {"puzzle_hash": ROUTER_PH.hex(), "bps": 300}
    wpath = [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None)]
    fee_c = offered_x * 300 // 10_000
    spend_c = offered_x - fee_c
    gross_c, backing_c = v14r.wrap_backing([xa, vault, xlp], wpath, spend_c)
    a_c, mint_c, back_c = v14r.simulate_chain([xa, vault, xlp], wpath, gross_c)
    check("  the two declared shares use up the net entry",
          gross_c + backing_c == spend_c, f"gross {gross_c} backing {backing_c} net {spend_c}")
    check("  the backing covers the mint of the entry's share", backing_c >= mint_c, f"backing {backing_c} vs mint {mint_c}")
    offer, want_c = exact_offer({**wroute, "dev_fee": charged_w}, {None: offered_x}, None, salt=0x6c, bps=300)
    check("  the preview is the XCH released plus the backing left over from the mint",
          want_c == back_c + (backing_c - mint_c), f"{want_c} vs {back_c} + {backing_c - mint_c}")
    out = attempt("wrap route, router charging", {**wroute, "dev_fee": charged_w, "offer": offer.to_bech32()})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        amts = [int(a) for a in out["amounts"]]
        check("  the entry is the declared gross of the net settlement", amts[0] == gross_c, f"builder {amts[0]} vs {gross_c}")
        check("  the router is paid its cut of the whole entry, once, by the trader's own spend",
              paid_to(bundle, ROUTER_PH, None) == fee_c, f"{paid_to(bundle, ROUTER_PH, None)} vs {fee_c}")
        check("  the trader is paid exactly the route's output, the leftover backing included",
              paid_to(bundle, TRADER_PH, None) == want_c and amts[-1] == back_c,
              f"trader {paid_to(bundle, TRADER_PH, None)}, route out {amts[-1]}, offline {back_c}")
        check("  successors in path order (charged)",
              [s["launcher_id"] for s in out["pools"]] == [xa.launcher_id.hex(), vault.launcher_id.hex(), xlp.launcher_id.hex()])
        vc = out["pools"][1]["state"]
        check("  the vault holds the deposit and minted the LP the route then sold",
              [int(r) for r in vc["reserves"]] == [vault.state[0][0] + amts[1]] and int(vc["total_lp"]) == vault.state[1] + amts[2],
              f"{vc['reserves']} lp {vc['total_lp']}")
        check("  no LP leaves the charged route",
              paid_to(bundle, TRADER_PH, vault.lp_asset_id) == 0 and paid_to(bundle, ROUTER_PH, vault.lp_asset_id) == 0)
    attempt("wrapping the wrong asset into the vault", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap(xb), snap(vault), snap(xlp)],
                                                        "path": [hx(None), hx(T_B), hx(vault.lp_asset_id), hx(None)], "current_height": H, "dev_fee": fee}, expect_ok=False)
    attempt("a wrap with no XCH to back it", {"action": "multihop-swap", "offer": fabricate_offer({T_A: 50_000}, {None: 1}, salt=0x6c).to_bech32(),
                                              "pools": [snap(vault), snap(xlp)], "path": [hx(T_A), hx(vault.lp_asset_id), hx(None)], "current_height": H, "dev_fee": fee}, expect_ok=False)
    print("wrap as the entry: the trader offers A plus the backing, and is paid XCH")
    dep_a = 50_000
    lp_mint2, x_back2 = v14r.simulate_chain([vault, xlp], [hx(T_A), hx(vault.lp_asset_id), hx(None)], dep_a)
    wentry = {"action": "multihop-swap", "pools": [snap(vault), snap(xlp)], "path": [hx(T_A), hx(vault.lp_asset_id), hx(None)],
              "current_height": H, "dev_fee": fee}
    offer, want_e = exact_offer(wentry, {T_A: dep_a, None: lp_mint2 + 777}, None, salt=0x6d)
    check("  the preview is the XCH released plus the 777 over-supplied", want_e == x_back2 + 777, f"{want_e}")
    out = attempt("wrap entry", {**wentry, "offer": offer.to_bech32()})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  the whole offered A is wrapped", int(out["wrap"]["deposit"]) == dep_a and int(out["wrap"]["minted"]) == lp_mint2)
        # The 777 mojos the trader over-supplied above the mint's backing are theirs: they
        # leave with the output, inside the group the trader signed for.
        check("  XCH beyond the backing comes back to the TRADER with the output",
              int(out["wrap"]["leftover_xch"]) == 777
              and paid_to(bundle, TRADER_PH, None) == x_back2 + 777
              and paid_to(bundle, ROUTER_PH, None) == 0,
              f"trader {paid_to(bundle, TRADER_PH, None)} of {x_back2 + 777}")

    # The same wrap entry WITH the router charging. The case above runs at 0 bps, where
    # "the whole offered A is wrapped" holds trivially. Charging, the entry hub carves the
    # fee out of the offered A before the vault sees it -- while the XCH backing is taken
    # from the offer directly and is NOT charged. A quote that wraps the whole A (the TS
    # mirror did, pinned by wrapRoute.check) promises a mint the vault never makes, and an
    # offer sized from it is refused. Found 2026-10-02 reading the lane after the same
    # mismatch on plain multi-hop paths ("route releases 142 ... asks 143").
    print("wrap as the entry with the router charging: the fee comes off the A, not the backing")
    charged_e = {"puzzle_hash": ROUTER_PH.hex(), "bps": 300}
    net_a = dep_a - dep_a * 300 // 10_000
    lp_net, x_net = v14r.simulate_chain([vault, xlp], [hx(T_A), hx(vault.lp_asset_id), hx(None)], net_a)
    offer, want_n = exact_offer({**wentry, "dev_fee": charged_e}, {T_A: dep_a, None: lp_net}, None, salt=0x6e, bps=300, fee_asset=T_A)
    check("  the preview prices the wrap on the net A", want_n == x_net, f"{want_n} vs {x_net}")
    out = attempt("wrap entry, router charging", {**wentry, "dev_fee": charged_e, "offer": offer.to_bech32()})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  the vault receives the A less the router's cut, and mints on that",
              int(out["wrap"]["deposit"]) == net_a and int(out["wrap"]["minted"]) == lp_net,
              f"{out['wrap']} vs deposit {net_a} mint {lp_net}")
        check("  the router is paid in A, its cut of the entry, by the trader's own spend, and nothing in XCH",
              paid_to(bundle, ROUTER_PH, T_A) == dep_a * 300 // 10_000 and paid_to(bundle, ROUTER_PH, None) == 0,
              f"A {paid_to(bundle, ROUTER_PH, T_A)} XCH {paid_to(bundle, ROUTER_PH, None)}")
        check("  the backing is exactly the net mint: none of it is charged, none left over",
              int(out["wrap"]["leftover_xch"]) == 0, f"{out['wrap']}")
    # An offer that pays no router fee at all, with the router charging, is refused.
    offer = fabricate_offer({T_A: dep_a, None: lp_mint2}, {None: x_back2}, salt=0x6f)
    attempt("an offer paying no router fee, router charging", {**wentry, "dev_fee": charged_e, "offer": offer.to_bech32()}, expect_ok=False)

    print("revisiting route: XCH -> A on xa, wrapped in the vault, the LP sold for XCH on xlp, that XCH buying A again on xa2")
    rpools = [xa, vault, xlp, xa2]
    rpath = [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None), hx(T_A)]
    offered_r = 400_000_000
    gross_r, backing_r = v14r.wrap_backing(rpools, rpath, offered_r)
    outs_r = v14r.simulate_chain(rpools, rpath, gross_r)
    rroute = {"action": "multihop-swap", "pools": [snap(p) for p in rpools], "path": rpath, "current_height": H, "dev_fee": fee}
    offer, want_r = exact_offer(rroute, {None: offered_r}, T_A, salt=0x6e)
    check("  the preview is the second pass's output", want_r == outs_r[-1], f"{want_r} vs {outs_r[-1]}")
    out = attempt("revisiting route", {**rroute, "offer": offer.to_bech32()})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  amounts follow the chain through both passes", [int(a) for a in out["amounts"]] == [gross_r, *outs_r], f"{out['amounts']}")
        check("  the second pass's whole output reaches the trader", paid_to(bundle, TRADER_PH, T_A) == outs_r[-1])
        check("  the router takes nothing out of the output", paid_to(bundle, ROUTER_PH, T_A) == 0)
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
    tri = {"action": "flow-balance", "current_height": H, "startAsset": hx(None),
           "legs": [{"pool": snap(xa), "assetIn": hx(None), "assetOut": hx(T_A), "amountIn": gross // 2},
                    {"pool": snap(ab), "assetIn": hx(T_A), "assetOut": hx(T_B), "amountIn": 1},
                    {"pool": snap(xb), "assetIn": hx(T_B), "assetOut": hx(None), "amountIn": 1}]}
    offer, want_t = exact_offer(tri, {None: gross}, None, salt=0x67)
    check("  the preview is the XCH the triangle returns", want_t == fx, f"{want_t} vs {fx}")
    out = attempt("triangle flow", {**tri, "offer": offer.to_bech32()})
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
    merge = {"action": "flow-balance", "current_height": H, "startAsset": hx(None),
             "legs": [{"pool": snap(xa), "assetIn": hx(None), "assetOut": hx(T_A), "amountIn": d1},
                      {"pool": snap(xb), "assetIn": hx(None), "assetOut": hx(T_B), "amountIn": d2},
                      {"pool": snap(ab), "assetIn": hx(T_A), "assetOut": hx(T_B), "amountIn": 1},
                      {"pool": snap(xb), "assetIn": hx(T_B), "assetOut": hx(None), "amountIn": 1}]}
    offer, _want_m = exact_offer(merge, {None: gx}, None, salt=0x68)
    out = attempt("merge flow", {**merge, "offer": offer.to_bech32()})
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
    rdep = {"action": "routed-deposit", "current_height": H, "pool": snap(triple),
            "sales": [{"pools": [snap(ab)], "path": [hx(T_A), hx(T_B)], "amountIn": sell_a}], "dev_fee": fee}
    offer, want_d = exact_offer(rdep, {None: dep_x + mint, T_A: dep_a}, triple.lp_asset_id, salt=0x69)
    check("  the preview is the mint", want_d == mint, f"{want_d} vs {mint}")
    # the same preview sized from amounts alone, a probe standing in for the offer: what
    # the page asks for before the wallet has built anything
    probe_out = run({**rdep, "preview": True, "probe": {"offered": {hx(None): str(dep_x + mint), hx(T_A): str(dep_a)},
                                                          "requested": [hx(triple.lp_asset_id)]}})
    check("  a preview from a probe (no offer) reports the same mint", int(probe_out["minted"]) == mint
          and probe_out.get("preview") is True and "bundle" not in probe_out, f"{probe_out.get('minted')} vs {mint}")
    out = attempt("routed deposit", {**rdep, "offer": offer.to_bech32()})
    if out:
        bundle = SpendBundle.from_json_dict(out["bundle"])
        minted = int(out["minted"])
        check("  the sale bought B for the deposit", [int(a) for a in out["sale_outputs"]] == [b_bought])
        check("  deposits: the unsold A, the bought B, and XCH net of backing",
              int(out["deposits"][hx(T_A)]) == dep_a - sell_a and int(out["deposits"][hx(T_B)]) == b_bought
              and int(out["deposits"][hx(None)]) + minted == dep_x + mint, f"{out['deposits']} minted {minted}")
        check("  the whole mint reaches the depositor", paid_to(bundle, TRADER_PH, triple.lp_asset_id) == minted == want_d)
        check("  the router takes no LP", paid_to(bundle, ROUTER_PH, triple.lp_asset_id) == 0)
        check("  successors: the sale pool then the target", [s["launcher_id"] for s in out["pools"]] == [ab.launcher_id.hex(), triple.launcher_id.hex()])
        check("  target snapshot grew total_lp by the mint", int(out["target"]["state"]["total_lp"]) == triple.state[1] + minted)

    print("zap: XCH only into xa, part of it swapped into A inside the same pool, then the add")
    zap_x, zap_swap = 3_000_000_000, 1_000_000_000
    x0, a0 = xa.state[0]
    gross_a = forge_math.swap_output(x0, a0, zap_swap, xa.fee_bps, xa.weights[0], xa.weights[1])
    a_out = gross_a - gross_a * xa.protocol_fee_bps // 10_000
    post = [x0 + zap_swap, a0 - gross_a]
    est = forge_math.invariant_lp_mint(post, [zap_x - zap_swap, a_out], xa.state[1], xa.fee_bps, xa.weights, version=10)
    zap = {"action": "routed-deposit", "current_height": H, "pool": snap(xa),
           "sales": [{"pools": [snap(xa)], "path": [hx(None), hx(T_A)], "amountIn": zap_swap}], "dev_fee": fee}
    offer, want_z = exact_offer(zap, {None: zap_x + est}, xa.lp_asset_id, salt=0x6a)
    out = attempt("zap through the target", {**zap, "offer": offer.to_bech32()})
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
        check("  the whole mint reaches the depositor, as the preview said", paid_to(bundle, TRADER_PH, xa.lp_asset_id) == minted == want_z)
        check("  the router takes no LP", paid_to(bundle, ROUTER_PH, xa.lp_asset_id) == 0)
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
    print(f"{passed}/{len(results)} V14 route-lane checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
