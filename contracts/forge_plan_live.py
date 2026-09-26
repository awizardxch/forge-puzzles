#!/usr/bin/env python3
"""What one pool would pay for one input, by the composer's own arithmetic.

The website asks this before it lets a wallet sign: "for the gross the pool will
see, how much does the pool release right now?" — and clamps what the offer asks
for down to that, so a quote the pools have moved past is corrected before it
becomes a stranded offer rather than refused after.

Until 2026-09-25 this question was routed through `sync_pool_from_chain.py`,
which had been archived to contracts/development/ in September because it
parses a puzzle shape retired before V6. The call returned 502 on every swap;
the browser logged a warning and proceeded on the quote alone. And the state it
would have planned from was the batch's *bootstrap* amounts through Number(),
which is neither post-swap nor exact. The security audit's own lesson names the
pattern: an archive is not neutral, and a live caller left pointing at it is a
silent failure.

So this is the three lines the settle lane runs, and nothing else:

    honest = swap_output(r_in, r_out, gross, fee_bps, w_in, w_out)
    pfee   = honest * protocol_fee_bps // 10_000 + honest * dao_fee_bps // 10_000
    out    = honest - pfee

over the pool state the responder persisted at the last push -- the same state
the lane will build from. `gross` is what reaches the pool: the caller's amount
net of the router's carve where that carve is on the input side, which is how the
site already sends it (`firstHop.amountIn`). Nothing here reads the chain: the
persisted state leads the chain by design, and agreeing with the lane is the
whole point.

stdin:  {"reserves":[...], "weights":[...], "fee_bps":30, "protocol_fee_bps":5,
         "dao_fee_bps":0, "in_index":0, "out_index":1, "amount_in":"...",
         "min_amount_out":"0"}
stdout: {"success":true, "quote":{"amount_out":..., "honest":..., "protocol_fee":...,
         "gross_input":...}, "source":"indexed-state"}
"""
from __future__ import annotations

import json
import sys

import forge_math


def plan(payload: dict) -> dict:
    reserves = [int(x) for x in payload.get("reserves") or []]
    weights = [int(x) for x in payload.get("weights") or [1] * len(reserves)]
    if len(reserves) < 2 or len(weights) != len(reserves):
        raise ValueError("a pool needs at least two reserves, with one weight each")
    i_in = int(payload.get("in_index", 0))
    i_out = int(payload.get("out_index", 1))
    if i_in == i_out or not (0 <= i_in < len(reserves)) or not (0 <= i_out < len(reserves)):
        raise ValueError("in_index and out_index must be distinct slots of this pool")
    gross = int(str(payload.get("amount_in", "0")))
    if gross <= 0:
        raise ValueError("amount_in must be positive whole mojos")
    fee_bps = int(payload.get("fee_bps", 30))
    protocol_fee_bps = int(payload.get("protocol_fee_bps", 0))
    dao_fee_bps = int(payload.get("dao_fee_bps", 0))
    min_out = int(str(payload.get("min_amount_out", "0") or "0"))

    # The router's carve, on the side the lane takes it from. The site sends the
    # amount BEFORE that carve (`firstHop.amountIn`); the lane sees the settlement
    # coin AFTER it. Trader receives a CAT: the fee comes off the input, and the
    # pool prices the remainder. Trader receives XCH: the pool prices the whole
    # input and the fee comes off the payout. Floor division both ways, as
    # `netRouterFee` in the aggregator does. Measured 2026-09-25: without this,
    # plan-live sat 1-3 mojos above the composer on a saturated pool, and the two
    # agree to the mojo with it (honest 48752 -> 48728 on both sides).
    router_bps = int(payload.get("router_fee_bps", 0))
    fee_on_output = bool(payload.get("asset_out_native", False))
    if router_bps > 0 and not fee_on_output:
        gross = gross - gross * router_bps // 10_000
        if gross <= 0:
            raise ValueError("nothing reaches the pool after the router's carve")

    honest = forge_math.swap_output(reserves[i_in], reserves[i_out], gross, fee_bps, weights[i_in], weights[i_out])
    pfee = honest * protocol_fee_bps // 10_000 + honest * dao_fee_bps // 10_000
    payout = honest - pfee
    router_fee = payout * router_bps // 10_000 if (router_bps > 0 and fee_on_output) else 0
    out = payout - router_fee
    if out <= 0:
        raise ValueError("the pool releases nothing for this input")
    if out < min_out:
        raise ValueError(f"the pool releases {out}, below the {min_out} asked for")
    return {
        "success": True,
        "source": "indexed-state",
        "quote": {
            "amount_out": out,
            "honest": honest,
            "protocol_fee": pfee,
            "router_fee": router_fee if fee_on_output else int(str(payload.get("amount_in", "0"))) - gross,
            "router_fee_side": "output" if fee_on_output else ("input" if router_bps > 0 else "none"),
            "gross_input": gross,
            "reserve_in_after": reserves[i_in] + gross,
            "reserve_out_after": reserves[i_out] - honest,
        },
    }


def main() -> int:
    try:
        print(json.dumps(plan(json.loads(sys.stdin.read() or "{}"))))
        return 0
    except Exception as exc:  # noqa: BLE001 -- the responder needs the message, not a trace
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
