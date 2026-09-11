"""On-chain multisig profile: which safes a key owns or watches, kept on chain.

The record is a 1-mojo coin the user sends to their own signer address with
memos. Memos live in the spend that created the coin, so the record is
permanent history, readable by anyone from the public key alone, and updated
by sending a newer one — the newest profile coin wins. Nothing is stored by
Forge; the registry only learns about a safe when the profile names it.

Memo layout on the profile coin's CREATE_COIN (all atoms, text-safe so any
wallet displays them as memos rather than choking on nested lists):

    memo[0]  the signer puzzle hash — the standard hint, so the wallet finds the coin
    memo[1]  "forge-multisig-profile/1"
    memo[2…] one entry per safe, either
             "<name>|<m>|<label>=<pubkey>;<label>=<pubkey>…"   (full policy), or
             "<display name>|<safe puzzle hash>"               (by address; the
             safe's own manifest supplies the policy, the name here overrides)

A safe describes itself the same way: a 1-mojo **manifest** coin at the safe's
own address, memos ``[hint, "forge-multisig-safe/1", "<name>|<m>|<owners>"]``.
It is self-verifying — the policy must hash to the address it sits at — so
anyone may post it and nobody can forge one. With a manifest on chain a safe
can be observed from its address alone.

Reading scans the signer address's coins newest-first, looks only at 1-mojo
coins (the profile amount by convention), fetches the spend that created each
one, and returns the first tagged record. Building produces the unsigned
spends from the wallet's own coins and puzzle reveals; the wallet signs its
own standard puzzles as it does for any other Forge transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Callable

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend, make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import MOD as STANDARD_MOD, puzzle_hash_for_synthetic_public_key
from chia_rs import G1Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from multisig_tool import (
    MAX_CLVM_COST,
    MultisigError,
    Node,
    Safe,
    coin_from_json,
    coin_to_json,
    hex32,
    network_config,
    parse_amount,
    parse_pubkey,
    pubkey_hex,
    record_coin,
    spend_to_json,
    strip0x,
)

PROFILE_TAG = b"forge-multisig-profile/1"
MANIFEST_TAG = b"forge-multisig-safe/1"
PROFILE_AMOUNT = 1
MAX_PROFILE_SAFES = 32
MAX_NAME = 40
MAX_LABEL = 24
# How many recent 1-mojo coins at the signer address to inspect for a record.
MAX_CANDIDATES = 40

CREATE_COIN = ConditionOpcode.CREATE_COIN
RESERVE_FEE = ConditionOpcode.RESERVE_FEE
CREATE_COIN_ANNOUNCEMENT = ConditionOpcode.CREATE_COIN_ANNOUNCEMENT
ASSERT_COIN_ANNOUNCEMENT = ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT
ASSERT_MY_COIN_ID = ConditionOpcode.ASSERT_MY_COIN_ID


def clean_text(value: Any, limit: int) -> str:
    """Memo-safe text: no separators, no control characters, no stray
    surrogates (a mis-decoded byte must never poison a record), bounded.
    Emoji and any other real Unicode are kept: they travel as UTF-8."""
    text = re.sub(r"[|;=\x00-\x1f\x7f]", " ", str(value or ""))
    text = "".join(ch for ch in text if not 0xD800 <= ord(ch) <= 0xDFFF).strip()
    return text[:limit]


def signer_puzzle_hash(pubkey_hex_value: str) -> bytes32:
    return puzzle_hash_for_synthetic_public_key(parse_pubkey(pubkey_hex_value, "signer"))


# ─── Encoding ────────────────────────────────────────────────────────────────


# The composition a lock's inner puzzle uses. Absent means legacy, because that
# is what every lock minted before MIPS wrote. See contracts/mips.py.
LEGACY_FORMAT = "forge/1"
MIPS_FORMAT = "mips/1"
KNOWN_FORMATS = (LEGACY_FORMAT, MIPS_FORMAT)


def normalize_safes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise MultisigError("safes must be a list")
    if len(raw) > MAX_PROFILE_SAFES:
        raise MultisigError(f"a profile holds at most {MAX_PROFILE_SAFES} safes")
    safes: list[dict[str, Any]] = []
    seen: set[tuple[int, tuple[str, ...]]] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise MultisigError(f"safe {index + 1} must be an object")
        owners_raw = entry.get("owners")
        ph_raw = strip0x(entry.get("puzzle_hash") or entry.get("puzzleHash") or "")
        if (not isinstance(owners_raw, list) or not owners_raw) and ph_raw:
            # Address form: the safe's manifest carries the policy.
            ph = hex32(ph_raw, f"safe {index + 1} puzzle_hash")
            key = ("ph", ph.hex())
            if key in seen:
                continue
            seen.add(key)
            safes.append({"name": clean_text(entry.get("name"), MAX_NAME), "puzzle_hash": ph.hex()})
            continue
        if not isinstance(owners_raw, list) or not owners_raw:
            raise MultisigError(f"safe {index + 1} needs owners or a puzzle hash")
        owners = []
        for owner in owners_raw:
            pubkey = owner.get("pubkey") if isinstance(owner, dict) else owner
            label = owner.get("label", "") if isinstance(owner, dict) else ""
            owners.append({"label": clean_text(label, MAX_LABEL), "pubkey": pubkey_hex(parse_pubkey(pubkey))})
        try:
            m = int(entry.get("m", entry.get("threshold")))
        except (TypeError, ValueError) as exc:
            raise MultisigError(f"safe {index + 1}: threshold must be an integer") from exc
        if m < 1 or m > len(owners):
            raise MultisigError(f"safe {index + 1}: threshold must be between 1 and {len(owners)}")
        key = (m, tuple(o["pubkey"] for o in owners))
        if key in seen:
            continue  # same policy twice is one safe
        seen.add(key)
        safe: dict[str, Any] = {"name": clean_text(entry.get("name"), MAX_NAME), "m": m, "owners": owners}
        fmt = entry.get("format")
        if fmt:
            if fmt not in KNOWN_FORMATS:
                raise MultisigError(f"safe {index + 1}: unknown policy format {fmt!r}")
            safe["format"] = fmt
        safes.append(safe)
    return safes


def encode_safe(safe: dict[str, Any]) -> bytes:
    if "owners" not in safe:
        return f"{safe['name']}|{safe['puzzle_hash']}".encode("utf-8")
    owners = ";".join(f"{o['label']}={o['pubkey']}" for o in safe["owners"])
    body = f"{safe['name']}|{safe['m']}|{owners}"
    # A fourth field names the puzzle composition. Locks minted before Forge
    # adopted MIPS wrote three fields and are read back as the legacy shape, so
    # the absent field is itself the answer and nothing on chain has to change.
    fmt = safe.get("format")
    if fmt and fmt != LEGACY_FORMAT:
        body = f"{body}|{fmt}"
    return body.encode("utf-8")


def decode_safe(memo: bytes) -> dict[str, Any] | None:
    try:
        text = memo.decode("utf-8")
    except UnicodeDecodeError:
        return None
    parts = text.split("|")
    fmt = LEGACY_FORMAT
    if len(parts) == 4:
        parts, fmt = parts[:3], parts[3]
        if fmt not in KNOWN_FORMATS:
            return None
    if len(parts) == 2:
        name, ph_text = parts
        ph_text = strip0x(ph_text)
        if len(ph_text) != 64:
            return None
        try:
            bytes.fromhex(ph_text)
        except ValueError:
            return None
        return {"name": name[:MAX_NAME], "puzzle_hash": ph_text}
    if len(parts) != 3:
        return None
    name, m_text, owners_text = parts
    try:
        m = int(m_text)
    except ValueError:
        return None
    owners = []
    for chunk in owners_text.split(";"):
        if "=" not in chunk:
            return None
        label, pubkey = chunk.rsplit("=", 1)
        pubkey = strip0x(pubkey)
        if len(pubkey) != 96:
            return None
        try:
            parse_pubkey(pubkey)
        except MultisigError:
            return None
        owners.append({"label": label[:MAX_LABEL], "pubkey": pubkey})
    if not owners or m < 1 or m > len(owners):
        return None
    return {"name": name[:MAX_NAME], "m": m, "owners": owners, "format": fmt}


def profile_memos(signer_ph: bytes32, safes: list[dict[str, Any]]) -> list[bytes]:
    return [bytes(signer_ph), PROFILE_TAG] + [encode_safe(safe) for safe in safes]


def parse_memos(memos: list[bytes], tag: bytes = PROFILE_TAG) -> list[dict[str, Any]] | None:
    """The safes in a memo list, or None when it is not a record of ``tag``."""
    if len(memos) < 2 or memos[1] != tag:
        return None
    safes = []
    for memo in memos[2:]:
        decoded = decode_safe(memo)
        if decoded is not None:
            safes.append(decoded)
    return safes


def policy_puzzle_hash(safe: dict[str, Any]) -> bytes32:
    return Safe.from_json({"m": safe["m"], "pubkeys": [o["pubkey"] for o in safe["owners"]]}).puzzle_hash()


# ─── Reading ─────────────────────────────────────────────────────────────────


def memos_creating(spend_puzzle: Program, spend_solution: Program, coin: Coin) -> list[bytes] | None:
    """Memos on the CREATE_COIN that made ``coin``, or None if this spend did not.

    Runs the puzzle directly: the consensus condition parser drops memos, and
    the memos are the whole point here.
    """
    _cost, output = spend_puzzle.run_with_cost(MAX_CLVM_COST, spend_solution)
    for condition in output.as_iter():
        try:
            parts = list(condition.as_iter())
        except (ValueError, TypeError):
            continue
        if len(parts) < 3 or parts[0].atom != CREATE_COIN:
            continue
        if bytes(parts[1].atom or b"") != bytes(coin.puzzle_hash):
            continue
        if parts[2].as_int() != int(coin.amount):
            continue
        if len(parts) < 4:
            return []
        memos = []
        try:
            for atom in parts[3].as_iter():
                if atom.atom is None:
                    return []
                memos.append(bytes(atom.atom))
        except (ValueError, TypeError):
            return []
        return memos
    return None


def newest_record(node: Node, puzzle_hash: bytes32, tag: bytes, accept: Callable[[list[dict[str, Any]]], bool]) -> tuple[dict[str, Any] | None, int]:
    """Newest 1-mojo coin at ``puzzle_hash`` whose creating spend carries a
    ``tag`` record that ``accept`` approves. Returns (info, coins inspected)."""
    body = node.rpc("get_coin_records_by_puzzle_hash", {"puzzle_hash": puzzle_hash.hex(), "include_spent_coins": True})
    records = [r for r in body.get("coin_records") or [] if isinstance(r, dict)]
    candidates = [r for r in records if int((r.get("coin") or {}).get("amount", 0)) == PROFILE_AMOUNT]
    candidates.sort(key=lambda r: int(r.get("confirmed_block_index") or 0), reverse=True)

    inspected = 0
    for record in candidates[:MAX_CANDIDATES]:
        coin = record_coin(record)
        height = int(record.get("confirmed_block_index") or 0)
        if height <= 0:
            continue
        inspected += 1
        try:
            spend = node.rpc("get_puzzle_and_solution", {"coin_id": coin.parent_coin_info.hex(), "height": height})
        except MultisigError:
            continue
        payload = spend.get("coin_solution") or spend.get("coin_spend")
        if not isinstance(payload, dict) or not payload.get("puzzle_reveal") or not payload.get("solution"):
            continue
        puzzle = Program.from_bytes(bytes.fromhex(strip0x(payload["puzzle_reveal"])))
        solution = Program.from_bytes(bytes.fromhex(strip0x(payload["solution"])))
        try:
            memos = memos_creating(puzzle, solution, coin)
        except Exception:  # noqa: BLE001 — a foreign parent puzzle that will not run is just not ours
            continue
        if memos is None:
            continue
        safes = parse_memos(memos, tag)
        if safes is None or not accept(safes):
            continue
        return ({
            "coin_id": coin.name().hex(),
            "height": height,
            "timestamp": int(record.get("timestamp") or 0),
            "safes": safes,
        }, inspected)
    return (None, inspected)


def read_manifest(node: Node, network: str, puzzle_hash: bytes32) -> dict[str, Any]:
    """The safe's own description, if it has posted one. Self-verifying."""
    config = network_config(network)

    def accept(safes: list[dict[str, Any]]) -> bool:
        return len(safes) == 1 and "owners" in safes[0] and policy_puzzle_hash(safes[0]) == puzzle_hash

    found, inspected = newest_record(node, puzzle_hash, MANIFEST_TAG, accept)
    base = {"success": True, "network": network, "puzzle_hash": puzzle_hash.hex(), "address": encode_puzzle_hash(puzzle_hash, config["hrp"])}
    if found is None:
        return {**base, "found": False, "inspected": inspected}
    safe = found["safes"][0]
    return {**base, "found": True, "coin_id": found["coin_id"], "height": found["height"], "timestamp": found["timestamp"], "safe": safe}


def resolve_entries(node: Node, network: str, safes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill address-form profile entries from their manifests. The profile's own
    name, when given, is the display name; the manifest's is the fallback."""
    resolved = []
    for entry in safes:
        if "owners" in entry:
            resolved.append({**entry, "puzzle_hash": policy_puzzle_hash(entry).hex(), "resolved": True, "source": "profile"})
            continue
        target = hex32(entry["puzzle_hash"], "profile entry puzzle_hash")
        manifest = read_manifest(node, network, target)
        if not manifest.get("found"):
            # A vault's deposit address carries a pointer to its launcher, and
            # the launcher's lineage carries the policy. Lazy import: vault_tool
            # imports this module.
            try:
                import vault_tool

                launcher_id = vault_tool.find_launcher_from_deposit(node, target)
                if launcher_id is not None:
                    state = vault_tool.read_vault(node, launcher_id)
                    policy = state.policy.to_json()
                    resolved.append({
                        "name": entry.get("name") or policy["name"],
                        "manifest_name": policy["name"],
                        "m": policy["m"],
                        "owners": policy["owners"],
                        "puzzle_hash": entry["puzzle_hash"],
                        "resolved": True,
                        "source": "vault",
                        "puzzle": "vault",
                        "launcher_id": launcher_id.hex(),
                    })
                    continue
            except MultisigError:
                pass
        if manifest.get("found"):
            policy = manifest["safe"]
            resolved.append({
                "name": entry.get("name") or policy["name"],
                "manifest_name": policy["name"],
                "m": policy["m"],
                "owners": policy["owners"],
                "puzzle_hash": entry["puzzle_hash"],
                "resolved": True,
                "source": "manifest",
                "manifest_coin_id": manifest["coin_id"],
                "manifest_height": manifest["height"],
            })
        else:
            resolved.append({"name": entry.get("name") or "", "puzzle_hash": entry["puzzle_hash"], "resolved": False, "source": "manifest-missing"})
    return resolved


def read_profile(node: Node, network: str, pubkey_hex_value: str) -> dict[str, Any]:
    config = network_config(network)
    signer_ph = signer_puzzle_hash(pubkey_hex_value)
    found, inspected = newest_record(node, signer_ph, PROFILE_TAG, lambda _safes: True)
    base = {
        "success": True,
        "network": network,
        "signer": pubkey_hex(parse_pubkey(pubkey_hex_value)),
        "address": encode_puzzle_hash(signer_ph, config["hrp"]),
    }
    if found is None:
        return {**base, "found": False, "inspected": inspected, "safes": []}
    return {
        **base,
        "found": True,
        "coin_id": found["coin_id"],
        "height": found["height"],
        "timestamp": found["timestamp"],
        "safes": resolve_entries(node, network, found["safes"]),
    }


# ─── Building ────────────────────────────────────────────────────────────────


def build_publish(pubkey_hex_value: str, network: str, coins_raw: Any, safes_raw: Any, fee: int, nonce: bytes | None = None, manifest_raw: Any = None) -> dict[str, Any]:
    """The profile record: a 1-mojo coin at the signer's own address.

    With ``manifest_raw`` — a full policy — the same spend also posts that
    safe's manifest at the safe's address. That is how creating a safe becomes
    one on-chain act: the creator's wallet signs once, and the chain carries
    both the safe's description and the creator's claim to it.
    """
    network_config(network)
    signer_ph = signer_puzzle_hash(pubkey_hex_value)
    safes = normalize_safes(safes_raw)
    records: list[tuple[bytes32, list[bytes], bytes]] = [(signer_ph, profile_memos(signer_ph, safes), PROFILE_TAG)]
    manifests_raw = list(manifest_raw) if isinstance(manifest_raw, list) else ([manifest_raw] if manifest_raw is not None else [])
    manifest_infos: list[dict[str, Any]] = []
    seen_targets: set[bytes32] = set()
    for entry in manifests_raw:
        policy = normalize_safes([entry])
        if len(policy) != 1 or "owners" not in policy[0]:
            raise MultisigError("a manifest needs the full policy: name, threshold, owners")
        target = policy_puzzle_hash(policy[0])
        if target in seen_targets:
            continue
        seen_targets.add(target)
        records.append((target, [bytes(target), MANIFEST_TAG, encode_safe(policy[0])], MANIFEST_TAG))
        manifest_infos.append({"puzzle_hash": target.hex(), "address": encode_puzzle_hash(target, network_config(network)["hrp"]), "safe": policy[0]})
    result = build_records(records, coins_raw, fee, nonce)
    return {
        **result,
        "signer": pubkey_hex(parse_pubkey(pubkey_hex_value)),
        "profile_puzzle_hash": signer_ph.hex(),
        "safes": safes,
        "manifest": manifest_infos[0] if manifest_infos else None,
        "manifests": manifest_infos,
    }


def build_manifest(network: str, coins_raw: Any, safe_raw: Any, fee: int, nonce: bytes | None = None) -> dict[str, Any]:
    """The safe's manifest: a 1-mojo coin at the safe's own address. Any wallet may pay."""
    network_config(network)
    safes = normalize_safes([safe_raw])
    if len(safes) != 1 or "owners" not in safes[0]:
        raise MultisigError("a manifest needs the full policy: name, threshold, owners")
    safe = safes[0]
    target = policy_puzzle_hash(safe)
    memos = [bytes(target), MANIFEST_TAG, encode_safe(safe)]
    result = build_record(target, memos, coins_raw, fee, nonce, tag=MANIFEST_TAG)
    return {**result, "safe": safe, "puzzle_hash": target.hex(), "address": encode_puzzle_hash(target, network_config(network)["hrp"])}


def build_record(target_ph: bytes32, memos: list[bytes], coins_raw: Any, fee: int, nonce: bytes | None = None, tag: bytes = PROFILE_TAG) -> dict[str, Any]:
    return build_records([(target_ph, memos, tag)], coins_raw, fee, nonce)


def build_records(records: list[tuple[bytes32, list[bytes], bytes]], coins_raw: Any, fee: int, nonce: bytes | None = None) -> dict[str, Any]:
    """One spend creating a 1-mojo record coin per (puzzle hash, memos, tag)."""
    nonce = nonce or os.urandom(32)
    if not records:
        raise MultisigError("nothing to record")

    if not isinstance(coins_raw, list) or not coins_raw:
        raise MultisigError("the wallet reported no XCH coins to pay from")
    wallet_coins: list[tuple[Coin, Program]] = []
    for entry in coins_raw:
        if not isinstance(entry, dict):
            continue
        reveal = strip0x(entry.get("puzzle") or entry.get("puzzle_reveal") or "")
        if not reveal:
            continue
        coin = coin_from_json(entry.get("coin"))
        puzzle = Program.from_bytes(bytes.fromhex(reveal))
        if puzzle.get_tree_hash() != coin.puzzle_hash:
            raise MultisigError(f"puzzle reveal does not match coin {coin.name().hex()}")
        wallet_coins.append((coin, puzzle))
    if not wallet_coins:
        raise MultisigError("no wallet coin came with a puzzle reveal")

    needed = PROFILE_AMOUNT * len(records) + fee
    chosen: list[tuple[Coin, Program]] = []
    total = 0
    for coin, puzzle in sorted(wallet_coins, key=lambda pair: -int(pair[0].amount)):
        chosen.append((coin, puzzle))
        total += int(coin.amount)
        if total >= needed:
            break
    if total < needed:
        raise MultisigError(f"wallet coins total {total} mojos; the record needs {needed} ({len(records)} mojo plus the fee)")

    spends: list[CoinSpend] = []
    (first_coin, first_puzzle), rest = chosen[0], chosen[1:]
    conditions: list[Any] = [[CREATE_COIN, target_ph, PROFILE_AMOUNT, memos] for target_ph, memos, _tag in records]
    change = total - needed
    if change > 0:
        # Change stays at the coin's own address, which the wallet already watches.
        conditions.append([CREATE_COIN, first_coin.puzzle_hash, change, [first_coin.puzzle_hash]])
    if fee > 0:
        conditions.append([RESERVE_FEE, fee])
    conditions.append([ASSERT_MY_COIN_ID, first_coin.name()])
    conditions.append([CREATE_COIN_ANNOUNCEMENT, nonce])
    spends.append(make_spend(first_coin, first_puzzle, standard_solution(conditions)))
    announcement = bytes32(hashlib.sha256(first_coin.name() + nonce).digest())
    for coin, puzzle in rest:
        spends.append(make_spend(coin, puzzle, standard_solution([[ASSERT_MY_COIN_ID, coin.name()], [ASSERT_COIN_ANNOUNCEMENT, announcement]])))

    # Prove every record reads back exactly as written before anyone signs it.
    for target_ph, memos, tag in records:
        created = memos_creating(first_puzzle, standard_solution(conditions), Coin(first_coin.name(), target_ph, uint64(PROFILE_AMOUNT)))
        if created is None or parse_memos(created, tag) != parse_memos(memos, tag):
            raise MultisigError("built record does not round-trip; refusing to hand it out")

    return {
        "success": True,
        "target_puzzle_hashes": [target_ph.hex() for target_ph, _m, _t in records],
        "coin_spends": [spend_to_json(s) for s in spends],
        "memo_bytes": sum(len(m) for _ph, memos, _t in records for m in memos),
        "fee": fee,
        "coin_count": len(spends),
    }


def standard_solution(conditions: list[Any]) -> Program:
    """p2_delegated_puzzle_or_hidden_puzzle: (() (q . conditions) ())."""
    return Program.to([[], (1, conditions), []])


# ─── Commands ────────────────────────────────────────────────────────────────


def cmd_read(payload: dict[str, Any], node_factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    node = node_factory(str(payload.get("node_url") or network_config(network)["node_url"]))
    return read_profile(node, network, str(payload.get("pubkey") or ""))


def cmd_build(payload: dict[str, Any], _node_factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    fee = parse_amount(payload.get("fee", 0), "fee")
    nonce_raw = strip0x(payload.get("nonce"))
    return build_publish(
        str(payload.get("pubkey") or ""),
        network,
        payload.get("coins"),
        payload.get("safes"),
        fee,
        bytes.fromhex(nonce_raw) if nonce_raw else None,
        payload.get("manifests") if payload.get("manifests") is not None else payload.get("manifest"),
    )


MAX_KEY_LOOKUP_SPENDS = 20


def key_for_address(
    node: Node, network: str, puzzle_hash: bytes32, known: str | None = None
) -> dict[str, Any]:
    """The synthetic public key behind a standard address.

    An address is a hash, so nothing can be computed from it; but the first
    time it spends, the puzzle reveal is on chain and carries the key. A
    profile publish is such a spend, so anyone who used the tab is findable.

    ``known`` is a key Forge has already seen for this address -- from a lock's
    owner list, from a signer lookup, from an earlier resolution. It is checked
    before the chain, and **checking costs nothing to trust**: an address IS the
    hash of the standard puzzle curried with its key, so a candidate either
    hashes to this address or it does not. A wrong or stale entry cannot slip
    through, which is what makes a remembered key as good as a chain read and
    lets an address be resolved before it has ever spent.
    """
    config = network_config(network)
    base_known = {"success": True, "network": network, "puzzle_hash": puzzle_hash.hex(),
                  "address": encode_puzzle_hash(puzzle_hash, config["hrp"])}
    candidate = strip0x(known or "")
    if candidate:
        try:
            key = G1Element.from_bytes(bytes.fromhex(candidate))
            if puzzle_hash_for_synthetic_public_key(key) == puzzle_hash:
                return {**base_known, "found": True, "public_key": pubkey_hex(key), "source": "known"}
        except Exception:  # noqa: BLE001 — a bad entry is simply not an answer
            pass
    body = node.rpc("get_coin_records_by_puzzle_hash", {"puzzle_hash": puzzle_hash.hex(), "include_spent_coins": True})
    records = [r for r in body.get("coin_records") or [] if isinstance(r, dict) and r.get("spent")]
    records.sort(key=lambda r: int(r.get("spent_block_index") or 0), reverse=True)
    base = {"success": True, "network": network, "puzzle_hash": puzzle_hash.hex(), "address": encode_puzzle_hash(puzzle_hash, config["hrp"])}
    inspected = 0
    for record in records[:MAX_KEY_LOOKUP_SPENDS]:
        coin = record_coin(record)
        height = int(record.get("spent_block_index") or 0)
        if height <= 0:
            continue
        inspected += 1
        try:
            spend = node.rpc("get_puzzle_and_solution", {"coin_id": coin.name().hex(), "height": height})
        except MultisigError:
            continue
        payload = spend.get("coin_solution") or spend.get("coin_spend")
        if not isinstance(payload, dict) or not payload.get("puzzle_reveal"):
            continue
        puzzle = Program.from_bytes(bytes.fromhex(strip0x(payload["puzzle_reveal"])))
        mod, args = puzzle.uncurry()
        if mod != STANDARD_MOD:
            continue
        try:
            key = G1Element.from_bytes(bytes(args.first().as_atom()))
        except Exception:  # noqa: BLE001 — not a key-shaped argument
            continue
        if puzzle_hash_for_synthetic_public_key(key) != puzzle_hash:
            continue
        return {**base, "found": True, "public_key": pubkey_hex(key), "source": "chain", "spent_height": height, "coin_id": coin.name().hex()}
    return {**base, "found": False, "inspected": inspected, "spent_coins": len(records)}


def cmd_key_for_address(payload: dict[str, Any], node_factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    node = node_factory(str(payload.get("node_url") or network_config(network)["node_url"]))
    return key_for_address(node, network, parse_target(payload, network), payload.get("known_public_key"))


def parse_target(payload: dict[str, Any], network: str) -> bytes32:
    address = str(payload.get("address") or "").strip().lower()
    if address:
        expected = f"{network_config(network)['hrp']}1"
        if not address.startswith(expected):
            raise MultisigError(f"address must start with {expected}")
        try:
            return decode_puzzle_hash(address)
        except Exception as exc:
            raise MultisigError(f"invalid address ({exc})") from exc
    return hex32(payload.get("puzzle_hash"), "puzzle_hash")


def cmd_manifest_read(payload: dict[str, Any], node_factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    node = node_factory(str(payload.get("node_url") or network_config(network)["node_url"]))
    return read_manifest(node, network, parse_target(payload, network))


def cmd_manifest_build(payload: dict[str, Any], _node_factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    fee = parse_amount(payload.get("fee", 0), "fee")
    nonce_raw = strip0x(payload.get("nonce"))
    return build_manifest(network, payload.get("coins"), payload.get("safe"), fee, bytes.fromhex(nonce_raw) if nonce_raw else None)


COMMANDS = {
    "read": cmd_read,
    "build-publish": cmd_build,
    "manifest-read": cmd_manifest_read,
    "manifest-build": cmd_manifest_build,
    "key-for-address": cmd_key_for_address,
}


def run(command: str, payload: dict[str, Any], node_factory: Callable[[str], Node] = Node) -> dict[str, Any]:
    handler = COMMANDS.get(command)
    if handler is None:
        raise MultisigError(f"unknown command {command!r}")
    return handler(payload, node_factory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args()
    # UTF-8 on both pipes whatever the console code page is: labels and names
    # may carry emoji, and JSON output must survive the trip back unchanged.
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="strict")
        except (AttributeError, ValueError):
            pass
    raw = sys.stdin.read().strip()
    try:
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise MultisigError("stdin payload must be a JSON object")
        result = run(args.command, payload)
    except MultisigError as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
