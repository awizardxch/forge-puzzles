#!/usr/bin/env python3
"""The V20 route lanes carry each rCAT's hidden puzzle hash H end to end, offline.

_test_v20_hidden_lanes.py proves the single-pool lanes (swap, add, remove) settle against layered
coins at the pool CONFIG's H. This suite proves the ROUTE composer (forge_v20_route) does the same
for every coin it builds between pools, through forge_stdin (the router's own entry point):

  * multi-hop with the rCAT as the entry, the middle (bridge) asset, the exit, at both ends under two
    different H, and on a 3-hop route shaped like the one the testnet router quotes;
  * split routes: an rCAT exit paid from a hub of two pools' payouts (a layered ring), an rCAT entry
    split into layered children, a branch bridging through an rCAT, and a split beside a taken
    (Dexie-style) maker offer selling the rCAT layered;
  * a route across revisions (a V14 pool, then a V20 rCAT pool) keeps the V14 leg as it was;
  * the router's fee: site-built on an rCAT entry (paid layered by the trader's own spend) and
    wallet-built off an rCAT payout (the router's group paid layered);
  * a probe preview (no offer yet) sizes an rCAT entry from the pools' H;
  * every accepted bundle is checked spend by spend: each rCAT coin spent is under the revocation
    layer with its asset's H, through the owner path, and every coin those spends create is
    CAT(R, rev(H, p)) -- no plain coin of an rCAT is spent or created (keeps_h).

Negatives: a route that drops H on a bridge coin (the composer as it was before this change) is
refused at consensus; a plain coin of R offered as the entry is not R's settlement; a requested rCAT
payment the maker asserted at the plain settlement hash is refused; two pools naming different H for
one asset are refused.

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
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH, Offer
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin
import forge_v20_driver as drv
import forge_v20_offer as v20
import forge_v20_route as v20r
import _v14_testkit as kit14
import forge_v14_offer as v14
from forge_offer import ZERO_32

IDENTITY = Program.to(1)
TRADER_PH = bytes32(b"\x77" * 32)
ROUTER_PH = bytes32(b"\x66" * 32)
MAKER_PH = bytes32(b"\x88" * 32)
PLAIN = bytes32(b"\xa1" * 32)
R = bytes32(b"\xc8" * 32)            # an rCAT under H
R2 = bytes32(b"\xe2" * 32)           # a second rCAT, under H2 (a different issuer)
H = bytes32(b"\x48" * 32)
H2 = bytes32(b"\x49" * 32)
H_BAD = bytes32(b"\x4a" * 32)
HEIGHT = 4_700_000
RCATS = {R: H, R2: H2}
CAT_MOD_HASH = CAT_MOD.get_tree_hash()
REV_HASH = bytes32(drv.REVOCATION_LAYER.get_tree_hash())
results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def run(payload):
    return forge_stdin.build(json.loads(json.dumps(payload)))


def hx(asset):
    return (ZERO_32 if asset is None else asset).hex()


def snap(pool):
    return v20.pool_to_snapshot(pool)


def pool(assets, reserves, salt, hiddens=None, weights=None):
    return drv.make_pool(assets, reserves, total_lp=5_000_000, leaves="forge", salt=salt, weights=weights,
                         protocol_ph=bytes32(b"\x55" * 32), last_height=HEIGHT - 10,
                         hidden_puzzle_hashes=hiddens or [ZERO_32] * len(assets))


def full_ph(asset, inner_ph, hidden=None) -> bytes32:
    if asset is None:
        return inner_ph
    inner = inner_ph if drv.is_plain(hidden) else drv.REVOCATION_LAYER.curry(REV_HASH, bytes(hidden), inner_ph).get_tree_hash()
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


def fabricate(offered: dict, requested: dict, salt: int, hide: dict | None = None, payments: dict | None = None,
              announce_hide: dict | None = None) -> Offer:
    """A trader's offer. An offered rCAT coin is CAT(asset, rev(H, (1))) spent through the layer's
    owner path, so its settlement is CAT(asset, rev(H, OFFER_MOD)) -- the coin a wallet holding the
    rCAT makes; `payments` (asset -> [(ph, amount)]) are created beside it by the same spend (the
    router's input-side fee, re-wrapped with H by the layer). Each requested group is asserted over
    the settlement puzzle hash at `announce_hide` (default: `hide`, the honest H)."""
    hide = dict(hide if hide is not None else RCATS)
    announce_hide = dict(hide if announce_hide is None else announce_hide)
    payments = payments or {}
    coins = []
    for i, (asset, amount) in enumerate(offered.items()):
        held = amount + sum(a for _, a in payments.get(asset, []))
        if asset is None:
            coin = Coin(bytes32(bytes([salt, i]) * 16), IDENTITY.get_tree_hash(), uint64(held))
        else:
            outer = construct_cat_puzzle(CAT_MOD, asset, drv.layered(hide.get(asset), IDENTITY)).get_tree_hash()
            grand = bytes32(bytes([salt, 0x10 + i]) * 16)
            coin = Coin(drv.coin_id(grand, outer, held), outer, uint64(held))
        coins.append((asset, coin))
    notarized = Offer.notarize_payments(
        {a: [CreateCoin(ph, uint64(v), [ph]) for ph, v in (spec if isinstance(spec, list) else [(TRADER_PH, spec)])]
         for a, spec in requested.items()}, [c for _, c in coins])
    announcements = []
    for asset, group in notarized.items():
        settle = v20.settle_ph(asset, announce_hide.get(asset))
        msg = Program.to((group[0].nonce, [p.as_condition_args() for p in group])).get_tree_hash()
        announcements.append([63, bytes32(drv.hashlib.sha256(bytes(settle) + bytes(msg)).digest())])
    spends, rings = [], {}
    for i, (asset, coin) in enumerate(coins):
        conditions = [[51, OFFER_MOD_HASH, int(offered[asset])], *[[51, ph, a, [ph]] for ph, a in payments.get(asset, [])],
                      *announcements]
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


# ---- the H post-check, spend by spend (the same rules as testnet/tn_v17_rcat_chain_postcheck.py) ----

def _cat_parts(puzzle: Program):
    mod, args = puzzle.uncurry()
    if mod.get_tree_hash() != CAT_MOD_HASH:
        return None
    a = list(args.as_iter())
    return bytes32(a[1].as_atom()), a[2]


def _creates(conditions: Program) -> list:
    return [(bytes32(c.rest().first().as_atom()), c.rest().rest().first().as_int())
            for c in conditions.as_iter() if c.first().atom is not None and c.first().as_int() == 51]


def keeps_h(coin_spends, rcats: dict) -> dict:
    """For every spend of a coin of an rCAT in `rcats` (asset -> H): is its CAT inner the revocation
    layer curried with that H, is it spent through the owner path, and is every coin it creates
    CAT(R, rev(H, p)) for a p the layer's inner puzzle created? Returns counts and failures."""
    out = {"spends": 0, "outputs": 0, "hidden_path": 0, "bad": []}
    for cs in coin_spends:
        puzzle = Program.from_bytes(bytes(cs.puzzle_reveal))
        solution = Program.from_bytes(bytes(cs.solution))
        parts = _cat_parts(puzzle)
        if parts is None or parts[0] not in rcats:
            continue
        tail, inner = parts
        h = rcats[tail]
        out["spends"] += 1
        imod, iargs = inner.uncurry()
        ia = list(iargs.as_iter()) if imod.get_tree_hash() == REV_HASH else []
        if not ia or bytes32(ia[1].as_atom()) != h:
            out["bad"].append(f"spend of {cs.coin.name().hex()[:10]} is not under rev(H)")
            continue
        inner_solution = solution.first()
        if inner_solution.first().atom not in (None, b""):
            out["hidden_path"] += 1
            out["bad"].append(f"spend of {cs.coin.name().hex()[:10]} takes the hidden path")
            continue
        raw = _creates(inner_solution.rest().first().run(inner_solution.rest().rest().first()))
        allowed = {full_ph(tail, p, h) for p, _ in raw}
        for ph, amt in _creates(puzzle.run(solution)):
            out["outputs"] += 1
            if ph not in allowed:
                out["bad"].append(f"output {ph.hex()[:10]} {amt} of {tail.hex()[:8]} does not keep H")
    return out


def layered_tails(bundle) -> set:
    """The TAIL of every CAT spent in `bundle` whose CAT inner is the revocation layer."""
    tails = set()
    for cs in bundle.coin_spends:
        parts = _cat_parts(Program.from_bytes(bytes(cs.puzzle_reveal)))
        if parts is not None and parts[1].uncurry()[0].get_tree_hash() == REV_HASH:
            tails.add(parts[0])
    return tails


def check_keeps_h(bundle, rcats=RCATS, min_spends=1):
    k = keeps_h(bundle.coin_spends, rcats)
    check(f"  every rCAT coin spent is under rev(H) via the owner path, every one created keeps H "
          f"({k['spends']} spends, {k['outputs']} outputs)", k["spends"] >= min_spends and not k["bad"], "; ".join(k["bad"][:3]))
    return k


# ---- one route, end to end -------------------------------------------------------------------------

def released(payload, offered, out_asset, salt, payments=None, mode=None):
    probe = fabricate(offered, {out_asset: 1}, salt, payments=payments)
    p = {**payload, "offer": probe.to_bech32(), "preview": True}
    if mode:
        p["probe"] = {"router_fee_mode": mode}
    forge = run(p)["forge"]
    return int(forge["releases"][hx(out_asset)]), forge


def route_case(label, payload, offered: dict, out_asset, salt, expect=None, rcats=RCATS):
    """Preview, then settle an offer asking exactly the release; validate; check the trader is paid
    in a layered coin for an rCAT exit and every rCAT coin keeps H. Returns the bundle."""
    print(f"\n{label}")
    want, _ = released(payload, offered, out_asset, salt)
    if expect is not None:
        check("  the preview releases what the hop-by-hop chain computes", want == expect, f"{want} vs {expect}")
    offer = fabricate(offered, {out_asset: want}, salt)
    try:
        out = run({**payload, "offer": offer.to_bech32()})
    except Exception as exc:  # noqa: BLE001
        check("  forge_stdin builds the route", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return None
    bundle = SpendBundle.from_json_dict(out["bundle"])
    try:
        conds, _ = drv.validate(bundle)
        check("  the bundle validates under the mempool's rules", True, f"cost {conds.cost:,}")
    except Exception as exc:  # noqa: BLE001
        check("  the bundle validates under the mempool's rules", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return None
    h_out = rcats.get(out_asset)
    check(f"  the trader is paid {want} of {hx(out_asset)[:8]}" + (" in a layered coin (H kept)" if h_out else ""),
          paid_to(bundle, TRADER_PH, out_asset, h_out) == want
          and (h_out is None or paid_to(bundle, TRADER_PH, out_asset) == 0), f"{paid_to(bundle, TRADER_PH, out_asset, h_out)}")
    for s in out["pools"]:
        lane = v14 if int(s["protocol_version"]) == 15 else v20
        if lane.snapshot_to_pool(s).coin.puzzle_hash.hex() != s["pool_coin"]["puzzle_hash"]:
            check("  every successor snapshot rebuilds", False, s["launcher_id"][:8])
            break
    else:
        check("  every successor snapshot rebuilds (H in each V20 successor)", True)
    check_keeps_h(bundle, rcats)
    return bundle


def multihop(pools, path):
    return {"action": "multihop-swap", "pools": [p if isinstance(p, dict) else snap(p) for p in pools],
            "path": [hx(a) for a in path], "current_height": HEIGHT, "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}}


def split(branches):
    return {"action": "split-swap", "current_height": HEIGHT, "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0},
            "branches": [{"pools": [snap(p) for p in ps], "path": [hx(a) for a in path], "amountIn": int(n)}
                         for ps, path, n in branches]}


def main() -> int:
    if not drv.v20_available():
        print("  [skip] V20 puzzles not built")
        return 2
    assert PLAIN < R < R2, "asset order XCH < PLAIN < R < R2"
    xr = pool([None, R], [10_000_000_000, 30_000_000], 0x61, [ZERO_32, H])
    xp = pool([None, PLAIN], [10_000_000_000, 20_000_000], 0x62)
    xpr = pool([None, PLAIN, R], [10_000_000_000, 20_000_000, 30_000_000], 0x63, [ZERO_32, ZERO_32, H])
    rr2 = pool([R, R2], [30_000_000, 40_000_000], 0x64, [H, H2])
    xr2 = pool([None, R2], [10_000_000_000, 40_000_000], 0x65, [ZERO_32, H2])
    xr_b = pool([None, R], [6_000_000_000, 20_000_000], 0x66, [ZERO_32, H])
    sim = v20r.simulate_chain

    # ---- multi-hop, the rCAT in every position ----
    route_case("multi-hop, rCAT ENTRY: R -> XCH -> PLAIN", multihop([xr, xp], [R, None, PLAIN]), {R: 200_000}, PLAIN, 0x71,
               expect=sim([xr, xp], [R, None, PLAIN], 200_000)[-1])
    route_case("multi-hop, rCAT MIDDLE (bridge coin): PLAIN -> R -> XCH", multihop([xpr, xr], [PLAIN, R, None]),
               {PLAIN: 200_000}, None, 0x72, expect=sim([xpr, xr], [PLAIN, R, None], 200_000)[-1])
    route_case("multi-hop, rCAT EXIT: PLAIN -> XCH -> R", multihop([xp, xr], [PLAIN, None, R]), {PLAIN: 200_000}, R, 0x73,
               expect=sim([xp, xr], [PLAIN, None, R], 200_000)[-1])
    route_case("multi-hop, two rCATs (H then H2): XCH -> R -> R2 (the router's TXCH -> R1 -> R2 shape)",
               multihop([xr, rr2], [None, R, R2]), {None: 100_000_000}, R2, 0x74,
               expect=sim([xr, rr2], [None, R, R2], 100_000_000)[-1])
    route_case("multi-hop, rCAT in and a different rCAT out: R -> XCH -> R2", multihop([xr, xr2], [R, None, R2]),
               {R: 300_000}, R2, 0x75, expect=sim([xr, xr2], [R, None, R2], 300_000)[-1])
    route_case("multi-hop, 3 hops (the router's R1 -> T6 -> TXCH -> R2 shape): R -> PLAIN -> XCH -> R2",
               multihop([xpr, xp, xr2], [R, PLAIN, None, R2]), {R: 300_000}, R2, 0x76,
               expect=sim([xpr, xp, xr2], [R, PLAIN, None, R2], 300_000)[-1])

    # ---- split routes ----
    route_case("split, rCAT EXIT from a two-payout hub (layered ring): XCH -> R over [XCH/R] and [XCH/PLAIN/R]",
               split([([xr], [None, R], 60_000_000), ([xpr], [None, R], 40_000_000)]), {None: 100_000_000}, R, 0x77)
    route_case("split, rCAT ENTRY into layered children: R -> XCH over [XCH/R] and [XCH/PLAIN/R]",
               split([([xr], [R, None], 120_000), ([xpr], [R, None], 80_000)]), {R: 200_000}, None, 0x78)
    route_case("split, a branch bridging through R: XCH -> R -> R2 beside XCH -> R2",
               split([([xr_b, rr2], [None, R, R2], 50_000_000), ([xr2], [None, R2], 50_000_000)]), {None: 100_000_000}, R2, 0x79)
    # A taken (Dexie-style) maker offer selling R: its settlement is found only under H, and its
    # layered R coin joins the pool's layered payout in one ring that pays the trader.
    maker = fabricate({R: 50_000}, {None: [(MAKER_PH, 1_000_000)]}, 0x5E)
    route_case("split beside a taken maker offer selling R (layered): XCH -> R over [XCH/R] + the maker",
               {**split([([xr], [None, R], 60_000_000)]), "dexieOffers": [maker.to_bech32()]}, {None: 61_000_000}, R, 0x5F)

    # ---- across revisions: the V14 leg stays as it was ----
    xp14 = kit14.make_pool([None, PLAIN], [10_000_000_000, 20_000_000], total_lp=1_000_000, weights=[1, 1], fee_bps=30,
                           protocol_fee_bps=5, protocol_ph=bytes32(b"\x55" * 32), salt=0x6A, leaves="forge",
                           last_height=HEIGHT - 10)
    b = route_case("across revisions: PLAIN -> XCH on a V14 pool, then XCH -> R on a V20 rCAT pool",
                   multihop([v14.pool_to_snapshot(xp14), xr], [PLAIN, None, R]), {PLAIN: 200_000}, R, 0x7A)
    if b is not None:
        check("  the revocation layer appears only on R's coins (the V14 leg's PLAIN and XCH spends are plain)",
              layered_tails(b) <= {R}, f"{sorted(t.hex()[:8] for t in layered_tails(b))}")

    # ---- the router's fee ----
    print("\nsite-built router fee on an rCAT ENTRY (R -> XCH -> PLAIN at 100 bps): the trader's own spend pays it, layered")
    charged = {**multihop([xr, xp], [R, None, PLAIN]), "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 100}}
    settle_amt = 200_000
    fee = v20.minimum_router_fee(settle_amt, 100)
    want, _ = released(charged, {R: settle_amt}, PLAIN, 0x7B, payments={R: [(ROUTER_PH, fee)]})
    offer = fabricate({R: settle_amt}, {PLAIN: want}, 0x7B, payments={R: [(ROUTER_PH, fee)]})
    try:
        out = run({**charged, "offer": offer.to_bech32()})
        bundle = SpendBundle.from_json_dict(out["bundle"])
        drv.validate(bundle)
        check("  builds and validates", True)
        check("  the router's fee is read from the layered fee coin", int(out["forge"].get("router_fee", 0)) == fee
              and paid_to(bundle, ROUTER_PH, R, H) == fee, f"{out['forge'].get('router_fee')} vs {fee}")
        check_keeps_h(bundle)
    except Exception as exc:  # noqa: BLE001
        check("  builds and validates", False, f"{type(exc).__name__}: {str(exc)[:200]}")
    short = fabricate({R: settle_amt}, {PLAIN: want}, 0x7B, payments={R: [(ROUTER_PH, fee - 1)]})
    try:
        run({**charged, "offer": short.to_bech32()})
        check("  an rCAT entry paying the router a mojo short is refused", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("  an rCAT entry paying the router a mojo short is refused", "router's fee" in str(exc), str(exc)[:120])

    print("\nwallet-built router fee off an rCAT PAYOUT (PLAIN -> XCH -> R at 100 bps): the router's group is layered")
    charged = {**multihop([xp, xr], [PLAIN, None, R]), "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 100}}
    rel, forge = released(charged, {PLAIN: 200_000}, R, 0x7C, mode="surplus")
    fee_out = int(forge.get("router_fee_out", -1))
    offer = fabricate({PLAIN: 200_000}, {R: rel - fee_out}, 0x7C)
    try:
        out = run({**charged, "offer": offer.to_bech32()})
        bundle = SpendBundle.from_json_dict(out["bundle"])
        drv.validate(bundle)
        check("  builds and validates", True)
        check("  the trader gets the release less the fee, layered", paid_to(bundle, TRADER_PH, R, H) == rel - fee_out,
              f"{paid_to(bundle, TRADER_PH, R, H)} vs {rel - fee_out}")
        check("  the router gets its fee in R, layered (no plain R anywhere)",
              fee_out > 0 and paid_to(bundle, ROUTER_PH, R, H) == fee_out and paid_to(bundle, ROUTER_PH, R) == 0,
              f"{paid_to(bundle, ROUTER_PH, R, H)} vs {fee_out}")
        check_keeps_h(bundle)
    except Exception as exc:  # noqa: BLE001
        check("  builds and validates", False, f"{type(exc).__name__}: {str(exc)[:200]}")

    # ---- a probe preview: no offer yet, the router sizes an rCAT entry from the pools' H ----
    print("\nprobe preview (the router's quote path, no offer): rCAT entry R -> XCH -> PLAIN")
    try:
        forge = run({**multihop([xr, xp], [R, None, PLAIN]), "preview": True,
                     "probe": {"offered": {hx(R): 200_000}, "requested": [hx(PLAIN)]}})["forge"]
        check("  the probe's layered R settlement is found and the release matches the chain",
              int(forge["releases"][hx(PLAIN)]) == sim([xr, xp], [R, None, PLAIN], 200_000)[-1], f"{forge['releases']}")
    except Exception as exc:  # noqa: BLE001
        check("  the probe's layered R settlement is found and the release matches the chain", False, str(exc)[:160])

    # ---- negatives ----
    print("\nNEGATIVE: a route that drops H on the bridge coin (the composer before this change) is refused")
    mid = multihop([xpr, xr], [PLAIN, R, None])
    want, _ = released(mid, {PLAIN: 200_000}, None, 0x72)
    offer = fabricate({PLAIN: 200_000}, {None: want}, 0x72)
    saved = (v20r.settle_ph, v20r.settle_inner, v20r.settle_solution, v20r._Composer.payout_coin)
    plain_ph = saved[0]

    def plain_payout(self, pool_idx, asset, amount, label):          # the old payout_coin: no H, bare inner
        r = self.reserve(pool_idx, asset)
        coin = self.new_coin(Coin(r.coin.name(), plain_ph(asset), uint64(amount)))
        lineage = None if asset is None else LineageProof(r.coin.parent_coin_info, r.inner_hash, r.coin.amount)
        return v20r.Producer(coin, asset, lineage, label)
    v20r.settle_ph = lambda asset, hidden=None: plain_ph(asset)
    v20r.settle_inner = lambda hidden=None: OFFER_MOD
    v20r.settle_solution = lambda hidden, solution: Program.to(solution)
    v20r._Composer.payout_coin = plain_payout
    try:
        run({**mid, "offer": offer.to_bech32()})
        check("  the H-dropping bridge is refused", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("  the H-dropping bridge is refused at consensus validation", "consensus" in str(exc), str(exc)[:160])
    finally:
        v20r.settle_ph, v20r.settle_inner, v20r.settle_solution, v20r._Composer.payout_coin = saved
    try:
        out = run({**mid, "offer": offer.to_bech32()})
        drv.validate(SpendBundle.from_json_dict(out["bundle"]))
        check("  control: the same route with H on the bridge validates (the refusal is the dropped H)", True)
    except Exception as exc:  # noqa: BLE001
        check("  control: the same route with H on the bridge validates (the refusal is the dropped H)", False, str(exc)[:160])

    print("\nNEGATIVE: the bridge coin's puzzle hash alone drops H (spent layered, created plain)")
    v20r.settle_ph = lambda asset, hidden=None: plain_ph(asset)
    try:
        run({**mid, "offer": offer.to_bech32()})
        check("  refused", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("  refused (the reserve's owner path only creates CAT(R, rev(H, ...)))", "consensus" in str(exc), str(exc)[:160])
    finally:
        v20r.settle_ph = saved[0]

    print("\nNEGATIVE: a plain coin of R offered as the entry is not R's settlement (another market)")
    entry = multihop([xr, xp], [R, None, PLAIN])
    plain_r = fabricate({R: 200_000}, {PLAIN: 1}, 0x7D, hide={})
    try:
        run({**entry, "offer": plain_r.to_bech32()})
        check("  refused", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("  refused: the offer does not provide R", "does not provide" in str(exc), str(exc)[:140])

    print("\nNEGATIVE: the maker asserted the requested R group at the PLAIN settlement hash")
    exit_r = multihop([xp, xr], [PLAIN, None, R])
    want, _ = released(exit_r, {PLAIN: 200_000}, R, 0x73)
    unbound = fabricate({PLAIN: 200_000}, {R: want}, 0x7E, announce_hide={})
    try:
        run({**exit_r, "offer": unbound.to_bech32()})
        check("  refused", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("  refused: the requested R group is not asserted by the maker", "not asserted" in str(exc), str(exc)[:140])

    print("\nNEGATIVE: two pools naming different H for one asset")
    xr_bad = pool([None, R], [10_000_000_000, 30_000_000], 0x67, [ZERO_32, H_BAD])
    try:
        run({**multihop([xpr, xr_bad], [PLAIN, R, None]), "offer": fabricate({PLAIN: 200_000}, {None: 1}, 0x7F).to_bech32(),
             "preview": True})
        check("  refused", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("  refused: different hidden puzzle hashes", "different hidden puzzle hashes" in str(exc), str(exc)[:140])

    print(f"\n{sum(results)}/{len(results)} routed-rCAT checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
