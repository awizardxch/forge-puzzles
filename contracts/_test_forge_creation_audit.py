#!/usr/bin/env python3
"""Creating a pool, and what a creator can do with one.

Creation is permissionless and keyless: the offer carries every mojo and the
builder assembles the launcher, the reserves and the LP genesis. At genesis the
creator therefore controls both numbers that matter -- the pool's curried initial
state and the LP supply the TAIL is told to mint -- because the coin authorizing
the mint is an ordinary coin in their own bundle, not a pool spend (there is no
pool yet to do the authorizing).

That is inherent to permissionless creation, and it is not by itself a hole: the
two numbers are only dangerous if they let a creator take out more than they put
in, or leave a pool that misleads whoever uses it next. So rather than assert the
builder is honest, this checks the properties that hold no matter what it did:

  * a pool that is minted can actually be spent -- a config the puzzle rejects
    would strand every deposit in an inert singleton;
  * the locked minimum liquidity really is unreachable, so a pool can never be
    emptied and its LP supply always exceeds what holders own;
  * the creator cannot withdraw more than they deposited;
  * the successor is a genuine singleton of the same launcher, carrying one mojo.

Plus the refusals: parameters the puzzle would reject must be caught at build
time, because a pool minted with them is inert and its funds are stuck.
"""
import sys

sys.path.insert(0, ".")

from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton
from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_puzzles
import forge_stdin as fs
from _forge_testkit import USER_PH, audit, cat_maker_spend
from _forge_testkit import (
    FEE_BPS, NONCE, TREASURY, X, Y, creation_offer_for, external_of, make_offer, paid_to,
)
from forge_create_pool import MINIMUM_LOCKED_LP, deploy
from forge_math import swap_output, withdrawal_amounts
from forge_offer import MODE_REMOVE, MODE_SWAP
from forge_transition import build_transition

FORGE = forge_puzzles.FORGE_VERSION
BOOT = 5_000_000
ASSETS = sorted([X, Y])


def creation(**overrides):
    execution = {
        "protocolVersion": FORGE,
        "assetIds": [a.hex() for a in ASSETS],
        "bootstrapAmounts": [str(BOOT), str(BOOT)],
        "swapFeeBps": FEE_BPS,
        "protocolFeeBps": 25,
        "protocolPuzzleHash": TREASURY.hex(),
        "lpRecipientPuzzleHash": USER_PH.hex(),
    }
    execution.update(overrides)
    salt = overrides.pop("_salt", 0xC1)
    return {"offer": creation_offer_for(ASSETS, {a: BOOT for a in ASSETS}, salt).to_bech32(),
            "dry_run": True, "execution": execution}


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def refuses(label, **overrides):
    try:
        result = deploy(creation(**overrides))
        if not result.get("success"):
            return check(label, True)
        return check(label, False, "a pool was minted")
    except Exception:
        return check(label, True)


def main() -> int:
    results = []

    print("a pool the puzzle would reject must never be minted:")
    results.append(refuses("a liquidity fee above the ceiling", swapFeeBps=201))
    results.append(refuses("a negative liquidity fee", swapFeeBps=-1))
    results.append(refuses("a protocol fee above the ceiling", protocolFeeBps=101))
    results.append(refuses("a protocol fee with no recipient", protocolPuzzleHash=""))
    results.append(refuses("assets out of canonical order",
                           assetIds=[a.hex() for a in sorted(ASSETS, reverse=True)]))
    results.append(refuses("duplicate assets", assetIds=[ASSETS[0].hex(), ASSETS[0].hex()]))
    results.append(refuses("a zero bootstrap amount", bootstrapAmounts=[str(BOOT), "0"]))
    results.append(refuses("a zero weight", weights=[0, 1]))
    results.append(refuses("a weight above the cap", weights=[9, 1]))
    results.append(refuses("weights summing past the cap", weights=[8, 8, 8]))
    results.append(refuses("no LP recipient", lpRecipientPuzzleHash=""))
    results.append(refuses("a superseded revision without the explicit opt-in",
                           protocolVersion=FORGE - 1))

    print()
    print("the pool that IS minted is live and sane:")
    created = deploy(creation())
    if not created.get("success"):
        print(f"  [FAIL] a valid creation succeeds: {created.get('error')}")
        return 1
    results.append(check("a valid creation succeeds", True))
    pool = fs._pool(created["poolSnapshot"])
    reserves = [int(r[2]) for r in pool.state[0]]
    total_lp = int(pool.state[1])
    user_lp = total_lp - MINIMUM_LOCKED_LP

    results.append(check(f"the pool reports V{FORGE}", int(pool.config[0]) == FORGE))
    results.append(check("the pool coin carries one mojo", pool.pool.coin.amount == 1))
    results.append(check("LP supply exceeds what the creator holds",
                         total_lp > user_lp, f"{total_lp} > {user_lp}"))

    # Inert-pool check: a config validate_config rejects would fail here, and the
    # deposit would be stranded in a singleton nobody can spend.
    amount_in = 50_000
    released = swap_output(reserves[0], reserves[1], amount_in, FEE_BPS)
    net = released - released * 25 // 10_000
    spends = cat_maker_spend(ASSETS[0], amount_in, 0xC5)
    offer = make_offer(spends, {ASSETS[1]: [NotarizedPayment(USER_PH, uint64(net), [], NONCE)]})
    try:
        swapped = build_transition(pool, offer, MODE_SWAP)
        problems, checks = audit(swapped.bundle, external_of([pool], spends))
        results.append(check(f"the new pool can be spent ({checks} assertions)", not problems))
        successor = swapped.pool.pool
        expected = puzzle_for_singleton(pool.launcher_id, successor.inner_puzzle).get_tree_hash()
        results.append(check("its successor is a singleton of the same launcher",
                             successor.coin.puzzle_hash == expected))
        results.append(check("the successor still carries one mojo", successor.coin.amount == 1))
    except Exception as exc:
        results.append(check(f"the new pool can be spent: {type(exc).__name__}: {str(exc)[:60]}", False))

    print()
    print("the creator cannot take out more than they put in:")
    # Burn every LP the creator holds. The locked minimum stays behind, so the
    # payout is strictly short of the deposit -- that is what keeps the pool
    # alive and its last unit of liquidity unreachable.
    payouts = withdrawal_amounts(reserves, user_lp, total_lp, 0)
    results.append(check("burning all creator LP returns less than was deposited",
                         all(p < r for p, r in zip(payouts, reserves)),
                         f"{payouts} < {reserves}"))
    results.append(check("the pool keeps a non-zero reserve of every asset",
                         all(r - p > 0 for p, r in zip(payouts, reserves))))

    lp_outer_spends = cat_maker_spend(pool.lp_asset_id, user_lp, 0xC6)
    rm_offer = make_offer(lp_outer_spends, {
        ASSETS[i]: [NotarizedPayment(USER_PH, uint64(payouts[i]), [], NONCE)]
        for i in range(len(ASSETS)) if payouts[i] > 0})
    try:
        removed = build_transition(pool, rm_offer, MODE_REMOVE)
        problems, checks = audit(removed.bundle, external_of([pool], lp_outer_spends))
        results.append(check(f"the full withdrawal builds and audits ({checks} assertions)",
                             not problems))
        for p in problems:
            print(f"         - {p}")
        for i, asset in enumerate(ASSETS):
            got = paid_to(removed.bundle, asset, USER_PH)
            results.append(check(f"asset {i}: paid {got}, deposited {reserves[i]}",
                                 got <= reserves[i]))
        results.append(check("the pool survives the withdrawal",
                             int(removed.pool.state[1]) >= MINIMUM_LOCKED_LP))
    except Exception as exc:
        results.append(check(f"the full withdrawal builds: {type(exc).__name__}: {str(exc)[:60]}", False))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} creation probes passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
