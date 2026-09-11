#!/usr/bin/env python3
"""stdin entry point for Forge V6 N-asset transitions.

Separate from forge_stdin so the proven V4/V5 lane keeps its own surface. The
responder picks between them on the snapshot's protocol_version. Both will fold
together once V6 has real mileage.

stdin:  JSON {action, offer, pool}
stdout: JSON {success, action, transaction_id, bundle, pool, lp_delta}
exit:   0 success, 1 failure
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from chia.wallet.trading.offer import Offer

from chia.util.bech32m import decode_puzzle_hash
from chia_rs.sized_bytes import bytes32

from forge_offer import MODE_ADD, MODE_REMOVE, MODE_SWAP
from forge_stdin import _pool, _pool_json
from forge_math import DepositTooSmall
from forge_transition import build_transition

MODES = {"add": MODE_ADD, "swap": MODE_SWAP, "remove": MODE_REMOVE}

# The pool revisions `build_transition` can drive. Named rather than inline so
# the next version bump has one obvious place to land.
SUPPORTED_VERSIONS = (6, 7, 8, 9, 10)


def _recipient(value: Any) -> bytes32 | None:
    """Accept either a raw puzzle hash or a bech32m address."""
    if not value:
        return None
    text = str(value).strip()
    if text.startswith(("xch1", "txch1")):
        return decode_puzzle_hash(text)
    return bytes32.fromhex(text.removeprefix("0x"))


def build(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", "")).lower()
    if action not in MODES:
        raise ValueError(f"V6 action must be one of {sorted(MODES)}, got {action!r}")

    pool = _pool(payload["pool"])
    # Mirrors the range `build_transition` actually accepts, which is the only
    # thing this wrapper does with the pool. The two drifted apart when V9 and
    # V10 shipped: the builder was widened and this gate was not, so add and
    # remove liquidity failed on every current pool with a version error, while
    # swaps -- which reach the puzzle through a different entry point -- kept
    # working. Keep this list and the one in `build_transition` in step.
    version = int(pool.config[0])
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(
            "forge_stdin_nasset handles protocol versions "
            f"{', '.join(str(v) for v in sorted(SUPPORTED_VERSIONS))}; got V{version}")

    # Swap-only, and always optional: an absent dev_fee block simply means no
    # fee is collected, never a build failure.
    dev_fee = payload.get("dev_fee") or {}
    dev_fee_puzzle_hash = _recipient(dev_fee.get("puzzle_hash") or dev_fee.get("recipient"))
    dev_fee_bps = int(dev_fee.get("bps") or 0)

    result = build_transition(
        pool,
        Offer.from_bech32(str(payload["offer"])),
        MODES[action],
        dev_fee_puzzle_hash=dev_fee_puzzle_hash,
        dev_fee_bps=dev_fee_bps,
    )
    return {
        "success": True,
        "action": action,
        "transaction_id": result.bundle.name().hex(),
        "bundle": result.bundle.to_json_dict(),
        "pool": _pool_json(result.pool),
        "lp_delta": str(result.lp_delta),
        "dev_fee_collected": str(result.dev_fee_collected),
    }


def main() -> int:
    try:
        print(json.dumps(build(json.loads(sys.stdin.read() or "{}"))))
        return 0
    except DepositTooSmall as exc:
        # Actionable for a trader, so surface it verbatim rather than as a
        # generic builder failure.
        print(json.dumps({"success": False, "error": str(exc), "code": "DEPOSIT_TOO_SMALL"}))
        return 1
    except Exception as exc:
        # str() on some exceptions (notably KeyError) renders only the payload,
        # so "<bytes32: ...>" reached the UI with no hint it was a missing key.
        # Always name the type, and keep a traceback for the server log.
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
