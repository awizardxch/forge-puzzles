#!/usr/bin/env python3
"""What a V11 swap pays out, and who can influence it (phase 5, the hostile-router probe re-run).

The router builds the bundle, so the trader's protection cannot rest on the router
behaving. On V11 it rests on three things: the offer's notarised payment, which the
payout coin must satisfy or the trader's own coin spend fails its announcement; the
protocol fee, which the leaf derives from the pool's curried rate and owes inside
the reserve coin; and the curve, which the leaf checks against its own mirror, so
the payout coin holds exactly what the reserve released.

These probes turn the knob the router does control -- the fee rate it asks for --
and check what must hold regardless:

  * the trader is paid at least what they notarised, at any rate;
  * the protocol fee is owed exactly, at any rate;
  * nothing is invented or lost: trader + router + protocol == what the curve released;
  * asking for more than the pool releases refuses the whole bundle;
  * the router takes its RATE and no more, and the overage above that rate is
    refunded to the trader instead of kept.

That last one is the fix this file was rewritten for. The router fee used to be
"whatever the pool pays above what the trader asked for", so a trader who widened
their slippage tolerance widened the router take by the same amount and paid it
without ever having been quoted it.

Which leg the fee comes off is the builder decision and never the caller's: XCH has
priority wherever it sits, so a trader buying a CAT with XCH pays the fee out of the
XCH they offered, and a trader selling a CAT for XCH pays it out of the XCH they
receive. Both directions are probed below.

Exits 0 when every check passes, 1 otherwise, 2 when the V11 build is absent.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

import forge_math  # noqa: E402
import forge_stdin  # noqa: E402
import forge_v14_driver as drv  # noqa: E402
import forge_v14_offer as v14  # noqa: E402
from _test_v14_offer_lane import ROUTER_PH, T_A, TRADER_PH, H, fabricate_offer, make, paid_to  # noqa: E402
from chia_rs import SpendBundle  # noqa: E402

results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def main() -> int:
    if not drv.v14_available():
        print("  [skip] V11 puzzles not built"); return 2
    pool = make([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x81)
    r, w = pool.state[0], pool.weights
    gross = 100_000_000
    honest = forge_math.swap_output(r[0], r[1], gross, pool.fee_bps, w[0], w[1])
    pfee = honest * pool.protocol_fee_bps // 10_000
    released = honest - pfee
    print(f"pool releases {released} of A for {gross} XCH (curve {honest}, protocol fee {pfee})")

    # Buying a CAT with XCH: XCH is the offered leg, so the fee is paid in XCH by the
    # trader's own spend, beside the settlement coin, which holds the net the curve sees.
    # The pool receives the net, so every downstream number is computed at the net input.
    print("paying XCH for a CAT -- the fee is paid beside the input, by the trader's own spend:")
    for bps in (0, 30, 300, 500):
        in_fee = gross * bps // 10_000
        net_in = gross - in_fee
        curve = forge_math.swap_output(r[0], r[1], net_in, pool.fee_bps, w[0], w[1])
        net_pfee = curve * pool.protocol_fee_bps // 10_000
        net_released = curve - net_pfee
        offer = fabricate_offer({None: net_in}, {T_A: net_released}, salt=0x90 + (bps % 200),
                                payments={None: [(ROUTER_PH, in_fee)]} if in_fee else None)
        out = forge_stdin.build({"action": "swap", "offer": offer.to_bech32(), "current_height": H, "pool": v14.pool_to_snapshot(pool),
                                 "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": bps}})
        bundle = SpendBundle.from_json_dict(out["bundle"])
        trader = paid_to(bundle, TRADER_PH, T_A)
        router_out = paid_to(bundle, ROUTER_PH, T_A)
        router_in = paid_to(bundle, ROUTER_PH, None)
        owed = [int(x) for x in out["pool"]["state"]["fees_owed"]]
        label = f"router asks {bps:,} bps"
        check(f"{label}: the fee is paid in XCH, on the input leg, and the response says so",
              out["forge"]["router_fee_side"] == "input" and router_in == in_fee and out["forge"]["router_fee"] == in_fee,
              f"{router_in} vs {in_fee}")
        check(f"{label}: nothing is taken twice -- the payout leg is untouched", router_out == 0, f"{router_out}")
        check(f"{label}: the trader is paid exactly what they signed for, the whole release",
              trader == net_released, f"{trader} vs {net_released}")
        check(f"{label}: the protocol fee is owed exactly", owed == [0, net_pfee], f"{owed}")
        check(f"{label}: nothing invented or lost", trader + router_out + net_pfee == curve)
        check(f"{label}: the reserve grew by the NET input, not the gross",
              [int(x) for x in out["pool"]["state"]["reserves"]][0] == r[0] + net_in)

    # A router asking for the entire input leaves nothing to swap: whatever the offer
    # pays, the rate on net + fee is never covered, and the swap is refused whole.
    offer = fabricate_offer({None: 1}, {T_A: 1}, salt=0xC7, payments={None: [(ROUTER_PH, gross - 1)]})
    try:
        forge_stdin.build({"action": "swap", "offer": offer.to_bech32(), "current_height": H,
                           "pool": v14.pool_to_snapshot(pool),
                           "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 10_000}})
        check("a router asking for 100% of the input is refused", False, "it settled")
    except Exception as exc:  # noqa: BLE001
        check("a router asking for 100% of the input is refused", True, str(exc)[:70])

    # Selling a CAT for XCH: XCH is what the trader receives, so the fee is a requested
    # payment to the router inside the trader's own group, at exactly the rate on the
    # payout; the trader's own payment is the rest, and the group is the whole payout.
    print("selling a CAT for XCH -- the fee is a requested payment to the router:")
    sell = 10_000
    sell_curve = forge_math.swap_output(r[1], r[0], sell, pool.fee_bps, w[1], w[0])
    sell_pfee = sell_curve * pool.protocol_fee_bps // 10_000
    sell_released = sell_curve - sell_pfee
    for n, bps in enumerate((0, 30, 300, 500)):
        cut = sell_released * bps // 10_000
        group = [(TRADER_PH, sell_released - cut)] + ([(ROUTER_PH, cut)] if cut else [])
        offer = fabricate_offer({T_A: sell}, {None: group}, salt=0xB0 + n)
        out = forge_stdin.build({"action": "swap", "offer": offer.to_bech32(), "current_height": H, "pool": v14.pool_to_snapshot(pool),
                                 "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": bps}})
        bundle = SpendBundle.from_json_dict(out["bundle"])
        trader = paid_to(bundle, TRADER_PH, None)
        router = paid_to(bundle, ROUTER_PH, None)
        label = f"router asks {bps:,} bps"
        check(f"{label}: the fee is taken in XCH, on the output leg", out["forge"]["router_fee_side"] == "output")
        check(f"{label}: the router is paid its RATE, inside the trader's group", router == cut, f"{router} vs {cut}")
        check(f"{label}: the trader is paid the rest, exactly", trader == sell_released - cut, f"{trader} vs {sell_released - cut}")
        check(f"{label}: nothing invented or lost", trader + router + sell_pfee == sell_curve)
        check(f"{label}: the response names the fee and no refund",
              out["forge"]["router_fee"] == cut and out["forge"]["refund"] == 0 and out["forge"]["surplus"] == 0)
    # ...and a group that shorts the router is refused, however the trader splits it
    short = [(TRADER_PH, sell_released - sell_released * 300 // 10_000 + 1), (ROUTER_PH, sell_released * 300 // 10_000 - 1)]
    try:
        forge_stdin.build({"action": "swap", "offer": fabricate_offer({T_A: sell}, {None: short}, salt=0xB9).to_bech32(),
                           "current_height": H, "pool": v14.pool_to_snapshot(pool), "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 300}})
        check("a group paying the router one mojo under the rate is refused", False, "it settled")
    except Exception as exc:  # noqa: BLE001
        check("a group paying the router one mojo under the rate is refused", "rebuild the offer" in str(exc), str(exc)[:80])

    print("a hostile request:")
    for delta, label in ((1, "above"), (-1, "below")):
        offer = fabricate_offer({None: gross}, {T_A: released + delta}, salt=0xA1 if delta > 0 else 0xA3)
        try:
            forge_stdin.build({"action": "swap", "offer": offer.to_bech32(), "current_height": H, "pool": v14.pool_to_snapshot(pool),
                               "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}})
            check(f"asking one mojo {label} what the pool releases is refused", False, "it settled")
        except Exception as exc:  # noqa: BLE001
            check(f"asking one mojo {label} what the pool releases is refused", "quote again" in str(exc), str(exc)[:80])
    # A request with no fee configured at all: no recipient and no rate, so nobody but
    # the trader has any claim on the payout and all of it goes to them.
    offer = fabricate_offer({None: gross}, {T_A: released}, salt=0xA2)
    out = forge_stdin.build({"action": "swap", "offer": offer.to_bech32(), "current_height": H, "pool": v14.pool_to_snapshot(pool)})
    bundle = SpendBundle.from_json_dict(out["bundle"])
    check("with no fee configured neither the router nor the protocol address takes anything",
          paid_to(bundle, ROUTER_PH, T_A) == 0 and paid_to(bundle, pool.protocol_ph, T_A) == 0)
    # the payout coin holds exactly what the reserve released net of the protocol fee,
    # and with nothing owed to anyone else the trader is the only place it can go
    check("the payout coin reaches the trader in full, nothing stranded",
          paid_to(bundle, TRADER_PH, T_A) == released)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} payout-audit checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
