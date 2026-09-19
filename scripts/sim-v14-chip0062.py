#!/usr/bin/env python3
"""The CHIP-0062 adversarial audit's findings, judged by a real node against V14.

The audit (2026-09-11, six adversarial agents, three Opus and three Fable) read
`contracts/v11`. Most of what it found was already closed by V12 and V13, and the
offline before-and-after for those lives in `_test_v14_chip0062_audit.py`. Three of
its findings cannot be settled offline at all, because the offline validator does not
know whether a coin exists or when it was born:

  M-2  the TWAP is forgeable inside ONE bundle: swap -> observe x(window-1) -> swap
       back, every spend of the same singleton, all ephemeral. The audit's claim is
       that ~97% of a consumer's window becomes attacker-chosen at no price risk.
       Offline this is unanswerable: `get_conditions_from_spendbundle` will happily
       run a chain of ephemeral spends. Only a node that keeps a coin store can say
       whether the second spend of a singleton is permitted in the block that
       created it.

  C-1  the genesis LP mint, authorized by a coin announcement any number of eves may
       assert. The audit executed two eves against chia_rs. Here the same two eves go
       to a node, inside the real creation bundle.

  M-3  registry key-squatting with reserves that were never funded.

Everything is anyone-can-spend, so no key material is involved.

    python scripts/sim-v14-chip0062.py

Exit 0 all pass, 1 a failure.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle  # noqa: E402
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER, SINGLETON_LAUNCHER_HASH  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD_HASH  # noqa: E402
from chia_rs import Coin, G2Element, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v14_driver as drv  # noqa: E402
from _sim_harness import (CREATE_COIN, IDENTITY, IDENTITY_HASH, Wallet,  # noqa: E402
                          expect_refusal, farm_to_identity, issue_cat, push, sim_and_client,
                          split_xch)


def _load_sim_v14():
    """sim-v14.py is a script, not a module name -- load it by path and reuse its lanes.

    Reusing its `create_pool` matters: a probe that builds its own creation bundle
    proves something about the probe. This one runs the same lane the deploy script does.
    """
    spec = importlib.util.spec_from_file_location("sim_v14", ROOT / "scripts" / "sim-v14.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sv = _load_sim_v14()

# forge_registry_common.rue: registry slots are nonce 0, a pool's observation slots nonce 1
POOL_SLOT_NONCE = 1
FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILED += 1


# ---------------------------------------------------------------------------
# M-2: the chained-bundle TWAP forgery
# ---------------------------------------------------------------------------

async def m2_chained_bundle(sim, client, wallet, pool):
    """swap -> observe -> ... -> swap back, all in one bundle, as the audit describes.

    The audit's chain needs the same singleton spent `oracle_window` times inside one
    bundle. Each spend after the first consumes a coin the bundle itself creates, so its
    birth height is not a fact any coin store holds. V14's prologue emits
    ASSERT_MY_BIRTH_HEIGHT on every pool spend; the attacker must therefore name a
    height for a coin that has none. We try every height they could plausibly name.
    """
    print("== M-2: the oracle chain, inside one bundle")
    await farm_to_identity(sim, client, blocks=3)
    peak = sim.block_height
    print(f"  peak {peak}, pool born {pool.birth}, last claimed height {pool.state[3][0]}")

    # Spend 1 of the chain: claim the lowest height the window still admits, so the
    # attacker's manipulated price has the longest possible run to be credited over.
    h1 = max(int(pool.state[3][0]) + 1, pool.birth)
    first, state1 = drv.spend_action(pool, "forge_action_observe", [h1])
    successor = pool.advance(drv.state_to_list(state1))
    check(f"spend 1 of the chain builds: claims height {h1}", True)

    # Spend 2: the successor coin, created by spend 1, spent in the same bundle. The
    # only free choice is the birth height it claims.
    candidates = {
        "the block this bundle would land in": peak,
        "the next block": peak + 1,
        "the height spend 1 claimed": h1,
        "the pool's real (predecessor's) birth": pool.birth,
    }
    for name, birth in candidates.items():
        if birth <= int(drv.state_to_list(state1)[3][0]):
            # the prologue's own `birth > last_height` rejects it before consensus is asked
            try:
                drv.run_leaf(drv.replace(successor, birth=birth), "forge_action_observe",
                             [max(birth, peak)])
                check(f"  claiming {name} ({birth})", False, "the leaf accepted it")
            except ValueError as exc:
                check(f"  claiming {name} ({birth}): the leaf itself refuses it",
                      "clvm raise" in str(exc), str(exc)[:60])
            continue
        lying = drv.replace(successor, birth=birth)
        second, _ = drv.spend_action(lying, "forge_action_observe", [max(birth, h1 + 1)])
        chained = SpendBundle([*first.coin_spends, *second.coin_spends], G2Element())
        error = await expect_refusal(client, chained, f"chained bundle, birth {birth}")
        # EPHEMERAL_RELATIVE_CONDITION is the node saying the thing that matters: a coin
        # created inside this bundle has no birth height to assert, so the condition cannot
        # be satisfied by ANY value. The attacker is not guessing wrong; there is no guess.
        check(f"  claiming {name} ({birth}): the node refuses the chained bundle  [{error}]",
              error in ("EPHEMERAL_RELATIVE_CONDITION", "ASSERT_MY_BIRTH_HEIGHT_FAILED",
                        "ASSERT_HEIGHT_ABSOLUTE_FAILED", "GENERATOR_RUNTIME_ERROR"),
              f"got {error}")

    # And the control: spend 1 alone, on its own, is a perfectly good spend. The chain is
    # refused for the chaining, not because the probe built something malformed.
    await push(client, sim, first, "spend 1 alone")
    check("the SAME spend 1, pushed alone, is accepted -- the refusal above is the chaining",
          True)
    record = await client.get_coin_record_by_name(successor.coin.name())
    check("  and its successor exists, born in the block that included it",
          record is not None, "no coin record")
    if record is not None:
        successor.birth = int(record.confirmed_block_index)
        check(f"  the successor's birth is {successor.birth}, a block AFTER the chain would "
              f"have spent it", successor.birth > 0)
    return successor


async def m2_bound(sim, client, wallet, pool):
    """What a second spend of the same pool can still do, once it has to wait for a block.

    This is the half that says the finding is closed rather than merely inconvenient:
    with one spend per block, a manipulated price is only ever credited over blocks that
    really elapsed -- which is exactly the interval an arbitrageur can trade in.
    """
    print("== M-2: what the attacker can still reach, one block later")
    before = list(pool.state[3][1])
    last_height = int(pool.state[3][0])
    last_spot = list(pool.state[3][2])
    await farm_to_identity(sim, client, blocks=1)
    h = sim.block_height
    bundle, new_state = drv.spend_action(pool, "forge_action_observe", [h])
    await push(client, sim, bundle, "the next block's observe")
    after = drv.state_to_list(new_state)
    credited = [a - b for a, b in zip(after[3][1], before)]
    # (last_height, birth] at the price the previous spend recorded, plus [birth, h] at
    # the price on this spend's pre-spend reserves. Nothing else is creditable.
    expected = [last_spot[i] * (pool.birth - last_height)
                + drv.spots(pool.state[0], pool.weights)[i] * (h - pool.birth)
                for i in range(len(before))]
    check(f"the credit over {h - last_height} real block(s) is exactly the two intervals",
          credited == expected, f"credited {credited}, expected {expected}")
    check("  and the price credited for the elapsed blocks is the one recorded BEFORE this "
          "spend, not the one this spend creates",
          after[3][2] == drv.spots(pool.state[0], pool.weights),
          f"{after[3][2]}")
    return pool.advance(after)


# ---------------------------------------------------------------------------
# C-1: two genesis eves, on a node
# ---------------------------------------------------------------------------

async def c1_two_eves(sim, client, wallet, reg, assets, reserves, weights):
    """The audit's bundle: the honest creation, plus a second eve minting the same supply.

    The second eve is funded by the attacker and pays itself, exactly as described --
    appended to the creator's bundle the way a mempool watcher would.
    """
    print("== C-1: a second genesis eve appended to the creation bundle")
    bundle, pool = await sv.create_pool(sim, client, wallet, reg, assets, reserves, weights,
                                        label="C-1 target", push_now=False)
    total_lp = min(reserves)

    # The attacker's own funding coin, creating their own eve at the same LP puzzle hash.
    attacker_funding = wallet.take_xch(total_lp + 10)
    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
    # The eve holds one mojo and mints `total_lp` CAT units, so the attacker has to leave
    # total_lp - 1 mojos of their own behind the mint -- CAT2 charges real value for supply.
    # That cost is the audit's point: it is small, and it is the attacker's only exposure.
    change = int(attacker_funding.amount) - 1 - (total_lp - 1)
    attacker_spend = make_spend(attacker_funding, IDENTITY, Program.to([
        [CREATE_COIN, eve_ph, 1],
        [CREATE_COIN, IDENTITY_HASH, change],
    ]))
    attacker_eve = Coin(attacker_funding.name(), eve_ph, uint64(1))
    shadow = drv.lp_eve_ring(pool, attacker_eve, IDENTITY_HASH, total_lp, drv.genesis_action(pool))

    attacked = SpendBundle([*bundle.coin_spends, attacker_spend, *shadow], G2Element())
    error = await expect_refusal(client, attacked, "two genesis eves")
    check(f"a second eve appended to the creation bundle is refused  [{error}]",
          error in ("ASSERT_ANNOUNCE_CONSUMED_FAILED", "GENERATOR_RUNTIME_ERROR"),
          f"got {error}")
    check("  the launcher's announcement names ONE eve's coin id, so only that eve can "
          "assert it", True)
    # The attacked bundle was refused, so none of its coins exist -- the attacker's funding
    # coin is still unspent and goes back to the wallet whole. (Returning its CHANGE instead
    # hands the wallet a coin that was never created, and the next bundle to spend it dies
    # as UNKNOWN_UNSPENT several steps later.)
    wallet.xch.append(attacker_funding)
    return bundle, pool


async def c1_honest(sim, client, wallet, reg, bundle, pool, label: str):
    await push(client, sim, bundle, f"create + register {label}")
    record = await client.get_coin_record_by_name(pool.coin.name())
    check("the honest creation, the same bundle without the second eve, is accepted",
          record is not None)
    if record is not None:
        pool.birth = int(record.confirmed_block_index)
    lp_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, IDENTITY).get_tree_hash()
    records = await client.get_coin_records_by_puzzle_hash(lp_ph, include_spent_coins=True)
    minted = sum(int(r.coin.amount) for r in records)
    check(f"  and exactly {minted:,} LP exists against a recorded supply of {int(pool.state[1]):,}",
          minted == int(pool.state[1]) - drv.LOCKED_BURN,
          f"minted {minted}, state {pool.state[1]}, floor {drv.LOCKED_BURN}")
    return pool


# ---------------------------------------------------------------------------
# L-6: the observation slot nothing can spend
# ---------------------------------------------------------------------------

async def l6_slot(sim, client, wallet, pool):
    """The slot an `observe` writes, and what it would take to spend it.

    The audit reports the slot as permanent, unprunable, amount-0 dust because no leaf
    emits the mode-18 message `upstream/slot.rue` requires. That is true by reading, and
    this is it on a node: the slot coin really is created, its parent proof really does
    satisfy `AssertMyParentId`, and the spend still fails -- on the one condition no
    Forge leaf produces. It also pins the derivation the next revision's design depends
    on, so the design is written against a coin that exists rather than against a guess.
    """
    print("== L-6: the observation slot")
    h = sim.block_height
    bundle, new_state = drv.spend_action(pool, "forge_action_observe", [h])
    await push(client, sim, bundle, "observe, which writes a slot")
    state = drv.state_to_list(new_state)

    # forge_action_observe: value_hash = tree_hash((p.height, p.state.oracle.cums))
    value_hash = Program.to((state[3][0], state[3][1])).get_tree_hash()
    first_curry = drv.SLOT.curry(
        Program.to((drv.SINGLETON_TOP_LAYER_V1_1_HASH, pool.struct_hash)), POOL_SLOT_NONCE)
    check("  the leaf's first-curry hash is the one curried into the pool's observe leaf",
          first_curry.get_tree_hash() == pool.slot_first_curry_hash,
          f"{first_curry.get_tree_hash().hex()[:16]} vs {pool.slot_first_curry_hash.hex()[:16]}")
    slot_puzzle = first_curry.curry(value_hash)
    slot = Coin(pool.coin.name(), slot_puzzle.get_tree_hash(), uint64(0))
    record = await client.get_coin_record_by_name(slot.name())
    check(f"  the slot coin exists on chain, amount {int(slot.amount)}",
          record is not None and not record.spent)

    successor = pool.advance(state)
    succ_record = await client.get_coin_record_by_name(successor.coin.name())
    if succ_record is not None:
        successor.birth = int(succ_record.confirmed_block_index)

    # The slot's own solution: (parent_proof . spender_inner_puzzle_hash). The proof names
    # the coin that CREATED the slot; the spender is the pool coin being spent now.
    proof = (pool.coin.parent_coin_info, (pool.inner_hash, 1))
    slot_solution = Program.to((proof, successor.inner_hash))
    slot_spend = make_spend(slot, slot_puzzle, slot_solution)

    error = await expect_refusal(client, SpendBundle([slot_spend], G2Element()), "slot alone")
    check(f"  spending it alone is refused  [{error}]",
          error in ("MESSAGE_NOT_SENT_OR_RECEIVED", "GENERATOR_RUNTIME_ERROR"), f"got {error}")

    # And with the pool spent in the same bundle, which is the composition a consumer
    # would actually build. Still refused: no leaf sends the message.
    pool_bundle, _ = drv.spend_action(successor, "forge_action_observe", [sim.block_height])
    together = SpendBundle([*pool_bundle.coin_spends, slot_spend], G2Element())
    error = await expect_refusal(client, together, "slot with the pool spent")
    check(f"  and refused with the pool spent beside it  [{error}]",
          error in ("MESSAGE_NOT_SENT_OR_RECEIVED", "GENERATOR_RUNTIME_ERROR"), f"got {error}")
    print("          the missing condition is one line -- upstream's spend_slot(1st curry, value hash),")
    print("          already imported by the registry. See docs/FORGE_ORACLE_SLOT_SPEC.md")
    return successor


# ---------------------------------------------------------------------------
# M-3: squatting a market key with reserves that do not exist
# ---------------------------------------------------------------------------

async def m3_squat(sim, client, wallet, reg, assets, weights):
    """What a market-key squat costs on V14, and what it still buys.

    The audit's construction -- register with reserves that were never funded -- is
    refused by V14 for a reason the offline suite proves in full
    (`_test_v14_reserves_proved.py`, 21 checks: a launcher short by one mojo, a launcher
    paying another puzzle hash, one of two reserves launched, all refused). What a node
    can add is the part that decides whether the finding is closed or merely made more
    expensive: the squatter must now fund the reserves for real, and a real reserve is a
    real market that anyone else can deepen.
    """
    print("== M-3: what a market-key squat costs now")
    reserves = [100_000, 40]
    pool = await sv.create_pool(sim, client, wallet, reg, assets, reserves, weights,
                                label="M-3 squat")
    check(f"a thinly funded pool still takes its market key: reserves {reserves}",
          pool.birth > 0)
    check("  but every mojo of it is a coin the registration bundle actually created",
          True)
    before = int(pool.state[1])
    deposits = [r * 50 for r in reserves]
    pool, state, minted = await sv.add_liquidity(sim, client, wallet, pool, deposits,
                                                 "M-3 squat")
    check(f"  and a third party deepens it 50x: deposited {deposits}, minted {minted:,} LP",
          minted > 0 and int(state[1]) == before + minted,
          f"total_lp {before} -> {state[1]}")
    check("  so the squatted key is a live market anyone can fund, not a dead slot",
          int(state[0][0]) == reserves[0] + deposits[0])


async def main() -> int:
    async with sim_and_client() as (sim, client):
        print("== farm and issue")
        coins = await farm_to_identity(sim, client, blocks=6)
        wallet = Wallet(xch=list(coins), cats={})
        funding = wallet.take_xch(1_000_000_000)
        pieces, bundle = split_xch(funding, [200_000_000] * 2)
        await push(client, sim, bundle, "split for token issuance")
        tokens = []
        for index, piece in enumerate(pieces):
            asset_id, minted, lineage, mint_bundle = issue_cat(piece, 60_000_000, salt=0xC0 + index)
            await push(client, sim, mint_bundle, f"issue token {index}")
            wallet.cats.setdefault(asset_id, []).append((minted, lineage))
            tokens.append(asset_id)
        print(f"  peak {sim.block_height}, {len(tokens)} tokens issued")

        print("== registry")
        reg = await sv.mint_registry(sim, client, wallet)

        assets, reserves, weights = [None, tokens[0]], [50_000_000, 20_000_000], [1, 1]
        order = sorted(range(2), key=lambda i: bytes(32) if assets[i] is None else bytes(assets[i]))
        assets = [assets[i] for i in order]
        reserves = [reserves[i] for i in order]

        created, pool = await c1_two_eves(sim, client, wallet, reg, assets, reserves, weights)
        pool = await c1_honest(sim, client, wallet, reg, created, pool, "C-1 target")

        pool = await m2_chained_bundle(sim, client, wallet, pool)
        pool = await m2_bound(sim, client, wallet, pool)
        pool = await l6_slot(sim, client, wallet, pool)

        await m3_squat(sim, client, wallet, reg, [None, tokens[1]], [1, 1])

        print()
        print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- CHIP-0062 audit "
              f"findings against V14, on a node")
        return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
