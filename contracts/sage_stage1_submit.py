"""sage_stage1_submit.py — Sign and submit a pre-built Stage 1 pool launcher spend bundle.

Called by the /api/stage1-sage-submit endpoint. Accepts JSON from stdin:
  { "coin_spends": [...], "launcherCoinId": "hex" }

The coin_spends are built by the TypeScript buildPoolCreationBundle function. Sage
signs the wallet puzzle spends it owns (the XCH funding coin) and broadcasts.

Outputs a single JSON line:
  { "success": true, "launcherCoinId": "...", "txId": "..." }
or on failure exits 1 and prints:
  { "success": false, "error": "..." }
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sage_rpc import SageRPC


def normalize_coin_spend(cs: dict) -> dict:
    """Strip 0x prefixes and normalize amount to int."""
    coin = cs.get("coin") or {}
    return {
        "coin": {
            "parent_coin_info": str(coin.get("parent_coin_info", "")).replace("0x", ""),
            "puzzle_hash": str(coin.get("puzzle_hash", "")).replace("0x", ""),
            "amount": int(coin.get("amount", 0)),
        },
        "puzzle_reveal": str(cs.get("puzzle_reveal", "")).replace("0x", ""),
        "solution": str(cs.get("solution", "")).replace("0x", ""),
    }


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("[aWizard] Missing JSON payload on stdin")

    payload = json.loads(raw)
    coin_spends = payload.get("coin_spends")
    launcher_coin_id = payload.get("launcherCoinId", "")

    if not isinstance(coin_spends, list) or not coin_spends:
        raise ValueError("[aWizard] coin_spends must be a non-empty list")

    normalized = [normalize_coin_spend(cs) for cs in coin_spends]

    sage = SageRPC()

    print(f"[aWizard] Stage 1: signing {len(normalized)} coin spend(s) via Sage RPC...", file=sys.stderr)
    signed = sage.sign_coin_spends(normalized)
    spend_bundle = signed.get("spend_bundle")

    if not spend_bundle:
        raise RuntimeError(
            f"[aWizard] Sage sign_coin_spends did not return a spend_bundle. Response: {signed}"
        )

    print("[aWizard] Stage 1: submitting signed bundle to testnet11...", file=sys.stderr)
    raw_result = sage.submit_transaction(spend_bundle)

    # Sage returns {} (empty dict) on success
    if isinstance(raw_result, dict) and "error" in raw_result:
        raise RuntimeError(f"[aWizard] Sage rejected Stage 1 bundle: {raw_result.get('error')}")

    tx_id = ""
    if isinstance(raw_result, dict):
        tx_id = str(raw_result.get("transaction_id") or raw_result.get("tx_id") or "")

    print(json.dumps({
        "success": True,
        "launcherCoinId": launcher_coin_id,
        "txId": tx_id,
        "raw": raw_result,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        sys.exit(1)
