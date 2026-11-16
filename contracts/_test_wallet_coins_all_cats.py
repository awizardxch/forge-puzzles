#!/usr/bin/env python3
"""Every CAT coin of an asset from the chain, lineage proved with few node reads
(wallet_coins.cat_coins_for_keys(..., all_coins=True)).

Mainnet, 2026-10-23: the website asked the wallet for 196 LOVE coins over WalletConnect before
a combine, a burst its relay refused ("Failed to publish payload"). The coins now come from the
chain. Two node reads per coin would be ~400 from the router's host, so parents' records are
batched and a parent's inner puzzle hash is read off its puzzle hash where it is one this asset
is known to sit at. Pinned with a fake node holding real CAT coins whose parents are:
  - one of the wallet's own CAT addresses (change)        -> proved, no spend fetched
  - the offer settlement puzzle (a swap's payout)         -> proved, no spend fetched
  - another wallet's CAT address                          -> proved from the parent's spend
  - a TAIL mint (not a CAT of this asset)                 -> skipped
and every proved coin spends: a combine of them validates, which it cannot with a wrong
lineage proof (the CAT layer checks the parent).
Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import coin_combine as cc  # noqa: E402
import wallet_coins as wc  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD  # noqa: E402
from chia_rs import Coin, PrivateKey  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

ASSET = bytes32(b"\xd7" * 32)
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))


SK = PrivateKey.from_bytes(bytes([0x62]) * 32)
INNER = puzzle_for_synthetic_public_key(SK.get_g1())
OWN = construct_cat_puzzle(CAT_MOD, ASSET, INNER)
OTHER_INNER = puzzle_for_synthetic_public_key(PrivateKey.from_bytes(bytes([0x63]) * 32).get_g1())
OTHER = construct_cat_puzzle(CAT_MOD, ASSET, OTHER_INNER)
SETTLE = construct_cat_puzzle(CAT_MOD, ASSET, OFFER_MOD)
TAIL_PARENT = Program.to(1)   # some non-CAT puzzle a mint came from


def rec(coin: Coin, spent: bool, height: int = 0) -> dict:
    return {"coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(), "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
                     "amount": int(coin.amount)}, "spent": spent, "spent_block_index": height, "confirmed_block_index": 1}


class FakeNode:
    def __init__(self, coins, parents, spends):
        self.coins, self.parents, self.spends = coins, parents, spends
        self.calls = {"by_names": 0, "puzzle_and_solution": 0, "coin_record": 0}
        self.lock = threading.Lock()
        # As hex, made here: a Program touched on a worker thread panics (pyo3 "unsendable").
        self.reveals = {key: bytes(puzzle).hex() for key, puzzle in spends.items()}

    def coin_records_by_puzzle_hashes(self, hashes):
        wanted = set(bytes(h) for h in hashes)
        return [rec(c, False) for c in self.coins if bytes(c.puzzle_hash) in wanted]

    def rpc(self, route, body):
        if route == "get_puzzle_and_solution":   # the spends wallet_coins prefetches side by side
            with self.lock:
                self.calls["puzzle_and_solution"] += 1
            return {"coin_solution": {"puzzle_reveal": self.reveals[bytes.fromhex(body["coin_id"])], "solution": "80"}}
        assert route == "get_coin_records_by_names", route
        self.calls["by_names"] += 1
        names = {n.removeprefix("0x") for n in body["names"]}
        return {"coin_records": [rec(p, True, 7) for p in self.parents if p.name().hex() in names]}

    def coin_record(self, coin_id):
        self.calls["coin_record"] += 1
        return next((rec(p, True, 7) for p in self.parents if p.name() == bytes32(coin_id)), None)

    def puzzle_and_solution(self, coin_id, height):
        self.calls["puzzle_and_solution"] += 1
        return self.spends[bytes(coin_id)], Program.to(0)


def main() -> int:
    coins, parents, spends, expect = [], [], {}, {}
    # 1. change: the parent sat at the wallet's own CAT address
    for i in range(120):
        parent = Coin(bytes32(bytes([1, i % 256, i // 256]) + b"\x00" * 29), OWN.get_tree_hash(), 1_000 + i)
        parents.append(parent)
        coins.append(Coin(parent.name(), OWN.get_tree_hash(), 500 + i))
        expect[coins[-1].name()] = INNER.get_tree_hash()
    # 2. a swap payout: the parent was the CAT offer settlement coin
    for i in range(60):
        parent = Coin(bytes32(bytes([2, i]) + b"\x00" * 30), SETTLE.get_tree_hash(), 2_000 + i)
        parents.append(parent)
        coins.append(Coin(parent.name(), OWN.get_tree_hash(), 700 + i))
        expect[coins[-1].name()] = OFFER_MOD.get_tree_hash()
    # 3. sent from another wallet: only its spend tells its inner puzzle
    for i in range(16):
        parent = Coin(bytes32(bytes([3, i]) + b"\x00" * 30), OTHER.get_tree_hash(), 3_000 + i)
        parents.append(parent)
        spends[bytes(parent.name())] = OTHER
        coins.append(Coin(parent.name(), OWN.get_tree_hash(), 900 + i))
        expect[coins[-1].name()] = OTHER_INNER.get_tree_hash()
    # 4. fresh from its TAIL: not provable this way
    mint_parent = Coin(bytes32(b"\x04" * 32), TAIL_PARENT.get_tree_hash(), 5)
    parents.append(mint_parent)
    spends[bytes(mint_parent.name())] = TAIL_PARENT
    coins.append(Coin(mint_parent.name(), OWN.get_tree_hash(), 5))

    node = FakeNode(coins, parents, spends)
    out = wc.cat_coins_for_keys(node, [bytes(SK.get_g1()).hex()], [ASSET.hex()], all_coins=True)[ASSET.hex()]
    by_id = {Coin(bytes32.fromhex(e["coin"]["parent_coin_info"]), bytes32.fromhex(e["coin"]["puzzle_hash"]), e["coin"]["amount"]).name(): e
             for e in out}

    print("every CAT coin of the asset, its lineage from the chain")
    check("all 196 provable coins come back; the TAIL mint is skipped", len(out) == 196, f"{len(out)}")
    wrong = [cid for cid, inner in expect.items() if by_id.get(cid, {}).get("lineage_proof", {}).get("inner_puzzle_hash") != inner.hex()]
    check("each lineage names the parent's true inner puzzle hash (own address, settlement, other wallet)", not wrong,
          f"{len(wrong)} wrong")
    check("parents read 100 to a call: 2 calls for 197 coins, no single-record reads",
          node.calls["by_names"] == 2 and node.calls["coin_record"] == 0, str(node.calls))
    check("a spend is fetched only for the parents nothing else identifies (16 others + the mint)",
          node.calls["puzzle_and_solution"] == 17, str(node.calls))
    plain = wc.cat_coins_for_keys(FakeNode(coins, parents, spends), [bytes(SK.get_g1()).hex()], [ASSET.hex()])[ASSET.hex()]
    check("without all_coins, still only the largest few (a creation's need)", len(plain) == wc.MAX_CATS_PER_ASSET, f"{len(plain)}")

    print("the coins spend: a combine of them validates (a wrong lineage could not)")
    built = cc.build({"action": "build", "network": "testnet11", "asset_id": ASSET.hex(), "own_puzzle_hash": INNER.get_tree_hash().hex(),
                      "coins": [e for e in out if e["lineage_proof"]["inner_puzzle_hash"] == INNER.get_tree_hash().hex()][:40]
                      + [e for e in out if e["lineage_proof"]["inner_puzzle_hash"] == OFFER_MOD.get_tree_hash().hex()][:20]
                      + [e for e in out if e["lineage_proof"]["inner_puzzle_hash"] == OTHER_INNER.get_tree_hash().hex()][:10],
                      "network_fee": "0"})
    check("70 of them (all three kinds of parent) combine into a bundle that passes consensus", built.get("success") is True and built["cost"] > 0,
          f"cost {built.get('cost')}")
    broken = [dict(e) for e in out[:3]]
    broken[1] = {**broken[1], "lineage_proof": {**broken[1]["lineage_proof"], "inner_puzzle_hash": OFFER_MOD.get_tree_hash().hex()}}
    try:
        cc.build({"action": "build", "network": "testnet11", "asset_id": ASSET.hex(), "own_puzzle_hash": INNER.get_tree_hash().hex(),
                  "coins": broken, "network_fee": "0"})
        check("and one wrong lineage proof is refused by consensus", False)
    except Exception as exc:  # noqa: BLE001
        check("and one wrong lineage proof is refused by consensus", True, str(exc)[:80])

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} all-cats checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
