"""The wallet's spendable XCH coins, read from the chain rather than the wallet.

Every lock flow used to begin by asking the wallet for its coins over
WalletConnect. That request approves nothing and shows the user nothing — it is
a query — and it is the step that keeps failing: a session that pings, signs and
creates offers will still leave `chip0002_getAssetCoins` unanswered, and the flow
stops before it starts.

It does not need to be asked at all. A standard wallet address is the tree hash
of `p2_delegated_puzzle_or_hidden_puzzle` curried with one of the wallet's public
keys, and the wallet already shares those keys. So given the keys, Forge can
derive every address itself, ask the NODE which coins sit there, and rebuild each
coin's puzzle reveal from the key it came from. The result is exactly what the
wallet would have returned, and it is available anywhere Forge can reach a node —
a browser included, with no local wallet of any kind.

The wallet is still the only thing that can sign. That does not change: this
replaces a question, never an authorisation.

Reads one JSON object on stdin, prints one on stdout.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from chia_rs import G1Element
from chia_rs.sized_bytes import bytes32
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key

import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from multisig_tool import MultisigError, Node, network_config, record_coin, strip0x  # noqa: E402

# A wallet hands out addresses in order and rarely runs far ahead of what it has
# used, so this is generous. It is one node query however many are given.
# The site sends its first 500 keys (WalletContext COIN_KEY_WINDOW); a cap below that
# hid funds at a wallet's 488th address (the mainnet registry wallet, 2026-10-06).
MAX_KEYS = 600
# Enough coins for any spend Forge builds, newest and largest first.
MAX_COINS = 200


def coins_for_keys(node: Node, pubkeys: list[str]) -> list[dict[str, Any]]:
    """Unspent XCH coins at the addresses these keys derive, with their puzzles.

    The key a coin came from is what lets its puzzle reveal be rebuilt, so the
    mapping from address back to puzzle is kept rather than recomputed.
    """
    puzzles: dict[bytes32, Any] = {}
    for raw in pubkeys[:MAX_KEYS]:
        text = strip0x(str(raw or ""))
        if len(text) != 96:
            continue
        try:
            key = G1Element.from_bytes(bytes.fromhex(text))
        except Exception:  # noqa: BLE001 — a bad key is simply not an address
            continue
        # The wallet reports SYNTHETIC keys: the ones actually curried into the
        # standard puzzle, which is why the address it shows agrees with this.
        puzzle = puzzle_for_synthetic_public_key(key)
        puzzles[puzzle.get_tree_hash()] = puzzle

    if not puzzles:
        raise MultisigError("no usable public keys were given")

    records = node.coin_records_by_puzzle_hashes(list(puzzles))
    coins: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        if record.get("spent"):
            continue
        coin = record_coin(record)
        puzzle = puzzles.get(bytes32(coin.puzzle_hash))
        if puzzle is None:
            continue
        coins.append((int(coin.amount), {
            "coin": {
                "parent_coin_info": coin.parent_coin_info.hex(),
                "puzzle_hash": coin.puzzle_hash.hex(),
                "amount": int(coin.amount),
            },
            "puzzle": bytes(puzzle).hex(),
        }))

    # Largest first: every builder downstream wants one coin that covers the
    # whole amount, and looking at the biggest first is how it finds one.
    coins.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in coins[:MAX_COINS]]


# Lineage costs two node reads per coin (the parent's record, then its spend), so
# only the largest few of each asset are proved; a creation spends one per asset.
MAX_CATS_PER_ASSET = 5


def cat_coins_for_keys(node: Node, pubkeys: list[str], asset_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Unspent CAT coins of each asset at the wallet's addresses, with what a spend needs.

    A CAT address is the CAT layer curried with the asset id around the same
    standard inner puzzle the XCH address uses, so it is derived the same way.
    Each coin also needs a lineage proof -- its parent's parent, inner puzzle hash
    and amount -- which the parent's own spend on chain supplies. A coin whose
    parent is not a CAT of the same asset (one fresh from its TAIL) cannot be
    proved this way and is skipped; the wallet's other coins still count.
    """
    from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle, match_cat_puzzle
    from chia.wallet.uncurried_puzzle import uncurry_puzzle

    inners = []
    for raw in pubkeys[:MAX_KEYS]:
        text = strip0x(str(raw or ""))
        if len(text) != 96:
            continue
        try:
            inners.append(puzzle_for_synthetic_public_key(G1Element.from_bytes(bytes.fromhex(text))))
        except Exception:  # noqa: BLE001
            continue

    out: dict[str, list[dict[str, Any]]] = {}
    for raw_asset in asset_ids:
        asset_hex = strip0x(str(raw_asset or "")).lower()
        if len(asset_hex) != 64 or set(asset_hex) == {"0"}:
            continue
        asset = bytes32.fromhex(asset_hex)
        by_hash = {}
        for inner in inners:
            by_hash[construct_cat_puzzle(CAT_MOD, asset, inner).get_tree_hash()] = inner
        if not by_hash:
            out[asset_hex] = []
            continue
        records = [r for r in node.coin_records_by_puzzle_hashes(list(by_hash)) if not r.get("spent")]
        records.sort(key=lambda r: int(record_coin(r).amount), reverse=True)
        found: list[dict[str, Any]] = []
        for record in records[:MAX_CATS_PER_ASSET]:
            coin = record_coin(record)
            parent = node.coin_record(bytes32(coin.parent_coin_info))
            if not parent or not parent.get("spent"):
                continue
            parent_coin = record_coin(parent)
            try:
                parent_puzzle, _ = node.puzzle_and_solution(bytes32(coin.parent_coin_info), int(parent.get("spent_block_index") or 0))
            except Exception:  # noqa: BLE001 -- a parent the node cannot show cannot prove lineage
                continue
            matched = match_cat_puzzle(uncurry_puzzle(parent_puzzle))
            if matched is None:
                continue
            _, parent_tail, parent_inner = matched
            if bytes(parent_tail.as_atom()) != bytes(asset):
                continue
            found.append({
                "asset_id": asset_hex,
                "coin": {
                    "parent_coin_info": coin.parent_coin_info.hex(),
                    "puzzle_hash": coin.puzzle_hash.hex(),
                    "amount": int(coin.amount),
                },
                "inner_puzzle": bytes(by_hash[bytes32(coin.puzzle_hash)]).hex(),
                "lineage_proof": {
                    "parent_name": parent_coin.parent_coin_info.hex(),
                    "inner_puzzle_hash": parent_inner.get_tree_hash().hex(),
                    "amount": int(parent_coin.amount),
                },
            })
        out[asset_hex] = found
    return out


def main() -> int:
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
        network = str(payload.get("network") or "testnet11")
        config = network_config(network)
        node = Node(str(payload.get("node_url") or config["node_url"]))
        pubkeys = payload.get("pubkeys")
        if not isinstance(pubkeys, list) or not pubkeys:
            raise MultisigError("pubkeys must be a non-empty list")
        coins = coins_for_keys(node, [str(key) for key in pubkeys])
        asset_ids = payload.get("asset_ids") if isinstance(payload.get("asset_ids"), list) else []
        cats = cat_coins_for_keys(node, [str(key) for key in pubkeys], [str(a) for a in asset_ids]) if asset_ids else {}
        print(json.dumps({
            "success": True,
            "network": network,
            "addresses": len(pubkeys[:MAX_KEYS]),
            "coins": coins,
            "balance": sum(entry["coin"]["amount"] for entry in coins),
            # Only when asked for: {asset_id: [{asset_id, coin, inner_puzzle, lineage_proof}]}
            **({"cats": cats} if asset_ids else {}),
        }))
        return 0
    except MultisigError as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001 — one JSON line either way
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
