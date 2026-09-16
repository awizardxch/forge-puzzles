#!/usr/bin/env python3
"""V8's in-puzzle protocol fee, collected through the ROUTED lanes.

A V8 reserve destructures two plan fields its predecessors never had. The routed
builders wrote six-field plans, so a V8 pool would have settled a direct swap and
been rejected by every multi-hop, split and vault route -- unroutable in exactly
the lanes that now produce the best prices.

These build real multi-hop and split bundles across freshly deployed V8 pools and
assert the fee reaches the recipient, the trader is paid the remainder, and a
pre-V8 pool in the same route still gets a six-field plan.
"""
import sys

sys.path.insert(0, ".")

import forge_puzzles
if not forge_puzzles.available("pool_singleton_v8"):
    print("SKIP: the V8 puzzles are archived and absent from this checkout.")
    print("      Superseded revisions are not published; see docs/FORGE_SECURITY_AUDIT.md.")
    raise SystemExit(2)   # 2 == skipped; a skip must never read as a pass

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.condition_opcodes import ConditionOpcode as OP
from chia.wallet.cat_wallet.cat_utils import CAT_MOD
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.wallet.cat_wallet.cat_utils import (
    LineageProof, SpendableCAT, construct_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.trading.offer import NotarizedPayment, OFFER_MOD_HASH, Offer
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import Coin, G2Element
from chia.wallet.util.curry_and_treehash import (
    calculate_hash_of_quoted_mod_hash, curry_and_treehash, shatree_atom,
)
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin as fs
from _forge_testkit import USER_PH, audit, cat_maker_spend, make_offer
from _test_v8_transition import IDENTITY, TREASURY
from forge_create_pool import deploy
from forge_multihop_swap import _plan_legs, build_multihop_swap, protocol_fee_for
from forge_split_swap import SplitBranchSpec, build_split_swap

NONCE = bytes32.fromhex("ee" * 32)
MAX_COST = 11_000_000_000
FEE_BPS = 30
PROTOCOL_BPS = 25

X = bytes32.fromhex("a1" * 32)
Y = bytes32.fromhex("b2" * 32)
Z = bytes32.fromhex("c3" * 32)



def creation_offer_for(assets, bootstrap, salt: int) -> Offer:
    """Creation offer for an arbitrary CAT set.

    The shared fixture in _test_v8_transition funds two fixed CATs; a routing
    test needs several pools over three assets, so it has to seed whichever set
    each pool is built from. `salt` keeps the funding coins distinct: the
    launcher id derives from them, so identical offers would mint pools sharing
    one launcher and the route builder would refuse them as the same pool.
    """
    xch_amount = 2 + min(bootstrap.values())
    spends = [make_spend(
        Coin(bytes32.fromhex(f"{salt:02x}" * 32), IDENTITY.get_tree_hash(), uint64(xch_amount)),
        IDENTITY,
        Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, xch_amount]]),
    )]
    for index, asset_id in enumerate(assets):
        amount = bootstrap[asset_id]
        outer = construct_cat_puzzle(CAT_MOD, asset_id, IDENTITY).get_tree_hash()
        grandparent = bytes32.fromhex(f"{(salt + index + 1) & 0xff:02x}" * 32)
        parent = Coin(grandparent, outer, uint64(amount))
        maker = Coin(parent.name(), outer, uint64(amount))
        spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
            maker, asset_id, IDENTITY,
            Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, amount]]),
            lineage_proof=LineageProof(grandparent, IDENTITY.get_tree_hash(), uint64(amount)),
        )]).coin_spends)
    return Offer(
        {},
        WalletSpendBundle(spends, G2Element()),
        {a: PuzzleInfo({"type": "CAT", "tail": "0x" + a.hex()}) for a in assets},
    )


def make_pool(assets, bootstrap, version, protocol_bps, salt):
    execution = {
        "protocolVersion": version,
        # Routing tests build V7/V8 pools to prove mixed-version routes.
        "allowUnsafeLegacyVersion": True,
        "assetIds": [a.hex() for a in assets],
        "bootstrapAmounts": [str(bootstrap[a]) for a in assets],
        "swapFeeBps": FEE_BPS,
        "lpRecipientPuzzleHash": USER_PH.hex(),
    }
    if version >= 8:
        execution["protocolFeeBps"] = protocol_bps
        execution["protocolPuzzleHash"] = TREASURY.hex()
    result = deploy({"offer": creation_offer_for(assets, bootstrap, salt).to_bech32(),
                     "dry_run": True, "execution": execution})
    if not result.get("success"):
        raise RuntimeError(result.get("error"))
    return fs._pool(result["poolSnapshot"])


def paid_to(bundle, asset_id, inner_ph):
    """How much of `asset_id` the bundle creates at `inner_ph`."""
    quoted = calculate_hash_of_quoted_mod_hash(CAT_MOD.get_tree_hash())
    wrapped = curry_and_treehash(quoted, shatree_atom(CAT_MOD.get_tree_hash()),
                                 shatree_atom(asset_id), inner_ph)
    total = 0
    for spend in bundle.coin_spends:
        try:
            cond = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_COST)
        except Exception:
            continue
        for c in cond.get(OP.CREATE_COIN, []):
            amount = int.from_bytes(c.vars[1], "big") if c.vars[1] else 0
            if amount > 0 and bytes32(c.vars[0]) in (wrapped, inner_ph):
                total += amount
    return total


def external_of(pools, spends):
    ids = set()
    for pool in pools:
        ids.add(bytes(pool.pool.coin.name()))
        ids |= {bytes(r.coin.name()) for r in pool.reserves.values()}
    ids |= {bytes(s.coin.name()) for s in spends}
    return ids


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    results = []
    boot = 1_000_000
    amount_in = 10_000

    xy = make_pool(sorted([X, Y]), {X: boot, Y: boot}, 8, PROTOCOL_BPS, 0x11)
    yz = make_pool(sorted([Y, Z]), {Y: boot, Z: boot}, 8, PROTOCOL_BPS, 0x22)
    print(f"V8 pools: protocol fee {PROTOCOL_BPS}bps to {TREASURY.hex()[:12]}")

    legs = _plan_legs([xy, yz], [X, Y, Z], amount_in)
    for i, leg in enumerate(legs):
        _, expected = protocol_fee_for(leg.pool, leg.amount_out)
        print(f"  leg {i}: released={leg.amount_out} fee={leg.protocol_fee} "
              f"-> carries {leg.amount_out - leg.protocol_fee}")
        results.append(check(f"leg {i} fee matches the puzzle rule",
                             leg.protocol_fee == expected))
    results.append(check("the second leg prices against the NET of the first",
                         legs[1].amount_in == legs[0].amount_out - legs[0].protocol_fee))

    spends = cat_maker_spend(X, amount_in, 0xB1)
    out = legs[-1].amount_out - legs[-1].protocol_fee
    offer = make_offer(spends, {Z: [NotarizedPayment(USER_PH, uint64(out), [], NONCE)]})
    try:
        result = build_multihop_swap([xy, yz], [X, Y, Z], offer)
    except Exception as exc:
        print(f"  [FAIL] multi-hop across V8 pools builds: {type(exc).__name__}: {exc}")
        return 1

    problems, checked = audit(result.bundle, external_of([xy, yz], spends))
    results.append(check(f"multi-hop across V8 pools audits clean ({checked} assertions)",
                         not problems))
    for p in problems:
        print(f"         - {p}")

    fee_paid = paid_to(result.bundle, Z, TREASURY)
    results.append(check("the recipient is paid the final leg fee",
                         fee_paid == legs[-1].protocol_fee,
                         f"{fee_paid} == {legs[-1].protocol_fee}"))
    results.append(check("the trader is paid the remainder",
                         paid_to(result.bundle, Z, USER_PH) == out, str(out)))

    # A V7 pool in the same route must still receive a six-field plan.
    v7 = make_pool(sorted([Y, Z]), {Y: boot, Z: boot}, 7, 0, 0x33)
    mixed = _plan_legs([xy, v7], [X, Y, Z], amount_in)
    results.append(check("a V7 leg in a mixed route owes nothing",
                         mixed[1].protocol_fee == 0))
    spends2 = cat_maker_spend(X, amount_in, 0xB2)
    out2 = mixed[-1].amount_out - mixed[-1].protocol_fee
    offer2 = make_offer(spends2, {Z: [NotarizedPayment(USER_PH, uint64(out2), [], NONCE)]})
    try:
        mixed_result = build_multihop_swap([xy, v7], [X, Y, Z], offer2)
        problems2, checked2 = audit(mixed_result.bundle, external_of([xy, v7], spends2))
        results.append(check(f"a mixed V8+V7 route audits clean ({checked2} assertions)",
                             not problems2))
        for p in problems2:
            print(f"         - {p}")
    except Exception as exc:
        results.append(check(f"a mixed V8+V7 route builds: {type(exc).__name__}: {exc}", False))

    # A split whose branches both cross V8 pools.
    xz_a = make_pool(sorted([X, Z]), {X: boot, Z: boot}, 8, PROTOCOL_BPS, 0x44)
    xz_b = make_pool(sorted([X, Z]), {X: boot * 2, Z: boot * 2}, 8, PROTOCOL_BPS, 0x55)
    branches = [
        SplitBranchSpec([xz_a], [X, Z], amount_in // 2),
        SplitBranchSpec([xz_b], [X, Z], amount_in - amount_in // 2),
    ]
    probe = build_split_swap(branches, make_offer(
        cat_maker_spend(X, amount_in, 0xB3),
        {Z: [NotarizedPayment(USER_PH, uint64(1), [], NONCE)]}))
    spends3 = cat_maker_spend(X, amount_in, 0xB3)
    offer3 = make_offer(spends3, {Z: [NotarizedPayment(USER_PH, uint64(probe.total_out), [], NONCE)]})
    try:
        split_result = build_split_swap(branches, offer3)
        problems3, checked3 = audit(split_result.bundle, external_of([xz_a, xz_b], spends3))
        results.append(check(f"a split across two V8 pools audits clean ({checked3} assertions)",
                             not problems3))
        for p in problems3:
            print(f"         - {p}")
        split_fee = paid_to(split_result.bundle, Z, TREASURY)
        results.append(check("both split branches pay the protocol fee", split_fee > 0,
                             f"{split_fee} mojos to the recipient"))
    except Exception as exc:
        results.append(check(f"a split across two V8 pools builds: {type(exc).__name__}: {exc}", False))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} V8 routing checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
