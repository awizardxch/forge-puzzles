"""execute_stage2_liquidity_bootstrap.py — Sign and submit a pre-built Stage 2 bootstrap spend bundle.

Called by /api/stage2-bootstrap endpoint. Accepts JSON from stdin:
  {
    "coin_spends": [...],
    "launcherCoinId": "hex",
    "metadata": { ... }   // optional, echoed back
  }

The coin_spends are built by the TypeScript buildCanonicalStage2LiquidityBootstrapBundle
function in the browser. Sage signs the wallet puzzle spends it owns and broadcasts.

Outputs a single JSON line:
  {
    "success": true,
    "launcher_coin_id": "hex",
    "target_coin_id": "hex",
    "target_puzzle_hash": "hex",
    "pool_announcement_id": "hex",
    "lp_out": "string",
    "submit_result": { ... }
  }
or on failure exits 1 and prints:
  { "success": false, "error": "..." }
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sage_rpc import SageRPC


def _strip_0x(val: str) -> str:
    """Strip a leading 0x prefix only (not internal occurrences)."""
    s = str(val)
    return s[2:] if s.startswith("0x") else s


def normalize_coin_spend(cs: dict) -> dict:
    """Strip 0x prefixes and normalize amount to int."""
    coin = cs.get("coin") or {}
    return {
        "coin": {
            "parent_coin_info": _strip_0x(coin.get("parent_coin_info", "")),
            "puzzle_hash": _strip_0x(coin.get("puzzle_hash", "")),
            "amount": int(coin.get("amount", 0)),
        },
        "puzzle_reveal": _strip_0x(cs.get("puzzle_reveal", "")),
        "solution": _strip_0x(cs.get("solution", "")),
    }


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("[aWizard] Missing JSON payload on stdin")

    payload = json.loads(raw)
    coin_spends = payload.get("coin_spends")
    launcher_coin_id = payload.get("launcherCoinId", "")
    metadata = payload.get("metadata") or {}

    if not isinstance(coin_spends, list) or not coin_spends:
        raise ValueError("[aWizard] coin_spends must be a non-empty list")

    normalized = [normalize_coin_spend(cs) for cs in coin_spends]

    # Debug dump for diagnosing signing failures — save ALL spends
    debug_path = Path(__file__).parent / "compiled" / "_stage2_debug_payload.json"
    try:
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(json.dumps({
            "coin_spends_raw": coin_spends,
            "normalized": normalized,
            "count": len(normalized),
        }, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass  # non-critical debug dump

    sage = SageRPC()

    print(
        f"[aWizard] Stage 2: signing {len(normalized)} coin spend(s) via Sage RPC...",
        file=sys.stderr,
    )

    # Try signing one-by-one to identify which spend causes CLVM raise
    for idx, cs in enumerate(normalized):
        pr_len = len(cs.get("puzzle_reveal", ""))
        coin_amt = cs.get("coin", {}).get("amount", "?")
        try:
            sage.sign_coin_spends([cs])
            print(f"[aWizard]   spend {idx}: OK (pr_len={pr_len}, amt={coin_amt})", file=sys.stderr)
        except Exception as e:
            print(f"[aWizard]   spend {idx}: FAIL (pr_len={pr_len}, amt={coin_amt}): {e}", file=sys.stderr)

    signed = sage.sign_coin_spends(normalized)
    spend_bundle = signed.get("spend_bundle")

    if not spend_bundle:
        raise RuntimeError(
            f"[aWizard] Sage sign_coin_spends did not return a spend_bundle. Response: {signed}"
        )

    print("[aWizard] Stage 2: submitting signed bundle to testnet11...", file=sys.stderr)
    raw_result = sage.submit_transaction(spend_bundle)

    if isinstance(raw_result, dict) and "error" in raw_result:
        raise RuntimeError(f"[aWizard] Sage rejected Stage 2 bundle: {raw_result.get('error')}")

    tx_id = ""
    if isinstance(raw_result, dict):
        tx_id = str(raw_result.get("transaction_id") or raw_result.get("tx_id") or "")

    print(json.dumps({
        "success": True,
        "launcher_coin_id": launcher_coin_id,
        "transaction_id": tx_id,
        "target_coin_id": str(metadata.get("targetCoinId", "")),
        "target_puzzle_hash": str(metadata.get("targetPuzzleHash", "")),
        "pool_announcement_id": str(metadata.get("poolAnnouncementId", "")),
        "lp_out": str(metadata.get("lpOut", "")),
        "submit_result": raw_result,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": str(exc)}))
        sys.exit(1)
