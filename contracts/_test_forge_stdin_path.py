#!/usr/bin/env python3
"""The forge_stdin add/remove dispatch (the liquidity responder's lane) on V9.

The responder feeds {action, offer, pool} to forge_stdin.build. For V8+ pools
that now routes to build_transition (the v3 builder predates the protocol-fee
config), and from V9 that builder binds the LP action coin. This drives the real
stdin entrypoint against a V9 pool for both add and remove.
"""
import sys

sys.path.insert(0, ".")

from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode as OP
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD, LineageProof, SpendableCAT, construct_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.trading.offer import NotarizedPayment, OFFER_MOD_HASH
from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin as fs
from _forge_testkit import USER_PH, audit, cat_maker_spend, xch_maker_spend
from _forge_testkit import creation_offer, make_offer, external_of, ASSETS, BOOTSTRAP, FEE_BPS, TREASURY
from forge_create_pool import deploy
from forge_math import invariant_lp_mint, withdrawal_amounts

IDENTITY = Program.to(1)
NONCE = bytes32.fromhex("ee" * 32)


def v9_pool():
    res = deploy({
        "offer": creation_offer().to_bech32(), "dry_run": True,
        "execution": {
            "protocolVersion": 10,
            "assetIds": [a.hex() for a in ASSETS],
            "bootstrapAmounts": [str(BOOTSTRAP[a]) for a in ASSETS],
            "swapFeeBps": FEE_BPS, "protocolFeeBps": 25,
            "protocolPuzzleHash": TREASURY.hex(), "lpRecipientPuzzleHash": USER_PH.hex(),
        },
    })
    if not res.get("success"):
        raise RuntimeError(res.get("error"))
    return fs._pool(res["poolSnapshot"]), res["poolSnapshot"]


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    pool, res_snapshot = v9_pool()
    reserves = [int(r[2]) for r in pool.state[0]]
    total_lp = int(pool.state[1])
    results = []

    # ADD via the v3 builder.
    deposits = [reserves[0] // 4, reserves[1] // 4]
    mint = invariant_lp_mint(reserves, deposits, total_lp, FEE_BPS)
    add_spends = (cat_maker_spend(ASSETS[0], deposits[0], 0xD2)
                  + cat_maker_spend(ASSETS[1], deposits[1], 0xD3)
                  + xch_maker_spend(mint, 0xD4))
    add_offer = make_offer(add_spends, {pool.lp_asset_id: [
        NotarizedPayment(USER_PH, uint64(mint), [], NONCE)]})
    try:
        out = fs.build({"action": "add", "offer": add_offer.to_bech32(),
                        "pool": res_snapshot})
        results.append(check(f"stdin add succeeds (mint={mint})", out.get("success") is True,
                             "" if out.get("success") else str(out.get("error"))[:80]))
    except Exception as exc:
        results.append(check(f"stdin add: {type(exc).__name__}: {str(exc)[:80]}", False))

    # REMOVE via the v3 builder -- the critical path.
    burn = total_lp // 4
    payout = withdrawal_amounts(reserves, burn, total_lp)
    lp_outer = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, IDENTITY).get_tree_hash()
    gp = bytes32.fromhex("d5" * 32)
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
        out = fs.build({"action": "remove", "offer": rm_offer.to_bech32(),
                        "pool": res_snapshot})
        results.append(check(f"stdin remove succeeds (burn={burn})", out.get("success") is True,
                             "" if out.get("success") else str(out.get("error"))[:80]))
    except Exception as exc:
        results.append(check(f"stdin remove: {type(exc).__name__}: {str(exc)[:80]}", False))

    # ── the snapshot round trip ─────────────────────────────────────────────
    #
    # This is not a serialization nicety, it is the path every pool takes
    # between its first action and its second. `persistSuccessor` writes
    # `_pool_json(successor)` into the deployment index as `poolSnapshot`, and
    # `findForgePool` hands that same field straight back to `_pool()` next
    # time. If the two disagree the pool works exactly once.
    #
    # They did disagree: config is positional, V8 inserted the protocol fee and
    # its recipient at 5 and 6, and `_pool_json` still read the reserve inner
    # hash from 6 -- so every V8+ snapshot claimed the PROTOCOL FEE RECIPIENT
    # as its reserve hash, and dropped the fee fields entirely.
    print()
    print("a snapshot survives being persisted and read back:")
    snapshot = fs._pool_json(pool)
    results.append(check("the serialized reserve hash is the reserve's, not a neighbouring field",
                         snapshot["reserve_inner_puzzle_hash"] == bytes32(pool.config[-1]).hex(),
                         snapshot["reserve_inner_puzzle_hash"][:24] + "..."))
    results.append(check("the protocol fee survives serialization",
                         snapshot.get("protocol_fee_bps") == int(pool.config[5]),
                         str(snapshot.get("protocol_fee_bps"))))
    results.append(check("the protocol fee recipient survives serialization",
                         snapshot.get("protocol_puzzle_hash") == bytes32(pool.config[6]).hex()))
    try:
        reloaded = fs._pool(snapshot)
        same = (reloaded.config == pool.config
                and reloaded.pool.inner_puzzle.get_tree_hash()
                == pool.pool.inner_puzzle.get_tree_hash())
        results.append(check("the reloaded pool is the same pool, inner hash included", same))
    except Exception as exc:
        results.append(check(f"the snapshot reloads: {type(exc).__name__}: {str(exc)[:70]}", False))

    # The real shape of the bug: act, persist the successor, act again on it.
    # A pool that only survives its first action is bricked, not degraded.
    try:
        successor = fs._pool(fs._pool_json(fs._pool(fs._pool_json(pool))))
        results.append(check("two successive persists still describe the same pool",
                             successor.pool.inner_puzzle.get_tree_hash()
                             == pool.pool.inner_puzzle.get_tree_hash()))
    except Exception as exc:
        results.append(check(f"two successive persists survive: {type(exc).__name__}: "
                             f"{str(exc)[:70]}", False))

    print()
    passed = sum(results)

    print(f"{passed}/{len(results)} FORGE stdin-path checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
