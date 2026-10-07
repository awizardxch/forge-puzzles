"""cat_supply.py against a fake chain: an issuance coin, its eve CAT coins, and a
lineage of transfers below them.

Pins that the walk stops at the first parent that is not this CAT, that only a
genesis_by_coin_id TAIL matching the issuance coin is accepted, that every eve
sibling counts and nothing else does, that the burn address is subtracted, and
that a budget stops and resumes the walk.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle  # noqa: E402
from chia.wallet.puzzles.tails import EVERYTHING_WITH_SIG_MOD, GENESIS_BY_ID_MOD  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

import cat_supply as cs  # noqa: E402
from wallet_holdings import cat_puzzle_hash  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures += 1


class Chain:
    """Coin records and reveals, served the way the node's RPC serves them."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.reveals: dict[str, Program] = {}
        self.burn_coins: list[dict] = []

    def add(self, parent: bytes32, puzzle: Program, amount: int, spent: bool) -> bytes32:
        coin = Coin(parent, puzzle.get_tree_hash(), amount)
        self.records[bytes(coin.name()).hex()] = {
            "coin": {"parent_coin_info": "0x" + bytes(parent).hex(), "puzzle_hash": "0x" + bytes(coin.puzzle_hash).hex(),
                     "amount": amount},
            "spent": spent, "spent_block_index": 10 if spent else 0,
        }
        if spent:
            self.reveals[bytes(coin.name()).hex()] = puzzle
        return coin.name()

    def __call__(self, route: str, payload: dict) -> dict:
        if route == "get_coin_record_by_name":
            record = self.records.get(cs._strip(payload["name"]))
            return {"success": True, "coin_record": record} if record else {"success": False, "error": "not found"}
        if route == "get_puzzle_and_solution":
            reveal = self.reveals[cs._strip(payload["coin_id"])]
            return {"success": True, "coin_solution": {"puzzle_reveal": "0x" + bytes(reveal).hex()}}
        if route == "get_coin_records_by_parent_ids":
            parent = cs._strip(payload["parent_ids"][0])
            return {"success": True, "coin_records": [r for r in self.records.values()
                                                      if cs._strip(r["coin"]["parent_coin_info"]) == parent]}
        if route == "get_coin_records_by_puzzle_hash":
            return {"success": True, "coin_records": self.burn_coins}
        raise AssertionError(route)


def build(tail_for, siblings_spent=True, burn=0):
    """issuance coin -> two eves (60 + 40) and change -> a chain of 5 transfers below eve A."""
    chain = Chain()
    xch_puzzle = Program.to(1)
    issuance = chain.add(bytes32(b"\x01" * 32), xch_puzzle, 1_000, spent=True)
    tail = tail_for(issuance)
    asset = tail.get_tree_hash()
    cat = lambda inner: construct_cat_puzzle(CAT_MOD, asset, inner)  # noqa: E731
    eve_a = chain.add(issuance, cat(Program.to(2)), 60, spent=True)
    chain.add(issuance, cat(Program.to(3)), 40, spent=siblings_spent)
    chain.add(issuance, Program.to(4), 900, spent=False)  # change, never a CAT
    coin = eve_a
    for i in range(5):
        coin = chain.add(coin, cat(Program.to(10 + i)), 60, spent=True)
    tip = chain.add(coin, cat(Program.to(99)), 60, spent=False)
    if burn:
        chain.burn_coins = [{"coin": {"amount": burn}}]
    return chain, bytes(asset).hex(), bytes(tip).hex()


chain, asset, tip = build(lambda issuance: GENESIS_BY_ID_MOD.curry(issuance))
out = cs.supply({"asset_id": asset, "start": tip, "budget": 50}, rpc=chain)
check("a genesis_by_coin_id CAT is found", out["status"] == "found", str(out))
check("minted is every eve sibling, not the change", out.get("minted") == "100", out.get("minted", ""))
check("the walk climbs to the issuance coin", out.get("walked") == 7, str(out.get("walked")))

chain, asset, tip = build(lambda issuance: GENESIS_BY_ID_MOD.curry(issuance), burn=25)
out = cs.supply({"asset_id": asset, "start": tip, "budget": 50}, rpc=chain)
check("burned is read from the burn address", out.get("burned") == "25", out.get("burned", ""))
check("burned_only reads the burn address alone",
      cs.supply({"asset_id": asset, "burned_only": True}, rpc=chain) == {"success": True, "status": "burned", "burned": "25"})

chain, asset, tip = build(lambda issuance: GENESIS_BY_ID_MOD.curry(issuance), siblings_spent=False)
out = cs.supply({"asset_id": asset, "start": tip, "budget": 50}, rpc=chain)
check("an unspent sibling is reported, not counted", out.get("minted") == "60" and len(out.get("unspent_siblings", [])) == 2)

chain, asset, tip = build(lambda issuance: EVERYTHING_WITH_SIG_MOD.curry(Program.to(b"\x02" * 48)))
out = cs.supply({"asset_id": asset, "start": tip, "budget": 50}, rpc=chain)
check("a TAIL that can issue again is unsupported, not guessed", out["status"] == "unsupported" and "minted" not in out)

chain, asset, tip = build(lambda issuance: GENESIS_BY_ID_MOD.curry(issuance))
first = cs.supply({"asset_id": asset, "start": tip, "budget": 3}, rpc=chain)
check("a budget stops the walk with where to resume", first["status"] == "budget" and first["walked"] == 3)
second = cs.supply({"asset_id": asset, "start": first["next"], "budget": 50}, rpc=chain)
check("resuming finishes at the same supply", second.get("minted") == "100" and second["walked"] == 4)

# The burn coins' puzzle hash is this CAT at the burn address, nothing else.
asked: list[str] = []
probe = Chain()
probe.burn_coins = []
original = probe.__call__


def recording(route: str, payload: dict) -> dict:
    if route == "get_coin_records_by_puzzle_hash":
        asked.append(cs._strip(payload["puzzle_hash"]))
    return original(route, payload)


cs.burned(recording, "ab" * 32)
check("the burn query asks for this CAT at 0x...dead",
      asked == [bytes(cat_puzzle_hash(bytes32.fromhex("ab" * 32), cs.BURN_PUZZLE_HASH)).hex()])

print(f"\n{'all cat-supply checks passed' if failures == 0 else f'{failures} cat-supply check(s) failed'}")
raise SystemExit(1 if failures else 0)
