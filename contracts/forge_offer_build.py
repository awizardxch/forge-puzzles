#!/usr/bin/env python3
"""Build a trader's side of an Offer from coins a wallet already holds.

Why this exists: an app running inside Sage can read its own coins and sign what
it is handed, but the Sage app bridge has no `createOffer`. It has
`wallet.getAssetCoins` -- which returns each coin with its puzzle reveal and
lineage proof -- and `wallet.signCoinSpends`. Between them everything an Offer
needs is available except the assembly, and assembly is what this file does.

The split keeps every key where it already is:

    app        reads its coins, asks for a build, signs the spends it gets back
    responder  turns coins into unsigned maker spends, and later into an Offer
    wallet     holds the key, and is the only thing that ever signs

Two modes, because a signature has to happen between them:

    build     {offered, requested, coins, change_puzzle_hash, fee}
              -> unsigned coin spends, and the requested payments they answer

    finalize  {spends, requested, signature}
              -> `offer1...`

The requested side is notarized payments: the settlement puzzle pays the trader
exactly what they asked for, nonced against the coins being spent, which is what
makes an Offer safe to hand to a stranger. Nothing here decides a price; the
caller has already agreed one.

Testnet research only; unaudited.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD,
    SpendableCAT,
    construct_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    puzzle_for_conditions,
    solution_for_conditions,
)
from chia.wallet.conditions import CreateCoin
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.trading.offer import OFFER_MOD_HASH, Offer
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import Coin, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

ZERO_32 = bytes32([0] * 32)
CREATE_COIN = 51


def _hex(value: Any) -> bytes:
    text = str(value or "").strip()
    return bytes.fromhex(text[2:] if text.startswith("0x") else text)


def _b32(value: Any) -> bytes32:
    return bytes32(_hex(value))


def _asset_id(value: Any) -> bytes32 | None:
    """None is XCH. Everything else is a CAT's asset id."""
    if value in (None, "", "xch", "txch"):
        return None
    asset = _b32(value)
    return None if asset == ZERO_32 else asset


def _coin(record: dict[str, Any]) -> Coin:
    coin = record["coin"]
    return Coin(_b32(coin["parent_coin_info"]), _b32(coin["puzzle_hash"]), uint64(int(coin["amount"])))


def _condition_list(outputs: list[tuple[bytes32, int]]) -> Program:
    """Create exactly the coins named, and nothing else."""
    return Program.to([[CREATE_COIN, puzzle_hash, amount] for puzzle_hash, amount in outputs])


def _p2_solution(outputs: list[tuple[bytes32, int]]) -> Program:
    """The standard puzzle's solution for those conditions.

    A delegated puzzle is a *program* that returns conditions, not the condition
    list itself -- `puzzle_for_conditions` quotes them. Passing the bare list
    produces a solution the coin cannot run, which is the kind of thing that only
    shows up when something tries to execute it.
    """
    return solution_for_conditions(_condition_list(outputs))


def _xch_spends(coins: list[dict[str, Any]], offered: int, change_ph: bytes32, fee: int) -> list:
    """Spend the trader's XCH into a settlement coin, with change back to them.

    The first coin carries the whole delegated puzzle: it creates the settlement
    coin and the change. Every other coin is spent to nothing, which is how its
    value reaches the same transaction. The fee is simply value not recreated.
    """
    total = sum(int(record["coin"]["amount"]) for record in coins)
    change = total - offered - fee
    if change < 0:
        raise ValueError(f"coins hold {total} mojos, which does not cover {offered} plus a fee of {fee}")

    outputs: list[tuple[bytes32, int]] = [(bytes32(OFFER_MOD_HASH), offered)]
    if change > 0:
        outputs.append((change_ph, change))

    spends = []
    for index, record in enumerate(coins):
        puzzle = Program.fromhex(str(record["puzzle"]))
        solution = _p2_solution(outputs) if index == 0 else _p2_solution([])
        spends.append(make_spend(_coin(record), puzzle, solution))
    return spends


def _cat_spends(asset: bytes32, coins: list[dict[str, Any]], offered: int, change_ph: bytes32) -> list:
    """The CAT ring for one asset: settlement out, change back, value conserved."""
    total = sum(int(record["coin"]["amount"]) for record in coins)
    change = total - offered
    if change < 0:
        raise ValueError(f"CAT {asset.hex()[:8]} holds {total}, which does not cover {offered}")

    spendables = []
    for index, record in enumerate(coins):
        inner = Program.fromhex(str(record["inner_puzzle"]))
        if index == 0:
            outputs: list[tuple[bytes32, int]] = [(bytes32(OFFER_MOD_HASH), offered)]
            if change > 0:
                outputs.append((change_ph, change))
            inner_solution = _p2_solution(outputs)
        else:
            inner_solution = _p2_solution([])

        proof = record.get("lineage_proof") or {}
        spendables.append(
            SpendableCAT(
                _coin(record),
                asset,
                inner,
                inner_solution,
                lineage_proof=LineageProof(
                    _b32(proof["parent_name"]) if proof.get("parent_name") else None,
                    _b32(proof["inner_puzzle_hash"]) if proof.get("inner_puzzle_hash") else None,
                    uint64(int(proof["amount"])) if proof.get("amount") is not None else None,
                ),
            )
        )
    return list(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends)


def _requested_payments(requested: list[dict[str, Any]], puzzle_hash: bytes32, coins: list[Coin]) -> dict:
    """What the trader is owed, keyed by asset, notarized against the coins spent.

    The nonce is a tree hash over the sorted coins this offer consumes, which is
    what ties the payments to this offer and no other: a settlement answering
    these payments cannot be replayed against a different set of coins.
    """
    payments: dict[bytes32 | None, list[CreateCoin]] = {}
    for entry in requested:
        asset = _asset_id(entry.get("asset_id"))
        amount = int(entry["amount"])
        if amount <= 0:
            raise ValueError("a requested amount must be positive")
        payments.setdefault(asset, []).append(CreateCoin(puzzle_hash, uint64(amount), [puzzle_hash]))
    return Offer.notarize_payments(payments, coins)


def _drivers(requested: list[dict[str, Any]]) -> dict:
    drivers = {}
    for entry in requested:
        asset = _asset_id(entry.get("asset_id"))
        if asset is not None:
            drivers[asset] = PuzzleInfo({"type": "CAT", "tail": "0x" + asset.hex()})
    return drivers


def build(payload: dict[str, Any]) -> dict[str, Any]:
    """Unsigned maker spends for everything the trader is giving up."""
    change_ph = _b32(payload["change_puzzle_hash"])
    fee = int(payload.get("fee", 0))
    spends = []

    for leg in payload["offered"]:
        asset = _asset_id(leg.get("asset_id"))
        amount = int(leg["amount"])
        if amount <= 0:
            raise ValueError("an offered amount must be positive")
        coins = leg["coins"]
        if not coins:
            raise ValueError("an offered leg needs at least one coin")
        spends.extend(
            _xch_spends(coins, amount, change_ph, fee)
            if asset is None
            else _cat_spends(asset, coins, amount, change_ph)
        )

    return {
        "success": True,
        "coin_spends": [
            {
                "coin": {
                    "parent_coin_info": "0x" + spend.coin.parent_coin_info.hex(),
                    "puzzle_hash": "0x" + spend.coin.puzzle_hash.hex(),
                    "amount": int(spend.coin.amount),
                },
                "puzzle_reveal": bytes(spend.puzzle_reveal).hex(),
                "solution": bytes(spend.solution).hex(),
            }
            for spend in spends
        ],
    }


def finalize(payload: dict[str, Any]) -> dict[str, Any]:
    """The signed spends plus the requested payments, as an `offer1...` string."""
    spends = [
        make_spend(
            Coin(
                _b32(entry["coin"]["parent_coin_info"]),
                _b32(entry["coin"]["puzzle_hash"]),
                uint64(int(entry["coin"]["amount"])),
            ),
            Program.fromhex(str(entry["puzzle_reveal"])),
            Program.fromhex(str(entry["solution"])),
        )
        for entry in payload["coin_spends"]
    ]
    signature = G2Element.from_bytes(_hex(payload["signature"]))
    requested = payload["requested"]
    offer = Offer(
        _requested_payments(requested, _b32(payload["change_puzzle_hash"]), [spend.coin for spend in spends]),
        WalletSpendBundle(spends, signature),
        _drivers(requested),
    )
    return {"success": True, "offer": offer.to_bech32(), "offer_id": offer.name().hex()}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        action = str(payload.get("action", "build"))
        if action == "build":
            result = build(payload)
        elif action == "finalize":
            result = finalize(payload)
        else:
            raise ValueError(f"unknown action {action}")
    except Exception as error:  # the caller is a web request; it needs the reason, not a traceback
        json.dump({"success": False, "error": f"{type(error).__name__}: {error}"}, sys.stdout)
        return 1
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
