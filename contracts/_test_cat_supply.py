"""cat_supply.py against a fake chain: an issuance coin, its eve CAT coins, and a
lineage of transfers below them.

Pins that the walk stops at the first parent that is not this CAT, that only a
genesis_by_coin_id TAIL matching the issuance coin is accepted, that every eve
sibling counts and nothing else does, that the burn address is subtracted, and
that a budget stops and resumes the walk. And what the node is asked: a parent's
record and reveal side by side, the eve reveals together, many assets' burn
addresses in one call.
"""
from __future__ import annotations

import sys
import threading
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
    """Coin records and reveals, served the way the node's RPC serves them.

    As on chain, a coin is created in the block that spends its parent, and a spend is
    found only at the height it happened. `overlap` makes each parent's record read
    wait briefly for its reveal to be asked for, and notes whether it was.
    """

    def __init__(self, overlap: bool = False) -> None:
        # With `lenient`, a reveal read at a height the coin was not spent at answers
        # with a puzzle that is no CAT at all, which only the coin's record can catch.
        self.lenient = False
        self.records: dict[str, dict] = {}
        self.reveals: dict[str, str] = {}
        # puzzle hash (hex) -> unspent coin amounts there
        self.burns: dict[str, list[int]] = {}
        self.routes: list[str] = []
        self.lock = threading.Lock()
        self.overlap = overlap
        self.asked: dict[str, threading.Event] = {}
        self.overlapped: list[bool] = []
        # With `together`, each reveal read after the issuance's children are listed waits
        # for that many reveal reads to be in flight at once, and notes whether they were.
        self.together = 0
        self.listed = False
        self.in_flight = 0
        self.all_in = threading.Event()
        self.together_seen: list[bool] = []

    def add(self, parent: bytes32, puzzle: Program, amount: int, spent: bool) -> bytes32:
        coin = Coin(parent, puzzle.get_tree_hash(), amount)
        parent_record = self.records.get(bytes(parent).hex())
        height = parent_record["spent_block_index"] if parent_record else 1
        self.records[bytes(coin.name()).hex()] = {
            "coin": {"parent_coin_info": "0x" + bytes(parent).hex(), "puzzle_hash": "0x" + bytes(coin.puzzle_hash).hex(),
                     "amount": amount},
            "confirmed_block_index": height,
            "spent": spent, "spent_block_index": height + 10 if spent else 0,
        }
        if spent:
            # Serialized here: the walk reads side by side, and a Program panics on another thread.
            self.reveals[bytes(coin.name()).hex()] = "0x" + bytes(puzzle).hex()
        return coin.name()

    def burn(self, asset: str, amount: int, at: bytes32 = cs.BURN_PUZZLE_HASH) -> None:
        self.burns.setdefault(bytes(cat_puzzle_hash(bytes32.fromhex(asset), at)).hex(), []).append(amount)

    def _asked(self, coin_id: str) -> threading.Event:
        with self.lock:
            return self.asked.setdefault(coin_id, threading.Event())

    def __call__(self, route: str, payload: dict) -> dict:
        with self.lock:
            self.routes.append(route)
        if route == "get_coin_record_by_name":
            name = cs._strip(payload["name"])
            record = self.records.get(name)
            if self.overlap and record and record["spent"]:
                self.overlapped.append(self._asked(name).wait(1.0))
            return {"success": True, "coin_record": record} if record else {"success": False, "error": "not found"}
        if route == "get_puzzle_and_solution":
            name = cs._strip(payload["coin_id"])
            self._asked(name).set()
            if self.together and self.listed:
                with self.lock:
                    self.in_flight += 1
                    if self.in_flight >= self.together:
                        self.all_in.set()
                self.together_seen.append(self.all_in.wait(1.0))
            record = self.records[name]
            if payload["height"] != record["spent_block_index"]:
                if self.lenient:
                    return {"success": True, "coin_solution": {"puzzle_reveal": "0x01"}}
                return {"success": False, "error": f"no spend of {name[:12]} at height {payload['height']}"}
            return {"success": True, "coin_solution": {"puzzle_reveal": self.reveals[name]}}
        if route == "get_coin_records_by_parent_ids":
            self.listed = True
            parent = cs._strip(payload["parent_ids"][0])
            return {"success": True, "coin_records": [r for r in self.records.values()
                                                      if cs._strip(r["coin"]["parent_coin_info"]) == parent]}
        if route == "get_coin_records_by_puzzle_hashes":
            return {"success": True, "coin_records": [
                {"coin": {"puzzle_hash": ph, "amount": amount}, "spent": False}
                for ph in map(cs._strip, payload["puzzle_hashes"]) for amount in self.burns.get(ph, [])]}
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
        chain.burn(bytes(asset).hex(), burn)
    return chain, bytes(asset).hex(), bytes(tip).hex()


def build_overlapping(tail_for):
    chain, asset, tip = build(tail_for)
    chain.overlap = True
    return chain, asset, tip


chain, asset, tip = build(lambda issuance: GENESIS_BY_ID_MOD.curry(issuance))
out = cs.supply({"asset_id": asset, "start": tip, "budget": 50}, rpc=chain)
check("a genesis_by_coin_id CAT is found", out["status"] == "found", str(out))
check("minted is every eve sibling, not the change", out.get("minted") == "100", out.get("minted", ""))
check("the walk climbs to the issuance coin", out.get("walked") == 7, str(out.get("walked")))
# The tip's record, then a record and a reveal for each of the 7 parents, the issuance's
# children, a reveal for each of its 2 spent children, and one burn read.
check("the node calls are what the walk needs, and one burn read",
      len(chain.routes) == 1 + 2 * 7 + 1 + 2 + 1 and chain.routes.count("get_coin_records_by_puzzle_hashes") == 1,
      f"{len(chain.routes)} calls")

chain, asset, tip = build_overlapping(lambda issuance: GENESIS_BY_ID_MOD.curry(issuance))
side = cs.supply({"asset_id": asset, "start": tip, "budget": 50}, rpc=chain)
# A walk that waits for each parent's record before asking for its reveal leaves these
# False: the record read times out with the reveal not yet asked for.
check("each parent's reveal is asked for while its record is still being read",
      len(chain.overlapped) == 7 and all(chain.overlapped), str(chain.overlapped))
check("and the answer is the same", side == out)

chain, asset, tip = build(lambda issuance: GENESIS_BY_ID_MOD.curry(issuance))
chain.together = 2                                # the issuance's two spent children
together = cs.supply({"asset_id": asset, "start": tip, "budget": 50}, rpc=chain)
check("the eve candidates' reveals are read together, not one after another",
      chain.together_seen == [True, True] and together == out, str(chain.together_seen))

chain, asset, tip = build(lambda issuance: GENESIS_BY_ID_MOD.curry(issuance))
for record in chain.records.values():
    record["confirmed_block_index"] += 3          # no longer the parent's spent height
drifted = cs.supply({"asset_id": asset, "start": tip, "budget": 50}, rpc=chain)
check("a guessed height the record does not confirm is read again at the record's",
      drifted == out and len(chain.routes) == 1 + 3 * 7 + 1 + 2 + 1, f"{len(chain.routes)} calls")
chain.lenient = True
check("a reveal read at a height the record does not confirm is never used, even when the node answers it",
      cs.supply({"asset_id": asset, "start": tip, "budget": 50}, rpc=chain) == out)
chain.lenient = False

chain, asset, tip = build(lambda issuance: GENESIS_BY_ID_MOD.curry(issuance), burn=25)
out = cs.supply({"asset_id": asset, "start": tip, "budget": 50}, rpc=chain)
check("burned is read from the burn address", out.get("burned") == "25", out.get("burned", ""))
check("burned_only reads the burn address alone",
      cs.supply({"asset_id": asset, "burned_only": True}, rpc=chain) == {"success": True, "status": "burned", "burned": "25"})

chain, asset, tip = build(lambda issuance: GENESIS_BY_ID_MOD.curry(issuance), burn=25)
chain.burn(asset, 7, at=bytes32(b"\x00" * 32))
check("LP or tokens at the all-zero hash (Forge's genesis and Burn LP target) count as burned too",
      cs.supply({"asset_id": asset, "burned_only": True}, rpc=chain)["burned"] == "32")

# The periodic refresh: many assets' burn addresses in one call.
others = ["ab" * 32, "cd" * 32, "ef" * 32]
chain.burn(others[0], 5)
chain.burn(others[0], 6, at=bytes32(b"\x00" * 32))
chain.burn(others[2], 1_000)
chain.routes.clear()
many = cs.supply({"asset_ids": [asset, *others], "burned_only": True}, rpc=chain)
check("many assets' burn addresses are read in one call, each asset its own sum",
      many == {"success": True, "status": "burned",
               "burned": {asset: "32", others[0]: "11", others[1]: "0", others[2]: "1000"}}
      and chain.routes == ["get_coin_records_by_puzzle_hashes"], f"{many}, {chain.routes}")
wide = ["%064x" % n for n in range(1, 121)]       # 240 burn hashes
chain.routes.clear()
cs.supply({"asset_ids": wide, "burned_only": True}, rpc=chain)
check("past HASHES_PER_CALL hashes the read takes another call, not one per asset",
      chain.routes == ["get_coin_records_by_puzzle_hashes"] * 3, str(len(chain.routes)))

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
original = probe.__call__


def recording(route: str, payload: dict) -> dict:
    if route == "get_coin_records_by_puzzle_hashes":
        asked.extend(cs._strip(ph) for ph in payload["puzzle_hashes"])
    return original(route, payload)


cs.burned(recording, "ab" * 32)
check("the burn query asks for this CAT at 0x...dead and at the all-zero hash, nothing else",
      asked == [bytes(cat_puzzle_hash(bytes32.fromhex("ab" * 32), burn)).hex() for burn in cs.BURN_PUZZLE_HASHES]
      and len(set(asked)) == 2)

print(f"\n{'all cat-supply checks passed' if failures == 0 else f'{failures} cat-supply check(s) failed'}")
raise SystemExit(1 if failures else 0)
