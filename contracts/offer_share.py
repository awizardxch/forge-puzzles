#!/usr/bin/env python3
"""Offer-shaped spends + their signature -> the bech32 offer Dexie takes.

The Sage app settles a single-pool swap itself (src/lib/relay/v16/inAppSettle.ts): it
signs the trader's spends, which pay into the offer settlement puzzle exactly as an
offer's would, beside zero-parent placeholders for the requested payments. Dexie only
takes an offer file in Chia's compressed encoding, which the browser cannot produce,
so the router encodes it here with Chia's own Offer.

stdin:  {coin_spends: [...], aggregated_signature}
stdout: {success, offer, offered, requested}   (amounts per asset; "xch" for XCH)
Anything Offer does not read as an offer -- no requested payments, nothing offered --
is refused, so this cannot be used to post arbitrary bundles.
"""
from __future__ import annotations

import json
import sys

from chia.wallet.trading.offer import Offer
from chia_rs import CoinSpend, G2Element, SpendBundle


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        spends = [CoinSpend.from_json_dict(_prefixed(cs)) for cs in payload.get("coin_spends") or []]
        if not spends or len(spends) > 64:
            raise ValueError("coin_spends must list 1 to 64 spends")
        signature = G2Element.from_bytes(bytes.fromhex(str(payload.get("aggregated_signature") or "").lower().removeprefix("0x")))
        offer = Offer.from_spend_bundle(SpendBundle(spends, signature))
        offered = {("xch" if k is None else k.hex()): int(v) for k, v in offer.get_offered_amounts().items()}
        requested = {("xch" if k is None else k.hex()): int(v) for k, v in offer.get_requested_amounts().items()}
        if not offered or not requested:
            raise ValueError("these spends are not an offer: it must offer something and request something")
        print(json.dumps({"success": True, "offer": offer.to_bech32(), "offered": offered, "requested": requested}))
    except Exception as exc:  # noqa: BLE001 -- the caller reads the reason
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
    return 0


def _prefixed(cs: dict) -> dict:
    """Spends built in the browser carry hex without 0x; chia's JSON parser insists on it."""
    fix = lambda v: v if str(v).startswith("0x") else "0x" + str(v)  # noqa: E731
    coin = cs["coin"]
    return {"coin": {"parent_coin_info": fix(coin["parent_coin_info"]), "puzzle_hash": fix(coin["puzzle_hash"]), "amount": int(coin["amount"])},
            "puzzle_reveal": fix(cs["puzzle_reveal"]), "solution": fix(cs["solution"])}


if __name__ == "__main__":
    raise SystemExit(main())
