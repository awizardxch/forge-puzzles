#!/usr/bin/env python3
"""Combine a wallet's coins of one asset into one coin, back at the wallet's own address.

An offer spends every coin it gives up, and a wallet's createOffer picks its own: an add
of 22,012 LOVE held as 196 small coins became an offer of 197 inputs (mainnet,
2026-10-23). That offer was costly enough to be dropped from a full mempool, too big to
sign over WalletConnect, and too many coins for the router to read at once. Combining
first makes the offer one coin of the asset. The page asks before doing it
(src/lib/trade/consolidate.ts).

  build:    {action, network, asset_id|null, own_puzzle_hash, coins, fee_coins?, network_fee?}
            -> {coin_spends, network_fee, cost, combined: {coin_id, puzzle_hash, amount}}
  finalize: {action, network, coin_spends, signatures: [hex, ...]}
            -> {spend_bundle}    the chunk signatures aggregated, checked against the network

The coins are XCH ({coin, puzzle}) or one CAT ({coin, inner_puzzle, lineage_proof}; a full
CAT puzzle is accepted for inner_puzzle, as forge_offer_build takes it). Every one must be
a standard wallet coin -- checked by its shape before anything runs -- and the combined coin
goes to `own_puzzle_hash`, which must be the address one of the spent coins already sits
at: this sends nothing to anyone else. The combined coin is hinted to that address, or a
wallet would not see a CAT coin it holds. The fee comes out of the XCH being combined, or
out of `fee_coins` (XCH) when combining a CAT; sized at 5 mojos per cost plus 30% unless
given. The wallet signs the spends in chunks the page sizes; finalize aggregates the
chunk signatures and validates the whole bundle, signatures included, before handing it
back for the wallet to push.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats  # noqa: E402
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import MOD as P2_STANDARD_MOD  # noqa: E402
from chia_rs import AugSchemeMPL, CoinSpend, G2Element, SpendBundle, validate_clvm_and_signature  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v18_driver as drv  # noqa: E402
from forge_offer_build import _assert_reveal_matches, _b32, _cat_inner, _coin  # noqa: E402
from offer_take import constants_for  # noqa: E402
from untrusted_clvm import parse_untrusted_hex  # noqa: E402

CREATE_COIN, RESERVE_FEE = 51, 52
P2_STANDARD_MOD_HASH = P2_STANDARD_MOD.get_tree_hash()
MAX_COINS = 300


def _standard(puzzle: Program, what: str) -> None:
    """A standard wallet puzzle, judged by its shape: the curried p2 module, nothing else."""
    try:
        mod, _args = puzzle.uncurry()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{what} is not a standard wallet coin") from exc
    if mod.get_tree_hash() != P2_STANDARD_MOD_HASH:
        raise ValueError(f"{what} is not a standard wallet coin; combine it from its own wallet")


def _xch(records: list[dict], label: str) -> list[tuple]:
    out = []
    for record in records:
        coin = _coin(record)
        puzzle = parse_untrusted_hex(record["puzzle"])
        _assert_reveal_matches(coin, puzzle.get_tree_hash(), label)
        _standard(puzzle, f"{label} {coin.name().hex()[:10]}")
        out.append((coin, puzzle))
    return out


def _cats(asset: bytes32, records: list[dict]) -> list[tuple]:
    out = []
    for record in records:
        coin = _coin(record)
        inner = _cat_inner(asset, coin, parse_untrusted_hex(record["inner_puzzle"]))
        _assert_reveal_matches(coin, construct_cat_puzzle(CAT_MOD, asset, inner).get_tree_hash(), f"CAT {asset.hex()[:8]} coin")
        _standard(inner, f"CAT {asset.hex()[:8]} coin {coin.name().hex()[:10]}")
        proof = record.get("lineage_proof") or {}
        out.append((coin, inner, LineageProof(
            _b32(proof["parent_name"]) if proof.get("parent_name") else None,
            _b32(proof["inner_puzzle_hash"]) if proof.get("inner_puzzle_hash") else None,
            uint64(int(proof["amount"])) if proof.get("amount") is not None else None)))
    return out


def _xch_spends(coins: list[tuple], keep: int, to_ph: bytes32, fee: int) -> list:
    """The first coin recreates `keep` at `to_ph`, hinted; the others are spent into it."""
    conditions = ([[CREATE_COIN, to_ph, keep, [to_ph]]] if keep > 0 else []) + ([[RESERVE_FEE, fee]] if fee > 0 else [])
    return [make_spend(coin, puzzle, drv.p2_delegated_solution(conditions if i == 0 else []))
            for i, (coin, puzzle) in enumerate(coins)]


def plan(payload: dict[str, Any], fee: int) -> tuple[list, dict]:
    asset = _b32(payload["asset_id"]) if payload.get("asset_id") else None
    own = _b32(payload["own_puzzle_hash"])
    records = payload.get("coins") or []
    fee_records = payload.get("fee_coins") or []
    if len(records) < 2:
        raise ValueError("combining needs at least two coins")
    if len(records) + len(fee_records) > MAX_COINS:
        raise ValueError(f"at most {MAX_COINS} coins in one combine")
    if asset is None:
        coins = _xch(records, "XCH coin")
        owners = {coin.puzzle_hash for coin, _ in coins}
        if own not in owners:
            raise ValueError("the combined coin must go to the address one of these coins already sits at")
        total = sum(int(coin.amount) for coin, _ in coins)
        if total <= fee:
            raise ValueError(f"these coins hold {total} mojos, which does not cover a fee of {fee}")
        spends = _xch_spends(coins, total - fee, own, fee)
        combined = {"puzzle_hash": own, "amount": total - fee, "parent": coins[0][0].name()}
        return spends, combined
    cats = _cats(asset, records)
    owners = {inner.get_tree_hash() for _, inner, _ in cats}
    if own not in owners:
        raise ValueError("the combined coin must go to the address one of these coins already sits at")
    total = sum(int(coin.amount) for coin, _, _ in cats)
    spendables = [SpendableCAT(coin, asset, inner, drv.p2_delegated_solution([[CREATE_COIN, own, total, [own]]] if i == 0 else []),
                               lineage_proof=proof) for i, (coin, inner, proof) in enumerate(cats)]
    spends = list(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends)
    if fee > 0:
        if not fee_records:
            raise ValueError("combining a CAT with a fee needs XCH coins to pay it")
        fee_coins = _xch(fee_records, "fee coin")
        held = sum(int(coin.amount) for coin, _ in fee_coins)
        if held < fee:
            raise ValueError(f"the fee coins hold {held} mojos, which does not cover a fee of {fee}")
        spends += _xch_spends(fee_coins, held - fee, fee_coins[0][0].puzzle_hash, fee)
    own_inner = next(inner for _, inner, _ in cats if inner.get_tree_hash() == own)
    combined = {"puzzle_hash": construct_cat_puzzle(CAT_MOD, asset, own_inner).get_tree_hash(), "amount": total,
                "parent": cats[0][0].name()}
    return spends, combined


# The mempool admits a transaction of at most half a block's cost (11 billion). 150 CAT coins
# cost 4.55 billion to combine; past this cap the page splits the combine into transactions
# of fewer coins (src/lib/trade/consolidate.ts), which can confirm in the same block.
MAX_COMBINE_COST = 5_000_000_000


def build(payload: dict[str, Any]) -> dict[str, Any]:
    requested = payload.get("network_fee")
    spends, _ = plan(payload, 0)
    conds, _ = drv.validate(SpendBundle(spends, G2Element()))
    cost = int(conds.cost)
    if cost > MAX_COMBINE_COST:
        raise ValueError(f"combining these {len(payload.get('coins') or [])} coins would cost {cost:,}, more than one "
                         f"transaction may ({MAX_COMBINE_COST:,}); combine fewer at a time")
    fee = int(requested) if requested is not None else math.ceil(cost * 5 * 1.3)
    if fee:
        spends, _ = plan(payload, fee)
        conds, _ = drv.validate(SpendBundle(spends, G2Element()))
        cost = int(conds.cost)
    _, combined = plan(payload, fee)
    coin_id = drv.coin_id(combined["parent"], combined["puzzle_hash"], combined["amount"])
    return {"success": True, "coin_spends": [s.to_json_dict() for s in spends], "network_fee": str(fee), "cost": cost,
            "combined": {"coin_id": coin_id.hex(), "puzzle_hash": combined["puzzle_hash"].hex(), "amount": str(combined["amount"])}}


def finalize(payload: dict[str, Any]) -> dict[str, Any]:
    fix = lambda v: v if str(v).startswith("0x") else "0x" + str(v)  # noqa: E731
    spends = [CoinSpend.from_json_dict({
        "coin": {"parent_coin_info": fix(s["coin"]["parent_coin_info"]), "puzzle_hash": fix(s["coin"]["puzzle_hash"]),
                 "amount": int(s["coin"]["amount"])},
        "puzzle_reveal": fix(s["puzzle_reveal"]), "solution": fix(s["solution"])}) for s in payload["coin_spends"]]
    signatures = [G2Element.from_bytes(bytes.fromhex(str(sig).removeprefix("0x"))) for sig in payload.get("signatures") or []]
    if not signatures:
        raise ValueError("finalize needs the wallet's signatures")
    bundle = SpendBundle(spends, AugSchemeMPL.aggregate(signatures))
    constants = constants_for(str(payload.get("network") or "mainnet"))
    try:
        validate_clvm_and_signature(bundle, int(constants.MAX_BLOCK_COST_CLVM), constants, 10**8)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"the signed combine does not validate: {type(exc).__name__}: {str(exc)[:160]}") from exc
    return {"success": True, "spend_bundle": bundle.to_json_dict()}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        action = str(payload.get("action") or "build")
        out = finalize(payload) if action == "finalize" else build(payload)
        print(json.dumps(out))
    except Exception as exc:  # noqa: BLE001 -- the caller reads the reason
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
