#!/usr/bin/env python3
"""The surplus refund goes to a payment group the maker signed for
(2026-09-26 external review, finding 14).

Requested payment groups are unsigned offer metadata. A relayer can prepend
a 1-mojo group of its own and become "the first requested payment", which is
where `payout_solution` used to send everything the pool released above the
ask and the router's capped fee. What a relayer cannot forge is the maker's
ASSERT_PUZZLE_ANNOUNCEMENT of its own group, so the refund destination is now
the first group the maker's signed spends assert.

Proved here without a chain: an offer is built by the app-side builder (which
now asserts that announcement -- the second thing this exercise found), then
mutated the way a relayer would, and `_trader_ph` is asked where the refund
goes before and after.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk  # noqa: E402
from chia.wallet.trading.offer import NotarizedPayment, Offer  # noqa: E402
from chia_rs import Coin, G1Element  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

from forge_v14_offer import OfferRejected, _trader_ph  # noqa: E402

BUILDER = Path(__file__).resolve().parent / "forge_offer_build.py"
IDENTITY = puzzle_for_pk(G1Element())
IDENTITY_HASH = IDENTITY.get_tree_hash()
TRADER_PH = bytes32.fromhex("ab" * 32)      # where the trader asks to be paid (and takes change)
ATTACKER_PH = bytes32.fromhex("ee" * 32)
CAT_ASSET = bytes32.fromhex("c1" * 32)
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        FAILED += 1


def run(payload: dict) -> dict:
    result = subprocess.run([sys.executable, str(BUILDER)], input=json.dumps(payload), capture_output=True, text=True)
    if not result.stdout.strip():
        raise AssertionError(f"builder produced nothing: {result.stderr[:400]}")
    return json.loads(result.stdout)


def maker_offer() -> Offer:
    """An XCH -> CAT offer as the Sage app or the agent API would build it."""
    coin = Coin(bytes32.fromhex("11" * 32), IDENTITY_HASH, uint64(1_000_000))
    record = {"coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(), "puzzle_hash": "0x" + coin.puzzle_hash.hex(), "amount": 1_000_000}, "puzzle": bytes(IDENTITY).hex()}
    requested = [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 7_500}]
    built = run({"action": "build", "change_puzzle_hash": "0x" + TRADER_PH.hex(), "fee": 0,
                 "offered": [{"asset_id": None, "amount": 250_000, "coins": [record]}], "requested": requested})
    assert built.get("success"), built
    finalized = run({"action": "finalize", "change_puzzle_hash": "0x" + TRADER_PH.hex(), "coin_spends": built["coin_spends"],
                     "requested": requested, "signature": "c0" + "00" * 95})
    assert finalized.get("success"), finalized
    return Offer.from_bech32(finalized["offer"])


def relayed_with_prepended_group(offer: Offer) -> Offer:
    """What a relayer can do: add its own 1-mojo group in front, under its own nonce."""
    requested = dict(offer.requested_payments)
    theirs = requested[CAT_ASSET]
    mine = NotarizedPayment(ATTACKER_PH, uint64(1), [ATTACKER_PH], bytes32.fromhex("77" * 32))
    requested[CAT_ASSET] = [mine, *theirs]
    return Offer(requested, offer._bundle, offer.driver_dict)


print("an honest offer:")
offer = maker_offer()
check("the maker's spend asserts its payment group", _trader_ph(offer, CAT_ASSET) == TRADER_PH, str(_trader_ph(offer, CAT_ASSET)))
first = offer.get_requested_payments()[CAT_ASSET][0]
check("(and the first requested payment is the maker's, so the old rule agreed here)", bytes32(first.puzzle_hash) == TRADER_PH)

print("\nthe same offer after a relayer prepends a 1-mojo group:")
relayed = relayed_with_prepended_group(offer)
first = relayed.get_requested_payments()[CAT_ASSET][0]
check("the first requested payment is now the relayer's (the old refund destination)", bytes32(first.puzzle_hash) == ATTACKER_PH)
check("the refund still goes to the maker's group", _trader_ph(relayed, CAT_ASSET) == TRADER_PH, str(_trader_ph(relayed, CAT_ASSET)))

print("\nan offer whose groups the maker never asserted:")
only_theirs = dict(offer.requested_payments)
only_theirs[CAT_ASSET] = [NotarizedPayment(ATTACKER_PH, uint64(7_500), [ATTACKER_PH], bytes32.fromhex("77" * 32))]
unbound = Offer(only_theirs, offer._bundle, offer.driver_dict)
try:
    _trader_ph(unbound, CAT_ASSET)
    check("is refused rather than paid to a stranger", False, "no refusal")
except OfferRejected as exc:
    check("is refused rather than paid to a stranger", "bound by the maker's signed spends" in str(exc), str(exc))

print("\nno payment of the asset at all:")
check("answers None (the LP-add path keeps its fallback)", _trader_ph(offer, None) is None)

print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    raise SystemExit(1)
print("ALL PASSED -- the surplus refund follows the maker's signature, not the offer's metadata")
