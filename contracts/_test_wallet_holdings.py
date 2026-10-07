#!/usr/bin/env python3
"""wallet_holdings.py: the website's balances from the chain, without the wallet.

The address arithmetic is the whole claim, so it is checked against the real
puzzle construction: a CAT address computed from hashes alone must equal
construct_cat_puzzle(...).get_tree_hash(), or a balance would silently read zero.
The node is a stub that answers from a table, so the batching and the sums are
checked without a network.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs import AugSchemeMPL
from chia_rs.sized_bytes import bytes32
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key

import wallet_holdings as wh
from multisig_tool import MultisigError

results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))


keys = [AugSchemeMPL.key_gen(bytes([i]) * 32).get_g1() for i in range(1, 6)]
key_hex = [bytes(k).hex() for k in keys]
inners = [puzzle_for_synthetic_public_key(k) for k in keys]
assets = [bytes32(bytes([0x10 + i]) * 32) for i in range(3)]

# The arithmetic, against the real construction.
mismatch = [(a.hex()[:6], i) for a in assets for i, inner in enumerate(inners)
            if wh.cat_puzzle_hash(a, inner.get_tree_hash()) != construct_cat_puzzle(CAT_MOD, a, inner).get_tree_hash()]
check("a CAT address from hashes equals the constructed CAT puzzle's hash, for every asset and key",
      not mismatch, f"{len(assets) * len(inners)} pairs, {len(mismatch)} differ")
check("the inner hashes are the standard synthetic-key addresses",
      wh.inner_hashes(key_hex) == [p.get_tree_hash() for p in inners])
check("a malformed key is skipped, not fatal", wh.inner_hashes(["zz", key_hex[0], "00" * 48]) == [inners[0].get_tree_hash()])


class StubNode:
    """Answers coin records by puzzle hash from a table; records each call's size."""

    def __init__(self, coins):
        self.coins = coins          # puzzle_hash -> list of (amount, spent)
        self.calls = []

    def coin_records_by_puzzle_hashes(self, puzzle_hashes, include_spent=False):
        self.calls.append(len(puzzle_hashes))
        out = []
        for ph in puzzle_hashes:
            for n, (amount, spent) in enumerate(self.coins.get(bytes32(ph), [])):
                out.append({"coin": {"parent_coin_info": "0x" + bytes([n + 1]).hex() * 32,
                                     "puzzle_hash": "0x" + bytes(ph).hex(), "amount": amount},
                            "spent": spent, "confirmed_block_index": 1, "spent_block_index": 0})
        return out


xch0 = inners[0].get_tree_hash()
cat_a_k1 = wh.cat_puzzle_hash(assets[0], inners[1].get_tree_hash())
cat_a_k3 = wh.cat_puzzle_hash(assets[0], inners[3].get_tree_hash())
cat_b_k2 = wh.cat_puzzle_hash(assets[1], inners[2].get_tree_hash())
stranger = wh.cat_puzzle_hash(bytes32(b"\x77" * 32), inners[1].get_tree_hash())
node = StubNode({
    xch0: [(1000, False), (5, True)],          # the spent coin must not count
    cat_a_k1: [(40, False)],
    cat_a_k3: [(2, False), (3, False)],
    cat_b_k2: [(7, True)],                     # spent: zero
    stranger: [(999, False)],                  # an asset nobody asked about
})
out = wh.holdings(node, key_hex, [assets[0].hex(), "0x" + assets[1].hex(), assets[2].hex(), "00" * 32, "nothex"])
check("XCH sums only unspent coins at the wallet's addresses", out["xch"] == {"amount": 1000, "coins": 1}, str(out["xch"]))
check("a CAT sums across every address that holds it", out["cats"][assets[0].hex()] == {"amount": 45, "coins": 3},
      str(out["cats"].get(assets[0].hex())))
check("a spent CAT coin counts for nothing", out["cats"][assets[1].hex()] == {"amount": 0, "coins": 0})
check("an asset asked about and not held reads zero", out["cats"][assets[2].hex()] == {"amount": 0, "coins": 0})
check("an asset not asked about is never reported", bytes32(b"\x77" * 32).hex() not in out["cats"])
check("the native id and garbage are not treated as CATs", set(out["cats"]) == {a.hex() for a in assets})
check("addresses reports the keys used", out["addresses"] == 5)

# Batching: 3 assets x 400 keys = 1200 CAT addresses -> two node calls.
many = [bytes(AugSchemeMPL.key_gen(i.to_bytes(32, "big")).get_g1()).hex() for i in range(1, 401)]
batch_node = StubNode({})
wh.holdings(batch_node, many, [a.hex() for a in assets])
check(f"puzzle hashes go to the node in batches of {wh.BATCH}",
      batch_node.calls == [400, 1000, 200], str(batch_node.calls))

class FlakyNode(StubNode):
    """Refuses its first call, as coinset did with a 503 under parallel load."""

    def __init__(self, coins):
        super().__init__(coins)
        self.refused = 0

    def coin_records_by_puzzle_hashes(self, puzzle_hashes, include_spent=False):
        if self.refused == 0:
            self.refused += 1
            raise MultisigError("node get_coin_records_by_puzzle_hashes HTTP 503: upstream connect error")
        return super().coin_records_by_puzzle_hashes(puzzle_hashes, include_spent)


wh.RETRY_DELAY_S = 0
flaky = FlakyNode({xch0: [(1000, False)]})
check("a batch the node refuses once is asked again, not lost",
      wh.holdings(flaky, key_hex, [])["xch"]["amount"] == 1000 and flaky.refused == 1)

try:
    wh.holdings(StubNode({}), ["nope"], [])
    check("no usable key is an error, not an empty wallet", False)
except MultisigError:
    check("no usable key is an error, not an empty wallet", True)

# -- Discovery: every CAT the wallet holds, found by hint (2026-10-06, mainnet: the
#    site knew no tokens, so a creator's wallet showed none).
from chia.types.blockchain_format.program import Program  # noqa: E402


class HintNode:
    """Answers hint lookups and parent spends from a table of (hint, outer ph, amount, parent puzzle)."""

    def __init__(self, coins):
        self.coins = coins
        self.spends = 0
        # Serialized here, on the main thread: the code under test calls rpc from
        # worker threads, and chia's puzzle objects may not be touched from another.
        self.reveals = [bytes(entry[3]).hex() for entry in coins]

    def rpc(self, route, body):
        if route == "get_puzzle_and_solution":
            self.spends += 1
            n = bytes.fromhex(body["coin_id"].removeprefix("0x"))[0] - 1
            return {"success": True, "coin_solution": {"puzzle_reveal": self.reveals[n]}}
        assert route == "get_coin_records_by_hints", route
        hints = {bytes32.fromhex(h.removeprefix("0x")) for h in body["hints"]}
        out = []
        for n, (hint, ph, amount, _) in enumerate(self.coins):
            if hint in hints:
                out.append({"coin": {"parent_coin_info": "0x" + bytes([n + 1]).hex() * 32, "puzzle_hash": "0x" + bytes(ph).hex(),
                                     "amount": amount}, "spent": False, "confirmed_block_index": 10 + n})
        return {"success": True, "coin_records": out}



h1, h3 = inners[1].get_tree_hash(), inners[3].get_tree_hash()
hint_node = HintNode([
    (h1, wh.cat_puzzle_hash(assets[0], h1), 40, construct_cat_puzzle(CAT_MOD, assets[0], inners[1])),
    (h1, wh.cat_puzzle_hash(assets[0], h1), 5, construct_cat_puzzle(CAT_MOD, assets[0], inners[1])),   # same address: one lookup
    (h3, wh.cat_puzzle_hash(assets[1], h3), 7, construct_cat_puzzle(CAT_MOD, assets[1], inners[3])),
    (h1, h1, 999, Program.to(1)),                                                     # plain XCH hinted to itself
    (h3, bytes32(b"\x55" * 32), 1, Program.to(1)),                                    # hinted, not a CAT (an NFT, say)
    (h1, bytes32(b"\x66" * 32), 3, construct_cat_puzzle(CAT_MOD, assets[2], inners[1])),  # a CAT parent, not at our address
])
found = wh.discover_cats(hint_node, [p.get_tree_hash() for p in inners])
check("discovery finds every CAT the keys' addresses hold, by hint, without being told which",
      found == {assets[0].hex(): {"amount": 45, "coins": 2}, assets[1].hex(): {"amount": 7, "coins": 1}}, str(found))
check("  one parent lookup per CAT address, not per coin", hint_node.spends == 4, f"{hint_node.spends} lookups")
check("  plain XCH, a hinted non-CAT and a CAT not at the wallet's address are not counted",
      assets[2].hex() not in found and len(found) == 2)

# One asset at many of the wallet's addresses costs ONE parent lookup: the first names
# the asset, and its address at every other inner puzzle is then arithmetic (45 s for a
# 55-token wallet on the live router when every address was looked up, 2026-10-06).
spread = HintNode([(inner.get_tree_hash(), wh.cat_puzzle_hash(assets[0], inner.get_tree_hash()), 10 + i,
                    construct_cat_puzzle(CAT_MOD, assets[0], inner)) for i, inner in enumerate(inners)])
spread_found = wh.discover_cats(spread, [p.get_tree_hash() for p in inners])
check("one asset spread over every address takes at most one wave of lookups, not one per address",
      spread_found == {assets[0].hex(): {"amount": sum(10 + i for i in range(len(inners))), "coins": len(inners)}}
      and spread.spends <= wh.PARENT_LOOKUP_WORKERS, f"{spread.spends} lookups for {len(inners)} addresses")

import tempfile  # noqa: E402
cache_file = pathlib.Path(tempfile.mkdtemp()) / "cat-address-assets.json"
first = HintNode(list(hint_node.coins))
wh.discover_cats(first, [p.get_tree_hash() for p in inners], cache_file)
again = HintNode(list(hint_node.coins))
cached = wh.discover_cats(again, [p.get_tree_hash() for p in inners], cache_file)
check("a later request answers from the cache, with no parent lookups", again.spends == 0 and cached == found,
      f"{again.spends} lookups")

passed = sum(results)
print(f"\n{passed}/{len(results)} wallet-holdings checks passed")
sys.exit(0 if passed == len(results) else 1)
