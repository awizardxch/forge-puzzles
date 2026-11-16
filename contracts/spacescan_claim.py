#!/usr/bin/env python3
"""The checks behind an operator's Spacescan CAT update (api/admin/spacescan-update.js).

Spacescan's updatecat (docs.spacescan.io/api/cat/update) takes a CHIP-0002 signed
message from "the EVE coin puzzle hash or minter address of the CAT". The router
refuses to forward one it has not verified itself:

  verify  {key, message, signature}  -> {ok, reason, puzzle_hash}
          ok: the signature is `key`'s over the message as a wallet's
          chip0002_signMessage signs it (forge_fee_identity.chip0002_digest), and
          puzzle_hash is that key's standard address (Sage shares synthetic keys).
  eve-ph  {lp_asset_id}              -> {eve_puzzle_hash}
          the puzzle hash of a V16 Forge LP's eve coin: CAT2(asset, LP_MINT_INNER).
          The creation spends the creator's XCH coin into it (forge_v16_create.py),
          so the parent of the coin at this hash is the minter.

No network: the router reads the chain itself and runs only the arithmetic here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia_rs import AugSchemeMPL, G1Element, G2Element  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

from forge_fee_identity import _cat_hash, _clean, chip0002_digest, key_ph  # noqa: E402


def verify(payload: dict) -> dict:
    try:
        key = G1Element.from_bytes(bytes.fromhex(_clean(payload.get("key"))))
        sig = G2Element.from_bytes(bytes.fromhex(_clean(payload.get("signature"))))
    except Exception as exc:  # noqa: BLE001 -- a malformed key or signature is a reason
        return {"ok": False, "reason": f"malformed key or signature: {exc}", "puzzle_hash": None}
    message = str(payload.get("message") or "")
    if not AugSchemeMPL.verify(key, chip0002_digest(message), sig):
        return {"ok": False, "reason": "the signature is not this key's over the message", "puzzle_hash": None}
    return {"ok": True, "reason": None, "puzzle_hash": key_ph(_clean(payload.get("key")))}


def eve_ph(payload: dict) -> dict:
    import forge_v18_driver as drv
    asset = bytes32.fromhex(_clean(payload.get("lp_asset_id")))
    return {"eve_puzzle_hash": bytes(_cat_hash(asset, drv.LP_MINT_INNER.get_tree_hash())).hex()}


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    mode = payload.get("mode")
    if mode == "verify":
        out = verify(payload)
    elif mode == "eve-ph":
        out = eve_ph(payload)
    else:
        raise ValueError(f"unknown mode {mode!r}")
    print(json.dumps({"success": True, **out}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)
