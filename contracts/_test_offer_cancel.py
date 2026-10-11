#!/usr/bin/env python3
"""Cancelling a site-built offer (contracts/offer_cancel.py).

Owner, 2026-10-10: two site-built offers sat open; Sage never listed them (they were signed
through signCoinSpends), so there was nothing to cancel there. The offer dies when any of
its coins is spent. Pinned here, with standard wallets signing and the result validated as
a mempool would:
  - every offered coin -- XCH and a CAT -- is spent back to its own address, nothing else
    is paid but the network fee;
  - the wallet that made the offer can sign it; another wallet cannot;
  - the spends conflict with the offer's own (the same coins), so the offer can no longer
    be taken once they confirm.
Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import forge_offer_build as fob  # noqa: E402
import offer_cancel as oc  # noqa: E402
from chia.consensus.default_constants import DEFAULT_CONSTANTS  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle, match_cat_puzzle  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key  # noqa: E402
from chia.wallet.trading.offer import Offer  # noqa: E402
from chia.wallet.uncurried_puzzle import uncurry_puzzle  # noqa: E402
from chia_rs import AugSchemeMPL, Coin, CoinSpend, PrivateKey, SpendBundle, validate_clvm_and_signature  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

GENESIS = bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615")
ASSET = bytes32(b"\xc4" * 32)
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))


def wallet(seed):
    sk = PrivateKey.from_bytes(bytes([seed]) * 32)
    inner = puzzle_for_synthetic_public_key(sk.get_g1())
    return sk, inner, bytes32(inner.get_tree_hash())


def sign(sk, spends) -> bytes:
    sigs = []
    for cs in spends:
        puzzle, solution = Program.from_bytes(bytes(cs.puzzle_reveal)), Program.from_bytes(bytes(cs.solution))
        cat = match_cat_puzzle(uncurry_puzzle(puzzle))
        if cat is not None:
            puzzle, solution = list(cat)[2], solution.first()
        for cond in puzzle.run(solution).as_iter():
            items = list(cond.as_iter())
            if items[0].as_int() == 50:
                sigs.append(AugSchemeMPL.sign(sk, bytes(items[2].as_atom()) + bytes(cs.coin.name()) + GENESIS))
    return AugSchemeMPL.aggregate(sigs)


def mempool_valid(bundle) -> tuple[bool, str]:
    extra = {"AGG_SIG_ME_ADDITIONAL_DATA": GENESIS}
    for name, op in (("AGG_SIG_PARENT_ADDITIONAL_DATA", 43), ("AGG_SIG_PUZZLE_ADDITIONAL_DATA", 44),
                     ("AGG_SIG_AMOUNT_ADDITIONAL_DATA", 45), ("AGG_SIG_PUZZLE_AMOUNT_ADDITIONAL_DATA", 46),
                     ("AGG_SIG_PARENT_AMOUNT_ADDITIONAL_DATA", 47), ("AGG_SIG_PARENT_PUZZLE_ADDITIONAL_DATA", 48)):
        extra[name] = hashlib.sha256(GENESIS + bytes([op])).digest()
    constants = DEFAULT_CONSTANTS.replace(GENESIS_CHALLENGE=bytes32(GENESIS), **{k: bytes32(v) for k, v in extra.items()})
    try:
        validate_clvm_and_signature(bundle, int(constants.MAX_BLOCK_COST_CLVM), constants, 10**8)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:140]


def coin_records(sk, inner, ph, salt):
    """One XCH coin and one CAT coin of this wallet, as the site hands them to the builder."""
    xch = Coin(bytes32(bytes([salt]) * 32), ph, 2_000_000_000_000)
    outer = construct_cat_puzzle(CAT_MOD, ASSET, inner)
    grand = bytes32(bytes([salt, 0x5e]) * 16)
    parent = Coin(grand, outer.get_tree_hash(), 5_000)
    cat = Coin(parent.name(), outer.get_tree_hash(), 5_000)
    return xch, cat, grand


def make_offer(sk, inner, ph):
    xch, cat, grand = coin_records(sk, inner, ph, 0x31)
    payload = {
        "change_puzzle_hash": ph.hex(),
        "offered": [
            {"asset_id": None, "amount": "1000000000000", "coins": [{"coin": {"parent_coin_info": xch.parent_coin_info.hex(), "puzzle_hash": ph.hex(), "amount": str(xch.amount)},
                                                                       "puzzle": bytes(inner).hex()}]},
            {"asset_id": ASSET.hex(), "amount": "5000", "coins": [{"coin": {"parent_coin_info": cat.parent_coin_info.hex(), "puzzle_hash": cat.puzzle_hash.hex(), "amount": "5000"},
                                                                     "inner_puzzle": bytes(inner).hex(),
                                                                     "lineage_proof": {"parent_name": grand.hex(), "inner_puzzle_hash": ph.hex(), "amount": "5000"}}]},
        ],
        "requested": [{"asset_id": bytes32(b"\x77" * 32).hex(), "amount": "1234"}],
    }
    built = fob.build(payload)["coin_spends"]
    fix = lambda v: v if str(v).startswith("0x") else "0x" + str(v)  # noqa: E731
    spends = [CoinSpend.from_json_dict({
        "coin": {"parent_coin_info": fix(s["coin"]["parent_coin_info"]), "puzzle_hash": fix(s["coin"]["puzzle_hash"]), "amount": int(s["coin"]["amount"])},
        "puzzle_reveal": fix(s["puzzle_reveal"]), "solution": fix(s["solution"])}) for s in built]
    offer = Offer(fob._requested_payments(payload["requested"], ph, [s.coin for s in spends]),
                  SpendBundle(spends, sign(sk, spends)), fob._drivers(payload["requested"]))
    return offer.to_bech32(), {xch.name(), cat.name()}


def outputs(spends) -> list[tuple[bytes, int]]:
    out = []
    for cs in spends:
        puzzle, solution = Program.from_bytes(bytes(cs.puzzle_reveal)), Program.from_bytes(bytes(cs.solution))
        cat = match_cat_puzzle(uncurry_puzzle(puzzle))
        if cat is not None:
            puzzle, solution = list(cat)[2], solution.first()
        for cond in puzzle.run(solution).as_iter():
            items = list(cond.as_iter())
            if items[0].as_int() == 51:
                out.append((bytes(items[1].as_atom()), items[2].as_int()))
    return out


def main() -> int:
    sk, inner, ph = wallet(0x51)
    other_sk, _, _ = wallet(0x52)
    offer_text, offer_coins = make_offer(sk, inner, ph)
    fee = 60_000_000

    print("cancel: every offered coin back to its own address")
    spends, _ = oc.build(offer_text, fee)
    check("both offered coins are spent -- the XCH and the CAT", {s.coin.name() for s in spends} == offer_coins)
    outs = outputs(spends)
    check("the XCH comes back to its own address, less only the fee", (ph, 2_000_000_000_000 - fee) in outs)
    check("the CAT comes back to its own inner address, whole", (ph, 5_000) in outs)
    check("nothing else is paid", len(outs) == 2)
    ok, why = mempool_valid(SpendBundle(spends, sign(sk, spends)))
    check("signed by the wallet that made the offer, it passes validate_clvm_and_signature", ok, why)
    ok, _ = mempool_valid(SpendBundle(spends, sign(other_sk, spends)))
    check("signed by another wallet, consensus refuses it", not ok)
    try:
        oc.build(offer_text, 10**16)
        check("a fee the offer's XCH cannot cover is refused", False)
    except ValueError as exc:
        check("a fee the offer's XCH cannot cover is refused", "cover" in str(exc), str(exc)[:70])

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} offer cancel checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
