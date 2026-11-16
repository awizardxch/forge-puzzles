"""A CAT's circulating supply, read from the chain: minted, less what was burned.

Market cap is that supply times the price. Nothing here trusts a token list: the
amount minted is read from the issuance itself.

Minted. Every CAT coin descends from its issuance, because a CAT coin can only be
created by a spend of the same CAT or by the TAIL's issuance. So walking any coin's
parents back, the first parent that is NOT this CAT is the issuance coin, and the
coin below it is an eve coin. Only one TAIL is read: genesis_by_coin_id, the
single-issuance TAIL, whose asset id is that TAIL curried with the issuance coin's
id -- so the match proves the TAIL, and that TAIL can never issue again or melt.
The amount minted is then every eve coin the issuance spend created, found among
its children by their own reveals. Any other TAIL can issue again or melt, so its
supply is reported unknown rather than guessed.

Burned. Supply sent to the burn address (puzzle hash 0x...dead) is out of
circulation for good; its unspent CAT coins are found by their puzzle hash, which
needs no walk. Many assets' burn addresses are read in one call.

Node calls that do not wait on each other are made side by side: a parent's record
and its spend (it was spent in the block that created its child, so the child's
confirmed height names it), and every eve candidate's reveal. Only raw JSON crosses
threads; chia's Program panics on a thread that did not make it, so every reveal is
parsed on the calling thread.

stdin:  JSON {asset_id, start (a coin id of this CAT), budget?, node_url?}, or
        {asset_id, burned_only: true} to re-read the burn address alone, or
        {asset_ids: [...], burned_only: true} to re-read many burn addresses at once
stdout: JSON {success, status: "found"|"budget"|"unsupported", next?, walked,
              minted?, burned?, issuance_coin?, eve_coins?, unspent_siblings?};
        for asset_ids, {success, status: "burned", burned: {asset_id: amount}}
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import match_cat_puzzle  # noqa: E402
from chia.wallet.puzzles.tails import GENESIS_BY_ID_MOD  # noqa: E402
from chia.wallet.uncurried_puzzle import uncurry_puzzle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

import forge_network as _forge_network  # noqa: E402
from forge_v18_price_history import Rpc, _strip, node_rpc  # noqa: E402
from wallet_holdings import cat_puzzle_hash  # noqa: E402

DEFAULT_NODE = _forge_network.node_url()
MAX_BUDGET = 200
BURN_PUZZLE_HASH = bytes32.fromhex("0" * 60 + "dead")
# Both unspendable hashes in use. 0x...dead is the community's burn address; the
# all-zero hash is where V13-V15 pools paid their genesis LOCKED_BURN (forge_v15_driver
# ZERO_32) and where the site's "Burn LP Coin" sent the seed before 2026-10-09; V16 pays
# both to 0x...dead. Neither has a known preimage, so both count as burned.
BURN_PUZZLE_HASHES = (BURN_PUZZLE_HASH, bytes32(b"\x00" * 32))
# Puzzle hashes asked about in one get_coin_records_by_puzzle_hashes call.
HASHES_PER_CALL = 100
# Reveals read at once when checking an issuance's children.
REVEAL_WORKERS = 8


class SupplyError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _ok(answer: dict, what: str) -> dict:
    if not answer.get("success"):
        raise SupplyError(f"{what}: {answer.get('error') or 'node refused'}", "NODE_ERROR")
    return answer


def coin_record(rpc: Rpc, coin_id: str) -> dict:
    return _ok(rpc("get_coin_record_by_name", {"name": "0x" + coin_id}), f"coin {coin_id[:12]}")["coin_record"]


def reveal_json(rpc: Rpc, coin_id: str, spent_height: int) -> str:
    """A spend's puzzle reveal as hex: safe to fetch on any thread."""
    answer = _ok(rpc("get_puzzle_and_solution", {"coin_id": "0x" + coin_id, "height": spent_height}),
                 f"puzzle of {coin_id[:12]}")
    return _strip(answer["coin_solution"]["puzzle_reveal"])


def reveal_of(rpc: Rpc, coin_id: str, spent_height: int) -> Program:
    return Program.fromhex(reveal_json(rpc, coin_id, spent_height))


def cat_asset(reveal: Program) -> str | None:
    """The asset id of a CAT reveal, or None when the puzzle is not a CAT."""
    matched = match_cat_puzzle(uncurry_puzzle(reveal))
    if matched is None:
        return None
    _mod_hash, tail_hash, _inner = matched
    return _strip(bytes(tail_hash.as_atom()).hex())


def is_this_cat(rpc: Rpc, record: dict, asset_id: str, early: tuple[int, Future] | None = None) -> bool:
    """Whether a spent coin was this CAT, by its reveal.

    `early` is (height, read) for a reveal already asked for, at a height named before
    the record arrived. It is used only when the record confirms that height, and read
    again here when it failed.
    """
    if not record.get("spent"):
        return False
    coin_id = _coin_id(record)
    spent_height = int(record["spent_block_index"])
    reveal_hex = None
    if early is not None and early[0] == spent_height:
        try:
            reveal_hex = early[1].result()
        except Exception:  # noqa: BLE001 -- read again below, where a failure is raised
            reveal_hex = None
    if reveal_hex is None:
        reveal_hex = reveal_json(rpc, coin_id, spent_height)
    return cat_asset(Program.fromhex(reveal_hex)) == asset_id


def _coin_id(record: dict) -> str:
    coin = record["coin"]
    from chia_rs import Coin
    return bytes(Coin(bytes32.fromhex(_strip(coin["parent_coin_info"])), bytes32.fromhex(_strip(coin["puzzle_hash"])),
                      int(coin["amount"])).name()).hex()


def burned_many(rpc: Rpc, asset_ids: list[str]) -> dict[str, int]:
    """Each asset's unspent coins at every burn address: one call per HASHES_PER_CALL hashes."""
    owner: dict[str, str] = {}
    for asset_id in dict.fromkeys(asset_ids):
        for burn in BURN_PUZZLE_HASHES:
            owner[bytes(cat_puzzle_hash(bytes32.fromhex(asset_id), burn)).hex()] = asset_id
    totals = {asset_id: 0 for asset_id in asset_ids}
    hashes = list(owner)
    for start in range(0, len(hashes), HASHES_PER_CALL):
        answer = _ok(rpc("get_coin_records_by_puzzle_hashes",
                         {"puzzle_hashes": ["0x" + h for h in hashes[start:start + HASHES_PER_CALL]],
                          "include_spent_coins": False}), "burn addresses")
        for record in answer.get("coin_records") or []:
            asset_id = owner.get(_strip(record["coin"]["puzzle_hash"]))
            if asset_id is not None:
                totals[asset_id] += int(record["coin"]["amount"])
    return totals


def burned(rpc: Rpc, asset_id: str) -> int:
    return burned_many(rpc, [asset_id])[asset_id]


def supply(payload: dict[str, Any], rpc: Rpc | None = None) -> dict[str, Any]:
    if payload.get("burned_only") and payload.get("asset_ids") is not None:
        # The periodic refresh: every asset's burn addresses, one spawn and one call.
        asset_ids = [_strip(a) for a in payload.get("asset_ids") or []]
        if not asset_ids or any(len(a) != 64 for a in asset_ids):
            raise SupplyError("asset_ids must be a list of 32-byte hex ids", "BAD_INPUT")
        rpc = rpc or node_rpc(str(payload.get("node_url") or DEFAULT_NODE))
        return {"success": True, "status": "burned",
                "burned": {asset_id: str(amount) for asset_id, amount in burned_many(rpc, asset_ids).items()}}
    asset_id = _strip(payload.get("asset_id"))
    if len(asset_id) != 64:
        raise SupplyError("asset_id must be 32 bytes of hex", "BAD_INPUT")
    rpc = rpc or node_rpc(str(payload.get("node_url") or DEFAULT_NODE))
    # The issuance never changes once found; the burn address can, and is one call.
    if payload.get("burned_only"):
        return {"success": True, "status": "burned", "burned": str(burned(rpc, asset_id))}
    start = _strip(payload.get("start"))
    if len(start) != 64:
        raise SupplyError("start must be 32 bytes of hex", "BAD_INPUT")
    budget = max(1, min(int(payload.get("budget") or 40), MAX_BUDGET))

    with ThreadPoolExecutor(max_workers=REVEAL_WORKERS) as pool:
        return _walk_to_issuance(rpc, pool, asset_id, start, budget)


def _walk_to_issuance(rpc: Rpc, pool: ThreadPoolExecutor, asset_id: str, start: str, budget: int) -> dict[str, Any]:
    # Walk parents until one is not this CAT: that one is the issuance coin.
    coin_id = start
    record = coin_record(rpc, coin_id)
    walked = 0
    while True:
        if walked >= budget:
            return {"success": True, "status": "budget", "next": coin_id, "walked": walked}
        parent_id = _strip(record["coin"]["parent_coin_info"])
        # The parent was spent in the block that created this coin: its reveal is asked
        # for at that height while its record is read, not after.
        guess = int(record.get("confirmed_block_index") or 0)
        parent_read = pool.submit(coin_record, rpc, parent_id)
        early = (guess, pool.submit(reveal_json, rpc, parent_id, guess)) if guess > 0 else None
        parent = parent_read.result()
        walked += 1
        if not is_this_cat(rpc, parent, asset_id, early):
            break
        coin_id, record = parent_id, parent

    # Only the single-issuance TAIL has a supply that the issuance fixes.
    if GENESIS_BY_ID_MOD.curry(bytes32.fromhex(parent_id)).get_tree_hash().hex() != asset_id:
        return {"success": True, "status": "unsupported", "walked": walked, "issuance_coin": parent_id,
                "reason": "the TAIL is not genesis_by_coin_id, so more can be issued or melted"}

    # Every eve coin is a child of the issuance spend; each proves itself by its reveal.
    children = _ok(rpc("get_coin_records_by_parent_ids", {"parent_ids": ["0x" + parent_id], "include_spent_coins": True}),
                   "issuance children").get("coin_records") or []
    minted = 0
    eves: list[str] = []
    unspent: list[str] = []
    # Every spent child's reveal at once; each is still judged here, in order.
    reveals = {_coin_id(child): (int(child["spent_block_index"]),
                                 pool.submit(reveal_json, rpc, _coin_id(child), int(child["spent_block_index"])))
               for child in children if child.get("spent")}
    for child in children:
        child_id = _coin_id(child)
        if not child.get("spent"):
            # An unspent sibling cannot show its puzzle: it is change, or an eve never moved.
            unspent.append(child_id)
            continue
        if is_this_cat(rpc, child, asset_id, reveals[child_id]):
            minted += int(child["coin"]["amount"])
            eves.append(child_id)
    if minted == 0:
        raise SupplyError("the issuance spend created no eve coin of this CAT", "NO_EVE")

    return {"success": True, "status": "found", "walked": walked, "issuance_coin": parent_id,
            "eve_coins": eves, "unspent_siblings": unspent, "minted": str(minted), "burned": str(burned(rpc, asset_id))}


def main() -> int:
    try:
        print(json.dumps(supply(json.loads(sys.stdin.read() or "{}"))))
        return 0
    except SupplyError as exc:
        print(json.dumps({"success": False, "error": str(exc), "code": exc.code}))
        return 1
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}", "code": "SUPPLY_ERROR"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
