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
    CAT_MOD_HASH,
    SpendableCAT,
    construct_cat_puzzle,
    match_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.uncurried_puzzle import uncurry_puzzle
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

from untrusted_clvm import parse_untrusted_hex

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


def _assert_reveal_matches(coin: Coin, puzzle_hash: bytes32, what: str) -> None:
    """Refuse a coin whose puzzle hash is not the hash of the reveal supplied.

    The build used to execute every caller-supplied CAT inner puzzle before
    anything checked that the coin could exist (2026-09-26 external review,
    finding 15). A coin that is not what its reveal says can never be spent,
    so there is nothing to build -- and the check is a tree hash, not an
    execution, so it costs the caller's puzzle nothing to fail.
    """
    if coin.puzzle_hash != puzzle_hash:
        raise ValueError(
            f"{what} {coin.name().hex()[:10]} has puzzle hash {coin.puzzle_hash.hex()[:10]} "
            f"but its reveal hashes to {puzzle_hash.hex()[:10]}; the coin cannot be spent with this reveal"
        )


def _condition_list(outputs: list[tuple[bytes32, int]]) -> Program:
    """Create exactly the coins named, and nothing else."""
    return Program.to([[CREATE_COIN, puzzle_hash, amount] for puzzle_hash, amount in outputs])


def _p2_solution(outputs: list[tuple[bytes32, int]], asserts: list[Program] | None = None) -> Program:
    """The standard puzzle's solution for those conditions.

    A delegated puzzle is a *program* that returns conditions, not the condition
    list itself -- `puzzle_for_conditions` quotes them. Passing the bare list
    produces a solution the coin cannot run, which is the kind of thing that only
    shows up when something tries to execute it.

    `asserts` are the ASSERT_PUZZLE_ANNOUNCEMENT conditions that make the spend
    an offer rather than a gift: see `_announcements`.
    """
    conditions = list(_condition_list(outputs).as_iter()) + list(asserts or [])
    return solution_for_conditions(Program.to(conditions))


def _announcements(requested: list[dict[str, Any]], puzzle_hash: bytes32, coins: list[Coin]) -> list[Program]:
    """The conditions that bind the trader's coins to being paid.

    A maker's spend must assert the announcement the settlement puzzle makes
    when it pays the notarized requested payments; without that assertion the
    coins it spends into the settlement coin can be released to anyone by
    whoever settles. Until 2026-09-26 this builder produced spends with the
    CREATE_COINs and nothing else -- found while remediating the external
    review's finding 14 (which binds the surplus refund to exactly these
    assertions) and not among the review's findings. The announcements are the
    ones `finalize` will notarize, over the same coins and the same requested
    side, so build and finalize agree by construction.
    """
    notarized = _requested_payments(requested, puzzle_hash, coins)
    return [ann.to_program() for ann in Offer.calculate_announcements(notarized, _drivers(requested))]


MAX_PAYMENTS = 4


def _payments(leg: dict[str, Any]) -> list[tuple[bytes32, int]]:
    """Extra coins a leg's own spend creates, in that leg's asset.

    The router's fee on a Dexie-only swap (owner, 2026-10-05: "the dexie only route
    should charge our protocol fee"). Dexie fills the offer with its own liquidity,
    so nothing of ours settles it; the fee coin is instead created by the trader's
    spend itself, the spend that also funds the settlement coin and asserts the
    settlement's announcement. It therefore exists exactly when the swap does: the
    offer taken, the fee paid; the offer never taken, nothing paid. It is not part
    of the offered amount, so the offer still reads as what it trades.
    """
    entries = list(leg.get("payments") or [])
    if len(entries) > MAX_PAYMENTS:
        raise ValueError(f"at most {MAX_PAYMENTS} payments per leg")
    payments = []
    for entry in entries:
        amount = int(entry["amount"])
        if amount <= 0:
            raise ValueError("a payment amount must be positive")
        payments.append((_b32(entry["puzzle_hash"]), amount))
    return payments


def _payment_conditions(payments: list[tuple[bytes32, int]]) -> list[Program]:
    """CREATE_COIN for each payment, hinted to its own puzzle hash so the
    recipient's wallet finds it (an unhinted CAT coin is invisible to wallets)."""
    return [Program.to([CREATE_COIN, puzzle_hash, amount, [puzzle_hash]]) for puzzle_hash, amount in payments]


def _xch_spends(coins: list[dict[str, Any]], offered: int, change_ph: bytes32, fee: int, asserts: list[Program],
                payments: list[tuple[bytes32, int]] | None = None) -> list:
    """Spend the trader's XCH into a settlement coin, with change back to them.

    The first coin carries the whole delegated puzzle: it creates the settlement
    coin, any payments, and the change. Every other coin is spent to nothing, which
    is how its value reaches the same transaction. The fee is simply value not
    recreated.
    """
    payments = payments or []
    paid = sum(amount for _, amount in payments)
    total = sum(int(record["coin"]["amount"]) for record in coins)
    change = total - offered - paid - fee
    if change < 0:
        raise ValueError(f"coins hold {total} mojos, which does not cover {offered} plus payments of {paid} plus a fee of {fee}")

    # offered == 0 is a fee-only spend (fee_coins): nothing goes to the settlement.
    outputs: list[tuple[bytes32, int]] = [(bytes32(OFFER_MOD_HASH), offered)] if offered > 0 else []
    if change > 0:
        outputs.append((change_ph, change))

    # The fee is ALSO stated, as RESERVE_FEE (52), the way a wallet's own offers state
    # theirs. Left implicit, the value was paid but invisible: Sage's approval sums
    # only RESERVE_FEE conditions (sage-wallet transaction.rs), so the Sage app showed
    # "FEE 0" on every swap whatever fee was chosen (2026-10-03). Stated, the wallet
    # shows it and the chain requires the whole bundle to leave at least that much.
    conditions = _payment_conditions(payments) + list(asserts) + ([Program.to([52, fee])] if fee > 0 else [])

    spends = []
    for index, record in enumerate(coins):
        puzzle = parse_untrusted_hex(record["puzzle"])
        coin = _coin(record)
        _assert_reveal_matches(coin, puzzle.get_tree_hash(), "XCH coin")
        solution = _p2_solution(outputs, conditions) if index == 0 else _p2_solution([])
        spends.append(make_spend(coin, puzzle, solution))
    return spends


def _cat_inner(asset: bytes32, coin: Coin, supplied: Program) -> Program:
    """The inner puzzle of a CAT coin, from either reveal a wallet may send.

    Sage's getAssetCoins returns a CAT coin's FULL puzzle (the CAT layer already
    around the p2 puzzle: `cat.info.construct_puzzle(ctx, p2_puzzle)` in its
    wallet_connect endpoint), while the CHIP-0002 relay path and
    contracts/wallet_coins.py send the inner one. Wrapping the full puzzle again
    made every CAT-paid swap from the Sage app fail the reveal check below
    (2026-10-01: "CAT 7f4c27d7 coin ... has puzzle hash 9cdfcd9bed but its reveal
    hashes to 1c3c4bfc62").

    The full puzzle is accepted only when it IS this coin's puzzle -- its hash
    is the coin's puzzle hash -- and it is the CAT layer over this asset; the
    inner puzzle is then read out of it, and the usual check still runs on the
    re-wrapped result. Anything else is returned unchanged for that check to
    judge, so this widens nothing the review's finding 15 closed.
    """
    if supplied.get_tree_hash() != coin.puzzle_hash:
        return supplied
    matched = match_cat_puzzle(uncurry_puzzle(supplied))
    if matched is None:
        return supplied
    mod_hash, tail_hash, inner = matched
    if bytes(mod_hash.as_atom()) != bytes(CAT_MOD_HASH) or bytes(tail_hash.as_atom()) != bytes(asset):
        return supplied
    return inner


def _cat_spends(asset: bytes32, coins: list[dict[str, Any]], offered: int, change_ph: bytes32, asserts: list[Program],
                payments: list[tuple[bytes32, int]] | None = None) -> list:
    """The CAT ring for one asset: settlement out, payments, change back, value conserved."""
    payments = payments or []
    paid = sum(amount for _, amount in payments)
    total = sum(int(record["coin"]["amount"]) for record in coins)
    change = total - offered - paid
    if change < 0:
        raise ValueError(f"CAT {asset.hex()[:8]} holds {total}, which does not cover {offered} plus payments of {paid}")

    spendables = []
    for index, record in enumerate(coins):
        inner = _cat_inner(asset, _coin(record), parse_untrusted_hex(record["inner_puzzle"]))
        # The CAT puzzle over this asset and inner must hash to the coin's own
        # puzzle hash, or the coin is not this CAT and nothing below can run.
        _assert_reveal_matches(_coin(record), construct_cat_puzzle(CAT_MOD, asset, inner).get_tree_hash(), f"CAT {asset.hex()[:8]} coin")
        if index == 0:
            outputs: list[tuple[bytes32, int]] = [(bytes32(OFFER_MOD_HASH), offered)]
            if change > 0:
                outputs.append((change_ph, change))
            inner_solution = _p2_solution(outputs, _payment_conditions(payments) + list(asserts))
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
        # Paid to the trader unless the entry names another recipient: the router's fee on
        # a swap that pays out XCH is a requested payment to the router in the trader's own
        # group, so it is covered by the trader's signature like the rest (F3, 2026-10-07).
        recipient = _b32(entry["puzzle_hash"]) if entry.get("puzzle_hash") else puzzle_hash
        payments.setdefault(asset, []).append(CreateCoin(recipient, uint64(amount), [recipient]))
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
    if fee < 0:
        raise ValueError("a fee cannot be negative")
    spends = []

    # The network fee rides on the XCH the offer gives up. An offer that gives up
    # none -- a CAT-paid swap -- pays it from `fee_coins`, XCH coins spent only for
    # that. It used to be dropped silently: the Sage app showed a 0.010693 TXCH fee
    # on a T6-paid 7-pool split (2026-10-02), the bundle went out with fee 0, and
    # it waited minutes for a block with room to spare.
    pays_xch = any(_asset_id(leg.get("asset_id")) is None for leg in payload["offered"])
    fee_coins = list(payload.get("fee_coins") or []) if fee > 0 and not pays_xch else []
    if fee > 0 and not pays_xch and not fee_coins:
        raise ValueError("a network fee is set but the offer gives up no XCH to pay it: send fee_coins")

    # Every coin the offer spends, in leg order then the fee coins: the nonce over
    # these is what the requested payments are notarized against, in build and in
    # finalize (which takes every spend's coin, fee coins included).
    all_coins = [_coin(record) for leg in payload["offered"] for record in leg["coins"]]
    all_coins += [_coin(record) for record in fee_coins]
    asserts = _announcements(payload["requested"], change_ph, all_coins)

    for leg in payload["offered"]:
        asset = _asset_id(leg.get("asset_id"))
        amount = int(leg["amount"])
        if amount <= 0:
            raise ValueError("an offered amount must be positive")
        coins = leg["coins"]
        if not coins:
            raise ValueError("an offered leg needs at least one coin")
        payments = _payments(leg)
        spends.extend(
            _xch_spends(coins, amount, change_ph, fee, asserts, payments)
            if asset is None
            else _cat_spends(asset, coins, amount, change_ph, asserts, payments)
        )
    if fee_coins:
        # Change back to the trader, the fee left unrecreated, and the same
        # assertions: the fee coin cannot be spent apart from the offer it pays for.
        spends.extend(_xch_spends(fee_coins, 0, change_ph, fee, asserts))

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
            parse_untrusted_hex(entry["puzzle_reveal"]),
            parse_untrusted_hex(entry["solution"]),
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
