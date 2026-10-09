#!/usr/bin/env python3
"""Check a registry record against the chain before a router keeps it, and keep it.

The router creates pools from the registry driver's record (its `registry` section:
the registry's current coin, lineage and slot list). On testnet that record shipped in
the state seed; on mainnet nothing ships, so the operator uploads the record the deploy
script wrote at launch (api/admin/registry.js). This is what stands between that upload
and the router's volume:

  - the record is for THIS network;
  - the registry rebuilt from the record's own constants (treasury, protocol recipient,
    creation fee, launcher parent, state; the scale and window must be the driver's) is
    the singleton the record names: same launcher id, its puzzle hash the recorded coin's;
  - on chain, the launcher exists and the recorded coin is the registry's current,
    unspent tip. A record that has fallen behind is refused, never kept: creating from
    it would only be refused later, after the creator had signed.

Then it is merged with what the router holds, never swapped blind: a different registry
replaces the held one only when the upload retires it (`registry --rollover`, how the dev
fee address changes for future pairs); the router's own pool records are kept and win;
retired registries accumulate. The file it replaces is copied aside first.

The record is handled as TEXT until Python parses it: it carries integers past 2**53
(price_scale is 2**64, oracle accumulators larger), which a JavaScript JSON round trip
silently rounds -- 114 of them in testnet11's record.

stdin:  {"action": "check" | "upload", "network": "mainnet", "node_url": "...",
         "record_text": "...", "held_path": "/data/v16-mainnet.json"}
        check: verifies record_text, or the held file when record_text is absent.
        upload: verifies record_text, merges it into held_path, writes held_path.
stdout: {"success": true, "summary": {...}, ...} or {"success": false, "code": ..., "error": ...}
`node_url` may be omitted to skip the chain checks (tests only; the route always sends one).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forge_v16_driver as drv  # noqa: E402
from chia.util.bech32m import encode_puzzle_hash  # noqa: E402
from forge_stdin import _lane_registry  # noqa: E402


class Refused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _rpc(node_url: str, route: str, body: dict) -> dict:
    request = urllib.request.Request(f"{node_url.rstrip('/')}/{route}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "aWizard-Forge/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _coin_record(node_url: str, coin_id: str) -> dict | None:
    return _rpc(node_url, "get_coin_record_by_name", {"name": coin_id.removeprefix("0x")}).get("coin_record")


def _hex(value: Any) -> str:
    return str(value or "").strip().lower().removeprefix("0x")


def verify(record: dict[str, Any], network: str, node_url: str | None, prefix: str) -> dict[str, Any]:
    if record.get("network") != network:
        raise Refused("WRONG_NETWORK", f"the record is for {record.get('network')!r}; this router serves {network!r}")
    registry = record.get("registry")
    if not isinstance(registry, dict) or not registry.get("coin"):
        raise Refused("NO_REGISTRY", "the record holds no registry; launch one with the deploy script first")
    for field in ("launcher_id", "launcher_parent", "treasury_ph", "protocol_ph", "creation_fee", "state", "lineage", "slots"):
        if registry.get(field) in (None, ""):
            raise Refused("INCOMPLETE", f"the registry record has no {field}")
    for field, constant in (("price_scale", drv.PRICE_SCALE), ("oracle_window", drv.ORACLE_WINDOW)):
        if registry.get(field) is not None and int(registry[field]) != constant:
            raise Refused("INCONSISTENT", f"the record's {field} is not this driver's ({registry[field]} != {constant})")

    try:
        rebuilt = _lane_registry(registry, drv)
        bare = drv.make_registry(creation_fee=int(registry["creation_fee"]), treasury_ph=rebuilt.treasury_ph,
                                 launcher_parent=rebuilt.launcher_parent, state=list(registry["state"]),
                                 protocol_ph=rebuilt.protocol_ph)
    except (ValueError, KeyError, TypeError) as exc:
        raise Refused("MALFORMED", f"the registry record does not rebuild: {exc}") from exc
    if bare.launcher_id.hex() != _hex(registry["launcher_id"]):
        raise Refused("INCONSISTENT", "the launcher id is not the one its launcher parent makes")
    recorded = rebuilt.coin
    if bare.coin.puzzle_hash != recorded.puzzle_hash:
        raise Refused("INCONSISTENT", "the recorded coin is not the registry these constants and state build "
                      "(a treasury, protocol recipient, fee or state that differs from the one launched)")

    tip = recorded.name().hex()
    if node_url:
        launcher = _coin_record(node_url, bare.launcher_id.hex())
        if not launcher:
            raise Refused("NOT_ON_CHAIN", f"no launcher {bare.launcher_id.hex()[:16]}... on chain")
        current = _coin_record(node_url, tip)
        if not current:
            raise Refused("NOT_ON_CHAIN", f"the recorded registry coin {tip[:16]}... is not on chain (yet)")
        if current.get("spent") or int(current.get("spent_block_index") or 0) > 0:
            raise Refused("STALE", "the registry has moved on since this record was written (its coin is spent); "
                          "upload the record from after the latest creation")

    return {
        "network": network,
        "launcher_id": bare.launcher_id.hex(),
        "tip": tip,
        "treasury": encode_puzzle_hash(bare.treasury_ph, prefix),
        "protocol": encode_puzzle_hash(bare.protocol_ph, prefix),
        "creation_fee": int(registry["creation_fee"]),
        "current_fee": bare.current_fee,          # V15: the fee in state, what register charges now
        "pools": len(record.get("pools") or []),
        "retired_registries": [r.get("launcher_id") for r in record.get("retired_registries") or []],
        "checked_on_chain": bool(node_url),
    }


def merge(held: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """The held record and the upload, by the rules in the module docstring."""
    held_id = _hex((held or {}).get("registry", {}).get("launcher_id"))
    incoming_id = _hex(incoming["registry"]["launcher_id"])
    if held_id and held_id != incoming_id:
        if not any(_hex(r.get("launcher_id")) == held_id for r in incoming.get("retired_registries") or []):
            raise Refused("REPLACES_REGISTRY", f"this router holds registry {held_id[:16]}..., and the upload does not "
                          "retire it. A different registry replaces it only by `registry --rollover`.")
    pools: dict[str, Any] = {}
    for pool in incoming.get("pools") or []:
        pools[_hex(pool.get("launcher_id"))] = pool
    for pool in (held or {}).get("pools") or []:
        pools[_hex(pool.get("launcher_id"))] = pool          # the router's own wins: it may be newer
    retired: dict[str, Any] = {}
    for entry in [*((held or {}).get("retired_registries") or []), *(incoming.get("retired_registries") or [])]:
        retired[_hex(entry.get("launcher_id"))] = entry
    merged = dict(incoming)
    merged["pools"] = list(pools.values())
    if retired:
        merged["retired_registries"] = list(retired.values())
    merged["log"] = [*(incoming.get("log") or []),
                     {"step": "uploaded", "at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                      "registry": incoming_id}]
    return merged


def upload(record_text: str, held_path: Path, network: str, node_url: str | None, prefix: str) -> dict[str, Any]:
    try:
        incoming = json.loads(record_text)
    except json.JSONDecodeError as exc:
        raise Refused("MALFORMED", f"the record is not JSON: {exc}") from exc
    summary = verify(incoming, network, node_url, prefix)
    held = json.loads(held_path.read_text(encoding="utf-8")) if held_path.is_file() else None
    merged = merge(held, incoming)
    held_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if held is not None:
        backup = f"{held_path.name}.before-upload-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        shutil.copyfile(held_path, held_path.parent / backup)
    temp = held_path.with_name(held_path.name + ".tmp")
    temp.write_text(json.dumps(merged, indent=1), encoding="utf-8")
    os.replace(temp, held_path)
    return {"summary": summary, "pools": len(merged["pools"]), "backup": backup}


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    network = str(payload.get("network") or "")
    prefix = "xch" if network == "mainnet" else "txch"
    node_url = payload.get("node_url") or None
    held_path = Path(payload["held_path"]) if payload.get("held_path") else None
    try:
        if payload.get("action") == "upload":
            if not held_path or not payload.get("record_text"):
                raise Refused("BAD_REQUEST", "upload needs record_text and held_path")
            out = upload(str(payload["record_text"]), held_path, network, node_url, prefix)
            print(json.dumps({"success": True, **out}))
            return 0
        if payload.get("record_text"):
            record = json.loads(str(payload["record_text"]))
        elif held_path and held_path.is_file():
            record = json.loads(held_path.read_text(encoding="utf-8"))
        else:
            print(json.dumps({"success": True, "held": False}))
            return 0
        print(json.dumps({"success": True, "held": True, "summary": verify(record, network, node_url, prefix)}))
    except Refused as exc:
        print(json.dumps({"success": False, "code": exc.code, "error": str(exc)}))
    except Exception as exc:  # noqa: BLE001 -- a node that cannot be reached is a refusal, never a pass
        print(json.dumps({"success": False, "code": "CHECK_FAILED", "error": f"{type(exc).__name__}: {exc}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
