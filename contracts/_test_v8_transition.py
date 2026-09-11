#!/usr/bin/env python3
"""V8 transitions built from a real Offer, audited the way a node would.

The pool is created first through the real creation path so its singleton
lineage is genuine -- a fabricated snapshot cannot be spent, which would make
the audit meaningless.

The protocol fee changes the shape of a swap bundle: a shrinking reserve creates
two coins instead of one, so the trader's settlement is smaller than what the
curve released and the difference is a coin the recipient owns.
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")

import forge_puzzles
if not forge_puzzles.available("pool_singleton_v8"):
    print("SKIP: the V8 puzzles are archived and absent from this checkout.")
    print("      Superseded revisions are not published; see docs/FORGE_SECURITY_AUDIT.md.")
    raise SystemExit(2)   # 2 == skipped; a skip must never read as a pass

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode as OP
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD, LineageProof, SpendableCAT, construct_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.trading.offer import NotarizedPayment, OFFER_MOD, OFFER_MOD_HASH, Offer
from chia.wallet.util.curry_and_treehash import (
    calculate_hash_of_quoted_mod_hash, curry_and_treehash, shatree_atom,
)
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import Coin, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin as fs
from _test_v6_transition import USER_PH, audit, cat_maker_spend, xch_maker_spend
from forge_create_pool import deploy
from forge_offer import MODE_ADD, MODE_REMOVE, MODE_SWAP
from forge_math import invariant_lp_mint, swap_output, withdrawal_amounts
from forge_transition import build_transition

IDENTITY = Program.to(1)
NONCE = bytes32.fromhex("ee" * 32)
TREASURY = bytes32.fromhex("7e" * 32)
FEE_BPS = 30
PROTOCOL_BPS = 25
MAX_COST = 11_000_000_000

A_CAT = bytes32.fromhex("a1" * 32)
B_CAT = bytes32.fromhex("b2" * 32)
ASSETS = sorted([A_CAT, B_CAT])
BOOTSTRAP = {A_CAT: 1_000_000, B_CAT: 1_000_000}


def creation_offer() -> Offer:
    """An all-CAT creation offer: XCH backs the LP mint, each CAT seeds a reserve."""
    initial_lp = min(BOOTSTRAP.values())
    xch_amount = 2 + initial_lp
    spends = [make_spend(
        Coin(bytes32.fromhex("cd" * 32), IDENTITY.get_tree_hash(), uint64(xch_amount)),
        IDENTITY,
        Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, xch_amount]]),
    )]
    for index, asset_id in enumerate(ASSETS):
        amount = BOOTSTRAP[asset_id]
        outer = construct_cat_puzzle(CAT_MOD, asset_id, IDENTITY).get_tree_hash()
        grandparent = bytes32.fromhex(f"{index + 1:02x}" * 32)
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
        {a: PuzzleInfo({"type": "CAT", "tail": "0x" + a.hex()}) for a in ASSETS},
    )


def make_offer(spends, requested):
    drivers = {a: PuzzleInfo({"type": "CAT", "tail": "0x" + a.hex()})
               for a in requested if a is not None}
    return Offer(requested, WalletSpendBundle(spends, G2Element()), drivers)


def build_pool(protocol_bps: int):
    result = deploy({
        "offer": creation_offer().to_bech32(),
        "dry_run": True,
        "execution": {
            "protocolVersion": 8,
            # Legacy revision, minted only to prove the V8 maths still holds.
            "allowUnsafeLegacyVersion": True,
            "assetIds": [a.hex() for a in ASSETS],
            "bootstrapAmounts": [str(BOOTSTRAP[a]) for a in ASSETS],
            "swapFeeBps": FEE_BPS,
            "protocolFeeBps": protocol_bps,
            "protocolPuzzleHash": TREASURY.hex(),
            "lpRecipientPuzzleHash": USER_PH.hex(),
        },
    })
    if not result.get("success"):
        raise RuntimeError(result.get("error"))
    return fs._pool(result["poolSnapshot"]), result


def payouts_in(bundle, asset_id):
    """Everything the bundle creates in `asset_id`, keyed by inner puzzle hash."""
    quoted = calculate_hash_of_quoted_mod_hash(CAT_MOD.get_tree_hash())

    def wrap(inner):
        return curry_and_treehash(quoted, shatree_atom(CAT_MOD.get_tree_hash()),
                                  shatree_atom(asset_id), inner)

    raw = {}
    for spend in bundle.coin_spends:
        try:
            cond = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_COST)
        except Exception:
            continue
        for c in cond.get(OP.CREATE_COIN, []):
            amount = int.from_bytes(c.vars[1], "big") if c.vars[1] else 0
            if amount > 0:
                key = bytes32(c.vars[0])
                raw[key] = raw.get(key, 0) + amount
    return {inner: raw.get(wrap(inner), 0) + raw.get(inner, 0)
            for inner in (USER_PH, TREASURY, bytes32(OFFER_MOD_HASH))}


def external_of(pool, spends):
    ids = {bytes(pool.pool.coin.name())}
    ids |= {bytes(r.coin.name()) for r in pool.reserves.values()}
    ids |= {bytes(s.coin.name()) for s in spends}
    return ids


def check(label, ok):
    verdict = "PASS" if ok else "FAIL"
    print(f"  [{verdict}] {label}")
    return ok


def main() -> int:
    results = []
    pool, created = build_pool(PROTOCOL_BPS)
    reserves = [int(r[2]) for r in pool.state[0]]
    total_lp = int(pool.state[1])
    print(f"V8 pool created: version={int(pool.config[0])} "
          f"protocol_fee={int(pool.config[5])}bps reserves={reserves} lp={total_lp}")
    print()

    amount_in = 100_000
    released = swap_output(reserves[0], reserves[1], amount_in, FEE_BPS)
    fee = released * PROTOCOL_BPS // 10_000
    to_trader = released - fee

    print("swap: the trader's settlement shrinks by exactly the protocol fee")
    spends = cat_maker_spend(ASSETS[0], amount_in, 0xC1)
    offer = make_offer(spends, {ASSETS[1]: [NotarizedPayment(USER_PH, uint64(to_trader), [], NONCE)]})
    result = build_transition(pool, offer, MODE_SWAP)
    problems, checks = audit(result.bundle, external_of(pool, spends))
    paid = payouts_in(result.bundle, ASSETS[1])
    print(f"    released={released}  fee={fee}  trader={paid[USER_PH]}  treasury={paid[TREASURY]}")
    results.append(check(f"recipient paid exactly {fee}", paid[TREASURY] == fee))
    results.append(check("trader receives the rest", paid[USER_PH] == to_trader))
    results.append(check("nothing invented: trader + recipient == released",
                         paid[USER_PH] + paid[TREASURY] == released))
    results.append(check(f"bundle audits clean ({checks} assertions)", not problems))
    for p in problems:
        print(f"         - {p}")

    print()
    print("with the fee off, the bundle is the V7 shape again:")
    plain, _ = build_pool(0)
    spends0 = cat_maker_spend(ASSETS[0], amount_in, 0xC1)
    offer0 = make_offer(spends0, {ASSETS[1]: [NotarizedPayment(USER_PH, uint64(released), [], NONCE)]})
    zero = build_transition(plain, offer0, MODE_SWAP)
    problems0, checks0 = audit(zero.bundle, external_of(plain, spends0))
    paid0 = payouts_in(zero.bundle, ASSETS[1])
    results.append(check("recipient gets nothing", paid0[TREASURY] == 0))
    results.append(check("trader gets the whole release", paid0[USER_PH] == released))
    results.append(check(f"bundle audits clean ({checks0} assertions)", not problems0))

    print()
    print("liquidity moves are unaffected by the fee:")
    deposits = [reserves[0] // 4, reserves[1] // 4]
    mint = invariant_lp_mint(reserves, deposits, total_lp, FEE_BPS)
    add_spends = (cat_maker_spend(ASSETS[0], deposits[0], 0xC2)
                  + cat_maker_spend(ASSETS[1], deposits[1], 0xC3)
                  + xch_maker_spend(mint, 0xC4))
    add_offer = make_offer(add_spends, {pool.lp_asset_id: [
        NotarizedPayment(USER_PH, uint64(mint), [], NONCE)]})
    try:
        added = build_transition(pool, add_offer, MODE_ADD)
        pr, ck = audit(added.bundle, external_of(pool, add_spends))
        results.append(check(f"add builds clean (mint={mint}, {ck} assertions)", not pr))
        for p in pr:
            print(f"         - {p}")
    except Exception as exc:
        results.append(check(f"add builds: {type(exc).__name__}: {str(exc)[:70]}", False))

    burn = total_lp // 4
    payout = withdrawal_amounts(reserves, burn, total_lp)
    lp_outer = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, IDENTITY).get_tree_hash()
    gp = bytes32.fromhex("c5" * 32)
    parent = Coin(gp, lp_outer, uint64(burn))
    maker = Coin(parent.name(), lp_outer, uint64(burn))
    rm_spends = list(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        maker, pool.lp_asset_id, IDENTITY,
        Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, burn]]),
        lineage_proof=LineageProof(gp, IDENTITY.get_tree_hash(), uint64(burn)),
    )]).coin_spends)
    rm_offer = make_offer(rm_spends, {
        ASSETS[i]: [NotarizedPayment(USER_PH, uint64(payout[i]), [], NONCE)]
        for i in range(len(ASSETS)) if payout[i] > 0
    })
    try:
        removed = build_transition(pool, rm_offer, MODE_REMOVE)
        pr, ck = audit(removed.bundle, external_of(pool, rm_spends))
        results.append(check(f"remove builds clean (burn={burn}, {ck} assertions)", not pr))
        for p in pr:
            print(f"         - {p}")
        got = payouts_in(removed.bundle, ASSETS[1])
        results.append(check("a withdrawal pays the recipient nothing", got[TREASURY] == 0))
    except Exception as exc:
        results.append(check(f"remove builds: {type(exc).__name__}: {str(exc)[:70]}", False))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} V8 transition checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
