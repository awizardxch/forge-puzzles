#!/usr/bin/env python3
"""Parse a Chia offer into its offered/requested asset totals.

Wallet offers are zlib streams compressed against a versioned preset puzzle
dictionary, and deriving the offered side requires executing the puzzles to
compute bundle additions. chia.wallet.trading.offer.Offer does all of that
correctly across compression versions, CAT wrapping and notarized payments, so
the JS API layer delegates here rather than reimplementing offer semantics.

stdin:  JSON {"offer": "offer1..."}
stdout: JSON {"success": true, "offeredAssets": [...], "requestedAssets": [...],
               "inputCoinIds": [...]}
exit:   0 success, 1 failure
"""
from __future__ import annotations

import json
import sys

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.trading.offer import Offer
from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

ZERO_ASSET_ID = "0" * 64
MAX_COST = 11_000_000_000


def _asset_id_hex(asset_id) -> str:
    """Native XCH is keyed as None by Offer; the API layer uses 32 zero bytes."""
    return ZERO_ASSET_ID if asset_id is None else bytes(asset_id).hex()


def summarize(offer_str: str) -> dict:
    offer = Offer.from_bech32(offer_str.strip())

    offered = [
        {"assetId": _asset_id_hex(asset_id), "amount": str(sum(coin.amount for coin in coins))}
        for asset_id, coins in offer.get_offered_coins().items()
    ]
    requested = [
        {"assetId": _asset_id_hex(asset_id), "amount": str(amount)}
        for asset_id, amount in offer.get_requested_amounts().items()
    ]

    # The maker's real on-chain inputs: spends whose coin is not itself created
    # inside the offer bundle. Once these are spent the offer has been taken, so
    # they are a settlement signal that works for any venue and any action.
    created: set[bytes] = set()
    for spend in offer._bundle.coin_spends:
        conditions = conditions_dict_for_solution(
            spend.puzzle_reveal, spend.solution, MAX_COST)
        for condition in conditions.get(ConditionOpcode.CREATE_COIN, []):
            amount = int.from_bytes(condition.vars[1], "big") if condition.vars[1] else 0
            if amount >= 0:
                created.add(bytes(Coin(
                    spend.coin.name(), bytes32(condition.vars[0]), uint64(amount)).name()))
    inputs = [
        bytes(spend.coin.name()).hex()
        for spend in offer._bundle.coin_spends
        if bytes(spend.coin.name()) not in created
    ]

    return {
        "success": True,
        "offeredAssets": offered,
        "requestedAssets": requested,
        "inputCoinIds": inputs,
    }


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    offer_str = payload.get("offer")
    if not isinstance(offer_str, str) or not offer_str.strip():
        raise ValueError("Missing offer string")

    print(json.dumps(summarize(offer_str)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        sys.exit(1)
