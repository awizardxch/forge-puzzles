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

  * the trader is paid exactly what they notarised, at any rate;
  * the protocol fee is owed exactly, at any rate;
  * nothing is invented or lost: trader + router + protocol == what the curve released;
  * asking for more than the pool releases refuses the whole bundle.

Exits 0 when every check passes, 1 otherwise, 2 when the V11 build is absent.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

import forge_math  # noqa: E402
import forge_stdin  # noqa: E402
import forge_v11_driver as drv  # noqa: E402
import forge_v11_offer as v11  # noqa: E402
from _test_v11_offer_lane import ROUTER_PH, T_A, TRADER_PH, H, fabricate_offer, make, paid_to  # noqa: E402
from chia_rs import SpendBundle  # noqa: E402

results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def main() -> int:
    if not drv.v11_available():
        print("  [skip] V11 puzzles not built"); return 2
    pool = make([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x81)
    r, w = pool.state[0], pool.weights
    gross = 100_000_000
    honest = forge_math.swap_output(r[0], r[1], gross, pool.fee_bps, w[0], w[1])
    pfee = honest * pool.protocol_fee_bps // 10_000
    released = honest - pfee
    print(f"pool releases {released} of A for {gross} XCH (curve {honest}, protocol fee {pfee})")

    for bps in (0, 30, 500, 10_000, 100_000):
        want = released * 97 // 100
        offer = fabricate_offer({None: gross}, {T_A: want}, salt=0x90 + (bps % 200))
        out = forge_stdin.build({"action": "swap", "offer": offer.to_bech32(), "current_height": H, "pool": v11.pool_to_snapshot(pool),
                                 "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": bps}})
        bundle = SpendBundle.from_json_dict(out["bundle"])
        trader = paid_to(bundle, TRADER_PH, T_A)
        router = paid_to(bundle, ROUTER_PH, T_A)
        owed = [int(x) for x in out["pool"]["state"]["fees_owed"]]
        label = f"router asks {bps:,} bps"
        check(f"{label}: the trader is paid exactly what they notarised", trader == want, f"{trader} vs {want}")
        check(f"{label}: the router's take stops exactly at the surplus", router == released - want, f"{router}")
        check(f"{label}: the protocol fee is owed exactly", owed == [0, pfee], f"{owed}")
        check(f"{label}: nothing invented or lost", trader + router + pfee == honest)

    print("a hostile request:")
    offer = fabricate_offer({None: gross}, {T_A: released + 1}, salt=0xA1)
    try:
        forge_stdin.build({"action": "swap", "offer": offer.to_bech32(), "current_height": H, "pool": v11.pool_to_snapshot(pool),
                           "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}})
        check("asking one mojo above what the pool releases is refused", False, "it settled")
    except Exception as exc:  # noqa: BLE001
        check("asking one mojo above what the pool releases is refused", True, str(exc)[:80])
    # a router that omits the fee recipient collects nothing: the surplus goes to the pool's protocol address
    offer = fabricate_offer({None: gross}, {T_A: released * 97 // 100}, salt=0xA2)
    out = forge_stdin.build({"action": "swap", "offer": offer.to_bech32(), "current_height": H, "pool": v11.pool_to_snapshot(pool)})
    bundle = SpendBundle.from_json_dict(out["bundle"])
    check("with no router recipient the surplus goes to the pool's protocol address, not nowhere",
          paid_to(bundle, ROUTER_PH, T_A) == 0 and paid_to(bundle, pool.protocol_ph, T_A) == released - released * 97 // 100)
    # the payout coin holds exactly what the reserve released net of the protocol fee: the
    # trader's floor and the surplus are the only two places it can go
    check("the payout coin is split between the trader's floor and the surplus, nothing else",
          paid_to(bundle, TRADER_PH, T_A) + paid_to(bundle, pool.protocol_ph, T_A) == released)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} payout-audit checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
