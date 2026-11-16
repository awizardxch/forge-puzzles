#!/usr/bin/env python3
"""A wallet's CAT coins of several assets in a fixed number of node reads
(wallet_coins.cat_coins_for_keys), however many assets and coins.

The Liquidity, Swap and Offers tabs and the coin combine read these from the router, ~1-1.5 s a
node call. Until 2026-10-24 the default path made one address read per asset and then, for each
of up to 5 coins an asset, the parent's record and the parent's spend, one after another: up to
11 calls an asset. Now: one address read for every asset, one read of every parent, and only the
spends nothing else identifies, fetched side by side, their JSON parsed on the calling thread
(chia's Program panics on any other). Pinned with a fake node holding real CAT coins of three
assets whose parents are the wallet's own address, the offer settlement puzzle, another wallet,
and a TAIL mint; every lineage is checked against the parent it was built from.
Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import wallet_coins as wc  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD  # noqa: E402
from chia_rs import Coin, PrivateKey  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))


KEYS = [PrivateKey.from_bytes(bytes([0x64 + i]) * 32).get_g1() for i in range(3)]
INNERS = [puzzle_for_synthetic_public_key(k) for k in KEYS]
OTHER_INNER = puzzle_for_synthetic_public_key(PrivateKey.from_bytes(bytes([0x6f]) * 32).get_g1())
ASSETS = [bytes32(bytes([0xe0 + i]) * 32) for i in range(3)]
TAIL_PARENT = Program.to(1)


def rec(coin: Coin, spent: bool, height: int = 0) -> dict:
    return {"coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(), "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
                     "amount": int(coin.amount)}, "spent": spent, "spent_block_index": height, "confirmed_block_index": 1}


class FakeNode:
    """Counts every read; spends answer as JSON from worker threads, slowly, so overlap shows."""

    def __init__(self, coins, parents, spends):
        self.coins, self.parents = coins, {p.name(): p for p in parents}
        self.reveals = {key: bytes(puzzle).hex() for key, puzzle in spends.items()}   # hex made on this thread
        self.calls = {"by_puzzle_hashes": 0, "by_names": 0, "spend_json": 0, "spend_parsed": 0, "coin_record": 0}
        self.lock = threading.Lock()
        self.in_flight = self.most_in_flight = 0
        self.spend_threads = set()

    def coin_records_by_puzzle_hashes(self, hashes):
        self.calls["by_puzzle_hashes"] += 1
        wanted = set(bytes(h) for h in hashes)
        return [rec(c, False) for c in self.coins if bytes(c.puzzle_hash) in wanted]

    def rpc(self, route, body):
        if route == "get_puzzle_and_solution":
            with self.lock:
                self.calls["spend_json"] += 1
                self.in_flight += 1
                self.most_in_flight = max(self.most_in_flight, self.in_flight)
                self.spend_threads.add(threading.get_ident())
            time.sleep(0.05)
            with self.lock:
                self.in_flight -= 1
            return {"coin_solution": {"puzzle_reveal": self.reveals[bytes.fromhex(body["coin_id"])], "solution": "80"}}
        assert route == "get_coin_records_by_names", route
        self.calls["by_names"] += 1
        return {"coin_records": [rec(self.parents[bytes32.fromhex(n.removeprefix("0x"))], True, 7)
                                 for n in body["names"] if bytes32.fromhex(n.removeprefix("0x")) in self.parents]}

    def coin_record(self, coin_id):
        self.calls["coin_record"] += 1
        p = self.parents.get(bytes32(coin_id))
        return rec(p, True, 7) if p else None

    def puzzle_and_solution(self, coin_id, height):
        self.calls["spend_parsed"] += 1
        return Program.from_bytes(bytes.fromhex(self.reveals[bytes(coin_id)])), Program.to(0)


def build():
    """Per asset: 3 coins from the wallet's own change, 2 from a swap payout, 3 sent by another
    wallet (only its spend proves it) and a large mint fresh from the TAIL (proves nothing)."""
    coins, parents, spends, expect = [], [], {}, {}
    for a, asset in enumerate(ASSETS):
        own = construct_cat_puzzle(CAT_MOD, asset, INNERS[a % 3])
        settle = construct_cat_puzzle(CAT_MOD, asset, OFFER_MOD)
        other = construct_cat_puzzle(CAT_MOD, asset, OTHER_INNER)
        plan = [(own, INNERS[a % 3]), (own, INNERS[a % 3]), (own, INNERS[a % 3]), (settle, OFFER_MOD), (settle, OFFER_MOD),
                (other, OTHER_INNER), (other, OTHER_INNER), (other, OTHER_INNER)]
        for i, (puzzle, inner) in enumerate(plan):
            parent = Coin(bytes32(bytes([a, i]) + b"\x00" * 30), puzzle.get_tree_hash(), 10_000 + i)
            parents.append(parent)
            if inner is OTHER_INNER:
                spends[bytes(parent.name())] = puzzle
            # Equal amounts in pairs, one pair across the cut at five: which are kept must not hang on the node's order.
            coins.append(Coin(parent.name(), own.get_tree_hash(), 1_000 - ((i + 1) // 2)))
            expect[coins[-1].name()] = (inner.get_tree_hash(), parent)
        mint = Coin(bytes32(bytes([a, 0xff]) + b"\x00" * 30), TAIL_PARENT.get_tree_hash(), 5)
        parents.append(mint)
        spends[bytes(mint.name())] = TAIL_PARENT
        coins.append(Coin(mint.name(), own.get_tree_hash(), 50_000))
    return coins, parents, spends, expect


def by_coin(out):
    return {Coin(bytes32.fromhex(e["coin"]["parent_coin_info"]), bytes32.fromhex(e["coin"]["puzzle_hash"]), e["coin"]["amount"]).name(): e
            for entries in out.values() for e in entries}


def main() -> int:
    coins, parents, spends, expect = build()
    pubkeys = [bytes(k).hex() for k in KEYS]
    assets = [a.hex() for a in ASSETS]

    print("the default path (a creation, a swap, an offer): three assets, the largest five coins of each")
    node = FakeNode(coins, parents, spends)
    out = wc.cat_coins_for_keys(node, pubkeys, assets)
    found = by_coin(out)
    check("one address read for all three assets", node.calls["by_puzzle_hashes"] == 1, str(node.calls))
    check("one read of every parent, no single-record reads", node.calls["by_names"] == 1 and node.calls["coin_record"] == 0, str(node.calls))
    # The five kept per asset: the mint (largest), coins 0-2 (own) and one of the settlement pair
    # 3/4 (equal amounts); only the mint's spend is unknown by puzzle hash, so one an asset is fetched.
    check("a spend fetched only where the puzzle hash does not tell (the mint, one an asset)",
          node.calls["spend_json"] == 3 and node.calls["spend_parsed"] == 0, str(node.calls))
    check("fetched side by side, off the calling thread", node.most_in_flight > 1 and threading.get_ident() not in node.spend_threads,
          f"{node.most_in_flight} at once")
    check("four coins an asset proved, the mint skipped", all(len(out[a]) == 4 for a in assets), str({a[:6]: len(v) for a, v in out.items()}))
    wrong = [cid for cid, e in found.items()
             if e["lineage_proof"] != {"parent_name": expect[cid][1].parent_coin_info.hex(), "inner_puzzle_hash": expect[cid][0].hex(),
                                       "amount": int(expect[cid][1].amount)}]
    check("each lineage names its parent's coin, inner puzzle hash and amount", not wrong and len(found) == 12, f"{len(wrong)} wrong")
    reversed_node = FakeNode(list(reversed(coins)), parents, spends)
    check("the coins kept do not depend on the order the node lists them in",
          wc.cat_coins_for_keys(reversed_node, pubkeys, assets) == out)

    print("every coin (a combine): the parents nothing else identifies, all at once")
    node = FakeNode(coins, parents, spends)
    out = wc.cat_coins_for_keys(node, pubkeys, assets, all_coins=True)
    found = by_coin(out)
    check("one address read, one parent read", node.calls["by_puzzle_hashes"] == 1 and node.calls["by_names"] == 1, str(node.calls))
    check("12 spends fetched (3 other-wallet parents and the mint, per asset), none on the calling thread",
          node.calls["spend_json"] == 12 and node.calls["spend_parsed"] == 0, str(node.calls))
    check("side by side", node.most_in_flight > 1, f"{node.most_in_flight} at once")
    wrong = [cid for cid, e in found.items() if e["lineage_proof"]["inner_puzzle_hash"] != expect[cid][0].hex()]
    check("all 24 provable coins, each lineage the parent's true inner puzzle hash", len(found) == 24 and not wrong, f"{len(found)}, {len(wrong)} wrong")

    print("a spend that does not come back is asked again, on the calling thread")
    node = FakeNode(coins, parents, spends)
    dropped = next(iter(spends))
    real_rpc = node.rpc

    def flaky(route, body):
        if route == "get_puzzle_and_solution" and bytes.fromhex(body["coin_id"]) == dropped:
            raise RuntimeError("node dropped it")
        return real_rpc(route, body)
    node.rpc = flaky
    again = wc.cat_coins_for_keys(node, pubkeys, assets, all_coins=True)
    check("the same coins and proofs, one spend read again", again == out and node.calls["spend_parsed"] == 1, str(node.calls))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} batched wallet-coin checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
