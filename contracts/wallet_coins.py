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
MAX_KEYS = 400
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
        print(json.dumps({
            "success": True,
            "network": network,
            "addresses": len(pubkeys[:MAX_KEYS]),
            "coins": coins,
            "balance": sum(entry["coin"]["amount"] for entry in coins),
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
