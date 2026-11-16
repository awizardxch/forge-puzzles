#!/usr/bin/env python3
"""Combining a wallet's coins of one asset before an offer (contracts/coin_combine.py).

Mainnet, 2026-10-23: an add of 22,012 LOVE held as 196 small coins became an offer of 197
inputs -- dropped from a full mempool, too big to sign over WalletConnect, too many coins to
read at once. The page now offers to combine them first. Pinned here, with standard wallets
signing and the bundle validated as a mempool would (testnet11 constants):
  - every coin is spent into ONE coin at the wallet's own address, hinted to it, and the
    combined coin is the one the builder names;
  - a CAT is combined whole, and the network fee comes out of an XCH coin;
  - XCH is combined less the fee;
  - the wallet signs in chunks, and finalize aggregates them; a missing chunk is refused;
  - only standard wallet coins, and only to an address the coins already sit at.
Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import coin_combine as cc  # noqa: E402
import forge_v16_driver as drv  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle, match_cat_puzzle  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key  # noqa: E402
from chia.wallet.uncurried_puzzle import uncurry_puzzle  # noqa: E402
from chia_rs import AugSchemeMPL, Coin, CoinSpend, PrivateKey, SpendBundle  # noqa: E402
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


def sign(sk, spends) -> str:
    sigs = []
    for spend in spends:
        cs = CoinSpend.from_json_dict(spend) if isinstance(spend, dict) else spend
        puzzle, solution = Program.from_bytes(bytes(cs.puzzle_reveal)), Program.from_bytes(bytes(cs.solution))
        cat = match_cat_puzzle(uncurry_puzzle(puzzle))
        if cat is not None:
            puzzle, solution = list(cat)[2], solution.first()
        for cond in puzzle.run(solution).as_iter():
            items = list(cond.as_iter())
            if items[0].as_int() == 50:
                sigs.append(AugSchemeMPL.sign(sk, bytes(items[2].as_atom()) + bytes(cs.coin.name()) + GENESIS))
    return bytes(AugSchemeMPL.aggregate(sigs)).hex()


def xch_records(inner, ph, amounts, salt):
    return [{"coin": {"parent_coin_info": bytes([salt, i % 256, i // 256]).hex() * 10 + "00" * 2, "puzzle_hash": ph.hex(), "amount": str(a)},
             "puzzle": bytes(inner).hex()} for i, a in enumerate(amounts)]


def cat_records(inner, ph, amounts, salt, full=False):
    outer = construct_cat_puzzle(CAT_MOD, ASSET, inner)
    out = []
    for i, a in enumerate(amounts):
        grand = bytes32(bytes([salt, i % 256, i // 256, 0x5e]) * 8)
        parent = Coin(grand, outer.get_tree_hash(), a)
        out.append({"coin": {"parent_coin_info": parent.name().hex(), "puzzle_hash": outer.get_tree_hash().hex(), "amount": str(a)},
                    "inner_puzzle": bytes(outer if full else inner).hex(),
                    "lineage_proof": {"parent_name": grand.hex(), "inner_puzzle_hash": ph.hex(), "amount": str(a)}})
    return out


def finalize(spends, sigs):
    return cc.finalize({"action": "finalize", "network": "testnet11", "coin_spends": spends, "signatures": sigs})


def additions(spends):
    bundle = SpendBundle([CoinSpend.from_json_dict(s) for s in spends], AugSchemeMPL.aggregate([]))
    conds, _ = drv.validate(bundle)
    return [(bytes32(c[0]), int(c[1]), bytes(c[2]) if c[2] is not None else None, s.coin_id) for s in conds.spends for c in s.create_coin]


def main() -> int:
    sk, inner, ph = wallet(0x41)
    other_sk, other_inner, other_ph = wallet(0x42)

    print("a CAT held as 150 small coins, combined into one; the fee from an XCH coin")
    amounts = [100_000 + 37 * i for i in range(150)]
    payload = {"action": "build", "network": "testnet11", "asset_id": ASSET.hex(), "own_puzzle_hash": ph.hex(),
               "coins": cat_records(inner, ph, amounts, 0x31, full=True), "fee_coins": xch_records(inner, ph, [5_000_000_000_000], 0x71)}
    out = cc.build(payload)
    spends = out["coin_spends"]
    fee = int(out["network_fee"])
    check("every CAT coin and the fee coin are spent", len(spends) == 151, f"{len(spends)} spends")
    check("the fee is sized from the bundle's cost: at least 5 mojos per cost, the 30% margin covering the fee's own spend",
          5 * out["cost"] <= fee <= 7 * out["cost"], f"fee {fee}, cost {out['cost']}")
    outer_ph = construct_cat_puzzle(CAT_MOD, ASSET, inner).get_tree_hash()
    adds = additions(spends)
    cat_outs = [a for a in adds if a[0] == outer_ph and a[1] == sum(amounts)]
    check("ONE coin of the whole CAT amount, at the wallet's own address (CAT-wrapped), hinted to that address",
          len(cat_outs) == 1 and cat_outs[0][2] == bytes(ph), f"{len(cat_outs)} combined coin(s)")
    check("nothing else is created but the fee coin's change", len(adds) == 2
          and any(a[0] == ph and a[1] == 5_000_000_000_000 - fee for a in adds))
    combined = out["combined"]
    parent = bytes32.fromhex(payload["coins"][0]["coin"]["parent_coin_info"])
    first = Coin(parent, outer_ph, amounts[0]).name()
    check("the combined coin the builder names is that coin (child of the first CAT coin)",
          combined["coin_id"] == drv.coin_id(first, outer_ph, sum(amounts)).hex() and combined["amount"] == str(sum(amounts)))
    print(f"    cost of a 150-coin CAT combine: {out['cost']:,} ({out['cost'] / 11_000_000_000:.1%} of a block), fee {fee:,} mojos")

    print("signed in chunks, as the page asks the wallet; finalize aggregates them")
    chunks = [spends[i:i + 25] for i in range(0, len(spends), 25)]
    sigs = [sign(sk, chunk) for chunk in chunks]
    done = finalize(spends, sigs)
    check(f"{len(chunks)} chunk signatures aggregate into one bundle that validates (signatures included)",
          done.get("success") is True and "spend_bundle" in done)
    try:
        finalize(spends, sigs[:-1])
        check("a missing chunk's signature is refused", False)
    except ValueError as exc:
        check("a missing chunk's signature is refused", "does not validate" in str(exc), str(exc)[:80])
    try:
        finalize(spends, [sign(other_sk, chunk) for chunk in chunks])
        check("another wallet's signatures are refused", False)
    except ValueError:
        check("another wallet's signatures are refused", True)

    print("XCH held as many coins, combined less the fee")
    xamounts = [1_000_000_000 + i for i in range(40)]
    xout = cc.build({"action": "build", "network": "testnet11", "asset_id": None, "own_puzzle_hash": ph.hex(),
                     "coins": xch_records(inner, ph, xamounts, 0x51)})
    xfee = int(xout["network_fee"])
    xadds = additions(xout["coin_spends"])
    print(f"    cost of a 40-coin XCH combine: {xout['cost']:,}")
    check("one coin, the total less the fee, at the wallet's own address, hinted", xadds == [(ph, sum(xamounts) - xfee, bytes(ph), xadds[0][3])])
    check("and it validates signed", finalize(xout["coin_spends"], [sign(sk, xout["coin_spends"])]).get("success") is True)

    print("refusals")
    def refused(label, change, needle):
        try:
            cc.build({**payload, **change})
            check(label, False)
        except ValueError as exc:
            check(label, needle in str(exc), str(exc)[:90])
    refused("to an address none of the coins sits at", {"own_puzzle_hash": other_ph.hex()}, "already sits at")
    odd = Program.to((1, [[51, ph, 1]]))   # anyone-can-spend: not a wallet's puzzle
    odd_outer = construct_cat_puzzle(CAT_MOD, ASSET, odd)
    bad = cat_records(inner, ph, [10, 20], 0x33)
    bad[1]["inner_puzzle"] = bytes(odd).hex()
    bad[1]["coin"]["puzzle_hash"] = odd_outer.get_tree_hash().hex()
    refused("a coin that is not a standard wallet coin", {"coins": bad}, "standard wallet")
    refused("one coin (nothing to combine)", {"coins": payload["coins"][:1]}, "at least two")
    refused("a CAT fee with no XCH to pay it", {"fee_coins": [], "network_fee": "1000"}, "XCH coins to pay")
    refused("more coins than one combine takes", {"coins": cat_records(inner, ph, [1] * 301, 0x35)}, "at most")
    # The mempool takes at most half a block's cost in one transaction: 150 CAT coins are 41% of
    # a block, so a combine over the cap is refused here and the page splits it (consolidate.ts).
    refused("a combine costlier than one transaction may be", {"coins": cat_records(inner, ph, [1_000 + i for i in range(200)], 0x36)}, "cost")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} coin combine checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
