#!/usr/bin/env python3
"""Set a CAT's display name/ticker in Sage wallet via update_cat.

Returns JSON suitable for the api/sage-add-cat route.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sage_rpc import SageRPC


def _safe_str(value: Any) -> str:
    return str(value) if value is not None else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Set CAT display metadata in Sage wallet")
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--ticker", default="")
    parser.add_argument(
        "--only-if-unset",
        action="store_true",
        help=(
            "Fill the display name/ticker only when Sage has none. Used for pools "
            "launched before the LP identity was persisted: there is no authoritative "
            "name to assert for them, so a hand-typed label must not be overwritten."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9257)
    args = parser.parse_args()

    asset_id = args.asset_id.strip().lower()

    try:
        sage = SageRPC(host=args.host, port=args.port)
    except Exception as exc:
        print(json.dumps({
            "success": False,
            "error": _safe_str(exc),
            "stage": "connect",
        }))
        return 1

    # Do NOT call sage.login() here: it switches Sage's globally active wallet
    # for the whole desktop app, not just this RPC call. Looping through every
    # known fingerprint and logging into the first one that succeeds can
    # silently swap the user away from the wallet they're actually using.
    # Rely on whichever wallet is already active, same as list_sage_cats.py
    # and sage_spendable_coins.py.

    # Sage has no "add a CAT" RPC -- `add_cat` was removed at some point and
    # this script kept calling it, 404ing on every attempt (confirmed live:
    # docs/FORGE_ROADMAP.md, the remove-liquidity LP-discovery fix). What
    # exists now is `update_cat`, which is a real update: it requires the
    # record's CURRENT shape echoed back in full -- including `balance` and
    # `selectable_balance`, which are Sage's own derived numbers, not
    # something a caller should invent. So this reads the existing record
    # first and only overwrites the display fields (name, ticker, visible).
    #
    # Sage discovers a CAT's balance on its own once the wallet's sync
    # catches up to the coin's confirmation -- no RPC call makes that happen
    # sooner. If the asset has no record yet, there is nothing safe to write:
    # returning "unsynced" here is honest, where fabricating a zero balance
    # record would not be.
    try:
        cats = sage.call("get_cats")["cats"]
    except Exception as exc:
        print(json.dumps({
            "success": False,
            "assetId": asset_id,
            "error": _safe_str(exc),
            "stage": "get_cats",
        }))
        return 1

    existing = next(
        (cat for cat in cats if str(cat.get("asset_id", "")).strip().lower() == asset_id),
        None,
    )

    if existing is None:
        print(json.dumps({
            "success": False,
            "assetId": asset_id,
            "error": "Sage has not indexed this asset yet. It will appear once wallet sync "
                     "catches up to the coin's confirmation; no RPC call can force that.",
            "stage": "unsynced",
        }))
        return 1

    # A supplied value normally wins: the launch name is the pool's real
    # identity and every wallet should converge on it. With --only-if-unset the
    # caller is offering a fallback rather than asserting a truth, so whatever
    # Sage already holds is kept.
    def display(supplied: str, current: Any) -> Any:
        current_text = str(current or "").strip()
        if args.only_if_unset and current_text:
            return current
        return supplied.strip() or current

    record = {
        "asset_id": existing["asset_id"],
        "name": display(args.name, existing.get("name")),
        "ticker": display(args.ticker, existing.get("ticker")),
        "precision": existing.get("precision", 3),
        "description": existing.get("description"),
        "icon_url": existing.get("icon_url"),
        "visible": True,
        "revocation_address": existing.get("revocation_address"),
        "balance": existing.get("balance", 0),
        "selectable_balance": existing.get("selectable_balance", 0),
    }

    try:
        result = sage.call("update_cat", {"record": record})
        print(json.dumps({
            "success": True,
            "assetId": asset_id,
            "result": result,
        }))
        return 0
    except Exception as exc:
        print(json.dumps({
            "success": False,
            "assetId": asset_id,
            "error": _safe_str(exc),
            "stage": "update_cat",
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
