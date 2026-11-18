#!/usr/bin/env python3
"""forge_names: hinted dust cannot stall name resolution (external review 2026-11-16, summary
note "hinted dust can stall pool adoption").

Anyone can send 1-mojo coins to a pool deployer's address hinted with the pool's launcher id.
forge_names.renames used to look up each such coin's parent with its own RPC, so enough dust
pushed the index script past the router's 240 s kill and a new pool was never listed. Against a
fake node that counts calls:

  - 3,000 dust coins cost a bounded number of node calls (batched parent lookups), not 3,000;
  - a real rename among 500 newer dust coins is still found;
  - with more dust than MAX_HINTED_COINS newer than the rename, the name falls back to the
    genesis memo -- bounded, and the pool still resolves;
  - a node without the batch route is asked one coin at a time, capped;
  - a slow node exhausts the pool's time budget and resolve raises NameBudgetExceeded promptly
    (the index scripts catch it and the record's name stands in);
  - the recomputed coin id matches chia's Coin.name() (the batch answers are keyed by it).

Exit 0 when every check passes, 1 otherwise. Run before the fix, the call-count and budget
checks failed.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import forge_names  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


LAUNCHER = "1a" * 32
DEPLOYER = "de" * 32
STRANGER = "5e" * 32
LAUNCHER_PARENT = Coin(bytes32(b"\x01" * 32), bytes32.fromhex(DEPLOYER), uint64(5_000))
GENESIS_SOLUTION = Program.to([[51, bytes.fromhex(forge_names.LAUNCHER_HASH_HEX), 1, [b"Genesis", b"GEN"]]])


def rec(coin: Coin, height: int, spent: bool = False, spent_height: int = 0) -> dict:
    return {"coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(), "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
                     "amount": int(coin.amount)},
            "confirmed_block_index": height, "spent": spent, "spent_block_index": spent_height}


class FakeNode:
    """Answers forge_names' five routes from in-memory coins; counts every call."""

    def __init__(self, dust: int, rename_height: int, dust_from: int, batch: bool = True, delay: float = 0.0):
        self.calls: dict[str, int] = {}
        self.batch, self.delay = batch, delay
        self.by_id: dict[str, dict] = {}
        self.spends: dict[str, Program] = {}
        self.hinted: list[dict] = []
        self.by_id[LAUNCHER_PARENT.name().hex()] = rec(LAUNCHER_PARENT, 10, spent=True, spent_height=11)
        self.spends[LAUNCHER_PARENT.name().hex()] = GENESIS_SOLUTION
        # the real rename: the deployer spends its own coin, creating a hinted coin at its address
        own = Coin(bytes32(b"\x02" * 32), bytes32.fromhex(DEPLOYER), uint64(777))
        self.by_id[own.name().hex()] = rec(own, rename_height - 1, spent=True, spent_height=rename_height)
        self.spends[own.name().hex()] = Program.to([[51, bytes.fromhex(DEPLOYER), 1, [bytes.fromhex(LAUNCHER), b"Renamed", b"RNM"]]])
        renamed = Coin(own.name(), bytes32.fromhex(DEPLOYER), uint64(1))
        self.hinted.append(rec(renamed, rename_height))
        # dust: a stranger's coins, each creating one hinted 1-mojo coin at the deployer's address
        for i in range(dust):
            funder = Coin(bytes32(i.to_bytes(4, "big") * 8), bytes32.fromhex(STRANGER), uint64(1))
            self.by_id[funder.name().hex()] = rec(funder, dust_from + i - 1, spent=True, spent_height=dust_from + i)
            self.hinted.append(rec(Coin(funder.name(), bytes32.fromhex(DEPLOYER), uint64(1)), dust_from + i))

    def __call__(self, node, route, body, timeout=60):
        self.calls[route] = self.calls.get(route, 0) + 1
        if self.delay:
            time.sleep(self.delay)
        if route == "get_coin_record_by_name":
            return {"coin_record": self.by_id.get(body["name"].removeprefix("0x"))}
        if route == "get_coin_records_by_names":
            if not self.batch:
                return {"success": False, "error": "unknown route"}
            return {"coin_records": [self.by_id[n.removeprefix("0x")] for n in body["names"] if n.removeprefix("0x") in self.by_id]}
        if route == "get_coin_records_by_hint":
            return {"coin_records": list(self.hinted)}
        if route == "get_puzzle_and_solution":
            solution = self.spends[body["coin_id"].removeprefix("0x")]
            return {"coin_solution": {"puzzle_reveal": bytes(Program.to(1)).hex(), "solution": bytes(solution).hex()}}
        raise AssertionError(route)

    @property
    def total(self) -> int:
        return sum(self.calls.values())


def run(node: FakeNode, **kw):
    forge_names._rpc = node
    try:
        return forge_names.resolve(LAUNCHER, LAUNCHER_PARENT.name().hex(), node="http://fake", **kw)
    except TypeError:   # a resolve with no deadline (before the fix): unbounded, as it was
        return forge_names.resolve(LAUNCHER, LAUNCHER_PARENT.name().hex(), node="http://fake")


def main() -> int:
    print("coin ids recomputed from node JSON match chia's Coin.name():")
    coin_id_hex = getattr(forge_names, "_coin_id_hex", lambda coin: "")
    ok = all(coin_id_hex(rec(Coin(bytes32(b"\x07" * 32), bytes32(b"\x08" * 32), uint64(a)), 1)["coin"])
             == Coin(bytes32(b"\x07" * 32), bytes32(b"\x08" * 32), uint64(a)).name().hex()
             for a in (0, 1, 127, 128, 255, 256, 32_767, 32_768, 10 ** 12, 2 ** 63, 2 ** 64 - 1))
    check("  for amounts 0 .. 2^64 - 1 across every sign-bit boundary", ok)

    print("3,000 dust coins, the rename older than all of them:")
    node = FakeNode(dust=3_000, rename_height=100, dust_from=1_000)
    out = run(node, deadline=time.monotonic() + 60)
    check("  resolution makes a bounded number of node calls (was one per dust coin)", node.total <= 20, f"{node.total} calls {node.calls}")
    check("  the name falls back to the genesis memo (the rename is past the newest MAX_HINTED_COINS)",
          out["name"] == "Genesis" and out["source"] == "genesis", f"{out['name']} / {out['source']}")

    print("500 dust coins newer than the rename:")
    node = FakeNode(dust=500, rename_height=100, dust_from=1_000)
    out = run(node, deadline=time.monotonic() + 60)
    check("  the rename is still found", out["name"] == "Renamed" and out["symbol"] == "RNM" and out["source"] == "rename", f"{out['name']}")
    check("  in a handful of calls", node.total <= 12, f"{node.total} calls {node.calls}")

    print("a node without the batch route:")
    node = FakeNode(dust=3_000, rename_height=100, dust_from=1_000, batch=False)
    run(node, deadline=time.monotonic() + 60)
    single = node.calls.get("get_coin_record_by_name", 0)
    cap = getattr(forge_names, "MAX_SINGLE_LOOKUPS", 50)
    check(f"  per-coin lookups are capped at MAX_SINGLE_LOOKUPS ({cap}) plus the launcher parent's", single <= cap + 2, f"{single}")

    print("a slow node (50 ms a call) with a 0.5 s pool budget:")
    node = FakeNode(dust=3_000, rename_height=100, dust_from=1_000, batch=False, delay=0.05)
    started = time.monotonic()
    raised = None
    budget_error = getattr(forge_names, "NameBudgetExceeded", None)
    try:
        run(node, deadline=time.monotonic() + 0.5)
    except Exception as exc:  # noqa: BLE001
        if budget_error is None or not isinstance(exc, budget_error):
            raise
        raised = exc
    elapsed = time.monotonic() - started
    check("  resolve raises NameBudgetExceeded", raised is not None)
    check("  within about the budget, not after every coin", elapsed < 1.5, f"{elapsed:.2f} s, {node.total} calls")

    print("the default deadline is the pool's budget, capped by the run's:")
    check("  POOL_BUDGET_S and RUN_DEADLINE are set", getattr(forge_names, "POOL_BUDGET_S", 0) > 0 and getattr(forge_names, "RUN_DEADLINE", 0) > 0)
    node = FakeNode(dust=10, rename_height=2_000, dust_from=1_000)
    out = run(node)
    check("  and an ordinary pool resolves under it (newest rename wins)", out["name"] == "Renamed", f"{out['name']}")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
