"""A balance that crosses a Forge pool and a Dexie open offer, in one bundle.

Every token on the Forge has one pool against XCH, so the pools alone never disagree; a
Dexie open offer is the second price (owner, 2026-10-29: "look for routes through dexie as
well for balancing"). flow_balance takes such offers (`externals`) in the MIDDLE of the
cycle, where split_swap takes them only beside the pools:

  ask  Dexie sells B cheaper than the pool buys it: the entry XCH pays the maker, the
       maker's B is sold to the pool, the pool's XCH pays the trader.
  bid  Dexie pays more XCH for B than the pool charges: the entry XCH buys B from the
       pool, the maker is paid its B out of that, the maker's XCH pays the trader.

Nothing settles unless all of it does: one bundle, the makers' signatures aggregated.

Run with the workspace venv:
    .venv/Scripts/python.exe projects/chia-cfmm/contracts/_test_v16_dexie_balance.py
"""
from __future__ import annotations

import hashlib
import json
import sys

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia_rs import SpendBundle, validate_clvm_and_signature
from chia_rs.sized_bytes import bytes32

import forge_stdin
import forge_v16_driver as drv
import forge_v16_offer as v16
import forge_v16_route as v16r
from _test_v16_dexie_split import GENESIS, MAKER_PH, maker_offer, pool_out, std_offer, wallet
from _test_v16_offer_lane import ROUTER_PH, T_A, T_B, TRADER_PH, H, fabricate_offer, make, paid_to
from forge_offer import ZERO_32

results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def refused(label, fn, needle):
    try:
        fn()
    except v16.OfferRejected as exc:
        return check(label, needle in str(exc), str(exc)[:120])
    except Exception as exc:  # noqa: BLE001
        return check(label, False, f"{type(exc).__name__}: {exc}"[:120])
    return check(label, False, "accepted")


def x_for_b(pool, want: int) -> int:
    """The least XCH the pool takes to release at least `want` B."""
    def out(x: int) -> int:
        try:
            return pool_out(pool, 0, 1, x)
        except ValueError:                    # an input that rounds to nothing
            return 0
    lo, hi = 1, 1
    while out(hi) < want:
        hi *= 2
    while lo < hi:
        mid = (lo + hi) // 2
        if out(mid) >= want:
            hi = mid
        else:
            lo = mid + 1
    return lo


def validate(bundle) -> tuple[bool, str]:
    extra = {"AGG_SIG_ME_ADDITIONAL_DATA": GENESIS}
    for name, op in (("AGG_SIG_PARENT_ADDITIONAL_DATA", 43), ("AGG_SIG_PUZZLE_ADDITIONAL_DATA", 44),
                     ("AGG_SIG_AMOUNT_ADDITIONAL_DATA", 45), ("AGG_SIG_PUZZLE_AMOUNT_ADDITIONAL_DATA", 46),
                     ("AGG_SIG_PARENT_AMOUNT_ADDITIONAL_DATA", 47), ("AGG_SIG_PARENT_PUZZLE_ADDITIONAL_DATA", 48)):
        extra[name] = hashlib.sha256(GENESIS + bytes([op])).digest()
    constants = DEFAULT_CONSTANTS.replace(GENESIS_CHALLENGE=bytes32(GENESIS), **{k: bytes32(v) for k, v in extra.items()})
    try:
        validate_clvm_and_signature(bundle, int(constants.MAX_BLOCK_COST_CLVM), constants, 10**8)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:140]


def main() -> int:
    if not drv.v16_available():
        print("  [skip] V16 puzzles not built")
        return 2
    xb = make([None, T_B], [8_000_000_000, 700_000], [1, 1], salt=0x63)   # XCH/B, ~11,400 mojos a B
    ab = make([T_A, T_B], [800_000, 600_000], [1, 1], salt=0x64)          # A/B

    print("ask: Dexie sells B under the pool's bid; buy it there, sell it to the pool")
    b_maker, x_maker = 10_000, 80_000_000                                 # the pool pays ~110M for 10,000 B
    back = pool_out(xb, 1, 0, b_maker)
    check("the setup has a spread to take", back > x_maker, f"{back} vs {x_maker}")
    for bps in (0, 300):
        fee = v16.minimum_router_fee(x_maker, bps) if bps else 0
        maker = maker_offer({T_B: b_maker}, {None: x_maker}, salt=0x71 + bps % 7)
        offer = fabricate_offer({None: x_maker}, {None: back}, salt=0x72 + bps % 7,
                                payments={None: [(ROUTER_PH, fee)]} if fee else None)
        result = v16r.flow_balance([(xb, T_B, None, b_maker)], offer, H, TRADER_PH, ZERO_32, bps,
                                   ROUTER_PH if bps else None, externals=[maker])
        b = result.bundle
        check(f"{bps} bps: the maker is paid exactly its XCH ask", paid_to(b, MAKER_PH) == x_maker, f"{paid_to(b, MAKER_PH)}")
        check(f"{bps} bps: the trader gets the pool's XCH for the maker's B", paid_to(b, TRADER_PH) == back,
              f"{paid_to(b, TRADER_PH)} vs {back}")
        check(f"{bps} bps: total_out is the XCH paid back", int(result.details["total_out"]) == back)
        check(f"{bps} bps: the router fee is on the entry", paid_to(b, ROUTER_PH) == fee, f"{paid_to(b, ROUTER_PH)}")
        check(f"{bps} bps: the response names the taken offer", result.details.get("taken_offers") == [
            {"asset_in": ZERO_32.hex(), "amount_in": x_maker, "asset_out": T_B.hex(), "amount_out": b_maker}])
        check(f"{bps} bps: the pool advanced by the sale", result.pools[0].state[0][1] == xb.state[0][1] + b_maker)

    print("bid: Dexie pays more for B than the pool charges; buy it from the pool, fill the bid")
    b_bid, x_bid = 10_000, 200_000_000                                    # the pool sells 10,000 B for ~116M
    x_in = x_for_b(xb, b_bid)
    dust = pool_out(xb, 0, 1, x_in) - b_bid
    check("the setup has a spread to take", x_bid > x_in, f"{x_bid} vs {x_in}")
    maker = maker_offer({None: x_bid}, {T_B: b_bid}, salt=0x81)
    offer = fabricate_offer({None: x_in}, {None: x_bid, **({T_B: dust} if dust else {})}, salt=0x82)
    result = v16r.flow_balance([(xb, None, T_B, x_in)], offer, H, TRADER_PH, ZERO_32, externals=[maker])
    b = result.bundle
    check("the maker is paid exactly its B ask, out of the pool's B", paid_to(b, MAKER_PH, T_B) == b_bid,
          f"{paid_to(b, MAKER_PH, T_B)}")
    check("the trader gets the maker's XCH", paid_to(b, TRADER_PH) == x_bid, f"{paid_to(b, TRADER_PH)}")
    check("the pool's B beyond the bid is the trader's dust", paid_to(b, TRADER_PH, T_B) == dust, f"{dust}")
    stale = fabricate_offer({None: x_in}, {None: x_bid, T_B: dust + 1}, salt=0x83)
    refused("asking a mojo of B more than the pool leaves after the bid is a stale plan",
            lambda: v16r.flow_balance([(xb, None, T_B, x_in)], stale, H, TRADER_PH, ZERO_32, externals=[maker]), "quote again")
    short = fabricate_offer({None: x_in - 1_000_000}, {None: x_bid}, salt=0x84)
    refused("an entry too small to buy the B the bid asks for",
            lambda: v16r.flow_balance([(xb, None, T_B, x_in - 1_000_000)], short, H, TRADER_PH, ZERO_32, externals=[maker]),
            "the taken offers ask")

    print("preview and settle through the router's builder (forge_stdin), as the flow responder calls it")
    maker = maker_offer({T_B: b_maker}, {None: x_maker}, salt=0x91)
    payload = {"action": "flow-balance", "current_height": H, "startAsset": "xch",
               "legs": [{"pool": v16.pool_to_snapshot(xb), "assetIn": T_B.hex(), "assetOut": ZERO_32.hex(),
                         "amountIn": str(b_maker)}],
               "dexieOffers": [maker.to_bech32()], "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 300}}
    fee = v16.minimum_router_fee(x_maker, 300)
    probe = fabricate_offer({None: x_maker}, {None: 1}, salt=0x92)
    preview = forge_stdin.build(json.loads(json.dumps({**payload, "offer": probe.to_bech32(), "preview": True})))
    check("the preview releases the pool's XCH and sizes the fee on the entry",
          int(preview["forge"]["releases"][ZERO_32.hex()]) == back and int(preview["forge"]["router_fee_required"]) == fee,
          f"{preview['forge'].get('releases')} fee {preview['forge'].get('router_fee_required')} vs {fee}")
    offer = fabricate_offer({None: x_maker}, {None: back}, salt=0x93, payments={None: [(ROUTER_PH, fee)]})
    out = forge_stdin.build(json.loads(json.dumps({**payload, "offer": offer.to_bech32()})))
    check("the balance builds through the router's builder, naming the taken offer",
          int(out["total_out"]) == back and len(out.get("taken_offers") or []) == 1, f"{out.get('total_out')}")
    check("its bundle pays the trader", paid_to(SpendBundle.from_json_dict(out["bundle"]), TRADER_PH) == back)

    print("several asks of one token, sold to the pool together")
    m1, m2 = maker_offer({T_B: 6_000}, {None: 50_000_000}, 0xA1), maker_offer({T_B: 4_000}, {None: 35_000_000}, 0xA2)
    both = pool_out(xb, 1, 0, 10_000)
    offer = fabricate_offer({None: 85_000_000}, {None: both}, salt=0xA3)
    result = v16r.flow_balance([(xb, T_B, None, 10_000)], offer, H, TRADER_PH, ZERO_32, externals=[m1, m2])
    check("two makers paid, their B sold in one leg", paid_to(result.bundle, MAKER_PH) == 85_000_000
          and paid_to(result.bundle, TRADER_PH) == both, f"{paid_to(result.bundle, MAKER_PH)}")

    print("what is refused")
    refused("a maker asking more XCH than the entry holds",
            lambda: v16r.flow_balance([(xb, T_B, None, b_maker)], fabricate_offer({None: x_maker - 1}, {None: back}, 0xB1), H,
                                      TRADER_PH, ZERO_32, externals=[maker_offer({T_B: b_maker}, {None: x_maker}, 0xB2)]),
            "the entry holds")
    refused("an entry holding more than the makers ask, with no pool to take the rest",
            lambda: v16r.flow_balance([(xb, T_B, None, b_maker)], fabricate_offer({None: x_maker + 5}, {None: back}, 0xB3), H,
                                      TRADER_PH, ZERO_32, externals=[maker_offer({T_B: b_maker}, {None: x_maker}, 0xB4)]),
            "more than the offer asks")
    # the only leg drinks the entry's XCH, so the maker's B would feed nothing and pay no one
    refused("a maker offering a token no later leg drinks and the trader does not ask for",
            lambda: v16r.flow_balance([(xb, None, T_B, 50_000_000)], fabricate_offer({None: 50_000_000 + x_maker}, {None: back}, 0xB5),
                                      H, TRADER_PH, ZERO_32, externals=[maker_offer({T_B: b_maker}, {None: x_maker}, 0xB6)]),
            "the trader does not ask for")
    refused("one leg and no taken offer is not a flow",
            lambda: v16r.flow_balance([(xb, None, T_B, 1_000)], fabricate_offer({None: 1_000}, {T_B: 1}, 0xB7), H,
                                      TRADER_PH, ZERO_32),
            "at least two legs")

    print("signed: standard wallets, validated as a mempool would (a CAT-rooted balance on A)")
    t_sk, t_inner, t_ph = wallet(0x31)
    m_sk, m_inner, m_ph = wallet(0x32)
    # ask: the maker sells B for A under the A/B pool's price; the trader buys it and sells it back for A
    a_ask, b_ask = 30_000, 40_000
    a_back = pool_out(ab, 1, 0, b_ask)
    trader = std_offer(t_sk, t_inner, t_ph, T_A, a_ask, T_A, a_back, 0xC1)
    maker = std_offer(m_sk, m_inner, m_ph, T_B, b_ask, T_A, a_ask, 0xC2)
    result = v16r.flow_balance([(ab, T_B, T_A, b_ask)], trader, H, t_ph, T_A, externals=[maker])
    ok, why = validate(result.bundle)
    check("ask: both wallets' signatures pass validate_clvm_and_signature", ok, why)
    check("ask: the trader's wallet is paid the pool's A, more than it put in",
          paid_to(result.bundle, t_ph, T_A) == a_back > a_ask, f"{a_back} vs {a_ask}")
    # bid: the maker pays A for exactly the B the pool releases for the trader's A
    a_in = 30_000
    b_out = pool_out(ab, 0, 1, a_in)
    a_bid = a_in + 5_000
    trader = std_offer(t_sk, t_inner, t_ph, T_A, a_in, T_A, a_bid, 0xC3)
    maker = std_offer(m_sk, m_inner, m_ph, T_A, a_bid, T_B, b_out, 0xC4)
    result = v16r.flow_balance([(ab, T_A, T_B, a_in)], trader, H, t_ph, T_A, externals=[maker])
    ok, why = validate(result.bundle)
    check("bid: both wallets' signatures pass validate_clvm_and_signature", ok, why)
    check("bid: the maker's wallet gets the pool's B, the trader's the maker's A",
          paid_to(result.bundle, m_ph, T_B) == b_out and paid_to(result.bundle, t_ph, T_A) == a_bid)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} Dexie balance checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
