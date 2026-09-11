#!/usr/bin/env python3
"""Pool names on chain: the genesis memo, and renames only the deployer can make.

A V11 pool's puzzles carry no label (a label inside the leaves would be a
revision), so the name lives in the chain's metadata instead, in two places:

* **genesis** -- the memo on the launcher's creation, written by the coin that
  paid for the pool (`create-pool --emoji`); immutable, like the launcher;
* **renames** -- any later coin the SAME address creates for itself, hinted with
  the pool's launcher id and carrying the new name as its second memo and the
  symbol as its third. The
  address is the launcher parent's puzzle hash: whoever paid for the launcher.
  A rename is authenticated by that address's own signature on the spend, so
  the router (which holds no key) cannot rename anything, and nobody else can
  either. The newest rename by height is the pool's name.

Resolution is a hint query (`get_coin_records_by_hint(launcher_id)`): the
pool's own singleton and reserves come back too, but they sit at other puzzle
hashes and are ignored; only coins AT the deployer's puzzle hash whose parent
also sat there count, and the memo is read from that parent's spend.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from chia.types.blockchain_format.program import Program

DEFAULT_NODE = "https://testnet11.api.coinset.org"
CREATE_COIN = 51
LAUNCHER_HASH_HEX = "eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9"


def _strip(v: Any) -> str:
    return str(v or "").removeprefix("0x")


def _rpc(node: str, route: str, body: dict) -> dict:
    req = urllib.request.Request(f"{node.rstrip('/')}/{route}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "aWizard-Forge/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _memos_of(node: str, coin_id_hex: str, spent_height: int, target_ph_hex: str, target_amount: int) -> list[bytes]:
    """The memos on the CREATE_COIN in `coin_id`'s spend that made (target_ph, target_amount)."""
    cs = _rpc(node, "get_puzzle_and_solution", {"coin_id": "0x" + coin_id_hex, "height": int(spent_height)})["coin_solution"]
    puzzle = Program.from_bytes(bytes.fromhex(_strip(cs["puzzle_reveal"])))
    solution = Program.from_bytes(bytes.fromhex(_strip(cs["solution"])))
    for cond in puzzle.run(solution).as_iter():
        items = list(cond.as_iter())
        if items[0].atom is None or items[0].as_int() != CREATE_COIN or len(items) < 4:
            continue
        if items[1].as_atom().hex() == target_ph_hex and items[2].as_int() == target_amount:
            return [m.as_atom() for m in items[3].as_iter()]
    return []


def _text(memo: bytes) -> str | None:
    if len(memo) == 32 or not memo:
        return None
    try:
        return memo.decode("utf-8")
    except UnicodeDecodeError:
        return None


def deployer_puzzle_hash(launcher_parent_hex: str, node: str = DEFAULT_NODE) -> str:
    """The address that paid for the launcher: the launcher parent's puzzle hash."""
    rec = _rpc(node, "get_coin_record_by_name", {"name": "0x" + launcher_parent_hex})["coin_record"]
    return _strip(rec["coin"]["puzzle_hash"])


def genesis_name(launcher_parent_hex: str, node: str = DEFAULT_NODE) -> tuple[str | None, str | None]:
    """(name, symbol) from the launcher's creation memos, either may be None."""
    rec = _rpc(node, "get_coin_record_by_name", {"name": "0x" + launcher_parent_hex})["coin_record"]
    if not rec.get("spent"):
        return None, None
    texts = [t for t in (_text(m) for m in _memos_of(node, launcher_parent_hex, rec["spent_block_index"], LAUNCHER_HASH_HEX, 1)) if t]
    return (texts[0] if texts else None), (texts[1] if len(texts) > 1 else None)


def renames(launcher_id_hex: str, deployer_ph_hex: str, node: str = DEFAULT_NODE) -> list[tuple[int, str, str | None]]:
    """[(height, name, symbol)] of every rename the deployer made, oldest first."""
    recs = _rpc(node, "get_coin_records_by_hint", {"hint": "0x" + launcher_id_hex, "include_spent_coins": True}).get("coin_records", [])
    found: list[tuple[int, str, str | None]] = []
    for r in recs:
        coin = r["coin"]
        if _strip(coin["puzzle_hash"]) != deployer_ph_hex:
            continue
        parent_hex = _strip(coin["parent_coin_info"])
        parent = _rpc(node, "get_coin_record_by_name", {"name": "0x" + parent_hex}).get("coin_record")
        if not parent or _strip(parent["coin"]["puzzle_hash"]) != deployer_ph_hex or not parent.get("spent"):
            continue
        memos = _memos_of(node, parent_hex, parent["spent_block_index"], deployer_ph_hex, int(coin["amount"]))
        if len(memos) >= 2 and memos[0].hex() == launcher_id_hex:
            text = _text(memos[1])
            if text:
                found.append((int(r["confirmed_block_index"]), text, _text(memos[2]) if len(memos) > 2 else None))
    found.sort(key=lambda t: t[0])
    return found


def resolve(launcher_id_hex: str, launcher_parent_hex: str, node: str = DEFAULT_NODE) -> dict[str, Any]:
    """{name, source ('rename'|'genesis'|None), deployer_ph, genesis, renames}."""
    deployer = deployer_puzzle_hash(launcher_parent_hex, node)
    history = renames(launcher_id_hex, deployer, node)
    genesis, genesis_symbol = genesis_name(launcher_parent_hex, node)
    if history:
        height, name, symbol = history[-1]
        return {"name": name, "symbol": symbol, "source": "rename", "height": height, "deployer_ph": deployer,
                "genesis": genesis, "genesis_symbol": genesis_symbol, "renames": history}
    if genesis:
        return {"name": genesis, "symbol": genesis_symbol, "source": "genesis", "deployer_ph": deployer,
                "genesis": genesis, "genesis_symbol": genesis_symbol, "renames": []}
    return {"name": None, "symbol": None, "source": None, "deployer_ph": deployer, "genesis": None,
            "genesis_symbol": genesis_symbol, "renames": []}
