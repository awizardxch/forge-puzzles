#!/usr/bin/env python3
"""The wallet side of a keyless pool creation, over Sage RPC.

Revision-agnostic: nothing here depends on which protocol the pool will run, only on
what the creator's wallet must hand over. The builder (forge_v14_create via
forge_stdin) never touches a key. What a creator's
wallet must do is exactly two things, and this bridge does them through the local
Sage the interface already talks to:

  pick   choose the creator's coins for the pool -- one XCH coin covering launcher,
         eve, XCH reserve, creation fee, LP backing and network fee, and one CAT coin
         per CAT asset -- with the puzzle reveals Sage will sign for, plus the wallet's
         puzzle hash as the LP recipient;
  sign   have Sage sign the creator's spends (`sign_coin_spends`), returning the
         spends as signed and the aggregate signature.

stdin:  {"action": "pick", "asset_ids": [...hex or "txch"], "reserves": [...], "total_lp", "creation_fee", "network_fee"}
        {"action": "sign", "coin_spends": [...]}
        {"action": "gate", "allowlist": ["txch1...", "<64 hex>"], "collection_id": "col1...", "address": "txch1..."}
            -- the creation gate (phase 8.9): is the router wallet on the allowlist or holding an NFT of the
               collection; `address` (the connected wallet) is checked against the allowlist too
stdout: one JSON object; `success` false with `error` on failure.
"""
from __future__ import annotations

import os
import re
import importlib.util
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia.util.bech32m import decode_puzzle_hash  # noqa: E402

def _load_deploy():
    """Load the CURRENT revision's deploy module for its Wallet and coin helpers.

    This used to name `deploy-v11-testnet.py` outright, and every revision since kept
    calling it -- so a live V14 creation picked its coins with a wallet three revisions
    old, and any fix made to the newer wallet never reached the path a creator takes.
    The bridge needs `Wallet`, `coin_to_json` and `strip`, which every revision's deploy
    script has; what it must not do is pin one.

    Highest version present wins, so cutting a revision needs no edit here.
    `FORGE_DEPLOY_SCRIPT` overrides it for driving a retired revision on purpose.
    """
    override = os.environ.get("FORGE_DEPLOY_SCRIPT")
    if override:
        path = Path(override)
        if not path.is_absolute():
            path = ROOT / path
    else:
        found = sorted(
            ((int(m.group(1)), p) for p in (ROOT / "scripts").glob("deploy-v*-testnet.py")
             if (m := re.fullmatch(r"deploy-v(\d+)-testnet\.py", p.name))),
            key=lambda t: t[0])
        if not found:
            raise SystemExit("[aWizard] no scripts/deploy-v<N>-testnet.py to load a wallet from")
        path = found[-1][1]
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for needed in ("Wallet", "coin_to_json", "strip"):
        if not hasattr(module, needed):
            raise SystemExit(f"[aWizard] {path.name} has no {needed}; this bridge needs it")
    return module


class _Deploy:
    """The deploy module, loaded on FIRST USE rather than at import.

    Importing this file must not need a deploy script. The public repository ships the
    puzzles and their suites but not the scripts that drive a wallet, so resolving the
    module at import time made `import forge_create_bridge` fail outright there -- the
    same shape as the retired lane that made `forge_stdin` unimportable. This bridge
    only ever needs a wallet when it actually runs an action.
    """

    _mod = None

    def __getattr__(self, name: str):
        if _Deploy._mod is None:
            _Deploy._mod = _load_deploy()
        return getattr(_Deploy._mod, name)


deploy = _Deploy()


def pick(payload: dict) -> dict:
    wallet = deploy.Wallet()
    ids = [None if str(a).lower() in ("txch", "xch", "0" * 64, "") else bytes32.fromhex(str(a).removeprefix("0x")) for a in payload["asset_ids"]]
    reserves = [int(x) for x in payload["reserves"]]
    total_lp = int(payload["total_lp"])
    xch_reserve = sum(r for a, r in zip(ids, reserves) if a is None)
    need = 2 + xch_reserve + int(payload["creation_fee"]) + (total_lp - 1) + int(payload.get("network_fee") or 0)
    xch_coin, xch_puzzle = wallet.xch_coin(need)
    cats = []
    for asset, reserve in zip(ids, reserves):
        if asset is None:
            continue
        coin, inner, lineage = wallet.cat_coin(asset, reserve)
        cats.append({"asset_id": asset.hex(), "coin": deploy.coin_to_json(coin), "inner_puzzle": bytes(inner).hex(),
                     "lineage_proof": {"parent_name": lineage.parent_name.hex(), "inner_puzzle_hash": lineage.inner_puzzle_hash.hex(),
                                       "amount": int(lineage.amount)}})
    return {"success": True, "creator": {"xch": {"coin": deploy.coin_to_json(xch_coin), "puzzle_reveal": bytes(xch_puzzle).hex()}, "cats": cats},
            "recipient_puzzle_hash": wallet.puzzle_hash.hex(), "wallet": wallet.key.get("name"), "need_xch": need}


def sign(payload: dict) -> dict:
    wallet = deploy.Wallet()
    spends = [{"coin": {"parent_coin_info": deploy.strip(cs["coin"]["parent_coin_info"]), "puzzle_hash": deploy.strip(cs["coin"]["puzzle_hash"]),
                        "amount": int(cs["coin"]["amount"])},
               "puzzle_reveal": deploy.strip(cs["puzzle_reveal"]), "solution": deploy.strip(cs["solution"])} for cs in payload["coin_spends"]]
    signed = wallet.sage.sign_coin_spends(spends)
    return {"success": True, "signed_creator_spends": signed["spend_bundle"]["coin_spends"],
            "aggregated_signature": deploy.strip(signed["spend_bundle"]["aggregated_signature"])}


def _allowlist(raw) -> set[str]:
    """Puzzle hashes (hex, lower) from a list or a comma/space separated string of addresses or hashes."""
    items = raw if isinstance(raw, list) else [x for x in str(raw or "").replace(",", " ").split() if x]
    out: set[str] = set()
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        if item.lower().startswith(("txch1", "xch1")):
            out.add(bytes32(decode_puzzle_hash(item)).hex())
        else:
            out.add(item.lower().removeprefix("0x"))
    return out


def gate(payload: dict) -> dict:
    """The creation gate, off chain first. Two rules, either one passes:
    an allowlist of addresses (the deployer approves creators by address), and an NFT collection.
    The router wallet is what signs a creation, so it is the one checked; the connected wallet's
    address, when the interface sends it, is checked against the allowlist as well."""
    allow = _allowlist(payload.get("allowlist"))
    collection = str(payload.get("collection_id") or "").strip()
    if not allow and not collection:
        return {"success": True, "gated": False, "holder": True, "count": 0, "collection_id": "", "allowlisted": True, "mode": "open"}
    wallet = deploy.Wallet()
    allowlisted = bool(allow) and wallet.puzzle_hash.hex() in allow
    count = 0
    if collection:
        result = wallet.sage.call("get_nfts", {"collection_id": collection, "offset": 0, "limit": 25,
                                               "sort_mode": "name", "include_hidden": False})
        nfts = result.get("nfts") if isinstance(result, dict) else result
        count = len(nfts or [])
    connected = str(payload.get("address") or "").strip()
    connected_allowed = None
    if connected and allow:
        try:
            connected_allowed = _allowlist([connected]).pop() in allow
        except Exception:  # noqa: BLE001  -- an address that does not decode is not on the list
            connected_allowed = False
    mode = "both" if (allow and collection) else ("allowlist" if allow else "collection")
    return {"success": True, "gated": True, "holder": allowlisted or count > 0, "allowlisted": allowlisted, "count": count,
            "collection_id": collection, "mode": mode, "allowlist_size": len(allow), "wallet": wallet.key.get("name"),
            "address": wallet.address, "connected_allowed": connected_allowed}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        action = str(payload.get("action", "")).lower()
        if action == "pick":
            print(json.dumps(pick(payload)))
        elif action == "sign":
            print(json.dumps(sign(payload)))
        elif action == "gate":
            print(json.dumps(gate(payload)))
        else:
            raise ValueError("action must be pick, sign or gate")
        return 0
    except SystemExit as exc:      # the wallet helpers raise SystemExit with a message
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
