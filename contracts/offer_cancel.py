#!/usr/bin/env python3
"""Cancel an offer the site built: spend its coins back to their own addresses.

A site-built offer (trade/appOffer.ts) is signed through signCoinSpends, so Sage never
lists it among its offers and has nothing to cancel. The offer dies the moment any of its
coins is spent; this spends all of them, each to its own puzzle hash (XCH to itself, a CAT
to its own inner puzzle), and pays the network fee out of the first XCH coin. The wallet
signs these spends -- only the wallet that made the offer can.

stdin:  {offer, network_fee?}      network_fee null/omitted -> sized at 5 mojos/cost + 30%
stdout: {success, coin_spends, network_fee, cost, coins}
Only standard-wallet coins (p2_delegated_puzzle_or_hidden_puzzle, inside a CAT or not) are
rebuilt; anything else is refused rather than guessed at.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, SpendableCAT, match_cat_puzzle, unsigned_spend_bundle_for_spendable_cats  # noqa: E402
from chia.wallet.lineage_proof import LineageProof, LineageProofField  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import MOD as P2_STANDARD_MOD  # noqa: E402
from chia.wallet.trading.offer import Offer  # noqa: E402
from chia.wallet.uncurried_puzzle import uncurry_puzzle  # noqa: E402
from chia_rs import G2Element, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v16_driver as drv  # noqa: E402

ZERO = bytes32(b"\x00" * 32)
P2_STANDARD_MOD_HASH = P2_STANDARD_MOD.get_tree_hash()
LINEAGE_FIELDS = [LineageProofField.PARENT_NAME, LineageProofField.INNER_PUZZLE_HASH, LineageProofField.AMOUNT]


def _standard(puzzle: Program) -> bool:
    mod, _args = puzzle.uncurry()
    return mod.get_tree_hash() == P2_STANDARD_MOD_HASH


def build(offer_text: str, fee: int) -> tuple[list, int]:
    bundle = Offer.from_bech32(offer_text).to_spend_bundle()
    xch, cats = [], {}
    for cs in bundle.coin_spends:
        if cs.coin.parent_coin_info == ZERO:
            continue                       # a requested-payment placeholder, not a coin
        puzzle = Program.from_bytes(bytes(cs.puzzle_reveal))
        cat = match_cat_puzzle(uncurry_puzzle(puzzle))
        if cat is None:
            if not _standard(puzzle):
                raise ValueError("an offered XCH coin is not a standard wallet coin; cancel it from its wallet")
            xch.append((cs.coin, puzzle))
            continue
        _mod_hash, asset_id, inner = cat
        inner = Program.from_bytes(bytes(inner)) if not isinstance(inner, Program) else inner
        if not _standard(inner):
            raise ValueError("an offered CAT coin's inner puzzle is not a standard wallet; cancel it from its wallet")
        lineage = Program.from_bytes(bytes(cs.solution)).at("rf")   # the CAT solution's lineage proof
        proof = LineageProof.from_program(lineage, LINEAGE_FIELDS) if lineage.atom is None else LineageProof()
        cats.setdefault(bytes32(asset_id.as_atom()), []).append((cs.coin, inner, proof))
    if not xch and not cats:
        raise ValueError("this offer spends no coins")

    spends = []
    fee_left = fee
    for coin, puzzle in xch:
        take = min(fee_left, int(coin.amount))
        fee_left -= take
        conditions = []
        if int(coin.amount) - take > 0:
            conditions.append([51, coin.puzzle_hash, int(coin.amount) - take, [coin.puzzle_hash]])
        if take > 0:
            conditions.append([52, take])
        spends.append(make_spend(coin, puzzle, drv.p2_delegated_solution(conditions)))
    if fee_left > 0:
        raise ValueError("this offer's XCH coins cannot cover the network fee; cancel with no fee")
    for asset_id, entries in cats.items():
        spendable = []
        for coin, inner, proof in entries:
            inner_ph = inner.get_tree_hash()
            spendable.append(SpendableCAT(coin, asset_id, inner, drv.p2_delegated_solution(
                [[51, inner_ph, int(coin.amount), [inner_ph]]]), lineage_proof=proof))
        spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendable).coin_spends)
    return spends, fee


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        offer_text = str(payload.get("offer") or "").strip()
        if not offer_text.startswith("offer1"):
            raise ValueError("offer is a bech32 offer string")
        requested = payload.get("network_fee")
        spends, _ = build(offer_text, 0)
        conds, _ = drv.validate(SpendBundle(spends, G2Element()))
        cost = int(conds.cost)
        has_xch = any(not match_cat_puzzle(uncurry_puzzle(Program.from_bytes(bytes(s.puzzle_reveal)))) for s in spends)
        fee = int(requested) if requested is not None else (math.ceil(cost * 5 * 1.3) if has_xch else 0)
        if fee:
            spends, _ = build(offer_text, fee)
            conds, _ = drv.validate(SpendBundle(spends, G2Element()))
            cost = int(conds.cost)
        print(json.dumps({
            "success": True, "coin_spends": [s.to_json_dict() for s in spends], "network_fee": str(fee), "cost": cost,
            "coins": [s.coin.name().hex() for s in spends],
        }))
    except Exception as exc:  # noqa: BLE001 -- the caller reads the reason
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
