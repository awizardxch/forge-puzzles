#!/usr/bin/env python3
"""Launch an LP CAT token for a Forge CFMM pool after Stage 2 bootstrap.

Uses Sage RPC issue_cat to mint a wallet-visible CAT representing LP ownership.
This is the non-authoritative operator bootstrap path — the CAT is named and
tracked by the operator, not yet governed by the pool singleton TAIL.

Called by /api/stage2-lp-cat endpoint or manually:

    python launch_lp_cat_from_stage2_history.py \
        --launcher-coin-id <hex> \
        --bootstrap-target-coin-id <hex> \
        --asset-symbols "SYM1/SYM2/SYM3" \
        --bootstrap-amounts "100,200,300" \
        [--pool-name "TM10"] \
        [--name "Forge LP - TM10"] \
        [--ticker "FLPTM10"] \
        [--fee 1000000] \
        [--allow-non-authoritative]

Outputs a single JSON line on success:
    {
      "launcher_coin_id": "hex",
      "bootstrap_target_coin_id": "hex",
      "expected_lp_share": "1100",
      "lp_cat_name": "Forge LP - TM10",
      "lp_cat_ticker": "FLPTM10",
      "issue_cat": {
        "asset_id": "hex",
        "name": "...",
        "ticker": "...",
        "minted_coin_id": "hex"
      }
    }
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sage_rpc import SageRPC

LP_SEED_AMOUNT = 1000  # bn = 1000n in the Forge bundle


def compute_expected_lp_share(bootstrap_amounts: list[str]) -> int:
    """Replicate static calculateExpectedLpShare from Forge bundle."""
    if not bootstrap_amounts:
        return LP_SEED_AMOUNT
    return LP_SEED_AMOUNT + int(bootstrap_amounts[0])


def build_lp_cat_ticker(pool_name: str, asset_symbols: list[str], launcher_coin_id: str) -> str:
    base = "".join(c for c in pool_name if c.isalnum()).upper()
    if base:
        return f"FLP{base[:6]}"
    parts = [
        "".join(c for c in s if c.isalnum()).upper()
        for s in asset_symbols
    ]
    parts = [p[:3] for p in parts if p]
    joined = "".join(parts)
    if joined:
        return f"FLP{joined[:7]}"
    return f"FLP{launcher_coin_id[:6].upper()}"


def build_lp_cat_name(pool_name: str, asset_symbols: list[str], target_coin_id: str) -> str:
    if pool_name.strip():
        return f"Forge LP - {pool_name.strip()}"
    if asset_symbols:
        return f"Forge LP - {' / '.join(asset_symbols)}"
    return f"Forge LP - {target_coin_id[:8]}"


def _coin_id(parent: str, puzzle_hash: str, amount: int) -> str:
    raw_parent = bytes.fromhex(parent.replace("0x", ""))
    raw_ph = bytes.fromhex(puzzle_hash.replace("0x", ""))
    raw_amount = struct.pack(">Q", amount)
    return hashlib.sha256(raw_parent + raw_ph + raw_amount).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch LP CAT from Stage 2 history")
    parser.add_argument("--launcher-coin-id", required=True)
    parser.add_argument("--bootstrap-target-coin-id", required=True)
    parser.add_argument("--asset-symbols", required=True, help="Slash-separated: SYM1/SYM2")
    parser.add_argument("--bootstrap-amounts", required=True, help="Comma-separated amounts")
    parser.add_argument("--pool-name", default="")
    parser.add_argument("--name", default=None, help="Override LP CAT name")
    parser.add_argument("--ticker", default=None, help="Override LP CAT ticker")
    parser.add_argument("--fee", type=int, default=1_000_000)
    parser.add_argument("--allow-non-authoritative", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9257)
    args = parser.parse_args()

    symbols = [s.strip() for s in args.asset_symbols.split("/") if s.strip()]
    amounts = [s.strip() for s in args.bootstrap_amounts.split(",") if s.strip()]

    expected_lp = compute_expected_lp_share(amounts)
    cat_name = args.name or build_lp_cat_name(args.pool_name, symbols, args.bootstrap_target_coin_id)
    cat_ticker = args.ticker or build_lp_cat_ticker(args.pool_name, symbols, args.launcher_coin_id)

    print(
        f"[aWizard] Issuing LP CAT: name={cat_name!r}, ticker={cat_ticker!r}, "
        f"amount={expected_lp}, fee={args.fee}",
        file=sys.stderr,
    )

    sage = SageRPC(host=args.host, port=args.port)

    # Step 1: Preview the issue_cat transaction
    preview = sage.call("issue_cat", {
        "name": cat_name,
        "ticker": cat_ticker,
        "amount": expected_lp,
        "fee": args.fee,
    })

    coin_spends = preview.get("coin_spends", [])
    if not coin_spends:
        raise RuntimeError("[aWizard] issue_cat returned no coin_spends")

    print(f"[aWizard] issue_cat preview: {len(coin_spends)} coin spend(s)", file=sys.stderr)

    # Step 2: Sign the coin spends
    signed = sage.sign_coin_spends(coin_spends)
    spend_bundle = signed.get("spend_bundle")
    if not spend_bundle:
        raise RuntimeError(
            f"[aWizard] sign_coin_spends did not return spend_bundle: {signed}"
        )

    print("[aWizard] LP CAT transaction signed, submitting...", file=sys.stderr)

    # Step 3: Submit the signed bundle
    submit_result = sage.submit_transaction(spend_bundle)

    if isinstance(submit_result, dict) and "error" in submit_result:
        raise RuntimeError(f"[aWizard] submit_transaction failed: {submit_result['error']}")

    # Step 4: Extract the minted CAT asset_id from the summary.
    # Sage issue_cat summary uses "inputs" — each input has an "asset" and "outputs" list.
    # The LP CAT entry has a non-null asset_id matching our ticker.
    summary = preview.get("summary", {})
    inputs = summary.get("inputs", [])
    asset_id = None
    minted_coin_id = None

    for inp in inputs:
        asset_info = inp.get("asset", {})
        aid = asset_info.get("asset_id")
        if aid and asset_info.get("ticker") == cat_ticker:
            asset_id = str(aid).replace("0x", "")
            # Find the receiving output — that's the minted coin
            for out in inp.get("outputs", []):
                if out.get("receiving"):
                    minted_coin_id = str(out["coin_id"]).replace("0x", "")
                    break
            break

    # Fallback: any input with a non-null asset_id (non-XCH)
    if not asset_id:
        for inp in inputs:
            asset_info = inp.get("asset", {})
            aid = asset_info.get("asset_id")
            if aid:
                asset_id = str(aid).replace("0x", "")
                for out in inp.get("outputs", []):
                    if out.get("receiving"):
                        minted_coin_id = str(out["coin_id"]).replace("0x", "")
                        break
                break

    if not asset_id:
        raise RuntimeError(
            f"[aWizard] Could not extract LP CAT asset_id from issue_cat summary. "
            f"Inputs: {json.dumps(inputs, default=str)[:500]}"
        )

    print(f"[aWizard] LP CAT issued: asset_id={asset_id}", file=sys.stderr)

    print(json.dumps({
        "launcher_coin_id": args.launcher_coin_id,
        "bootstrap_target_coin_id": args.bootstrap_target_coin_id,
        "expected_lp_share": str(expected_lp),
        "lp_cat_name": cat_name,
        "lp_cat_ticker": cat_ticker,
        "issue_cat": {
            "asset_id": asset_id,
            "name": cat_name,
            "ticker": cat_ticker,
            "minted_coin_id": minted_coin_id or "",
        },
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": str(exc)}))
        sys.exit(1)
