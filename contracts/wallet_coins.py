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
from concurrent.futures import ThreadPoolExecutor
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
# CAT addresses asked per get_coin_records_by_puzzle_hashes call, every asset's together
# (wallet_holdings.BATCH), and parents per get_coin_records_by_names call.
ADDRESS_BATCH = 1000
PARENT_BATCH = 100
# Parent spends fetched side by side: get_puzzle_and_solution has no batch form, and one at a
# time from the router's host is ~1-1.5 s a spend (vault_tool.SCAN_WORKERS).
SPEND_WORKERS = 8
# A combine (contracts/coin_combine.py) spends every coin of one asset, so it asks for all of
# them, up to what one combine takes. Their lineage is proved cheaply (all_coins below).
MAX_CATS_ALL = 300


def cat_coins_for_keys(node: Node, pubkeys: list[str], asset_ids: list[str],
                       all_coins: bool = False, hidden: dict | None = None) -> dict[str, list[dict[str, Any]]]:
    """Unspent CAT coins of each asset at the wallet's addresses, with what a spend needs.

    A CAT address is the CAT layer curried with the asset id around the same
    standard inner puzzle the XCH address uses, so it is derived the same way.
    Each coin also needs a lineage proof -- its parent's parent, inner puzzle hash
    and amount -- which the parent's own spend on chain supplies. A coin whose
    parent is not a CAT of the same asset (one fresh from its TAIL) cannot be
    proved this way and is skipped; the wallet's other coins still count.

    `all_coins` (a combine, before an offer of many coins; mainnet 2026-10-23, 196 LOVE coins):
    every coin, up to MAX_CATS_ALL, read from the chain so the website never asks the wallet
    for them over WalletConnect (a burst of those jams its relay). Two reads per coin would be
    ~400 from the router's host, so the parents' records come 100 to a call, and a parent's
    inner puzzle hash is read off its puzzle hash wherever that is one the asset is known to
    sit at -- one of this wallet's own addresses (change, a self-send) or the offer settlement
    puzzle (every swap's payout) -- a tree-hash match, the same proof the spend would give.
    Only any other parent's spend is fetched.

    `hidden` (V17, rCAT / CHIP-0038): asset id -> hidden puzzle hash H. Such an asset's coins sit
    at CAT(asset, rev(H, inner)) -- the revocation layer between the CAT layer and the wallet's own
    puzzle -- and are looked for there only; each comes back with `hidden_puzzle_hash` and the
    wallet's own `inner_puzzle` (the layer curries only its hash, so this is the one place it is
    known). Assets not named are plain, as before.
    """
    from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle, match_cat_puzzle
    from chia.types.blockchain_format.program import Program
    from chia.wallet.trading.offer import OFFER_MOD_HASH
    from chia.wallet.uncurried_puzzle import uncurry_puzzle

    from chia.wallet.vc_wallet.vc_drivers import REVOCATION_LAYER, REVOCATION_LAYER_HASH

    hidden_by_asset: dict[str, bytes32] = {}
    for raw_asset, raw_h in (hidden or {}).items():
        a, h = strip0x(str(raw_asset or "")).lower(), strip0x(str(raw_h or "")).lower()
        if len(a) == 64 and len(h) == 64 and set(h) != {"0"}:
            hidden_by_asset[a] = bytes32.fromhex(h)

    def under(asset_hex: str, inner_hash: bytes32) -> bytes32:
        """The puzzle hash under the CAT layer: the inner itself, or rev(H, inner) for an rCAT."""
        h = hidden_by_asset.get(asset_hex)
        if h is None:
            return inner_hash
        return REVOCATION_LAYER.curry(REVOCATION_LAYER_HASH, bytes(h), bytes32(inner_hash)).get_tree_hash()

    def cat_ph(asset_hex: str, asset, inner_hash: bytes32) -> bytes32:
        u = under(asset_hex, inner_hash)
        return construct_cat_puzzle(CAT_MOD, asset, Program.to(u)).get_tree_hash_precalc(u)

    inners = []
    for raw in pubkeys[:MAX_KEYS]:
        text = strip0x(str(raw or ""))
        if len(text) != 96:
            continue
        try:
            inners.append(puzzle_for_synthetic_public_key(G1Element.from_bytes(bytes.fromhex(text))))
        except Exception:  # noqa: BLE001
            continue

    assets: list[tuple[str, bytes32, dict]] = []
    for raw_asset in asset_ids:
        asset_hex = strip0x(str(raw_asset or "")).lower()
        if len(asset_hex) != 64 or set(asset_hex) == {"0"}:
            continue
        asset = bytes32.fromhex(asset_hex)
        by_hash = {}
        for inner in inners:
            by_hash[cat_ph(asset_hex, asset, inner.get_tree_hash())] = inner
        assets.append((asset_hex, asset, by_hash))

    # Every asset's addresses in one read (ADDRESS_BATCH to a call), not one read per asset.
    owner = {ph: index for index, (_, _, by_hash) in enumerate(assets) for ph in by_hash}
    held: list[list[dict]] = [[] for _ in assets]
    hashes = list(owner)
    for begin in range(0, len(hashes), ADDRESS_BATCH):
        for record in node.coin_records_by_puzzle_hashes(hashes[begin:begin + ADDRESS_BATCH]):
            index = owner.get(bytes32(record_coin(record).puzzle_hash))
            if index is not None and not record.get("spent"):
                held[index].append(record)

    out: dict[str, list[dict[str, Any]]] = {}
    wanted: list[tuple[int, list[dict]]] = []
    for index, (asset_hex, _, by_hash) in enumerate(assets):
        if not by_hash:
            out[asset_hex] = []
            continue
        records = held[index]
        # Largest first; equal amounts by coin id, so which few are proved never hangs on the node's order.
        records.sort(key=lambda r: (-int(record_coin(r).amount), bytes(record_coin(r).name())))
        wanted.append((index, records[:MAX_CATS_ALL] if all_coins else records[:MAX_CATS_PER_ASSET]))

    # Each kept coin's lineage comes from its parent. The parents' records are read 100 to a call
    # for every asset at once (all_coins, mainnet 2026-10-23: 196 LOVE coins, two reads each
    # would be ~400 from the router's host). A parent's inner puzzle hash is read off its puzzle
    # hash wherever that is one the asset is known to sit at -- one of this wallet's own addresses
    # (change, a self-send) or the offer settlement puzzle (every swap's payout) -- a tree-hash
    # match, the same proof the spend would give. Only any other parent's spend is fetched, and
    # those side by side.
    parents = _parent_records(node, [bytes32(record_coin(r).parent_coin_info) for _, records in wanted for r in records])
    known: list[dict] = []
    spends: list[tuple[bytes32, int]] = []
    for index, records in wanted:
        asset_hex_k, asset, by_hash = assets[index]
        # a lineage names the puzzle UNDER the CAT layer: for an rCAT that is the revocation layer
        known_inner = {cat_ph(asset_hex_k, asset, inner.get_tree_hash()): under(asset_hex_k, inner.get_tree_hash())
                       for inner in by_hash.values()}
        known_inner[cat_ph(asset_hex_k, asset, bytes32(OFFER_MOD_HASH))] = under(asset_hex_k, bytes32(OFFER_MOD_HASH))
        known.append(known_inner)
        for record in records:
            parent = parents.get(bytes32(record_coin(record).parent_coin_info))
            if parent and parent.get("spent") and bytes32(record_coin(parent).puzzle_hash) not in known_inner:
                spends.append((record_coin(parent).name(), int(parent.get("spent_block_index") or 0)))
    fetched = _prefetch_spends(node, spends)

    for (index, records), known_inner in zip(wanted, known):
        asset_hex, asset, by_hash = assets[index]
        out[asset_hex] = _prove(node, asset, asset_hex, by_hash, records, parents, known_inner, fetched,
                                match_cat_puzzle, uncurry_puzzle)
        if asset_hex in hidden_by_asset:
            for entry in out[asset_hex]:
                entry["hidden_puzzle_hash"] = hidden_by_asset[asset_hex].hex()
    return out


def _parent_records(node: Node, ids: list[bytes32]) -> dict[bytes32, dict]:
    """The records of these coins, spent included, PARENT_BATCH to a call."""
    ids = list(dict.fromkeys(ids))
    parents: dict[bytes32, dict] = {}
    for start in range(0, len(ids), PARENT_BATCH):
        body = node.rpc("get_coin_records_by_names", {"names": ["0x" + i.hex() for i in ids[start:start + PARENT_BATCH]],
                                                       "include_spent_coins": True})
        for parent in body.get("coin_records") or []:
            coin = record_coin(parent)
            parents[coin.name()] = parent
    return parents


def _prefetch_spends(node: Node, wanted: list[tuple[bytes32, int]]) -> dict[tuple[str, int], dict]:
    """These spends' raw JSON, SPEND_WORKERS at a time.

    Only JSON crosses threads: chia's Program is bound to the thread that made it and panics
    when another one touches it, so every reveal is parsed on the caller's (_parent_puzzle).
    A spend that did not come back is left out, and asked again there when it is used.
    """
    keys = list(dict.fromkeys((bytes32(coin_id).hex(), int(height)) for coin_id, height in wanted))

    def fetch(key: tuple[str, int]) -> dict | None:
        try:
            return node.rpc("get_puzzle_and_solution", {"coin_id": key[0], "height": key[1]})
        except Exception:  # noqa: BLE001 -- asked again when used
            return None

    if not keys:
        return {}
    with ThreadPoolExecutor(max_workers=min(SPEND_WORKERS, len(keys))) as pool:
        return {key: body for key, body in zip(keys, pool.map(fetch, keys)) if isinstance(body, dict)}


def _parent_puzzle(node: Node, fetched: dict, coin_id: bytes32, height: int):
    """A parent's puzzle reveal: parsed from its prefetched spend, else asked of the node now."""
    from chia.types.blockchain_format.program import Program

    body = fetched.get((bytes32(coin_id).hex(), int(height)))
    spend = (body.get("coin_solution") or body.get("coin_spend")) if body else None
    if isinstance(spend, dict) and spend.get("puzzle_reveal"):
        return Program.from_bytes(bytes.fromhex(strip0x(spend["puzzle_reveal"])))
    return node.puzzle_and_solution(bytes32(coin_id), int(height))[0]


def _prove(node: Node, asset, asset_hex: str, by_hash: dict, records: list[dict], parents: dict, known_inner: dict,
           fetched: dict, match_cat_puzzle, uncurry_puzzle) -> list[dict[str, Any]]:
    """Every record whose parent proves its lineage, in order. A parent that is missing, unspent,
    or not a CAT of this asset (a coin fresh from its TAIL) proves nothing; that coin is skipped."""
    found: list[dict[str, Any]] = []
    for record in records:
        coin = record_coin(record)
        parent = parents.get(bytes32(coin.parent_coin_info))
        if not parent or not parent.get("spent"):
            continue
        parent_coin = record_coin(parent)
        inner_hash = known_inner.get(bytes32(parent_coin.puzzle_hash))
        if inner_hash is None:
            try:
                parent_puzzle = _parent_puzzle(node, fetched, bytes32(coin.parent_coin_info), int(parent.get("spent_block_index") or 0))
            except Exception:  # noqa: BLE001 -- a parent the node cannot show cannot prove lineage
                continue
            matched = match_cat_puzzle(uncurry_puzzle(parent_puzzle))
            if matched is None:
                continue
            _, parent_tail, parent_inner = matched
            if bytes(parent_tail.as_atom()) != bytes(asset):
                continue
            inner_hash = parent_inner.get_tree_hash()
        found.append({
            "asset_id": asset_hex,
            "coin": {"parent_coin_info": coin.parent_coin_info.hex(), "puzzle_hash": coin.puzzle_hash.hex(), "amount": int(coin.amount)},
            "inner_puzzle": bytes(by_hash[bytes32(coin.puzzle_hash)]).hex(),
            "lineage_proof": {"parent_name": parent_coin.parent_coin_info.hex(), "inner_puzzle_hash": bytes32(inner_hash).hex(),
                              "amount": int(parent_coin.amount)},
        })
    return found


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
        hidden = payload.get("hidden_puzzle_hashes") if isinstance(payload.get("hidden_puzzle_hashes"), dict) else None
        cats = cat_coins_for_keys(node, [str(key) for key in pubkeys], [str(a) for a in asset_ids],
                                  all_coins=payload.get("all_cats") is True, hidden=hidden) if asset_ids else {}
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
