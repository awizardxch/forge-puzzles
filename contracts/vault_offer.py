"""An offer authored by the lock itself.

Every other thing a lock does is a payment: it spends its coins and names the
puzzle hashes they land on, and the transaction is complete the moment the
owners have signed it. A swap is not that. Forge settles swaps from an **offer**,
and an offer is written by whoever gives up the asset -- so for a lock to swap,
the lock has to be the maker.

The shape is the same one any wallet builds, which is the point: the result is an
ordinary offer file that Forge's router, Dexie, or a person with a wallet can
take without knowing what a Forge lock is.

    1. choose the lock's coins for what is being offered;
    2. notarize what is wanted back -- the payments, stamped with a nonce derived
       from exactly those coin ids, so this offer cannot be recycled;
    3. spend the chosen coins into the settlement puzzle, asserting the puzzle
       announcements those notarized payments imply. Nothing satisfies those
       announcements yet: whoever takes the offer satisfies them by paying.

What makes it a *lock's* offer is only where the authorisation comes from. The
singleton spend rides in the bundle and announces each funds coin, exactly as it
does for a payment, and the owners sign that one delegated puzzle. So the vote is
unchanged; what the vote approves is an offer instead of a transfer.

Two consequences worth stating plainly, because they are properties of the
design and not of this code:

* **An offer is not on chain.** Nothing moves until someone takes it. Until then
  the lock still holds every coin, and the offer is a file.
* **Any other spend of the lock cancels it.** Every proposal spends the
  singleton tip, and this offer's bundle names *this* tip. Execute anything else
  and the offer stops being valid -- which is also how it is cancelled on
  purpose: spend the coins it was written against.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.conditions import CreateCoin
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.trading.offer import OFFER_MOD_HASH, NotarizedPayment, Offer
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from multisig_tool import MultisigError, Node, Output, hex32, parse_amount, strip0x
from vault_tool import VaultPlan, VaultState, build_vault_plan, deposit_puzzle_hash

ASSERT_PUZZLE_ANNOUNCEMENT = ConditionOpcode.ASSERT_PUZZLE_ANNOUNCEMENT

# What the lock gives up: one amount per asset (None for XCH).
Side = list[tuple[bytes32 | None, int]]
# What it wants back: an asset, an amount, and where it is to be paid. The
# destination is the lock unless something says otherwise -- a route's dev fee is
# a leg of the same offer that has to land somewhere else, and an offer that
# quietly redirected it to the lock would simply never be taken.
Wanted = list[tuple[bytes32 | None, int, "bytes32 | None"]]


def _asset_of(entry: dict, label: str) -> bytes32 | None:
    asset_raw = strip0x(entry.get("asset_id"))
    return None if not asset_raw or asset_raw == "0" * 64 else hex32(asset_raw, f"{label} asset_id")


def parse_side(raw: Any, label: str = "offered") -> Side:
    """`[{asset_id, amount}]` -- XCH is a null or absent asset id."""
    if not isinstance(raw, list) or not raw:
        raise MultisigError(f"an offer needs at least one {label} asset")
    side: Side = []
    seen: set[bytes32 | None] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise MultisigError(f"{label} {index + 1} must be an object")
        asset_id = _asset_of(entry, label)
        amount = parse_amount(entry.get("amount"), f"{label} amount")
        if amount <= 0:
            raise MultisigError(f"{label} {index + 1}: amount must be positive")
        if asset_id in seen:
            raise MultisigError(f"{label}: the same asset is listed twice")
        seen.add(asset_id)
        side.append((asset_id, amount))
    return side


def parse_requested(raw: Any) -> Wanted:
    """`[{asset_id, amount, puzzle_hash?}]`.

    Unlike the offered side this may name one asset more than once, because a
    route can owe two payments of the same asset to two different places -- the
    lock's own proceeds and the router's fee. What may not repeat is a
    destination: two payments of one asset to one address are one payment, and
    ``Offer`` rejects the duplicate outright.
    """
    if not isinstance(raw, list) or not raw:
        raise MultisigError("an offer needs at least one requested asset")
    wanted: Wanted = []
    seen: set[tuple[bytes32 | None, bytes32 | None]] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise MultisigError(f"requested {index + 1} must be an object")
        asset_id = _asset_of(entry, "requested")
        amount = parse_amount(entry.get("amount"), "requested amount")
        if amount <= 0:
            raise MultisigError(f"requested {index + 1}: amount must be positive")
        ph_raw = strip0x(entry.get("puzzle_hash"))
        puzzle_hash = hex32(ph_raw, "requested puzzle_hash") if ph_raw else None
        if (asset_id, puzzle_hash) in seen:
            raise MultisigError("requested: the same asset is asked for twice to the same address")
        seen.add((asset_id, puzzle_hash))
        wanted.append((asset_id, amount, puzzle_hash))
    return wanted


def driver_dict(sides: list[Any]) -> dict[bytes32, PuzzleInfo]:
    """The asset drivers the offer carries, for every CAT on either side.

    A taker reads these out of the offer file to know what it is dealing with,
    and ``Offer`` refuses to be built without one for anything requested.
    """
    drivers: dict[bytes32, PuzzleInfo] = {}
    for side in sides:
        for entry in side:
            asset_id = entry[0]
            if asset_id is not None:
                drivers[asset_id] = PuzzleInfo({"type": "CAT", "tail": "0x" + asset_id.hex()})
    return drivers


def requested_payments(requested: Wanted, deposit_ph: bytes32) -> dict[bytes32 | None, list[CreateCoin]]:
    """What the taker must pay, and where.

    Back to the lock unless a payment names somewhere else. A CAT payment carries
    its destination as a memo, which is the hint that lets the coin be found
    afterwards; XCH needs none, being found by its puzzle hash.
    """
    payments: dict[bytes32 | None, list[CreateCoin]] = {}
    for asset_id, amount, puzzle_hash in requested:
        target = puzzle_hash or deposit_ph
        memos = [target] if asset_id is not None else []
        payments.setdefault(asset_id, []).append(CreateCoin(target, uint64(amount), memos))
    return payments


def build_offer_plan(
    node: Node,
    network: str,
    state: VaultState,
    offered: Side,
    requested: Wanted,
    nonce: bytes | None = None,
) -> VaultPlan:
    """A plan the owners sign that, once signed, IS an offer.

    The plan is an ordinary vault plan -- same singleton spend, same funds
    announcements, same signatures -- whose outputs happen to be settlement coins
    and which carries the announcements binding what comes back.
    """
    both = {asset for asset, _ in offered} & {asset for asset, _, _ in requested}
    if both:
        raise MultisigError("an offer cannot ask for the same asset it is giving up")

    deposit_ph = deposit_puzzle_hash(state.launcher_id)
    # No hint on a settlement coin: nobody owns it, and a hint there names a
    # puzzle no wallet is watching.
    outputs = [Output(OFFER_MOD_HASH, amount, asset_id, "", (), hint=False) for asset_id, amount in offered]
    payments = requested_payments(requested, deposit_ph)
    drivers = driver_dict([offered, requested])

    # The nonce is the tree hash of the offered coins, so it cannot be known
    # until they are chosen. `build_vault_plan` selects them and then calls this
    # back with what it took.
    notarized: dict[bytes32 | None, list[NotarizedPayment]] = {}

    def conditions_for(selected: dict[bytes32 | None, list[Coin]]) -> dict[bytes32 | None, list[Any]]:
        coins = [coin for asset_id, _amount in offered for coin in selected.get(asset_id, [])]
        if not coins:
            raise MultisigError("the lock has no coins to offer")
        notarized.update(Offer.notarize_payments(payments, coins))
        announcements = Offer.calculate_announcements(notarized, drivers)
        # All of them on one spend, the way a wallet does it: the first asset
        # given up carries the whole demand, so no part of it can be satisfied
        # while another part is dropped.
        return {offered[0][0]: [[ASSERT_PUZZLE_ANNOUNCEMENT, a.msg_calc] for a in announcements]}

    plan = build_vault_plan(
        node, network, state, outputs,
        fee=0, sponsor=None, successor=None, nonce=nonce,
        asset_conditions_builder=conditions_for,
    )
    plan.summary["kind"] = "offer"
    plan.summary["offer"] = {
        "offered": [{"asset_id": a.hex() if a else None, "amount": amount} for a, amount in offered],
        "requested": [
            {"asset_id": a.hex() if a else None, "amount": amount, "puzzle_hash": ph.hex() if ph else None}
            for a, amount, ph in requested
        ],
        "payments": [
            {
                "asset_id": asset_id.hex() if asset_id else None,
                "puzzle_hash": payment.puzzle_hash.hex(),
                "amount": int(payment.amount),
                "memos": [bytes(memo).hex() for memo in (payment.memos or [])],
                "nonce": payment.nonce.hex(),
            }
            for asset_id, group in notarized.items() for payment in group
        ],
    }
    return plan


def payments_from_summary(summary: dict[str, Any]) -> dict[bytes32 | None, list[NotarizedPayment]]:
    """Rebuild the notarized payments the plan was written with.

    They are recorded rather than recomputed: recomputing would mean trusting
    that the coins reachable now are the coins the owners signed against, and the
    whole point of the nonce is that those are one and the same set.
    """
    raw = (summary.get("offer") or {}).get("payments")
    if not isinstance(raw, list) or not raw:
        raise MultisigError("this plan carries no offer payments")
    payments: dict[bytes32 | None, list[NotarizedPayment]] = {}
    for entry in raw:
        asset_raw = entry.get("asset_id")
        asset_id = hex32(asset_raw, "offer asset_id") if asset_raw else None
        payments.setdefault(asset_id, []).append(NotarizedPayment(
            hex32(entry.get("puzzle_hash"), "offer puzzle_hash"),
            uint64(parse_amount(entry.get("amount"), "offer amount")),
            [bytes.fromhex(strip0x(memo)) for memo in entry.get("memos") or []],
            hex32(entry.get("nonce"), "offer nonce"),
        ))
    return payments


def offer_sides(summary: dict[str, Any]) -> list[list[tuple[bytes32 | None, int]]]:
    """The two sides as the plan recorded them, offered first."""
    sides: list[list[tuple[bytes32 | None, int]]] = []
    for name in ("offered", "requested"):
        entries = (summary.get("offer") or {}).get(name) or []
        sides.append([
            (hex32(entry["asset_id"], "offer asset_id") if entry.get("asset_id") else None, int(entry["amount"]))
            for entry in entries
        ])
    return sides


def assemble_offer(plan: VaultPlan, spends: list[Any], signature: G2Element) -> Offer:
    """The signed spends as an offer file.

    Nothing is pushed. An offer's announcements are unsatisfied by design, so
    this bundle is not a transaction and a node would refuse it; it becomes one
    only when a taker adds the other half.
    """
    offer = Offer(
        payments_from_summary(plan.summary),
        WalletSpendBundle(spends, signature),
        driver_dict(offer_sides(plan.summary)),
    )
    if not offer.get_offered_amounts():
        raise MultisigError("the assembled offer gives up nothing; the settlement coins are missing")
    return offer
