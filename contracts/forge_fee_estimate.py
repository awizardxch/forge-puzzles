#!/usr/bin/env python3
"""Measure what a trade will cost the mempool before the trader signs anything.

A trade's network fee has to be in the trader's offer, and the trader signs the
offer before the router assembles the pool spends around it. So the fee used to
be a model: 235M cost per pool plus 30%, measured on V13. V14 pool spends measure
185-266M depending on the action and the pool, and a route adds its own legs --
the model overpaid small trades and could underpay large ones, and an underpaid
offer is refused after it is signed and left resting in the wallet.

Two commands, and the router does the rest between them (api/forge-fee-estimate.js):

    standin  {offered, requested, fee?}
             -> an unsigned offer of the same shape as the trader's: one standard
                coin per offered asset, change back, the fee stated as RESERVE_FEE
                and paid from an extra XCH coin when the offer gives up none, as a
                wallet pays it. The responder's dry run builds the real pool
                spends around it against the live pools.
    cost     {bundle}
             -> what the mempool charges for that whole bundle. Signatures are
                not checked and an empty one has the same 96 bytes, so the cost is
                the cost of the signed bundle.

The stand-in key never signs and its coins are never on chain: nothing here can
spend anything. Testnet research only; unaudited.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key
from chia_rs import Coin, G2Element, PrivateKey, SpendBundle, get_conditions_from_spendbundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_offer_build as fob

# A key nobody holds a use for: the stand-in is never signed, and its coins are
# fabricated, so this only gives the puzzles the size a real wallet's have.
STANDIN_KEY = PrivateKey.from_bytes(bytes([0x5E]) * 32)
# Cost rules from here on are the current ones (after the 2024 hard fork).
COST_RULES_HEIGHT = 10_000_000
# Most offers an estimate will build: the same cap the routes put on legs.
MAX_LEGS = 8


def _standin_wallet() -> tuple[Program, bytes32]:
    inner = puzzle_for_synthetic_public_key(STANDIN_KEY.get_g1())
    return inner, inner.get_tree_hash()


def _record(coin: Coin, puzzle: Program | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {"coin": {"parent_coin_info": coin.parent_coin_info.hex(), "puzzle_hash": coin.puzzle_hash.hex(), "amount": str(int(coin.amount))}}
    if puzzle is not None:
        record["puzzle"] = bytes(puzzle).hex()
    return record


def standin(payload: dict[str, Any]) -> dict[str, Any]:
    offered = list(payload.get("offered") or [])
    requested = list(payload.get("requested") or [])
    if not offered or not requested:
        raise ValueError("an estimate needs the offered and the requested assets")
    if len(offered) > MAX_LEGS or len(requested) > MAX_LEGS:
        raise ValueError(f"at most {MAX_LEGS} offered and {MAX_LEGS} requested assets")
    fee = int(payload.get("fee", 1))
    if fee < 1:
        raise ValueError("the stand-in states a fee, as the real offer will")

    inner, ph = _standin_wallet()
    legs = []
    for index, leg in enumerate(offered):
        asset = fob._asset_id(leg.get("asset_id"))
        amount = int(leg["amount"])
        if amount <= 0:
            raise ValueError("an offered amount must be positive")
        salt = bytes([index + 1, 0x5E]) * 16
        if asset is None:
            # Enough for the amount and the fee, with change: a wallet's coin
            # rarely matches the amount exactly, and change is one more output.
            coin = Coin(bytes32(salt), ph, uint64(amount + fee + 1))
            legs.append({"asset_id": None, "amount": str(amount), "coins": [_record(coin, inner)]})
        else:
            outer = construct_cat_puzzle(CAT_MOD, asset, inner)
            parent = Coin(bytes32(salt), outer.get_tree_hash(), uint64(amount + 1))
            coin = Coin(parent.name(), outer.get_tree_hash(), uint64(amount + 1))
            record = _record(coin)
            record["inner_puzzle"] = bytes(inner).hex()
            record["lineage_proof"] = {"parent_name": parent.parent_coin_info.hex(), "inner_puzzle_hash": ph.hex(), "amount": str(amount + 1)}
            legs.append({"asset_id": asset.hex(), "amount": str(amount), "coins": [record]})

    pays_xch = any(fob._asset_id(leg.get("asset_id")) is None for leg in offered)
    fee_coins = [] if pays_xch else [_record(Coin(bytes32(bytes([0xFE, 0x5E]) * 16), ph, uint64(fee + 1)), inner)]
    build = {"change_puzzle_hash": ph.hex(), "offered": legs, "requested": requested, "fee": fee, "fee_coins": fee_coins}
    built = fob.build(build)
    offer = fob.finalize({"coin_spends": built["coin_spends"], "requested": requested, "change_puzzle_hash": ph.hex(),
                          "signature": bytes(G2Element()).hex()})
    return {"success": True, "offer": offer["offer"], "spends": len(built["coin_spends"])}


def _hex(value: Any) -> str:
    text = str(value or "")
    return text if text.startswith("0x") else "0x" + text


def bundle_cost(bundle_json: dict[str, Any]) -> int:
    spends = []
    for entry in bundle_json.get("coin_spends") or []:
        coin = entry["coin"]
        spends.append({
            "coin": {"parent_coin_info": _hex(coin["parent_coin_info"]), "puzzle_hash": _hex(coin["puzzle_hash"]), "amount": int(coin["amount"])},
            "puzzle_reveal": _hex(entry["puzzle_reveal"]),
            "solution": _hex(entry["solution"]),
        })
    if not spends:
        raise ValueError("the bundle has no spends")
    bundle = SpendBundle.from_json_dict({"coin_spends": spends, "aggregated_signature": "0x" + bytes(G2Element()).hex()})
    conditions = get_conditions_from_spendbundle(bundle, DEFAULT_CONSTANTS.MAX_BLOCK_COST_CLVM, DEFAULT_CONSTANTS, COST_RULES_HEIGHT)
    return int(conditions.cost)


def cost(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = payload.get("bundle")
    if not isinstance(bundle, dict):
        raise ValueError("cost needs the bundle a dry run returned")
    return {"success": True, "cost": bundle_cost(bundle), "spends": len(bundle.get("coin_spends") or [])}


COMMANDS = {"standin": standin, "cost": cost}


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    handler = COMMANDS.get(command)
    try:
        if handler is None:
            raise ValueError(f"usage: forge_fee_estimate.py {{{'|'.join(COMMANDS)}}} < payload.json")
        result = handler(json.loads(sys.stdin.read() or "{}"))
    except Exception as error:  # noqa: BLE001 -- every failure is reported as data
        result = {"success": False, "error": str(error)}
    print(json.dumps(result))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
