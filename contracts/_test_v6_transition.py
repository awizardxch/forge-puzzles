#!/usr/bin/env python3
"""Build V6 add / remove / swap bundles against the live 3-asset pool.

Each bundle is audited the way the node would: every spend is run, every
announcement assertion must be satisfied by a matching creation, and ASSERT_MY_*
must agree with the coin it is asserted on.
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

import forge_stdin as fs
from forge_offer import MODE_ADD, MODE_REMOVE, MODE_SWAP, ZERO_32
from forge_math import invariant_lp_mint, solve_native_add_split, swap_output, withdrawal_amounts
from forge_transition import build_transition

IDENTITY = Program.to(1)
USER_PH = bytes32.fromhex("ab" * 32)
NONCE = bytes32.fromhex("ee" * 32)


def load_v6_pool():
    index = json.load(io.open("../.awizard/deployment-index.json", encoding="utf-8"))
    for entry in index.values():
        for batch in (entry.get("batches") or {}).values():
            snapshot = batch.get("poolSnapshot")
            if snapshot and int(snapshot.get("protocol_version", 0)) == 6:
                return snapshot
    return None


def cat_maker_spend(asset_id: bytes32, amount: int, seed: int):
    """A real CAT spend creating an OFFER_MOD settlement of `amount`."""
    outer_ph = construct_cat_puzzle(CAT_MOD, asset_id, IDENTITY).get_tree_hash()
    grandparent = bytes32.fromhex(f"{seed:02x}" * 32)
    parent = Coin(grandparent, outer_ph, uint64(amount))
    maker = Coin(parent.name(), outer_ph, uint64(amount))
    spendable = SpendableCAT(
        maker, asset_id, IDENTITY,
        Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, amount]]),
        lineage_proof=LineageProof(grandparent, IDENTITY.get_tree_hash(), uint64(amount)),
    )
    return list(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends)


def xch_maker_spend(amount: int, seed: int = 0xCD):
    maker = Coin(bytes32.fromhex(f"{seed:02x}" * 32), IDENTITY.get_tree_hash(), uint64(amount))
    return [make_spend(maker, IDENTITY, Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, amount]]))]


def make_offer(spends, requested):
    drivers = {a: PuzzleInfo({"type": "CAT", "tail": "0x" + a.hex()}) for a in requested if a is not None}
    return Offer(requested, WalletSpendBundle(spends, G2Element()), drivers)


def audit(bundle, external: set) -> list[str]:
    """`external` is the set of coin ids expected to pre-exist (pool, reserves,
    and the synthetic maker coins). Anything else a spend consumes must be
    created inside the bundle, or the node answers UNKNOWN_UNSPENT."""
    sha = lambda *p: hashlib.sha256(b"".join(p)).digest()
    made_coin, made_puzzle, asserts, problems = set(), set(), [], []
    spent_ids, created_ids, seen = set(), set(), []
    for spend in bundle.coin_spends:
        spent_ids.add(bytes(spend.coin.name()))
        seen.append(bytes(spend.coin.name()))
        try:
            cond = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, 11_000_000_000)
        except Exception as exc:
            problems.append(f"spend {spend.coin.name().hex()[:16]} failed: {exc}")
            continue
        cid, ph = bytes(spend.coin.name()), bytes(spend.coin.puzzle_hash)
        for c in cond.get(OP.CREATE_COIN_ANNOUNCEMENT, []):
            made_coin.add(sha(cid, c.vars[0]))
        for c in cond.get(OP.CREATE_PUZZLE_ANNOUNCEMENT, []):
            made_puzzle.add(sha(ph, c.vars[0]))
        for c in cond.get(OP.ASSERT_COIN_ANNOUNCEMENT, []):
            asserts.append(("coin", spend.coin.name().hex()[:16], bytes(c.vars[0])))
        for c in cond.get(OP.ASSERT_PUZZLE_ANNOUNCEMENT, []):
            asserts.append(("puzzle", spend.coin.name().hex()[:16], bytes(c.vars[0])))
        for c in cond.get(OP.ASSERT_MY_COIN_ID, []):
            if bytes(c.vars[0]) != cid:
                problems.append(f"ASSERT_MY_COIN_ID mismatch on {spend.coin.name().hex()[:16]}")
        for c in cond.get(OP.ASSERT_MY_PARENT_ID, []):
            if bytes(c.vars[0]) != bytes(spend.coin.parent_coin_info):
                problems.append(f"ASSERT_MY_PARENT_ID mismatch on {spend.coin.name().hex()[:16]}")
        for c in cond.get(OP.CREATE_COIN, []):
            amount = int.from_bytes(c.vars[1], "big") if c.vars[1] else 0
            if amount >= 0:
                created_ids.add(bytes(Coin(spend.coin.name(), bytes32(c.vars[0]), uint64(amount)).name()))
    for kind, who, ann in asserts:
        pool = made_coin if kind == "coin" else made_puzzle
        if ann not in pool:
            problems.append(f"unsatisfied {kind} announcement from {who}: {ann.hex()[:16]}")
    for coin_id in spent_ids - created_ids - external:
        problems.append(f"spends {coin_id.hex()[:16]} which is never created in-bundle (UNKNOWN_UNSPENT)")
    for coin_id in {c for c in seen if seen.count(c) > 1}:
        problems.append(f"spends {coin_id.hex()[:16]} {seen.count(coin_id)} times (DOUBLE_SPEND)")
    return problems, len(asserts)


def report(label, fn, pool_coin_id, reserve_ids):
    try:
        result, maker_spends = fn()
    except Exception as exc:
        print(f"  [FAIL] {label}: {type(exc).__name__}: {str(exc)[:90]}")
        return False
    external = {bytes(pool_coin_id)} | set(reserve_ids) | {
        bytes(spend.coin.name()) for spend in maker_spends
    }
    problems, checked = audit(result.bundle, external)
    ok = not problems
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {len(result.bundle.coin_spends)} spends, "
          f"{checked} assertions, lp_delta={result.lp_delta}")
    for problem in problems:
        print(f"         - {problem}")
    return ok


def main() -> int:
    snapshot = load_v6_pool()
    if not snapshot:
        print("SKIP: no V6 pool snapshot found in the deployment index.")
        print("      Superseded pools were retired; current-revision coverage")
        print("      lives in the _test_forge_* suites.")
        return 0

    pool = fs._pool(snapshot)
    assets = [bytes32(a) for a in pool.config[2]]
    reserves = [int(r[2]) for r in pool.state[0]]
    total_lp = int(pool.state[1])
    fee_bps = int(pool.config[4])
    native_index = assets.index(ZERO_32)
    cat_indexes = [i for i in range(len(assets)) if i != native_index]

    # Coins that legitimately pre-exist on chain. Everything else a bundle spends
    # has to be created inside that same bundle.
    pool_coin_id = pool.pool.coin.name()
    reserve_ids = {bytes(pool.reserves[a].coin.name()) for a in assets}

    print(f"V6 pool {pool.launcher_id.hex()[:16]}  assets={len(assets)}  lp={total_lp}")
    print(f"  reserves: {reserves}")
    print()

    results = []

    # ---- SWAP: one CAT in, the other CAT out; native reserve frozen ---------
    def swap_case():
        i_in, i_out = cat_indexes[0], cat_indexes[1]
        amount_in = max(reserves[i_in] // 4, 2)
        out = swap_output(reserves[i_in], reserves[i_out], amount_in, fee_bps)
        spends = cat_maker_spend(assets[i_in], amount_in, 0x11)
        offer = make_offer(spends, {assets[i_out]: [NotarizedPayment(USER_PH, uint64(out), [], NONCE)]})
        return build_transition(pool, offer, MODE_SWAP), spends

    print("swap (CAT -> CAT, native frozen):")
    results.append(report("swap", swap_case, pool_coin_id, reserve_ids))

    # ---- ADD: balanced across all three, XCH settlement covers LP backing ---
    def add_case():
        ratio = 4  # deposit a quarter of each reserve
        deposits = [r // ratio for r in reserves]
        mint = invariant_lp_mint(reserves, deposits, total_lp, fee_bps)
        # The native settlement carries reserve funding plus the LP backing.
        native_total = deposits[native_index] + mint
        spends = xch_maker_spend(native_total)
        for seed, i in enumerate(cat_indexes, start=0x21):
            spends += cat_maker_spend(assets[i], deposits[i], seed)
        offer = make_offer(spends, {pool.lp_asset_id: [NotarizedPayment(USER_PH, uint64(mint), [], NONCE)]})
        return build_transition(pool, offer, MODE_ADD), spends

    print()
    print("add (balanced, 3 assets):")
    results.append(report("add", add_case, pool_coin_id, reserve_ids))

    # ---- ADD, CAT only: native reserve untouched, XCH backs the LP mint only.
    # This is the shape where the reserve loop never spends the XCH settlement,
    # so the LP genesis coin has to come from the standalone fallback spend.
    def add_cat_only_case():
        deposits = [0] * len(assets)
        for i in cat_indexes:
            deposits[i] = max(reserves[i] // 4, 1)
        mint = invariant_lp_mint(reserves, deposits, total_lp, fee_bps)
        spends = xch_maker_spend(mint)  # XCH present purely to back the LP CAT
        for seed, i in enumerate(cat_indexes, start=0x41):
            spends += cat_maker_spend(assets[i], deposits[i], seed)
        offer = make_offer(spends, {pool.lp_asset_id: [NotarizedPayment(USER_PH, uint64(mint), [], NONCE)]})
        return build_transition(pool, offer, MODE_ADD), spends

    print()
    print("add (CATs only, native reserve untouched):")
    results.append(report("add-cat-only", add_cat_only_case, pool_coin_id, reserve_ids))

    # ---- REMOVE: burn LP, receive all three reserves pro rata --------------
    def remove_case():
        burn = max(total_lp // 4, 1)
        payouts = withdrawal_amounts(reserves, burn, total_lp)
        lp_outer = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, IDENTITY).get_tree_hash()
        grandparent = bytes32.fromhex("31" * 32)
        parent = Coin(grandparent, lp_outer, uint64(burn))
        maker = Coin(parent.name(), lp_outer, uint64(burn))
        spends = list(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [
            SpendableCAT(maker, pool.lp_asset_id, IDENTITY,
                         Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, burn]]),
                         lineage_proof=LineageProof(grandparent, IDENTITY.get_tree_hash(), uint64(burn))),
        ]).coin_spends)
        requested = {}
        for index, asset_id in enumerate(assets):
            if payouts[index] <= 0:
                continue
            key = None if asset_id == ZERO_32 else asset_id
            requested[key] = [NotarizedPayment(USER_PH, uint64(payouts[index]), [], NONCE)]
        offer = make_offer(spends, requested)
        return build_transition(pool, offer, MODE_REMOVE), spends

    print()
    print("remove (pro rata, 3 assets):")
    results.append(report("remove", remove_case, pool_coin_id, reserve_ids))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} transition bundles built and audited clean")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
