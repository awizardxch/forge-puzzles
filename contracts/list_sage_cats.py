from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sage_rpc import SageRPC


def _sum_coin_amounts(coins: list[dict]) -> int:
    total = 0
    for coin in coins:
        try:
            total += int(coin.get("amount", 0))
        except (TypeError, ValueError):
            continue
    return total


def _pick_name(cat: dict) -> str:
    for key in ("name", "symbol", "ticker", "asset_name"):
        value = cat.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    asset_id = str(cat.get("asset_id") or cat.get("assetId") or "")
    return f"CAT {asset_id[:12]}" if asset_id else "Unknown CAT"


def _pick_symbol(cat: dict) -> str:
    for key in ("symbol", "ticker", "name", "asset_name"):
        value = cat.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    asset_id = str(cat.get("asset_id") or cat.get("assetId") or "")
    return asset_id[:8] if asset_id else "CAT"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9257)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    try:
        sage = SageRPC(host=args.host, port=args.port)
        cats = sage.get_cats()
        visible_cats: list[dict] = []

        for cat in cats:
            asset_id = str(cat.get("asset_id") or cat.get("assetId") or "").replace("0x", "").lower()
            if len(asset_id) != 64:
                continue

            coin_result = sage.get_coins(asset_id=asset_id, limit=args.limit)
            coins = coin_result.get("coins", []) if isinstance(coin_result, dict) else []
            balance_mojos = _sum_coin_amounts(coins)
            if balance_mojos <= 0:
                continue

            visible_cats.append({
                "assetId": asset_id,
                "name": _pick_name(cat),
                "symbol": _pick_symbol(cat),
                "balanceMojos": str(balance_mojos),
                "coinCount": len(coins),
            })

        visible_cats.sort(key=lambda cat: (cat["symbol"].lower(), cat["assetId"]))
        print(json.dumps({"success": True, "cats": visible_cats}))
        return 0
    except Exception as exc:  # pragma: no cover - local runtime helper
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())