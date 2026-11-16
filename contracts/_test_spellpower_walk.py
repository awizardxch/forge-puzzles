"""Spellpower's entry walk moves every NFT forward together.

Entering Spellpower walks each held NFT from its launcher (or the last coin a walk
ended on) to its current coin. Walked one NFT at a time, that was one serial node
call per hop per NFT: 20 mainnet NFTs were 169 calls and 20.7 s from a desk, past
the router's 90 s limit where a call costs over a second (owner, 2026-10-24:
"check spellpower as that seems to be slow"). Now the starts are one batch and
each hop is one ``get_coin_records_by_parent_ids`` call for every NFT still moving,
so the calls follow the longest walk, not the sum of them.

This suite pins, on a made-up chain, that the batched walk answers exactly what
the one-at-a-time walk (``current_nft``) answers -- unspent launchers and eves, a
melted coin, a coin that is not there, the hop limit, a resume point -- and how few
calls it takes. The owner read from a real NFT spend matched ``current_nft`` for
20 mainnet NFTs on 2026-10-24 (2.2 s, 30 calls); this file does not need the chain.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import spellpower_nfts as sp

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if ok:
        passed += 1
    else:
        failed += 1


def rec(coin: Coin, spent: bool) -> dict:
    return {"coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(), "puzzle_hash": "0x" + coin.puzzle_hash.hex(), "amount": int(coin.amount)},
            "spent": spent, "confirmed_block_index": 7, "spent_block_index": 9 if spent else 0}


class Chain:
    """Coins by id and by parent; answers the routes the walk uses and counts them."""

    def __init__(self) -> None:
        self.coins: dict[bytes32, dict] = {}
        self.calls: dict[str, int] = {}

    def add(self, coin: Coin, spent: bool) -> Coin:
        self.coins[coin.name()] = rec(coin, spent)
        return coin

    def rpc(self, route: str, body: dict) -> dict:
        self.calls[route] = self.calls.get(route, 0) + 1
        if route == "get_coin_record_by_name":
            return {"coin_record": self.coins.get(bytes32.fromhex(body["name"]))}
        if route == "get_coin_records_by_names":
            ids = [bytes32.fromhex(n.removeprefix("0x")) for n in body["names"]]
            return {"coin_records": [self.coins[i] for i in ids if i in self.coins]}
        if route == "get_coin_records_by_parent_ids":
            parents = {p.removeprefix("0x") for p in body["parent_ids"]}
            return {"coin_records": [r for r in self.coins.values() if r["coin"]["parent_coin_info"].removeprefix("0x") in parents]}
        if route == "get_puzzle_and_solution":
            return {"success": False}                       # no NFT spends here: owners are read live
        raise AssertionError(route)

    def coin_record(self, coin_id: bytes32) -> dict | None:
        return self.rpc("get_coin_record_by_name", {"name": coin_id.hex()}).get("coin_record")

    def puzzle_and_solution(self, coin_id, height):
        raise sp.MultisigError("no spend")


def singleton(chain: Chain, seed: int, hops: int, end: str = "unspent") -> tuple[bytes32, list[Coin]]:
    """A launcher and ``hops`` odd children after it. ``end`` = unspent | melted."""
    launcher = chain.add(Coin(bytes32(bytes([seed]) * 32), bytes32(b"\x01" * 32), uint64(1)), hops > 0 or end == "melted")
    path = [launcher]
    for i in range(hops):
        last = i == hops - 1
        spent = not last or end == "melted"
        child = chain.add(Coin(path[-1].name(), bytes32(bytes([seed, i]) + b"\x02" * 30), uint64(1)), spent)
        chain.add(Coin(path[-1].name(), bytes32(b"\x03" * 32), uint64(1000 + 2 * i)), False)   # an even sibling: change
        path.append(child)
    return launcher.name(), path


chain = Chain()
nfts = {
    "unspent launcher": singleton(chain, 0x10, 0),
    "eve, unspent": singleton(chain, 0x11, 1),
    "three hops": singleton(chain, 0x12, 3),
    "nine hops": singleton(chain, 0x13, 9),
    "melted": singleton(chain, 0x14, 2, end="melted"),
}
missing = bytes32(b"\x99" * 32)
items = [{"launcher": launcher.hex()} for launcher, _ in nfts.values()] + [{"launcher": missing.hex()}]

print("same answers as one NFT at a time")
one_by_one = {i["launcher"]: sp.current_nft(chain, sp.hex32(i["launcher"]), None) for i in items}
chain.calls.clear()
together = sp.current(chain, items)["nfts"]
check("every NFT, in the caller's order, answers what current_nft answers", together == one_by_one and list(together) == [i["launcher"] for i in items],
      "" if together == one_by_one else str({k: (one_by_one[k], together[k]) for k in together if one_by_one[k] != together[k]}))
check("the walk reaches the end of the longest path", together[nfts["nine hops"][0].hex()]["hops"] == 9)
check("a melted NFT and a coin that is not there are errors, not owners",
      "error" in together[nfts["melted"][0].hex()] and together[missing.hex()] == {"error": "the chain has no such coin"})

print("\ncalls follow the longest walk")
check("the starts are one batch, no coin read one at a time",
      chain.calls.get("get_coin_records_by_names") == 1 and chain.calls.get("get_coin_record_by_name", 0) == 0, str(chain.calls))
check("one parent-ids call per hop of the longest walk (9), not one per hop per NFT (15)",
      chain.calls.get("get_coin_records_by_parent_ids") == 9, str(chain.calls))

print("\nresume and limits")
launcher, path = nfts["nine hops"]
resumed = sp.current(chain, [{"launcher": launcher.hex(), "from": path[5].name().hex()}])["nfts"][launcher.hex()]
check("a resume point walks only the rest", resumed == sp.current_nft(chain, launcher, path[5].name()) and resumed["hops"] == 4, str(resumed))
saved = sp.MAX_HOPS
sp.MAX_HOPS = 4
try:
    capped = sp.current(chain, [{"launcher": launcher.hex()}])["nfts"][launcher.hex()]
    check("the hop limit stops a long walk and says where it stopped",
          capped == sp.current_nft(chain, launcher, None) and capped["error"] == "the walk ran out of hops" and capped["spent_to"] == path[4].name().hex(), str(capped))
finally:
    sp.MAX_HOPS = saved

print(f"\n{passed}/{passed + failed} spellpower walk checks passed")
sys.exit(1 if failed else 0)
