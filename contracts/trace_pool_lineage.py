#!/usr/bin/env python3
"""Trace Forge pool singleton lineage proofs from on-chain coin records.

For each coin in the chain, this script attempts to derive the lineage tuple
needed to spend that coin:
  (launcher_parent, prev_pool_state_hash, 1)

Usage:
  python trace_pool_lineage.py --coin-id <pool_coin_id_hex> [--max-depth 6]
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from typing import Optional
from urllib import request as urlrequest

from chia.types.blockchain_format.program import Program


@dataclass
class LineageStep:
    coin_id: str
    parent_coin_info: str
    puzzle_hash: str
    amount: int
    confirmed_block_index: int
    spent: bool
    spent_block_index: int
    lineage_launcher_parent: Optional[str]
    lineage_prev_pool_state_hash: Optional[str]
    lineage_parent_amount: int


def strip_0x(s: str) -> str:
    if isinstance(s, str) and (s.startswith("0x") or s.startswith("0X")):
        return s[2:]
    return s if isinstance(s, str) else ""


def rpc(node_url: str, route: str, payload: dict) -> dict:
    url = f"{node_url.rstrip('/')}/{route.lstrip('/')}"
    req = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "aWizard-Familiar/trace-pool-lineage",
        },
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=30) as res:
        body = res.read().decode("utf-8")
    return json.loads(body)


def get_coin_record(node_url: str, coin_id: str) -> Optional[dict]:
    data = rpc(node_url, "get_coin_record_by_name", {"name": strip_0x(coin_id)})
    return data.get("coin_record")


def get_parent_prev_state_hash(node_url: str, parent_coin_id: str, parent_spent_height: int) -> Optional[str]:
    """Extract prev_pool_state_hash from the parent's puzzle reveal.

    For pool_singleton_v2, the curried args are:
      [singleton_struct, prev_pool_state_prog]
    We return tree_hash(prev_pool_state_prog).
    """
    if not parent_spent_height:
        return None

    ps = rpc(
        node_url,
        "get_puzzle_and_solution",
        {"coin_id": strip_0x(parent_coin_id), "height": int(parent_spent_height)},
    )
    coin_solution = ps.get("coin_solution") or {}
    puzzle_hex = strip_0x(coin_solution.get("puzzle_reveal", ""))
    if not puzzle_hex:
        return None

    outer = Program.from_bytes(bytes.fromhex(puzzle_hex))
    _mod, args = outer.uncurry()
    if not args.listp():
        return None

    first = args.first()
    rest = args.rest()
    if not rest.listp():
        return None

    prev_state_prog = rest.first()
    _ = first  # singleton struct, kept for readability
    return bytes(prev_state_prog.get_tree_hash()).hex()


def trace_lineage(node_url: str, start_coin_id: str, max_depth: int) -> list[LineageStep]:
    out: list[LineageStep] = []
    current = strip_0x(start_coin_id)

    for _ in range(max_depth):
        record = get_coin_record(node_url, current)
        if not record:
            break

        coin = record["coin"]
        parent_coin_info = strip_0x(coin["parent_coin_info"])

        parent_record = get_coin_record(node_url, parent_coin_info)
        lineage_launcher_parent = None
        lineage_prev_pool_state_hash = None

        if parent_record:
            lineage_launcher_parent = strip_0x(parent_record["coin"]["parent_coin_info"])
            lineage_prev_pool_state_hash = get_parent_prev_state_hash(
                node_url,
                parent_coin_info,
                int(parent_record.get("spent_block_index", 0)),
            )

        out.append(
            LineageStep(
                coin_id=current,
                parent_coin_info=parent_coin_info,
                puzzle_hash=strip_0x(coin["puzzle_hash"]),
                amount=int(coin["amount"]),
                confirmed_block_index=int(record.get("confirmed_block_index", 0)),
                spent=bool(record.get("spent", False)),
                spent_block_index=int(record.get("spent_block_index", 0)),
                lineage_launcher_parent=lineage_launcher_parent,
                lineage_prev_pool_state_hash=lineage_prev_pool_state_hash,
                lineage_parent_amount=1,
            )
        )

        # Walk upward.
        current = parent_coin_info

    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Trace pool singleton lineage proof chain")
    parser.add_argument("--coin-id", required=True, help="Current pool coin id hex (with or without 0x)")
    parser.add_argument("--node-url", default="https://testnet11.api.coinset.org", help="Full node RPC URL")
    parser.add_argument("--max-depth", type=int, default=6, help="Max number of ancestor steps")
    args = parser.parse_args()

    steps = trace_lineage(args.node_url, args.coin_id, args.max_depth)
    print(json.dumps({"success": True, "steps": [asdict(s) for s in steps]}, indent=2))


if __name__ == "__main__":
    main()
