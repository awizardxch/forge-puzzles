#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PRECISION = 1_000_000_000_000
FEE_SCALE = 10_000
PROTOCOL_FEE_PPM_SCALE = 1_000_000
NEWTON_ITERS = 16
ZERO_32 = "0" * 64


@dataclass
class AssetConfig:
    asset_id: str
    reserve: int
    weight: int


@dataclass
class PoolFixture:
    fixture_path: Path
    manifest_path: Path
    launcher_id: str
    fee_bps: int
    protocol_fee_ppm: int
    assets: list[AssetConfig]
    total_lp: int


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def normalize_asset_id(value: str | None) -> str:
    if not value:
        return ZERO_32
    normalized = value.strip().lower().removeprefix("0x")
    if normalized in {"xch", "txch", ZERO_32}:
        return ZERO_32
    return normalized


def integer_pow(base_scaled: int, exponent: int) -> int:
    if exponent == 0:
        return PRECISION
    if exponent == 1:
        return base_scaled
    if exponent % 2 == 0:
        half = integer_pow(base_scaled, exponent // 2)
        return (half * half) // PRECISION
    return (base_scaled * integer_pow(base_scaled, exponent - 1)) // PRECISION


def pow_frac(base_scaled: int, p: int, q: int) -> int:
    if base_scaled == PRECISION:
        return PRECISION
    if p == q:
        return base_scaled
    if p == 0:
        return PRECISION

    value = base_scaled
    for _ in range(NEWTON_ITERS):
        y_qm1 = integer_pow(value, q - 1)
        correction = (base_scaled * integer_pow(PRECISION, q - 1)) // y_qm1
        value = (((q - 1) * value) + correction) // q
    return value


def calc_swap_out(
    reserve_in: int,
    reserve_out: int,
    weight_in: int,
    weight_out: int,
    amount_in: int,
    swap_fee_bps: int,
    protocol_fee_ppm: int = 0,
) -> tuple[int, int, int, int]:
    swap_fee_amount = (amount_in * swap_fee_bps) // FEE_SCALE
    protocol_fee_amount = (amount_in * protocol_fee_ppm) // PROTOCOL_FEE_PPM_SCALE
    amount_in_net = amount_in - swap_fee_amount - protocol_fee_amount
    if amount_in_net <= 0:
        raise ValueError("amount_in_net must be positive")

    base_scaled = (reserve_in * PRECISION) // (reserve_in + amount_in_net)
    power = pow_frac(base_scaled, weight_in, weight_out)
    one_minus_power = PRECISION - power
    amount_out = (reserve_out * one_minus_power) // PRECISION
    return amount_out, swap_fee_amount, protocol_fee_amount, amount_in_net


def load_pool_fixture(fixture_path: Path) -> PoolFixture:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    manifest_rel = payload.get("manifest_path")
    if not isinstance(manifest_rel, str) or not manifest_rel.strip():
        raise ValueError("Fixture is missing manifest_path")

    manifest_path = workspace_root() / Path(manifest_rel)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    final_state = payload.get("final_confirmed_state") or {}
    reserves = final_state.get("reserves") or []
    manifest_assets = manifest.get("assets") or []
    if len(reserves) != len(manifest_assets):
        raise ValueError("Fixture reserve count does not match manifest asset count")

    assets: list[AssetConfig] = []
    for asset, reserve in zip(manifest_assets, reserves):
        asset_id = normalize_asset_id(asset.get("asset_id"))
        assets.append(AssetConfig(
            asset_id=asset_id,
            reserve=int(reserve),
            weight=int(asset.get("weight", 0)),
        ))

    return PoolFixture(
        fixture_path=fixture_path,
        manifest_path=manifest_path,
        launcher_id=str(payload.get("launcher_id", "")).removeprefix("0x"),
        fee_bps=int(payload.get("fee_bps", manifest.get("fee_bps", 0))),
        protocol_fee_ppm=int(manifest.get("protocol_fee_ppm", 0) or 0),
        assets=assets,
        total_lp=int(final_state.get("total_lp", 0)),
    )


def build_offer_request(asset_in: str, asset_out: str, amount_in: int, min_amount_out: int) -> dict[str, Any]:
    return {
        "offered_assets": [
            {
                "asset_id": None if asset_in == ZERO_32 else asset_in,
                "amount": str(amount_in),
            }
        ],
        "requested_assets": [
            {
                "asset_id": None if asset_out == ZERO_32 else asset_out,
                "amount": str(min_amount_out),
            }
        ],
        "fee": "0",
    }


def plan_swap(
    pool: PoolFixture,
    in_index: int,
    out_index: int,
    amount_in: int,
    min_amount_out: int = 0,
    recipient_puzzle_hash: str | None = None,
) -> dict[str, Any]:
    if in_index == out_index:
        raise ValueError("in_index and out_index must differ")
    if amount_in <= 0:
        raise ValueError("amount_in must be positive")
    if in_index < 0 or in_index >= len(pool.assets) or out_index < 0 or out_index >= len(pool.assets):
        raise ValueError("swap indices out of range")

    asset_in = pool.assets[in_index]
    asset_out = pool.assets[out_index]
    if asset_in.reserve <= 0 or asset_out.reserve <= 0:
        raise ValueError("swap reserves must be positive")

    amount_out, swap_fee_amount, protocol_fee_amount, amount_in_net = calc_swap_out(
        reserve_in=asset_in.reserve,
        reserve_out=asset_out.reserve,
        weight_in=asset_in.weight,
        weight_out=asset_out.weight,
        amount_in=amount_in,
        swap_fee_bps=pool.fee_bps,
        protocol_fee_ppm=pool.protocol_fee_ppm,
    )
    if amount_out <= 0:
        raise ValueError("computed amount_out must be positive")
    if amount_out >= asset_out.reserve:
        raise ValueError("swap would exhaust reserve_out")
    if amount_out < min_amount_out:
        raise ValueError(f"computed amount_out {amount_out} is below min_amount_out {min_amount_out}")

    reserves_before = [asset.reserve for asset in pool.assets]
    reserves_after = list(reserves_before)
    reserves_after[in_index] = asset_in.reserve + amount_in - protocol_fee_amount
    reserves_after[out_index] = asset_out.reserve - amount_out

    return {
        "launcher_id": pool.launcher_id,
        "fixture_path": str(pool.fixture_path),
        "manifest_path": str(pool.manifest_path),
        "settlement_boundary": "swap-engine",
        "swap_request": {
            "in_index": in_index,
            "out_index": out_index,
            "asset_in_id": asset_in.asset_id,
            "asset_out_id": asset_out.asset_id,
            "amount_in": str(amount_in),
            "min_amount_out": str(min_amount_out),
            "recipient_puzzle_hash": recipient_puzzle_hash or ZERO_32,
        },
        "quote": {
            "amount_out": str(amount_out),
            "swap_fee_amount": str(swap_fee_amount),
            "protocol_fee_amount": str(protocol_fee_amount),
            "amount_in_net": str(amount_in_net),
            "fee_bps": pool.fee_bps,
            "protocol_fee_ppm": pool.protocol_fee_ppm,
        },
        "pool_before": {
            "reserves": [str(value) for value in reserves_before],
            "total_lp": str(pool.total_lp),
        },
        "pool_after": {
            "reserves": [str(value) for value in reserves_after],
            "total_lp": str(pool.total_lp),
        },
        "offer_request": build_offer_request(asset_in.asset_id, asset_out.asset_id, amount_in, max(min_amount_out, amount_out)),
        "submission_context": {
            "type": "forge-swap-fixture",
            "pool_aware": True,
            "uses_live_fixture": True,
            "can_broadcast_live": False,
            "reason": "Deterministic pool-aware submission plan against the confirmed live fixture. Live on-chain broadcasting remains gated until a responder can spend the actual swap settlement lane.",
        },
    }


def pool_from_inline_state(state: dict[str, Any]) -> PoolFixture:
    """Build a PoolFixture from an inline pool-state dict (no fixture file needed).

    Expected shape:
        {
          "launcher_id": str,
          "fee_bps": int,
          "protocol_fee_ppm": int,          # optional, defaults to 0
          "total_lp": int | str,            # optional
          "assets": [
              {"asset_id": str, "reserve": int | str, "weight": int}
          ]
        }
    """
    launcher_id = str(state.get("launcher_id") or "").removeprefix("0x")
    fee_bps = int(state.get("fee_bps") or 0)
    protocol_fee_ppm = int(state.get("protocol_fee_ppm") or 0)
    total_lp = int(state.get("total_lp") or 0)
    raw_assets = state.get("assets") or []
    if not raw_assets:
        raise ValueError("inline pool_state must include a non-empty 'assets' list")

    assets: list[AssetConfig] = []
    for asset in raw_assets:
        asset_id = normalize_asset_id(str(asset.get("asset_id") or ""))
        reserve = int(asset.get("reserve") or 0)
        weight = int(asset.get("weight") or 0)
        assets.append(AssetConfig(asset_id=asset_id, reserve=reserve, weight=weight))

    # Provide a sentinel Path so callers don't get attribute errors
    sentinel = Path(__file__)
    return PoolFixture(
        fixture_path=sentinel,
        manifest_path=sentinel,
        launcher_id=launcher_id,
        fee_bps=fee_bps,
        protocol_fee_ppm=protocol_fee_ppm,
        assets=assets,
        total_lp=total_lp,
    )


def run_action(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "plan").strip().lower()

    # Prefer inline pool state (no fixture file needed — works for any pool)
    inline_state = payload.get("pool_state")
    if isinstance(inline_state, dict) and inline_state:
        pool = pool_from_inline_state(inline_state)
    else:
        fixture_raw = payload.get("fixture_path") or payload.get("fixture")
        if not isinstance(fixture_raw, str) or not fixture_raw.strip():
            fixture_path = Path(__file__).parent / "compiled" / "testnet_lifecycle_e35984cdde6c.json"
        else:
            supplied_path = Path(fixture_raw)
            fixture_path = supplied_path if supplied_path.is_absolute() else workspace_root() / Path(fixture_raw)
        pool = load_pool_fixture(fixture_path)

    if action == "status":
        return {
            "success": True,
            "available": True,
            "launcher_id": pool.launcher_id,
            "fixture_path": str(pool.fixture_path),
            "message": "Forge swap fixture planner is available.",
        }

    if action not in {"plan", "validate", "submit"}:
        raise ValueError(f"Unsupported action: {action}")

    plan = plan_swap(
        pool=pool,
        in_index=int(payload.get("in_index", 0)),
        out_index=int(payload.get("out_index", 1)),
        amount_in=int(payload.get("amount_in", 0)),
        min_amount_out=int(payload.get("min_amount_out", 0)),
        recipient_puzzle_hash=str(payload.get("recipient_puzzle_hash") or ZERO_32).removeprefix("0x"),
    )

    if action == "validate":
        expected_amount_out = payload.get("expected_amount_out")
        if expected_amount_out is not None and int(expected_amount_out) != int(plan["quote"]["amount_out"]):
            raise ValueError(
                f"expected_amount_out {expected_amount_out} does not match computed amount_out {plan['quote']['amount_out']}"
            )

    if action == "submit":
        offer = str(payload.get("offer") or "").strip()
        if not offer:
            raise ValueError("submit action requires an offer string")

        host = str(payload.get("host") or "127.0.0.1")
        port = int(payload.get("port") or 9257)
        fee = int(payload.get("fee") or 0)

        from sage_rpc import SageRPC  # noqa: PLC0415
        sage = SageRPC(host=host, port=port)
        sage_result = sage.take_offer(offer, fee=fee)

        return {
            "success": True,
            "action": "submit",
            "transaction_id": sage_result.get("transaction_id"),
            "offer_id": sage_result.get("offer_id"),
            "quote": plan["quote"],
            "pool_before": plan["pool_before"],
            "pool_after": plan["pool_after"],
            "submission_context": {
                "type": "forge-swap-sage-taker",
                "pool_aware": True,
                "can_broadcast_live": True,
                "note": "Sage wallet takes the user offer. Pool singleton reserves require on-chain swap mode for trustless settlement.",
            },
            "raw_sage": sage_result,
        }

    return {
        "success": True,
        "action": action,
        **plan,
    }


def cli() -> int:
    parser = argparse.ArgumentParser(description="Plan or validate a deterministic Forge swap against a live fixture")
    parser.add_argument("--fixture", default=str(Path(__file__).parent / "compiled" / "testnet_lifecycle_e35984cdde6c.json"))
    parser.add_argument("--in-index", type=int, default=0)
    parser.add_argument("--out-index", type=int, default=1)
    parser.add_argument("--amount-in", type=int, default=1000)
    parser.add_argument("--min-amount-out", type=int, default=0)
    parser.add_argument("--recipient-puzzle-hash", default=ZERO_32)
    parser.add_argument("--expected-amount-out", type=int)
    parser.add_argument("--action", choices=["plan", "validate", "status"], default="plan")
    parser.add_argument("--summary-out")
    args = parser.parse_args()

    summary = run_action({
        "action": args.action,
        "fixture_path": args.fixture,
        "in_index": args.in_index,
        "out_index": args.out_index,
        "amount_in": args.amount_in,
        "min_amount_out": args.min_amount_out,
        "recipient_puzzle_hash": args.recipient_puzzle_hash,
        "expected_amount_out": args.expected_amount_out,
    })

    rendered = json.dumps(summary, indent=2)
    if args.summary_out:
        Path(args.summary_out).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


def stdin_main() -> int:
    if len(sys.argv) > 1 or sys.stdin.isatty():
        return cli()

    raw = sys.stdin.read().strip()
    if not raw:
        return cli()

    payload = json.loads(raw)
    result = run_action(payload)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(stdin_main())
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        raise