#!/usr/bin/env python3
"""Rebuild the lock board (the router's multisig-index.json) from the chain.

The board is the one piece of a router's state a pool recovery (forge_recover.py) cannot give
back: which locks the site lists, their names, owners and thresholds, and the key directory. The
locks themselves are Chia vaults on chain, and three things on chain name them:

  * a lock's launcher carries its policy (name, threshold, owners with labels and keys), and
    every re-key writes the new policy into the recreated singleton's memos (vault_tool.read_vault
    walks them all to the tip);
  * a lock's deposit address holds a 1-mojo pointer naming the launcher
    (vault_tool.find_launcher_from_deposit), so an address alone finds the lock;
  * an owner's profile coin -- a 1-mojo coin at their own address whose memos list their locks
    (multisig_profile.read_profile) -- names locks the owner keeps;
  * since owner hints, every lock leaves a membership coin at each owner's address
    (vault_tool.OWNER_TAG, read by vault_tool.locks_for_owner), so any one owner's address
    finds the lock with nothing else pointing at it.

So this crawls: seed addresses (the registry's treasury and protocol recipients and every pool's
DAO recipient from a recovered record, plus any given on the command line) -> locks found at
those addresses -> their owners' keys -> those owners' profiles -> more locks, until nothing new
turns up. A plain address is turned into a key from its own spends (multisig_profile
.key_for_address), so its profile can be read too.

Every lock in the output was read from its launcher to its tip; nothing is taken from a profile's
say-so. What the chain cannot give back: proposals still collecting signatures (each is signed
again), the operator's public/hidden flags (recovered locks are listed for their owners only,
the default), and display names an operator changed off chain.

usage:
  python contracts/forge_recover_locks.py --network mainnet [--record v16-mainnet.json]
         [--address xch1...]... [--pubkey <hex>]... [--launcher <hex>]... [--out multisig-index.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_hash_for_synthetic_public_key  # noqa: E402
from chia_rs import G1Element  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

import multisig_profile as profile  # noqa: E402
import vault_tool  # noqa: E402
from multisig_tool import MultisigError, Node, network_config  # noqa: E402

MAX_ITEMS = 2_000


def _ph(value: str) -> bytes32:
    text = value.strip()
    if text.startswith(("xch1", "txch1")):
        return bytes32(decode_puzzle_hash(text))
    return bytes32.fromhex(text.removeprefix("0x"))


def seeds_from_record(record: dict) -> list[tuple[bytes32, str]]:
    """Every address a recovered registry record names that a lock may sit at."""
    out: list[tuple[bytes32, str]] = []
    reg = record.get("registry") or {}
    for key, why in (("treasury_ph", "registry treasury"), ("protocol_ph", "registry protocol recipient")):
        if reg.get(key):
            out.append((bytes32.fromhex(reg[key]), why))
    for pool in record.get("pools") or []:
        label = pool.get("label") or pool.get("launcher_id", "")[:12]
        for key, why in (("dao_ph", "DAO recipient"), ("protocol_ph", "protocol recipient"), ("lp_recipient_ph", "genesis LP recipient")):
            value = pool.get(key)
            # Not the zero hash, and not the burn address V16 sends the genesis LP floor to: no key holds it.
            if value and value not in ("00" * 32, "00" * 30 + "dead"):
                out.append((bytes32.fromhex(value), f"{why} of pool {label}"))
    return out


def crawl(node: Node, network: str, addresses: list[tuple[bytes32, str]], pubkeys: list[str], launchers: list[bytes32],
          progress=None) -> dict[str, Any]:
    hrp = network_config(network)["hrp"]
    locks: dict[str, dict] = {}          # launcher hex -> vault state json + how it was found
    keys: dict[str, dict] = {}           # owner puzzle hash -> {pubkey, address}
    profiles: dict[str, dict] = {}       # pubkey -> profile summary
    seen_ph: set[bytes32] = set()
    seen_pk: set[str] = set()
    queue: list[tuple[str, Any, str]] = ([("ph", ph, why) for ph, why in addresses] + [("pk", pk, "given") for pk in pubkeys]
                                         + [("launcher", lid, "given") for lid in launchers])
    misses: list[dict] = []
    steps = 0

    def add_lock(launcher_id: bytes32, why: str) -> None:
        if launcher_id.hex() in locks:
            locks[launcher_id.hex()]["found_by"].append(why)
            return
        state = vault_tool.read_vault(node, launcher_id)
        locks[launcher_id.hex()] = {"state": state.to_json(network), "found_by": [why]}
        if progress:
            progress(f"lock {state.policy.name!r} {launcher_id.hex()[:12]} ({state.policy.m} of {len(state.policy.owners)}) via {why}")
        seen_ph.add(vault_tool.deposit_puzzle_hash(launcher_id))
        for label, key in state.policy.owners:
            queue.append(("pk", bytes(key).hex(), f"owner {label!r} of {state.policy.name!r}"))

    while queue:
        steps += 1
        if steps > MAX_ITEMS:
            raise MultisigError("the crawl did not settle; refusing to walk further")
        kind, value, why = queue.pop(0)
        try:
            if kind == "launcher":
                add_lock(value, why)
            elif kind == "ph":
                if value in seen_ph:
                    continue
                seen_ph.add(value)
                launcher_id = vault_tool.find_launcher_from_deposit(node, value)
                if launcher_id is not None:
                    add_lock(launcher_id, why)
                    continue
                found = profile.key_for_address(node, network, value)
                if found.get("found"):
                    queue.append(("pk", found["public_key"], f"key of {why}"))
                else:
                    misses.append({"address": encode_puzzle_hash(value, hrp), "why": why, "result": "no lock pointer, no key on chain"})
            elif kind == "pk":
                pk = str(value).removeprefix("0x").lower()
                if pk in seen_pk:
                    continue
                seen_pk.add(pk)
                owner_ph = puzzle_hash_for_synthetic_public_key(G1Element.from_bytes(bytes.fromhex(pk)))
                keys[owner_ph.hex()] = {"pubkey": pk, "address": encode_puzzle_hash(owner_ph, hrp)}
                # Membership coins at the owner's own address (vault_tool.OWNER_TAG): every lock
                # launched or re-keyed since owner hints, whether or not anything else names it.
                for member in vault_tool.locks_for_owner(node, owner_ph):
                    if member.get("verified"):
                        queue.append(("launcher", bytes32.fromhex(member["launcher_id"]), f"membership coin at {encode_puzzle_hash(owner_ph, hrp)[:16]}"))
                read = profile.read_profile(node, network, pk)
                if not read.get("found"):
                    continue
                profiles[pk] = {"address": read["address"], "coin_id": read["coin_id"], "height": read["height"], "entries": len(read["safes"])}
                for entry in read["safes"]:
                    if entry.get("launcher_id"):
                        queue.append(("launcher", bytes32.fromhex(entry["launcher_id"]), f"profile of {read['address'][:16]}"))
                    elif entry.get("puzzle_hash"):
                        queue.append(("ph", bytes32.fromhex(entry["puzzle_hash"]), f"profile of {read['address'][:16]}"))
        except MultisigError as exc:
            misses.append({"kind": kind, "value": value.hex() if isinstance(value, bytes) else str(value), "why": why, "error": str(exc)})
    return {"locks": locks, "keys": keys, "profiles": profiles, "misses": misses}


def board(network: str, found: dict) -> dict[str, Any]:
    """The crawl as multisig-index.json, in the shape api/_multisigIndex.js normalizeSafe keeps."""
    hrp = network_config(network)["hrp"]
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    safes = {}
    for launcher, lock in found["locks"].items():
        st = lock["state"]
        policy = st["policy"]
        deposit_ph = st["deposit_puzzle_hash"]
        launch = next((h for h in st["history"] if h.get("event") == "launch"), {})
        last_policy = [h for h in st["history"] if "policy" in h][-1]
        safe_id = "safe-" + hashlib.sha256(bytes.fromhex(launcher)).hexdigest()[:16]
        owners = []
        for owner in policy["owners"]:
            owner_ph = puzzle_hash_for_synthetic_public_key(G1Element.from_bytes(bytes.fromhex(owner["pubkey"])))
            owners.append({"pubkey": owner["pubkey"], "label": owner["label"], "address": encode_puzzle_hash(owner_ph, hrp)})
        safes[safe_id] = {
            "id": safe_id, "name": policy["name"], "network": network, "threshold": int(policy["m"]), "owners": owners,
            "puzzleHash": deposit_ph, "address": encode_puzzle_hash(bytes32.fromhex(deposit_ph), hrp), "puzzle": "vault",
            "launcherId": launcher, "policyFormat": "mips/1" if policy.get("format") == "mips/1" else "forge/1",
            "custodyHash": st.get("custody_hash"), "launch": {"confirmed": True},
            "policyHeight": int(last_policy.get("height") or launch.get("height") or 0),
            "hidden": False, "public": False, "createdAt": now, "updatedAt": now,
        }
    keys = {ph: {"pubkey": k["pubkey"], "address": k["address"], "source": "chain", "seenAt": now} for ph, k in found["keys"].items()}
    return {"safes": safes, "proposals": {}, "keys": keys}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--network", default=os.environ.get("FORGE_NETWORK", "mainnet"), choices=["mainnet", "testnet11"])
    ap.add_argument("--node", default=None)
    ap.add_argument("--record", default=None, help="a recovered registry record (forge_recover.py --out): its addresses seed the crawl")
    ap.add_argument("--address", action="append", default=[], help="an address or puzzle hash to start from (repeatable)")
    ap.add_argument("--pubkey", action="append", default=[], help="an owner's public key to start from (repeatable)")
    ap.add_argument("--launcher", action="append", default=[], help="a lock's launcher id (repeatable)")
    ap.add_argument("--out", default=None, help="write the board here (multisig-index.json shape)")
    ap.add_argument("--report", default=None, help="write the crawl (locks with history, profiles, misses) here")
    args = ap.parse_args()
    node = Node(args.node or os.environ.get("FORGE_NODE_URL") or network_config(args.network)["node_url"])
    addresses = [(_ph(a), "given") for a in args.address]
    if args.record:
        addresses += seeds_from_record(json.loads(Path(args.record).read_text(encoding="utf-8")))
    found = crawl(node, args.network, addresses, args.pubkey, [bytes32.fromhex(x.removeprefix("0x")) for x in args.launcher],
                  progress=lambda m: print(m, file=sys.stderr))
    result = board(args.network, found)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    if args.report:
        Path(args.report).write_text(json.dumps(found, indent=1, default=str), encoding="utf-8")
    print(json.dumps({"success": True, "network": args.network, "locks": len(result["safes"]), "keys": len(result["keys"]),
                      "profiles": len(found["profiles"]),
                      "names": sorted(s["name"] for s in result["safes"].values()), "misses": found["misses"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
