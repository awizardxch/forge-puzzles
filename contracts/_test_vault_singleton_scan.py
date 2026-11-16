"""A lock's NFT/DID scan reads the chain in batches, not one coin at a time.

The Assets tab waited on this scan for every lock, and the scan made two serial
node calls per hinted coin: the Forge Protocol Fee lock's 12 hinted coins cost 27
calls, 36 s to over 90 s on the router (owner, 2026-10-24: "why does it take soo
long for the holdings of a single multisig wallet to show up?"). Now:

* coin records come from ``get_coin_records_by_names``, 100 ids a call, spent
  ones included, and a record the batch did not return is still asked for;
* the parents' spends (which have no batch form) are fetched side by side, but
  only as JSON: chia's ``Program`` panics when a thread that did not make it
  touches it, so every reveal is parsed on the calling thread;
* the scan answers exactly what classifying each coin one by one answers, with
  no per-coin record lookups left;
* a DID the lock launched and has not moved is still found, from batched reads.

On 2026-10-24 the old and new scans also answered byte-identically for the three
mainnet locks (Forge Protocol Fee, the DAO lock, the dev lock), 3.6-4.5x faster
from here, with 41-45% fewer node calls.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import multisig_tool as tool
import vault_tool as vt

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if ok:
        passed += 1
    else:
        failed += 1


def record(coin: Coin, spent_at: int = 0, confirmed: int = 10) -> dict:
    return {
        "coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(), "puzzle_hash": "0x" + coin.puzzle_hash.hex(), "amount": int(coin.amount)},
        "spent": spent_at > 0,
        "spent_block_index": spent_at,
        "confirmed_block_index": confirmed,
    }


class StubNode:
    """Answers from fixed records and spends; counts every route and which threads asked."""

    def __init__(self, records: list[dict], spends: dict[bytes32, Program], hinted: list[dict]) -> None:
        self.records = {tool.record_coin(r).name(): r for r in records}
        self.spends = spends
        self.hinted = hinted
        self.calls: dict[str, int] = {}
        self.batch_sizes: list[int] = []
        self.spend_threads: set[int] = set()
        self.lock = threading.Lock()

    def _count(self, route: str) -> None:
        with self.lock:
            self.calls[route] = self.calls.get(route, 0) + 1

    def rpc(self, route: str, body: dict) -> dict:
        self._count(route)
        if route == "get_coin_records_by_names":
            self.batch_sizes.append(len(body["names"]))
            assert body.get("include_spent_coins") is True
            wanted = [bytes32.fromhex(name.removeprefix("0x")) for name in body["names"]]
            return {"coin_records": [self.records[i] for i in wanted if i in self.records]}
        if route == "get_coin_record_by_name":
            return {"coin_record": self.records.get(bytes32.fromhex(body["name"]))}
        if route == "get_puzzle_and_solution":
            with self.lock:
                self.spend_threads.add(threading.get_ident())
            reveal = self.spends.get(bytes32.fromhex(body["coin_id"]))
            if reveal is None:
                return {"success": False}
            return {"coin_solution": {"puzzle_reveal": bytes(reveal).hex(), "solution": "80"}}
        raise AssertionError(f"unexpected route {route}")

    def coin_record(self, coin_id: bytes32) -> dict | None:
        return self.rpc("get_coin_record_by_name", {"name": coin_id.hex()}).get("coin_record")

    def puzzle_and_solution(self, coin_id: bytes32, height: int):
        return tool.Node.puzzle_and_solution(self, coin_id, height)

    def puzzle_reveal(self, coin_id: bytes32, height: int) -> Program:
        return self.puzzle_and_solution(coin_id, height)[0]

    def coin_records_by_hint(self, hint: bytes32) -> list[dict]:
        self._count("get_coin_records_by_hint")
        return self.hinted

    def coin_records_by_puzzle_hashes(self, hashes, include_spent=False):
        self._count("get_coin_records_by_puzzle_hashes")
        wanted = set(hashes)
        return [r for r in self.records.values() if tool.record_coin(r).puzzle_hash in wanted and (include_spent or not r["spent"])]


lock_launcher = bytes32(b"\x11" * 32)
deposit_ph = vt.deposit_puzzle_hash(lock_launcher)

# -- Batched reads ------------------------------------------------------------------
print("records by names")
many = [Coin(bytes32(i.to_bytes(32, "big")), bytes32(b"\x22" * 32), uint64(3)) for i in range(1, 251)]
node = StubNode([record(c, spent_at=5 if i % 2 else 0) for i, c in enumerate(many)], {}, [])
got = vt._records_by_names(node, [c.name() for c in many] + [many[0].name()])
check("250 ids are read 100 at a time, a repeated id asked once", node.batch_sizes == [100, 100, 50], str(node.batch_sizes))
check("spent records are returned with unspent ones", len(got) == 250 and sum(1 for r in got.values() if r["spent"]) == 125)

prefetched = vt._Prefetched(node, {many[0].name(): got[many[0].name()]})
before = node.calls.get("get_coin_record_by_name", 0)
prefetched.coin_record(many[0].name())
check("a prefetched record is answered without the node", node.calls.get("get_coin_record_by_name", 0) == before)
prefetched.coin_record(many[1].name())
check("a record the batch did not return is still asked for, not reported absent",
      node.calls.get("get_coin_record_by_name", 0) == before + 1)

# -- The hint scan: same answer as one coin at a time, from batched reads ------------
print("\nhinted singletons")
reveals = [Program.to([1, i]) for i in range(1, 13)]           # not NFTs or DIDs: classified unknown
parents = [Coin(bytes32(bytes([0x30 + i]) * 32), reveal.get_tree_hash(), uint64(1)) for i, reveal in enumerate(reveals)]
hinted_coins = [Coin(parent.name(), bytes32(bytes([0x60 + i]) * 32), uint64(1)) for i, parent in enumerate(parents)]
hinted = [record(c) for c in hinted_coins]
hinted.append(record(Coin(bytes32(b"\x55" * 32), bytes32(b"\x56" * 32), uint64(2))))   # even: a payment
hinted.append(record(Coin(bytes32(b"\x57" * 32), deposit_ph, uint64(1))))               # at the address: XCH
records = [record(p, spent_at=20 + i) for i, p in enumerate(parents)] + hinted
spends = {p.name(): r for p, r in zip(parents, reveals)}

one_by_one = StubNode(records, spends, hinted)
expected = [vt._singleton_kind(one_by_one, r) for r in hinted[:12]]
node = StubNode(records, spends, hinted)
main = threading.get_ident()
found = vt.scan_singletons(node, lock_launcher, deposit_ph)
hinted_found = [entry for entry in found if entry.get("hinted")]
check("the 12 odd hinted coins are classified; the even payment and the address's own coin are not",
      [entry["coin_id"] for entry in hinted_found] == [c.name().hex() for c in hinted_coins])
check("each answer is what classifying that coin alone gives",
      [{k: v for k, v in e.items() if k in ("kind", "reason", "launcher_id")} for e in hinted_found]
      == [{k: v for k, v in (x or {}).items() if k in ("kind", "reason", "launcher_id")} for x in expected])
check("no per-coin record lookups: the parents came in one batch",
      node.calls.get("get_coin_record_by_name", 0) == 0 and node.calls.get("get_coin_records_by_names", 0) >= 1,
      str(node.calls))
check("each parent's spend is fetched once", node.calls.get("get_puzzle_and_solution", 0) == 12, str(node.calls))
check("the spends are fetched on worker threads", bool(node.spend_threads) and main not in node.spend_threads)

# -- A launched DID that no hint points at yet ----------------------------------------
print("\nlaunched DIDs")
payers = [Coin(bytes32(bytes([0x80 + i]) * 32), deposit_ph, uint64(1000 + i)) for i in range(5)]
launchers = [Coin(p.name(), vt.SINGLETON_LAUNCHER_HASH, uint64(vt.SINGLETON_AMOUNT)) for p in payers]
did_launcher = launchers[2].name()
eve = vt.did_eve_coin(lock_launcher, did_launcher)
records = [record(p, spent_at=30 + i) for i, p in enumerate(payers)]
records.append(record(launchers[2], spent_at=33))              # only one payer launched anything
records.append(record(eve))
node = StubNode(records, {}, [])
dids = vt._launched_dids(node, lock_launcher, deposit_ph)
check("the lock's eve DID is found from its launch", [d["launcher_id"] for d in dids] == [did_launcher.hex()] and dids[0]["stage"] == "eve",
      str([d.get("launcher_id") for d in dids]))
check("launchers and eves are read in batches, none one at a time",
      node.calls.get("get_coin_record_by_name", 0) == 0 and node.calls.get("get_coin_records_by_names", 0) == 2, str(node.calls))

# -- The kinds cache: a coin's kind never changes, so a scan reads only new coins ----
print("\nkinds cache")
cache_dir = pathlib.Path(tempfile.mkdtemp())
cache_file = cache_dir / "kinds.json"
lost = Coin(bytes32(b"\x58" * 32), bytes32(b"\x59" * 32), uint64(1))      # its parent is not on chain
hinted_more = hinted + [record(lost)]
records_more = [record(p, spent_at=20 + i) for i, p in enumerate(parents)] + hinted_more
node = StubNode(records_more, spends, hinted_more)
cold = vt.scan_singletons(node, lock_launcher, deposit_ph, cache_file)
saved = json.loads(cache_file.read_text(encoding="utf-8"))
check("the first scan keeps what the chain settled, not a read that failed",
      len(saved) == 12 and lost.name().hex() not in saved, f"{len(saved)} kept")
node = StubNode(records_more, spends, hinted_more)
warm = vt.scan_singletons(node, lock_launcher, deposit_ph, cache_file)
check("a scan from the cache answers the same", warm == cold)
check("and reads no spends for the coins it knows, only the one parent it does not",
      node.calls.get("get_puzzle_and_solution", 0) == 0 and node.calls.get("get_coin_records_by_names", 0) == 1, str(node.calls))
check("a transient answer is not cached: the lost coin is still asked about",
      [e for e in warm if e["coin_id"] == lost.name().hex()][0].get("reason") == "parent spend not found")
check("only settled kinds are cacheable",
      vt._cacheable_kind({"kind": "nft", "launcher_id": "aa"}) and vt._cacheable_kind({"kind": "unknown", "reason": "not an NFT or DID singleton"})
      and not vt._cacheable_kind({"kind": "unknown", "reason": "parent reveal unavailable"})
      and not vt._cacheable_kind({"kind": "did", "name": None}) and vt._cacheable_kind({"kind": "did", "name": "Wizard"}))
unwritable = cache_dir / "kinds.json" / "nested.json"                   # a file where a directory must be
node = StubNode(records_more, spends, hinted_more)
check("a cache that cannot be written costs time, never the scan",
      vt.scan_singletons(node, lock_launcher, deposit_ph, unwritable) == cold)

print(f"\n{passed}/{passed + failed} singleton scan checks passed")
sys.exit(1 if failed else 0)
