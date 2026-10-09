"""One trade split between Forge pools and a Dexie maker's open offer, in one bundle.

A Dexie open offer is a fixed size: it must be taken whole. split_swap now takes such
offers inside the route (`externals`): the trader's entry hub pays what each maker asks,
under the maker's own notarized nonce, and the coin each maker offers joins the exit
pot that pays the trader. The pools route the rest. Nothing settles unless all of it
does: one bundle, the trader's and the makers' signatures aggregated.

The router's fee is for the router's service, so it is taken on the whole entry, a
taken offer's share included (owner, 2026-10-05).

Run with the workspace venv:
    .venv/Scripts/python.exe projects/chia-cfmm/contracts/_test_v16_dexie_split.py
"""
from __future__ import annotations

import hashlib
import json
import sys

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle, match_cat_puzzle
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key
from chia.wallet.trading.offer import Offer
from chia.wallet.uncurried_puzzle import uncurry_puzzle
from chia_rs import AugSchemeMPL, Coin, G2Element, PrivateKey, SpendBundle, validate_clvm_and_signature
from chia_rs.sized_bytes import bytes32

import forge_offer_build as fob
import forge_stdin
import forge_v16_driver as drv
import forge_v16_offer as v16
import forge_v16_route as v16r
import _test_v16_offer_lane as lane
from _test_v16_offer_lane import ROUTER_PH, T_A, T_B, TRADER_PH, H, fabricate_offer, make, paid_to
from forge_offer import ZERO_32

MAKER_PH = bytes32(b"\x4d" * 32)
results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def maker_offer(offered: dict, requested: dict, salt: int) -> Offer:
    """A maker's open offer, as fabricate_offer makes a trader's, paying MAKER_PH."""
    saved = lane.TRADER_PH
    lane.TRADER_PH = MAKER_PH
    try:
        return fabricate_offer(offered, requested, salt)
    finally:
        lane.TRADER_PH = saved


def pool_out(pool, i_in, i_out, gross):
    r = pool.state[0]
    honest = v16.forge_math.swap_output(r[i_in], r[i_out], gross, pool.fee_bps, pool.weights[i_in], pool.weights[i_out])
    return honest - honest * pool.protocol_fee_bps // 10_000


def refused(label, fn, needle):
    try:
        fn()
    except v16.OfferRejected as exc:
        return check(label, needle in str(exc), str(exc)[:110])
    except Exception as exc:  # noqa: BLE001
        return check(label, False, f"{type(exc).__name__}: {exc}"[:110])
    return check(label, False, "accepted")


def main() -> int:
    if not drv.v16_available():
        print("  [skip] V16 puzzles not built")
        return 2
    ab = make([T_A, T_B], [800_000, 600_000], [1, 1], salt=0x52)          # A/B, CAT to CAT
    xb = make([None, T_B], [8_000_000_000, 700_000], [1, 1], salt=0x53)   # XCH/B

    print("CAT -> CAT: the pool beside a maker selling B for A")
    a_total, a_maker, b_maker = 120_000, 40_000, 39_000          # the maker sells B near 1:1, better than the pool
    for bps in (0, 300):
        fee = a_total * bps // 10_000
        pool_in = a_total - a_maker - fee
        b_pool = pool_out(ab, 0, 1, pool_in)
        # exact (F3): the trader's own spend pays the router's fee beside the net settlement,
        # and asks for exactly the pool's B plus the maker's B
        offer = fabricate_offer({T_A: a_total - fee}, {T_B: b_pool + b_maker}, salt=0x71 + bps % 7,
                                payments={T_A: [(ROUTER_PH, fee)]} if fee else None)
        maker = maker_offer({T_B: b_maker}, {T_A: a_maker}, salt=0x81 + bps % 7)
        result = v16r.split_swap([([ab], [T_A, T_B], pool_in)], offer, H, TRADER_PH, bps,
                                 ROUTER_PH if bps else None, externals=[maker])
        b = result.bundle
        check(f"{bps} bps: one bundle, the pool's spend and both offers' spends",
              len([s for s in b.coin_spends if s.coin.parent_coin_info != ZERO_32]) > 4)
        check(f"{bps} bps: the maker is paid exactly what it asked, in A", paid_to(b, MAKER_PH, T_A) == a_maker,
              f"{paid_to(b, MAKER_PH, T_A)}")
        check(f"{bps} bps: the trader gets the pool's B and the maker's B", paid_to(b, TRADER_PH, T_B) == b_pool + b_maker,
              f"{paid_to(b, TRADER_PH, T_B)} vs {b_pool} + {b_maker}")
        check(f"{bps} bps: total_out counts both", int(result.details["total_out"]) == b_pool + b_maker)
        check(f"{bps} bps: the router fee is on the whole entry, the Dexie share included, in the entry asset",
              paid_to(b, ROUTER_PH, T_A) == a_total * bps // 10_000 and paid_to(b, ROUTER_PH, T_B) == 0,
              f"{paid_to(b, ROUTER_PH, T_A)}")
        check(f"{bps} bps: the response names the taken offer",
              result.details.get("taken_offers") == [{"asset_in": T_A.hex(), "amount_in": a_maker,
                                                       "asset_out": T_B.hex(), "amount_out": b_maker}])

    print("XCH -> CAT: the pool beside a maker selling B for XCH")
    x_total, x_maker, b_maker = 300_000_000, 100_000_000, 9_000
    b_pool = pool_out(xb, 0, 1, x_total - x_maker)
    offer = fabricate_offer({None: x_total}, {T_B: b_pool + b_maker}, salt=0x91)
    maker = maker_offer({T_B: b_maker}, {None: x_maker}, salt=0x92)
    result = v16r.split_swap([([xb], [None, T_B], x_total - x_maker)], offer, H, TRADER_PH, externals=[maker])
    check("the maker is paid its XCH", paid_to(result.bundle, MAKER_PH) == x_maker)
    check("the trader gets both B shares, exactly", paid_to(result.bundle, TRADER_PH, T_B) == b_pool + b_maker)
    stale = fabricate_offer({None: x_total}, {T_B: b_pool + b_maker - 5}, salt=0x91)
    refused("an offer asking 5 mojos under what the pool and the maker release is a stale quote",
            lambda: v16r.split_swap([([xb], [None, T_B], x_total - x_maker)], stale, H, TRADER_PH, externals=[maker]), "quote again")

    print("through the router's builder (forge_stdin), as the split responder calls it")
    fee = a_total * 300 // 10_000
    want = pool_out(ab, 0, 1, a_total - a_maker - fee) + b_maker
    maker = maker_offer({T_B: b_maker}, {T_A: a_maker}, salt=0x94)
    split_payload = {"action": "split-swap", "current_height": H,
                     "branches": [{"pools": [v16.pool_to_snapshot(ab)], "path": [T_A.hex(), T_B.hex()], "amountIn": str(a_total - a_maker)}],
                     "dexieOffers": [maker.to_bech32()], "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 300}}
    probe = fabricate_offer({T_A: a_total - fee}, {T_B: 1}, salt=0x93, payments={T_A: [(ROUTER_PH, fee)]})
    preview = forge_stdin.build(json.loads(json.dumps({**split_payload, "offer": probe.to_bech32(), "preview": True})))
    # the preview's fee is the least that covers the rate on net + fee; a quote taking the
    # rate off the whole coin pays that or a mojo more, never less
    check("the preview beside a Dexie offer releases the pool's B plus the maker's, and needs the fee on the whole entry",
          int(preview["forge"]["releases"][T_B.hex()]) == want
          and int(preview["forge"]["router_fee_required"]) == v16.minimum_router_fee(a_total - fee, 300) <= fee,
          f"{preview['forge'].get('releases')} fee {preview['forge'].get('router_fee_required')}")
    offer = fabricate_offer({T_A: a_total - fee}, {T_B: want}, salt=0x93, payments={T_A: [(ROUTER_PH, fee)]})
    out = forge_stdin.build(json.loads(json.dumps({**split_payload, "offer": offer.to_bech32()})))
    check("one branch beside a Dexie offer builds through the router's builder",
          int(out["total_out"]) == want, out.get("total_out"))

    print("what is refused")
    def split(maker, entry=a_total):
        return v16r.split_swap([([ab], [T_A, T_B], entry - 1_000)], fabricate_offer({T_A: entry}, {T_B: 1}, salt=0xA1), H,
                               TRADER_PH, externals=[maker])
    refused("a maker asking for an asset the trader does not offer", lambda: split(maker_offer({T_B: 10}, {None: 10}, 0xA2)),
            "the trader does not offer")
    refused("a maker offering an asset the trader does not ask for", lambda: split(maker_offer({None: 10}, {T_A: 10}, 0xA3)),
            "the trader does not ask for")
    refused("a maker asking for more than the whole entry", lambda: split(maker_offer({T_B: 10}, {T_A: a_total}, 0xA4)),
            "nothing is left for the pools")
    refused("one branch with no taken offer is still not a split",
            lambda: v16r.split_swap([([ab], [T_A, T_B], 1_000)], fabricate_offer({T_A: 1_000}, {T_B: 1}, salt=0xA5), H, TRADER_PH),
            "at least two branches")

    print("signed: standard wallets on both sides, validated as a mempool would")
    signed_case(ab)
    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} Dexie split checks passed")
    return 1 if failed else 0


# ---- real wallets ------------------------------------------------------------------------------------
GENESIS = bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615")   # testnet11


def wallet(seed: int):
    sk = PrivateKey.from_bytes(bytes([seed]) * 32)
    inner = puzzle_for_synthetic_public_key(sk.get_g1())
    return sk, inner, inner.get_tree_hash()


def std_offer(sk, inner, ph, asset, amount, request_asset, request_amount, salt) -> Offer:
    outer = construct_cat_puzzle(CAT_MOD, asset, inner)
    grand = bytes32(bytes([salt, 0x5e]) * 16)
    parent = Coin(grand, outer.get_tree_hash(), amount)
    coin = Coin(parent.name(), outer.get_tree_hash(), amount)
    record = {"coin": {"parent_coin_info": coin.parent_coin_info.hex(), "puzzle_hash": coin.puzzle_hash.hex(), "amount": str(amount)},
              "inner_puzzle": bytes(inner).hex(),
              "lineage_proof": {"parent_name": grand.hex(), "inner_puzzle_hash": ph.hex(), "amount": str(amount)}}
    payload = {"change_puzzle_hash": ph.hex(), "offered": [{"asset_id": asset.hex(), "amount": str(amount), "coins": [record]}],
               "requested": [{"asset_id": request_asset.hex(), "amount": str(request_amount)}]}
    built = fob.build(payload)["coin_spends"]
    spends = [drv.make_spend(Coin(bytes32.fromhex(s["coin"]["parent_coin_info"][2:]), bytes32.fromhex(s["coin"]["puzzle_hash"][2:]),
                                  int(s["coin"]["amount"])), Program.fromhex(s["puzzle_reveal"]), Program.fromhex(s["solution"]))
              for s in built]
    sigs = []
    for cs in spends:
        puzzle, solution = Program.from_bytes(bytes(cs.puzzle_reveal)), Program.from_bytes(bytes(cs.solution))
        cat = match_cat_puzzle(uncurry_puzzle(puzzle))
        if cat is not None:
            puzzle, solution = list(cat)[2], solution.first()
        for cond in puzzle.run(solution).as_iter():
            items = list(cond.as_iter())
            if items[0].as_int() == 50:
                sigs.append(AugSchemeMPL.sign(sk, bytes(items[2].as_atom()) + bytes(cs.coin.name()) + GENESIS))
    return Offer(fob._requested_payments(payload["requested"], ph, [s.coin for s in spends]),
                 SpendBundle(spends, AugSchemeMPL.aggregate(sigs)), fob._drivers(payload["requested"]))


def signed_case(ab) -> None:
    t_sk, t_inner, t_ph = wallet(0x21)
    m_sk, m_inner, m_ph = wallet(0x22)
    a_total, a_maker, b_maker = 120_000, 40_000, 39_000
    trader = std_offer(t_sk, t_inner, t_ph, T_A, a_total, T_B, pool_out(ab, 0, 1, a_total - a_maker) + b_maker, 0xB1)
    maker = std_offer(m_sk, m_inner, m_ph, T_B, b_maker, T_A, a_maker, 0xB2)
    result = v16r.split_swap([([ab], [T_A, T_B], a_total - a_maker)], trader, H, t_ph, externals=[maker])
    extra = {"AGG_SIG_ME_ADDITIONAL_DATA": GENESIS}
    for name, op in (("AGG_SIG_PARENT_ADDITIONAL_DATA", 43), ("AGG_SIG_PUZZLE_ADDITIONAL_DATA", 44),
                     ("AGG_SIG_AMOUNT_ADDITIONAL_DATA", 45), ("AGG_SIG_PUZZLE_AMOUNT_ADDITIONAL_DATA", 46),
                     ("AGG_SIG_PARENT_AMOUNT_ADDITIONAL_DATA", 47), ("AGG_SIG_PARENT_PUZZLE_ADDITIONAL_DATA", 48)):
        extra[name] = hashlib.sha256(GENESIS + bytes([op])).digest()
    constants = DEFAULT_CONSTANTS.replace(GENESIS_CHALLENGE=bytes32(GENESIS), **{k: bytes32(v) for k, v in extra.items()})
    try:
        validate_clvm_and_signature(result.bundle, int(constants.MAX_BLOCK_COST_CLVM), constants, 10**8)
        ok, why = True, ""
    except Exception as exc:  # noqa: BLE001
        ok, why = False, f"{type(exc).__name__}: {exc}"[:140]
    check("both wallets' signatures, aggregated, pass validate_clvm_and_signature", ok, why)
    check("the maker's standard wallet is paid what it asked", paid_to(result.bundle, m_ph, T_A) == a_maker)
    check("the trader's standard wallet gets the pool's and the maker's B",
          paid_to(result.bundle, t_ph, T_B) == pool_out(ab, 0, 1, a_total - a_maker) + b_maker)


if __name__ == "__main__":
    sys.exit(main())
