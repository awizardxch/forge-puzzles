#!/usr/bin/env python3
"""Return spendable coins with puzzle reveals using Sage RPC preview spends.

Usage:
    python sage_spendable_coins.py [--asset-id ASSET_ID] [--min-amount N]
                                   [--host HOST] [--port PORT]

Outputs JSON:  { "coins": [ { "coin": {...}, "puzzle": "0x...", "coinName": "hex",
                               "lineageProof": {...} | null } ] }
"""

import argparse
import hashlib
import json
import struct
import sys

from sage_rpc import SageRPC


def _coin_id(parent: str, puzzle_hash: str, amount: int) -> str:
    """Compute Chia coin ID: SHA256(parent_coin_info || puzzle_hash || uint64(amount))."""
    raw_parent = bytes.fromhex(parent.replace("0x", ""))
    raw_ph = bytes.fromhex(puzzle_hash.replace("0x", ""))
    raw_amount = struct.pack(">Q", amount)
    return hashlib.sha256(raw_parent + raw_ph + raw_amount).hexdigest()


def _extract_lineage_proof(solution_hex: str):
    """Extract CAT lineage proof from a preview spend's CLVM solution.

    CAT v2 outer solution structure:
      (inner_solution lineage_proof prev_coin_id this_coin_info next_coin_proof prev_subtotal extra_delta)

    lineage_proof = (parent_parent_coin_info  inner_puzzle_hash  parent_amount)

    Returns dict with {parentName, innerPuzzleHash, amount} or None.
    """
    try:
        from chia.types.blockchain_format.program import Program

        sol_hex = solution_hex
        if sol_hex.startswith("0x"):
            sol_hex = sol_hex[2:]
        sol = Program.from_bytes(bytes.fromhex(sol_hex))
        elements = list(sol.as_iter())
        if len(elements) < 2:
            return None
        lineage = elements[1]
        if not lineage.pair:
            return None  # nil lineage proof
        lp_items = list(lineage.as_iter())
        if len(lp_items) != 3:
            return None
        parent_name = lp_items[0].as_atom().hex()
        inner_ph = lp_items[1].as_atom().hex()
        amount = int.from_bytes(lp_items[2].as_atom(), "big")
        return {
            "parentName": "0x" + parent_name,
            "innerPuzzleHash": "0x" + inner_ph,
            "amount": amount,
        }
    except Exception:
        return None


def _get_wallet_address(sage):
    """Get wallet's own address for preview destination."""
    result = sage.get_coins(asset_id=None, limit=1)
    coins = result.get("coins", [])
    if not coins:
        raise RuntimeError("No XCH coins found in wallet")
    return coins[0]["address"]


def _extract_coins(coin_spends, is_cat=False):
    """Extract coin + puzzle pairs from preview coin_spends."""
    coins = []
    for cs in coin_spends:
        coin = cs["coin"]
        name = _coin_id(
            coin["parent_coin_info"],
            coin["puzzle_hash"],
            int(coin["amount"]),
        )
        entry = {
            "coin": coin,
            "puzzle": cs["puzzle_reveal"],
            "coinName": name,
        }
        if is_cat:
            entry["lineageProof"] = _extract_lineage_proof(cs.get("solution", ""))
        coins.append(entry)
    return coins


def get_xch_spendable(sage, address, min_amount):
    result = sage.send_xch(address=address, amount=max(min_amount, 1), fee=0)
    return _extract_coins(result.get("coin_spends", []), is_cat=False)


def get_cat_spendable(sage, asset_id, address, min_amount):
    result = sage.call("send_cat", {
        "asset_id": asset_id,
        "address": address,
        "amount": max(min_amount, 1),
        "fee": 0,
    })
    return _extract_coins(result.get("coin_spends", []), is_cat=True)


def main():
    parser = argparse.ArgumentParser(description="Sage spendable coins bridge")
    parser.add_argument("--asset-id", type=str, default=None)
    parser.add_argument("--min-amount", type=int, default=1)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9257)
    args = parser.parse_args()

    sage = SageRPC(host=args.host, port=args.port)
    address = _get_wallet_address(sage)

    if args.asset_id:
        coins = get_cat_spendable(sage, args.asset_id, address, args.min_amount)
    else:
        coins = get_xch_spendable(sage, address, args.min_amount)

    print(json.dumps({"coins": coins}))


if __name__ == "__main__":
    main()
