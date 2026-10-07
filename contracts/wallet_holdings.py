"""The wallet's balances -- XCH and every CAT asked about -- read from the chain.

The website used to ask Sage for each asset's balance over WalletConnect: about
55 `chip0002_getAssetBalance` requests after every connect and every settled
trade, one per pool asset and LP token. Sage answers WalletConnect requests one at
a time and stores each, so the next swap's offer request queued behind them -- the
popup took one to four minutes (2026-10-03) -- and Sage's own store churned until
its interface ran out of memory.

None of those need the wallet. An address is the standard puzzle curried with one
of the wallet's public keys (wallet_coins.py), and a CAT address is the CAT layer
curried with the asset id around that same puzzle. Both are hashes, so every
(asset, key) address is computed here without building a puzzle, and the node is
asked what sits there in a few batched calls. Balances only: no puzzle reveals,
no lineage -- a spend still gets those from wallet_coins.py.

stdin:  {network, pubkeys: [hex48], asset_ids: [hex32], node_url?}
stdout: {success, network, addresses, xch: {amount, coins}, cats: {asset_id: {amount, coins}}}
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from chia_rs import G1Element
from chia_rs.sized_bytes import bytes32
from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD_HASH
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key
from chia.wallet.util.curry_and_treehash import calculate_hash_of_quoted_mod_hash, curry_and_treehash, shatree_atom

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from multisig_tool import MultisigError, Node, network_config, record_coin, strip0x  # noqa: E402

# The site sends its first 500 keys (WalletContext COIN_KEY_WINDOW); a cap below that
# hid funds at a wallet's 488th address (the mainnet registry wallet, 2026-10-06).
MAX_KEYS = 600
MAX_ASSETS = 200
# Puzzle hashes per node call: well inside what a full node and coinset accept.
BATCH = 1000
# A batch the node refuses (a public node's transient 503) is asked once more after this.
RETRY_DELAY_S = 1.0

_QUOTED_CAT = calculate_hash_of_quoted_mod_hash(CAT_MOD_HASH)
_CAT_MOD_ATOM = shatree_atom(CAT_MOD_HASH)


def cat_puzzle_hash(asset: bytes32, inner_puzzle_hash: bytes32) -> bytes32:
    """construct_cat_puzzle(CAT_MOD, asset, inner).get_tree_hash(), from hashes alone."""
    return bytes32(curry_and_treehash(_QUOTED_CAT, _CAT_MOD_ATOM, shatree_atom(asset), inner_puzzle_hash))


def inner_hashes(pubkeys: list[str]) -> list[bytes32]:
    out: list[bytes32] = []
    for raw in pubkeys[:MAX_KEYS]:
        text = strip0x(str(raw or ""))
        if len(text) != 96:
            continue
        try:
            # Synthetic keys, as the wallet reports them (see wallet_coins.py).
            out.append(bytes32(puzzle_for_synthetic_public_key(G1Element.from_bytes(bytes.fromhex(text))).get_tree_hash()))
        except Exception:  # noqa: BLE001 -- a bad key is simply not an address
            continue
    return out


def unspent(node: Node, puzzle_hashes: list[bytes32]) -> list[dict[str, Any]]:
    """Unspent coin records at these puzzle hashes, one batch at a time.

    250 keys x 54 assets is ~13,500 addresses, 15 batches, about 12 s. Asking four
    batches side by side made coinset answer 503 ("upstream connect error"), so the
    batches go one after another, each retried once: a public node, shared with the
    hosted router, is not something to push.
    """
    records: list[dict[str, Any]] = []
    for start in range(0, len(puzzle_hashes), BATCH):
        batch = puzzle_hashes[start:start + BATCH]
        try:
            found = node.coin_records_by_puzzle_hashes(batch)
        except MultisigError:
            time.sleep(RETRY_DELAY_S)
            found = node.coin_records_by_puzzle_hashes(batch)
        records.extend(r for r in found if not r.get("spent"))
    return records


def holdings(node: Node, pubkeys: list[str], asset_ids: list[str]) -> dict[str, Any]:
    inners = inner_hashes(pubkeys)
    if not inners:
        raise MultisigError("no usable public keys were given")

    xch_records = unspent(node, inners)
    xch = {"amount": sum(int(record_coin(r).amount) for r in xch_records), "coins": len(xch_records)}

    assets: list[bytes32] = []
    for raw in asset_ids[:MAX_ASSETS]:
        text = strip0x(str(raw or "")).lower()
        if len(text) == 64 and set(text) != {"0"}:
            assets.append(bytes32.fromhex(text))
    owner: dict[bytes32, bytes32] = {}
    for asset in dict.fromkeys(assets):
        for inner in inners:
            owner[cat_puzzle_hash(asset, inner)] = asset
    cats: dict[str, dict[str, int]] = {asset.hex(): {"amount": 0, "coins": 0} for asset in dict.fromkeys(assets)}
    for record in unspent(node, list(owner)):
        coin = record_coin(record)
        asset = owner.get(bytes32(coin.puzzle_hash))
        if asset is None:
            continue
        cats[asset.hex()]["amount"] += int(coin.amount)
        cats[asset.hex()]["coins"] += 1
    return {"addresses": len(inners), "xch": xch, "cats": cats}


# Parent lookups per discovery, at most; about one per asset is needed (see below).
MAX_PARENT_LOOKUPS = 400
# Lookups side by side: enough to matter, few enough that coinset does not 503.
PARENT_LOOKUP_WORKERS = 6
# Seconds discovery may take per request, hint read included (the route allows 60).
DISCOVERY_BUDGET_S = 45.0
# Addresses kept in the asset cache across requests.
MAX_CACHED_ADDRESSES = 50_000


def discover_cats(node: Node, inners: list[bytes32], cache_path: pathlib.Path | None = None) -> dict[str, dict[str, int]]:
    """Every CAT the wallet's addresses hold, found without knowing which to ask for.

    The balance read above can only price assets it is told about, and on mainnet
    the site knows almost none: its token list is XCH and there are no pools yet,
    so a creator's wallet showed no tokens at all (2026-10-06). A CAT sent to an
    address carries that address as its hint, so the node's hint index finds every
    one of them in one batched call; the asset id is then read from the parent
    spend (the coin's confirmation height is the height its parent was spent at),
    once per distinct CAT address, and kept only if that asset over one of these
    inner puzzles really is the coin's puzzle hash -- a hinted NFT or a payment to
    some other puzzle is not a CAT this wallet can spend.
    """
    from chia.wallet.cat_wallet.cat_utils import match_cat_puzzle
    from chia.wallet.uncurried_puzzle import uncurry_puzzle

    # The clock starts here: the hint read alone took ~30 s for a 14,000-coin wallet
    # (2026-10-06), and the route allows 60 in all.
    deadline = time.monotonic() + DISCOVERY_BUDGET_S
    own = set(inners)
    records: list[dict[str, Any]] = []
    for start in range(0, len(inners), BATCH):
        batch = ["0x" + h.hex() for h in inners[start:start + BATCH]]
        try:
            body = node.rpc("get_coin_records_by_hints", {"hints": batch, "include_spent_coins": False})
        except MultisigError:
            time.sleep(RETRY_DELAY_S)
            body = node.rpc("get_coin_records_by_hints", {"hints": batch, "include_spent_coins": False})
        if body.get("success") is False:
            raise MultisigError(f"get_coin_records_by_hints failed: {body.get('error')}")
        records.extend(r for r in body.get("coin_records") or [] if isinstance(r, dict) and not r.get("spent"))

    by_address: dict[bytes32, list[dict[str, Any]]] = {}
    for record in records:
        coin = record_coin(record)
        if bytes32(coin.puzzle_hash) in own:
            continue                                    # plain XCH at one of the wallet's own addresses
        by_address.setdefault(bytes32(coin.puzzle_hash), []).append(record)

    # Which asset each CAT address holds. A parent lookup costs a node call, and one
    # per address took 45 s for a 55-token wallet on the live router (2026-10-06),
    # close to its 60 s limit. But one coin's parent names its asset, and that
    # asset's address at every one of the wallet's inner puzzles is then arithmetic,
    # so lookups fall to about one per asset. Answers are kept across requests
    # (a puzzle hash names one asset forever), and the lookups still needed go a few
    # at a time -- never the whole batch at once, which coinset answers with 503s.
    cache = _load_asset_cache(cache_path)
    addresses_of: dict[bytes32, set[bytes32]] = {}

    def known(address: bytes32) -> bytes32 | None | bool:
        """The asset at this address if already settled, None if settled as no CAT, False if unknown."""
        for asset, addresses in addresses_of.items():
            if address in addresses:
                return asset
        cached = cache.get(address.hex())
        if cached is None:
            return False
        if cached == "":
            return None
        asset = bytes32.fromhex(cached)
        addresses_of.setdefault(asset, {cat_puzzle_hash(asset, inner) for inner in inners})
        return asset if address in addresses_of[asset] else None

    # Worker threads do the network call only and hand back plain text: chia's
    # puzzle objects are bound to the thread that made them and panic if another
    # touches them, so every Program is built and read on this thread.
    work = {address: (strip0x(str(group[0]["coin"]["parent_coin_info"])), int(group[0].get("confirmed_block_index") or 0))
            for address, group in by_address.items()}

    def fetch_reveal(address: bytes32) -> tuple[bytes32, str | None]:
        parent_id, height = work[address]
        try:
            body = node.rpc("get_puzzle_and_solution", {"coin_id": parent_id, "height": height})
        except Exception:  # noqa: BLE001 -- a parent the node cannot show names no asset (and is asked again next time)
            return address, None
        spend = body.get("coin_solution") or body.get("coin_spend") or {}
        reveal = spend.get("puzzle_reveal") if isinstance(spend, dict) else None
        return address, (strip0x(str(reveal)) if reveal else None)

    def asset_of(address: bytes32, reveal: str | None) -> bytes32 | None:
        if reveal is None:
            return None
        try:
            matched = match_cat_puzzle(uncurry_puzzle(Program.from_bytes(bytes.fromhex(reveal))))
        except Exception:  # noqa: BLE001 -- unreadable is not a CAT
            matched = None
        if matched is None:
            cache[address.hex()] = ""
            return None
        _, tail, _ = matched
        asset = bytes32(tail.as_atom())
        cache[address.hex()] = asset.hex()
        return asset

    # Likely CATs first. Singletons -- NFTs, DIDs -- are hinted to their owner too, and
    # a wallet with many holds hundreds of hinted addresses that are not CATs (the
    # owner's: 400+, 2026-10-06). A singleton is always a 1-mojo coin, so addresses
    # holding more than one mojo are asked about first and 1-mojo ones only with
    # budget to spare -- a 1-mojo CAT balance is then the one thing that can wait.
    def priority(address: bytes32) -> int:
        return 0 if sum(int(r["coin"]["amount"]) for r in by_address[address]) > 1 else 1

    classified: dict[bytes32, bytes32 | None] = {}
    pending = sorted(by_address, key=priority)
    lookups = 0
    with ThreadPoolExecutor(max_workers=PARENT_LOOKUP_WORKERS) as pool:
        # Stops at a time budget well inside the route's 60 s limit; the cache keeps
        # every answer, so the next request goes on from here.
        while pending and lookups < MAX_PARENT_LOOKUPS and time.monotonic() < deadline:
            unknown = []
            for address in pending:
                settled = known(address)
                if settled is False:
                    unknown.append(address)
                else:
                    classified[address] = settled
            if not unknown:
                pending = []
                break
            wave = unknown[:min(PARENT_LOOKUP_WORKERS, MAX_PARENT_LOOKUPS - lookups)]
            lookups += len(wave)
            for address, reveal in pool.map(fetch_reveal, wave):
                asset = asset_of(address, reveal)
                if asset is not None:
                    addresses_of.setdefault(asset, {cat_puzzle_hash(asset, inner) for inner in inners})
                classified[address] = asset if asset is not None and address in addresses_of[asset] else None
            pending = [address for address in unknown if address not in classified]
    _save_asset_cache(cache_path, cache)

    found: dict[str, dict[str, int]] = {}
    for address, asset in classified.items():
        if asset is None:
            continue
        entry = found.setdefault(asset.hex(), {"amount": 0, "coins": 0})
        for record in by_address[address]:
            entry["amount"] += int(record_coin(record).amount)
            entry["coins"] += 1
    return found


def _default_cache_path() -> pathlib.Path:
    state = os.environ.get("AWIZARD_STATE_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    base = pathlib.Path(state) if state else pathlib.Path(__file__).resolve().parent.parent / ".awizard"
    return base / "cat-address-assets.json"


def _load_asset_cache(path: pathlib.Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_asset_cache(path: pathlib.Path | None, cache: dict[str, str]) -> None:
    if path is None:
        return
    try:
        if len(cache) > MAX_CACHED_ADDRESSES:              # keep the newest; it is only a cache
            cache = dict(list(cache.items())[-MAX_CACHED_ADDRESSES:])
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass                                                # a cache that cannot be written only costs the next request time


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
        node = Node(str(payload.get("node_url") or network_config(network)["node_url"]))
        pubkeys = payload.get("pubkeys")
        if not isinstance(pubkeys, list) or not pubkeys:
            raise MultisigError("pubkeys must be a non-empty list")
        asset_ids = payload.get("asset_ids") if isinstance(payload.get("asset_ids"), list) else []
        out = holdings(node, [str(k) for k in pubkeys], [str(a) for a in asset_ids])
        if payload.get("discover") is True:
            # {asset_id: {amount, coins}} for every CAT found by hint, asked about or not.
            out["discovered"] = discover_cats(node, inner_hashes([str(k) for k in pubkeys]), _default_cache_path())
        print(json.dumps({"success": True, "network": network, **out}))
        return 0
    except MultisigError as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001 -- one JSON line either way
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
