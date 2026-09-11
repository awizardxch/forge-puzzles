from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from chia.rpc.full_node_rpc_client import FullNodeRpcClient
except ModuleNotFoundError:
    from chia.full_node.full_node_rpc_client import FullNodeRpcClient
try:
    from chia.types.spend_bundle import SpendBundle
except ModuleNotFoundError:
    from chia.wallet.wallet_spend_bundle import WalletSpendBundle as SpendBundle
from chia.util.config import load_config
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia_rs import G2Element

from sage_rpc import SageRPC


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Broadcast a signed spend bundle through local Sage RPC or a Chia full node RPC")
    parser.add_argument("--chia-root", default=os.getenv("CHIA_ROOT", str(DEFAULT_ROOT_PATH)))
    parser.add_argument("--rpc-host", default=os.getenv("TESTNET_RPC_HOST", "127.0.0.1"))
    parser.add_argument("--rpc-port", type=int, default=int(os.getenv("TESTNET_RPC_PORT", "8555")))
    parser.add_argument("--sage-host", default=os.getenv("SAGE_RPC_HOST", "127.0.0.1"))
    parser.add_argument("--sage-port", type=int, default=int(os.getenv("SAGE_RPC_PORT", "9257")))
    parser.add_argument("--payload-file", help="Path to a saved JSON payload or Stage 2 artifact file")
    parser.add_argument("--skip-sage", action="store_true")
    parser.add_argument("--skip-full-node-fallback", action="store_true")
    return parser.parse_args()


def read_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_file:
        raw = Path(args.payload_file).read_text(encoding="utf-8").strip()
    else:
        raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("Missing JSON stdin payload")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object payload")
    return extract_broadcast_payload(payload)


def extract_broadcast_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("broadcast_payload", "signed_bundle", "spend_bundle", "full_spend_bundle"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return nested
    return payload


def normalize_coin_spends_payload(coin_spends: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_coin_spend(spend) for spend in coin_spends]


def normalize_coin_spend(spend: dict[str, Any]) -> dict[str, Any]:
    coin = spend.get("coin") or {}
    return {
        "coin": {
            "parent_coin_info": str(coin.get("parent_coin_info", "")).replace("0x", ""),
            "puzzle_hash": str(coin.get("puzzle_hash", "")).replace("0x", ""),
            "amount": int(coin.get("amount", 0)),
        },
        "puzzle_reveal": str(spend.get("puzzle_reveal", "")).replace("0x", ""),
        "solution": str(spend.get("solution", "")).replace("0x", ""),
    }


def normalize_spend_bundle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    coin_spends = payload.get("coin_spends")
    aggregated_signature = str(payload.get("aggregated_signature", "")).replace("0x", "")
    if not isinstance(coin_spends, list) or not aggregated_signature:
        raise ValueError("Payload must include coin_spends[] and aggregated_signature")

    return {
        "coin_spends": [normalize_coin_spend(spend) for spend in coin_spends],
        "aggregated_signature": aggregated_signature,
    }


def add_0x_prefix(value: str) -> str:
    return value if value.startswith("0x") else f"0x{value}"


def to_full_node_json_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_spend_bundle_payload(payload)
    return {
        "coin_spends": [
            {
                "coin": {
                    "parent_coin_info": add_0x_prefix(spend["coin"]["parent_coin_info"]),
                    "puzzle_hash": add_0x_prefix(spend["coin"]["puzzle_hash"]),
                    "amount": spend["coin"]["amount"],
                },
                "puzzle_reveal": add_0x_prefix(spend["puzzle_reveal"]),
                "solution": add_0x_prefix(spend["solution"]),
            }
            for spend in normalized["coin_spends"]
        ],
        "aggregated_signature": add_0x_prefix(normalized["aggregated_signature"]),
    }


def compact_json(value: Any, limit: int = 1200) -> str:
    try:
        rendered = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    except TypeError:
        rendered = repr(value)
    if len(rendered) <= limit:
        return rendered
    return f"{rendered[:limit]}..."


def extract_error_detail(value: Any, depth: int = 0) -> str | None:
    if depth > 4 or value is None:
        return None
    if isinstance(value, str):
        detail = value.strip()
        return detail or None
    if isinstance(value, list):
        for item in value:
            detail = extract_error_detail(item, depth + 1)
            if detail:
                return detail
        return None
    if not isinstance(value, dict):
        return None

    for key in ("error", "message", "detail", "reason", "exception", "failure", "error_message"):
        detail = extract_error_detail(value.get(key), depth + 1)
        if detail:
            code = value.get("error_code") or value.get("code")
            if isinstance(code, str) and code.strip() and code.strip() not in detail:
                return f"{code.strip()}: {detail}"
            return detail

    for key in ("result", "data", "response", "payload"):
        detail = extract_error_detail(value.get(key), depth + 1)
        if detail:
            return detail

    return None


def describe_submit_result(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        detail = value.strip()
        return detail or None
    if not isinstance(value, dict):
        return compact_json(value)

    detail = extract_error_detail(value)
    if detail:
        return detail

    return f"Raw submit result: {compact_json(value)}"


def submit_result_succeeded(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().upper() == "SUCCESS"
    if isinstance(value, (int, float)):
        return int(value) == 1
    if not isinstance(value, dict):
        return False

    # Sage returns {} on success — treat empty dict as success.
    if not value:
        return True

    status = value.get("status")
    if isinstance(status, str):
        return status.strip().upper() == "SUCCESS"
    if isinstance(status, (int, float)):
        return int(status) == 1

    success = value.get("success")
    if isinstance(success, bool):
        return success

    tx_id = value.get("transaction_id") or value.get("tx_id") or value.get("id")
    return isinstance(tx_id, str) and bool(tx_id.strip()) and not describe_submit_result(value)


def wrap_submit_result(result: Any, transport: str) -> dict[str, Any]:
    success = submit_result_succeeded(result)
    output = {
        "success": success,
        "transport": transport,
        "transaction_id": result.get("transaction_id") if isinstance(result, dict) else None,
        "result": result,
    }
    if not success:
        output["error"] = describe_submit_result(result) or f"{transport} rejected the spend bundle"
    return output


def build_signed_full_bundle_via_sage(payload: dict[str, Any], host: str, port: int) -> dict[str, Any]:
    sign_coin_spends = payload.get("sign_coin_spends")
    full_spend_bundle = payload.get("full_spend_bundle")
    if not isinstance(sign_coin_spends, list) or not isinstance(full_spend_bundle, dict):
        raise ValueError("Operator signing payload must include sign_coin_spends[] and full_spend_bundle")

    sage = SageRPC(host=host, port=port)
    signed = sage.sign_coin_spends(normalize_coin_spends_payload(sign_coin_spends))
    signed_bundle = signed.get("spend_bundle") or signed.get("spendBundle")
    if not isinstance(signed_bundle, dict):
        raise RuntimeError("Sage sign_coin_spends did not return a spend_bundle")

    aggregated_signature = str(signed_bundle.get("aggregated_signature") or signed_bundle.get("aggregatedSignature") or "").replace("0x", "")
    if not aggregated_signature:
        raise RuntimeError("Sage sign_coin_spends did not return an aggregated signature")

    normalized_full_bundle = normalize_spend_bundle_payload({
        **full_spend_bundle,
        "aggregated_signature": aggregated_signature,
    })
    return normalized_full_bundle


def to_spend_bundle(payload: dict[str, Any]) -> SpendBundle:
    normalized = to_full_node_json_payload(payload)
    try:
        return SpendBundle.from_json_dict(normalized, G2Element)
    except TypeError:
        return SpendBundle.from_json_dict(normalized)


def submit_via_sage(payload: dict[str, Any], host: str, port: int) -> dict[str, Any]:
    sage = SageRPC(host=host, port=port)
    normalized = normalize_spend_bundle_payload(payload)
    result = sage.submit_transaction(normalized)
    return wrap_submit_result(result, "sage")


def sign_and_submit_via_sage(payload: dict[str, Any], host: str, port: int) -> dict[str, Any]:
    sage = SageRPC(host=host, port=port)
    normalized_full_bundle = build_signed_full_bundle_via_sage(payload, host, port)
    result = sage.submit_transaction(normalized_full_bundle)
    return wrap_submit_result(result, "sage_sign_submit")


async def submit_via_full_node(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    spend_bundle = to_spend_bundle(payload)

    chia_root = Path(args.chia_root)
    config = load_config(chia_root, "config.yaml")
    self_hostname = config.get("self_hostname", args.rpc_host)

    client = await FullNodeRpcClient.create(
        self_hostname,
        uint16(args.rpc_port),
        chia_root,
        config,
    )
    try:
        result = await client.push_tx(spend_bundle)
        return wrap_submit_result(result, "full_node")
    finally:
        client.close()
        await client.await_closed()


async def main() -> int:
    args = parse_args()
    payload = read_payload(args)
    errors: list[str] = []

    if not args.skip_sage:
        try:
            if isinstance(payload.get("sign_coin_spends"), list) and isinstance(payload.get("full_spend_bundle"), dict):
                result = sign_and_submit_via_sage(payload, args.sage_host, args.sage_port)
            else:
                result = submit_via_sage(payload, args.sage_host, args.sage_port)
            if result.get("success"):
                print(json.dumps(result))
                return 0
            errors.append(f"Sage submit_transaction failed: {result.get('error') or compact_json(result.get('result'))}")
        except Exception as exc:
            errors.append(f"Sage submit_transaction failed: {exc}")

    if not args.skip_full_node_fallback:
        try:
            result = await submit_via_full_node(payload, args)
            if errors:
                result["warnings"] = errors
            print(json.dumps(result))
            return 0 if result.get("success") else 1
        except Exception as exc:
            errors.append(f"Full node push_tx failed: {exc}")

    print(json.dumps({
        "success": False,
        "error": " | ".join(errors) if errors else "No broadcast transport enabled.",
    }))
    return 1


if __name__ == "__main__":
    try:
        from chia_rs.sized_ints import uint16
    except ModuleNotFoundError:
        from chia.util.ints import uint16

    raise SystemExit(asyncio.run(main()))