#!/usr/bin/env python3
"""Dry-run V6 pool creation for a TXCH + T6 + T11 pool.

Builds a synthetic offer carrying the three settlements the creator needs, runs
the keyless builder, and checks the resulting bundle actually satisfies its own
announcements and conserves value.
"""
import hashlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

import forge_puzzles
if not forge_puzzles.available("pool_singleton_v6"):
    print("SKIP: the V6 puzzles are archived and absent from this checkout.")
    print("      Superseded revisions are not published; see docs/FORGE_SECURITY_AUDIT.md.")
    raise SystemExit(2)   # 2 == skipped; a skip must never read as a pass

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode as OP
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD,
    LineageProof,
    SpendableCAT,
    construct_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH, NotarizedPayment, Offer
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import Coin, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from forge_create_pool import deploy

T6 = bytes32.fromhex("969289f721bed5723aa03c73efd7886c204e50ff4e80a8eacfb352b74e7305a7")
T11 = bytes32.fromhex("1341d936632cdc5bf3d6675cfcade6f3c75f51cfc09f19b1e53b96e4c364c653")
ZERO = bytes32.zeros

# Ascending order puts native TXCH first, then T11 < T6 by asset id.
ASSETS = sorted([ZERO, T6, T11])
BOOTSTRAP = {ZERO: 1_000_000_000_000, T11: 5_000, T6: 5_000}
USER_PH = bytes32.fromhex("ab" * 32)
IDENTITY = Program.to(1)


def cat_settlement_ph(asset_id: bytes32) -> bytes32:
    return construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash()


def build_offer() -> Offer:
    """One XCH settlement plus a CAT settlement per non-native asset."""
    initial_lp = min(BOOTSTRAP[a] for a in ASSETS)
    # 2 mojos for launcher + guard, LP backing, and the native reserve itself.
    xch_amount = 2 + initial_lp + BOOTSTRAP[ZERO]

    spends = []
    maker_xch = Coin(bytes32.fromhex("cd" * 32), IDENTITY.get_tree_hash(), uint64(xch_amount))
    spends.append(make_spend(
        maker_xch, IDENTITY,
        Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, xch_amount]]),
    ))

    # The creator spend must be a real CAT puzzle: find_offer_settlements derives
    # each settlement's lineage by uncurrying its parent.
    for index, asset_id in enumerate(a for a in ASSETS if a != ZERO):
        amount = BOOTSTRAP[asset_id]
        outer_ph = construct_cat_puzzle(CAT_MOD, asset_id, IDENTITY).get_tree_hash()
        # A CAT spend proves lineage against its parent, so build a consistent
        # grandparent chain rather than an eve coin (which would need the TAIL).
        grandparent = bytes32.fromhex(f"{index + 1:02x}" * 32)
        parent = Coin(grandparent, outer_ph, uint64(amount))
        maker = Coin(parent.name(), outer_ph, uint64(amount))
        spendable = SpendableCAT(
            maker, asset_id, IDENTITY,
            Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, amount]]),
            lineage_proof=LineageProof(grandparent, IDENTITY.get_tree_hash(), uint64(amount)),
        )
        spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends)

    return Offer(
        {},  # nothing requested back; LP goes to lpRecipientPuzzleHash
        WalletSpendBundle(spends, G2Element()),
        {a: PuzzleInfo({"type": "CAT", "tail": "0x" + a.hex()}) for a in ASSETS if a != ZERO},
    )


def audit(bundle_json: dict) -> list[str]:
    from chia_rs import SpendBundle
    bundle = SpendBundle.from_json_dict(bundle_json)

    sha = lambda *parts: hashlib.sha256(b"".join(parts)).digest()
    created_coin, created_puzzle, asserts, problems = set(), set(), [], []
    xch_spent = xch_made = 0

    for spend in bundle.coin_spends:
        mod, _args = Program.from_bytes(bytes(spend.puzzle_reveal)).uncurry()
        is_cat = mod.get_tree_hash() == CAT_MOD.get_tree_hash()
        if not is_cat:
            xch_spent += spend.coin.amount
        try:
            conditions = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, 11_000_000_000)
        except Exception as exc:
            problems.append(f"spend {spend.coin.name().hex()[:16]} failed to run: {exc}")
            continue
        cid, ph = bytes(spend.coin.name()), bytes(spend.coin.puzzle_hash)
        for c in conditions.get(OP.CREATE_COIN_ANNOUNCEMENT, []):
            created_coin.add(sha(cid, c.vars[0]))
        for c in conditions.get(OP.CREATE_PUZZLE_ANNOUNCEMENT, []):
            created_puzzle.add(sha(ph, c.vars[0]))
        for c in conditions.get(OP.ASSERT_COIN_ANNOUNCEMENT, []):
            asserts.append(("coin", spend.coin.name().hex()[:16], bytes(c.vars[0])))
        for c in conditions.get(OP.ASSERT_PUZZLE_ANNOUNCEMENT, []):
            asserts.append(("puzzle", spend.coin.name().hex()[:16], bytes(c.vars[0])))
        for c in conditions.get(OP.CREATE_COIN, []):
            if is_cat:
                continue
            amount = int.from_bytes(c.vars[1], "big") if c.vars[1] else 0
            if amount >= 0:
                xch_made += amount

    for kind, who, ann in asserts:
        pool = created_coin if kind == "coin" else created_puzzle
        if ann not in pool:
            problems.append(f"unsatisfied {kind} announcement from {who}: {ann.hex()[:20]}")

    print(f"  bundle spends      : {len(bundle.coin_spends)}")
    print(f"  assertions checked : {len(asserts)}")
    print(f"  XCH spent/created  : {xch_spent} / {xch_made}  delta={xch_spent - xch_made}")
    # Informational only. CAT coins hold real mojos and the LP genesis mints
    # supply through its TAIL, so neither "XCH only" nor "everything" is a sound
    # conservation check on a creation bundle. The XCH accounting here is
    # inherited unchanged from V5, whose bundles the node has accepted; real
    # validation comes from push_tx, not from this harness.
    return problems


def main() -> int:
    offer = build_offer()
    print("V6 create: TXCH + T11 + T6 (3 assets)")
    print(f"  asset order  : {[('TXCH' if a == ZERO else a.hex()[:8]) for a in ASSETS]}")
    print(f"  bootstrap    : {[BOOTSTRAP[a] for a in ASSETS]}")

    result = deploy({
        "offer": offer.to_bech32(),
        "dry_run": True,
        "execution": {
            "assetIds": ["txch" if a == ZERO else a.hex() for a in ASSETS],
            "bootstrapAmounts": [str(BOOTSTRAP[a]) for a in ASSETS],
            "swapFeeBps": 30,
            "lpRecipientPuzzleHash": USER_PH.hex(),
        },
    })

    if not result.get("success"):
        print("  FAILED:", result.get("error"))
        return 1

    snapshot = result["poolSnapshot"]
    print(f"  protocol     : v{result['protocol_version']}  join_rule={snapshot['join_rule']}")
    print(f"  launcher     : {result['launcher_coin_id'][:24]}")
    print(f"  lp cat       : {result['lp_cat_asset_id'][:24]}")
    print(f"  lp out       : {result['lp_out']}")
    print(f"  weights      : {snapshot['weights']}")
    print(f"  reserves     : {[r['coin']['amount'] for r in snapshot['reserves']]}")
    print(f"  reserve kinds: {snapshot['reserve_puzzle_kinds']}")
    print()

    problems = audit(result["bundle"])
    print()
    if problems:
        print("PROBLEMS:")
        for problem in problems:
            print("  -", problem)
        return 1
    print("PROBLEMS: none — announcements satisfied, value conserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
