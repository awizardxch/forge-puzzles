from __future__ import annotations

import argparse
import json

from sage_rpc import SageRPC


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether local Sage RPC is reachable")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9257)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        sage = SageRPC(host=args.host, port=args.port)
        status = sage.get_sync_status()
        blockchain_state = status.get("blockchain_state", {}) if isinstance(status, dict) else {}
        peak = blockchain_state.get("peak", {}) if isinstance(blockchain_state, dict) else {}
        height = peak.get("height") if isinstance(peak, dict) else None
        synced_coins = status.get("synced_coins") if isinstance(status, dict) else None
        total_coins = status.get("total_coins") if isinstance(status, dict) else None
        checked_files = status.get("checked_files") if isinstance(status, dict) else None
        total_files = status.get("total_files") if isinstance(status, dict) else None

        explicit_syncing = bool(
            status.get("syncing")
            or status.get("is_syncing")
            or blockchain_state.get("syncing")
            or blockchain_state.get("is_syncing")
        ) if isinstance(status, dict) else False
        synced_flag = status.get("synced") if isinstance(status, dict) else None
        if synced_flag is None and isinstance(blockchain_state, dict):
            synced_flag = blockchain_state.get("synced")

        coins_ready = (
            isinstance(synced_coins, int)
            and isinstance(total_coins, int)
            and total_coins >= 0
            and synced_coins >= total_coins
        )
        files_ready = (
            isinstance(checked_files, int)
            and isinstance(total_files, int)
            and total_files >= 0
            and checked_files >= total_files
        )
        counts_indicate_ready = coins_ready or files_ready
        counts_indicate_syncing = (
            (isinstance(synced_coins, int) and isinstance(total_coins, int) and total_coins > synced_coins)
            or (isinstance(checked_files, int) and isinstance(total_files, int) and total_files > checked_files)
        )

        if explicit_syncing:
            syncing = True
        elif synced_flag is False and counts_indicate_ready:
            syncing = False
        else:
            syncing = counts_indicate_syncing

        if syncing:
            synced = False
        elif synced_flag is not None:
            synced = bool(synced_flag) or counts_indicate_ready
        else:
            synced = counts_indicate_ready or True

        detail_parts: list[str] = []
        if isinstance(synced_coins, int) and isinstance(total_coins, int):
            detail_parts.append(f"coins {synced_coins}/{total_coins}")
        if isinstance(checked_files, int) and isinstance(total_files, int):
            detail_parts.append(f"files {checked_files}/{total_files}")
        if isinstance(height, int):
            detail_parts.append(f"height {height}")
        detail = ", ".join(detail_parts)
        message = (
            f"Sage RPC syncing{': ' + detail if detail else ''}"
            if syncing
            else f"Sage RPC connected{': ' + detail if detail else ''}"
        )

        print(json.dumps({
            "success": True,
            "connected": True,
            "syncing": syncing,
            "synced": synced,
            "height": height,
            "status": "syncing" if syncing else "connected",
            "syncedCoins": synced_coins,
            "totalCoins": total_coins,
            "checkedFiles": checked_files,
            "totalFiles": total_files,
            "message": message,
        }))
        return 0
    except Exception as exc:
        print(json.dumps({
            "success": False,
            "connected": False,
            "error": str(exc),
            "message": "Sage RPC disconnected",
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())