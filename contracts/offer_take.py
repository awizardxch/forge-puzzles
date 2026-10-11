#!/usr/bin/env python3
"""Take a maker's offer (a fixed Dexie offer) with the trader's own counter-offer.

The Sage app settles trades itself, but its wallet bridge cannot take an offer file
(no chia_takeOffer; Sage's own take runs only over WalletConnect). So the app does
what a wallet's take does, in two halves:

  plan      {maker_offer}               -> what the maker offers and requests, so the
                                           app builds the exact counter-offer from its
                                           own coins and signs it (one prompt)
  assemble  {maker_offer, taker_offer}  -> Chia's own Offer.aggregate + to_valid_spend:
                                           one bundle, both signatures, validated as a
                                           mempool would (clvm + aggregate signature)

The router only assembles; it holds no key and pushes nothing -- the app pushes the
bundle through Sage's sendTransaction. The counter-offer must request exactly what the
maker offers and offer exactly what the maker requests: no arbitrage is left for
anyone to take, so nothing in the bundle is unsigned value.

stdin:  {action, maker_offer, taker_offer?, network: "mainnet" | "testnet11"}
stdout: {success, ...} | {success: false, error}
"""
from __future__ import annotations

import hashlib
import json
import sys

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.wallet.trading.offer import Offer
from chia_rs import validate_clvm_and_signature
from chia_rs.sized_bytes import bytes32

MAX_LEGS = 4
TESTNET11_GENESIS = bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615")


def constants_for(network: str):
    """Mainnet is chia's default; testnet11 swaps in its genesis and the AGG_SIG data."""
    if network == "mainnet":
        return DEFAULT_CONSTANTS
    extra = {"AGG_SIG_ME_ADDITIONAL_DATA": TESTNET11_GENESIS}
    for name, op in (("AGG_SIG_PARENT_ADDITIONAL_DATA", 43), ("AGG_SIG_PUZZLE_ADDITIONAL_DATA", 44),
                     ("AGG_SIG_AMOUNT_ADDITIONAL_DATA", 45), ("AGG_SIG_PUZZLE_AMOUNT_ADDITIONAL_DATA", 46),
                     ("AGG_SIG_PARENT_AMOUNT_ADDITIONAL_DATA", 47), ("AGG_SIG_PARENT_PUZZLE_ADDITIONAL_DATA", 48)):
        extra[name] = hashlib.sha256(TESTNET11_GENESIS + bytes([op])).digest()
    return DEFAULT_CONSTANTS.replace(GENESIS_CHALLENGE=bytes32(TESTNET11_GENESIS),
                                     **{k: bytes32(v) for k, v in extra.items()})


def _amounts(mapping) -> dict[str, int]:
    return {("xch" if k is None else k.hex()): int(v) for k, v in mapping.items()}


def _read(text: str, what: str) -> Offer:
    text = str(text or "").strip()
    if not text.startswith("offer1"):
        raise ValueError(f"{what} is not an offer")
    return Offer.from_bech32(text)


def plan(maker: Offer) -> dict:
    offered, requested = _amounts(maker.get_offered_amounts()), _amounts(maker.get_requested_amounts())
    if not offered or not requested:
        raise ValueError("the maker's offer must offer something and request something")
    if len(offered) > MAX_LEGS or len(requested) > MAX_LEGS:
        raise ValueError(f"an offer of more than {MAX_LEGS} assets a side is not taken here")
    return {"offered": offered, "requested": requested}


def assemble(maker: Offer, taker: Offer, network: str):
    made = plan(maker)
    countered = {"offered": _amounts(taker.get_offered_amounts()), "requested": _amounts(taker.get_requested_amounts())}
    # Exactly the mirror: the counter-offer pays what the maker asks and asks what the
    # maker offers, so the aggregate balances with no arbitrage left unsigned.
    if countered["offered"] != made["requested"] or countered["requested"] != made["offered"]:
        raise ValueError(f"the counter-offer must offer {made['requested']} and request {made['offered']}; "
                         f"it offers {countered['offered']} and requests {countered['requested']}")
    bundle = Offer.aggregate([maker, taker]).to_valid_spend()
    constants = constants_for(network)
    try:
        conds, _, _ = validate_clvm_and_signature(bundle, int(constants.MAX_BLOCK_COST_CLVM), constants, 10**8)
    except Exception as exc:  # noqa: BLE001 -- chia_rs raises a bare error code
        raise ValueError(f"the merged bundle does not validate on {network} (signatures or conditions): "
                         f"{type(exc).__name__} {exc}") from exc
    removed = sum(c.amount for c in bundle.removals())
    added = sum(c.amount for c in bundle.additions())
    return bundle, int(conds.cost), removed - added


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        action = payload.get("action")
        maker = _read(payload.get("maker_offer"), "maker_offer")
        if action == "plan":
            out = {"success": True, **plan(maker)}
        elif action == "assemble":
            taker = _read(payload.get("taker_offer"), "taker_offer")
            network = "mainnet" if payload.get("network") == "mainnet" else "testnet11"
            bundle, cost, fee = assemble(maker, taker, network)
            out = {"success": True, "bundle": bundle.to_json_dict(), "transaction_id": bundle.name().hex(),
                   "cost": cost, "fee": fee}
        else:
            raise ValueError("action must be plan or assemble")
        print(json.dumps(out))
    except Exception as exc:  # noqa: BLE001 -- the caller reads the reason
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
