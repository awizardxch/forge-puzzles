"""The lock tools' `status` command: whether each coin exists and is spent.

The lock refresh (api/multisig-refresh.js) asks it about every live proposal's coins,
and every offer's settlement coins, in one run. Pins that the answer is read in one
get_coin_records_by_names call per 100 coins however many groups ask, that each group
is answered as a `coin_ids` request for it alone would be, and that both tools --
vault_tool for vault locks, multisig_tool for legacy ones -- answer alike.

Exit 0 all pass, 1 a failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

import multisig_tool  # noqa: E402
import vault_tool  # noqa: E402
from multisig_tool import MultisigError  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures += 1


class FakeNode:
    """Coin records by name, singly or many at once, as the full node serves them."""

    def __init__(self, records: dict[str, dict]) -> None:
        self.records = records
        self.routes: list[str] = []

    def rpc(self, route: str, payload: dict) -> dict:
        self.routes.append(route)
        if route == "get_coin_records_by_names":
            assert len(payload["names"]) <= 100, "a node call asks about 100 coins at most"
            found = [self.records[n.removeprefix("0x")] for n in payload["names"] if n.removeprefix("0x") in self.records]
            return {"success": True, "coin_records": found}
        if route == "get_coin_record_by_name":
            record = self.records.get(payload["name"].removeprefix("0x"))
            return {"success": True, "coin_record": record} if record else {"success": False, "error": "not found"}
        raise AssertionError(route)

    def coin_record(self, coin_id: bytes32) -> dict | None:
        return self.rpc("get_coin_record_by_name", {"name": coin_id.hex()}).get("coin_record")


# 250 coins: every third spent, every seventh never existed.
coins = [Coin(bytes32(n.to_bytes(32, "big")), bytes32(b"\x11" * 32), 1 + n) for n in range(250)]
records = {}
for n, coin in enumerate(coins):
    if n % 7 == 3:
        continue
    records[coin.name().hex()] = {
        "coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(), "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
                 "amount": coin.amount},
        "spent": n % 3 == 0, "spent_block_index": 900 + n if n % 3 == 0 else 0, "confirmed_block_index": 800,
    }
ids = [coin.name().hex() for coin in coins]
groups = {f"p:{g}": ids[g * 25:(g + 1) * 25] for g in range(10)}


def one_by_one(coin_ids: list[str]) -> dict:
    """The answer the tool gave before it read in batches: one record per coin."""
    node = FakeNode(records)
    out = []
    for coin_id in coin_ids:
        record = node.coin_record(bytes32.fromhex(coin_id))
        out.append({"coin_id": coin_id, "found": record is not None, "spent": bool(record and record.get("spent")),
                    "spent_block_index": int((record or {}).get("spent_block_index") or 0)})
    return {"coins": out, "all_spent": all(c["spent"] for c in out), "any_spent": any(c["spent"] for c in out)}


for name, tool in (("vault_tool", vault_tool), ("multisig_tool", multisig_tool)):
    print(f"{name}:")
    node = FakeNode(records)
    out = tool.cmd_status({"network": "mainnet", "groups": groups}, lambda _url: node)
    check("ten groups of 25 coins: one node call per 100 coins", node.routes == ["get_coin_records_by_names"] * 3,
          f"{len(node.routes)} calls: {sorted(set(node.routes))}")
    check("each group answered as a request for it alone would be, coin by coin",
          out["success"] and out["groups"] == {key: one_by_one(group) for key, group in groups.items()})
    check("found, spent and never-existed coins all read",
          any(c["spent"] for c in out["groups"]["p:0"]["coins"])
          and any(not c["found"] for c in out["groups"]["p:0"]["coins"])
          and any(c["found"] and not c["spent"] for c in out["groups"]["p:0"]["coins"]))
    node = FakeNode(records)
    single = tool.cmd_status({"network": "mainnet", "coin_ids": ids[:30]}, lambda _url: node)
    check("coin_ids still answers as before, in one call", single == {"success": True, **one_by_one(ids[:30])}
          and node.routes == ["get_coin_records_by_names"], str(node.routes))
    for bad, label in (({"groups": {}}, "no groups"), ({"groups": {"p:x": []}}, "an empty group"), ({}, "nothing at all")):
        try:
            tool.cmd_status({"network": "mainnet", **bad}, lambda _url: FakeNode(records))
            check(f"{label} is refused", False)
        except MultisigError:
            check(f"{label} is refused", True)

check("vault_tool reads records with the same batch helper the singleton scan uses",
      vault_tool._records_by_names is multisig_tool.records_by_names)

print(f"\n{'all lock-status checks passed' if failures == 0 else f'{failures} lock-status check(s) failed'}")
raise SystemExit(1 if failures else 0)
