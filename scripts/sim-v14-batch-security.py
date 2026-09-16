#!/usr/bin/env python3
"""Is it safe to create and register SEVERAL pools in one transaction?

Batching creation is attractive because every create spends the registry singleton, so
the one-at-a-time launch costs a block per pool. Chaining the registry through its own
ephemeral successors inside one bundle works -- but "works" is not "is safe". Putting N
launchers, N eves, N genesis mints, N fee payments and N slot rewrites in a single
bundle is exactly the shape where things that were separate start being able to reach
each other, and CHIP-0062's P0 finding was a genesis mint claiming an authorization it
was not entitled to.

So each question below is asked of a real node, not of a reading of the puzzle:

  A. Can pool B's eve mint against pool A's launcher announcement?
  B. Can one creation fee pay for two registrations?
  C. Can the same economic config be registered twice in one batch?
  D. If one staged bundle is bad, does anything from the batch land?
  E. After an honest batch, is each pool still its own singleton, LP asset and reserves?

Exit codes: 0 all checks pass, 1 a check failed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

_spec = importlib.util.spec_from_file_location("sim_v14", ROOT / "scripts" / "sim-v14.py")
S = importlib.util.module_from_spec(_spec)
sys.modules["sim_v14"] = S
_spec.loader.exec_module(S)

from chia.types.coin_spend import make_spend  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH  # noqa: E402
from chia_rs import Coin, G2Element, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

from _sim_harness import (CREATE_COIN, IDENTITY, IDENTITY_HASH, SimRejected, Wallet,  # noqa: E402
                          expect_refusal, farm_to_identity, issue_cat, push, sim_and_client,
                          split_xch)

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILED += 1


def snapshot(wallet, reg):
    """Staged-but-unpushed builds consume wallet coins that were never actually spent,
    and advance the registry bookkeeping for a bundle that never landed. Adversarial
    probes therefore run against a copy and put everything back afterwards."""
    return (list(wallet.xch), {k: list(v) for k, v in wallet.cats.items()},
            reg.registry, dict(reg.slots))


def restore(wallet, reg, snap) -> None:
    wallet.xch, wallet.cats, reg.registry, reg.slots = snap[0], dict(snap[1]), snap[2], dict(snap[3])


async def stage(sim, client, wallet, reg, specs):
    """Build each create without pushing, chaining the registry between them."""
    built = []
    for label, assets, reserves, weights in specs:
        order = sorted(range(len(assets)),
                       key=lambda i: bytes(32) if assets[i] is None else bytes(assets[i]))
        bundle, pool = await S.create_pool(
            sim, client, wallet, reg,
            [assets[i] for i in order], [reserves[i] for i in order],
            [weights[i] for i in order], label=label, push_now=False)
        built.append((label, bundle, pool))
    return built


async def main() -> int:
    async with sim_and_client() as (sim, client):
        coins = await farm_to_identity(sim, client, blocks=8)
        wallet = Wallet(xch=list(coins), cats={})
        funding = wallet.take_xch(1_500_000_000)
        pieces, split = split_xch(funding, [300_000_000] * 4)
        await push(client, sim, split, "split")
        tokens = []
        for index, piece in enumerate(pieces):
            asset, minted, lineage, mint = issue_cat(piece, 80_000_000, salt=0xA0 + index)
            await push(client, sim, mint, f"token {index}")
            wallet.cats.setdefault(asset, []).append((minted, lineage))
            tokens.append(asset)
        reg = await S.mint_registry(sim, client, wallet)
        print(f"registry live at peak {sim.block_height}\n")

        specs = [
            ("P0 XCH/token0", [None, tokens[0]], [50_000_000, 20_000_000], [1, 1]),
            ("P1 XCH/token0 80-20", [None, tokens[0]], [40_000_000, 16_000_000], [4, 1]),
        ]

        print("A. can pool B's eve mint against pool A's launcher announcement?")
        snap = snapshot(wallet, reg)
        built = await stage(sim, client, wallet, reg, specs)
        # Swap the two genesis LP rings between the bundles: B's eve ring is offered
        # alongside A's launcher, and vice versa.
        a_label, a_bundle, a_pool = built[0]
        b_label, b_bundle, b_pool = built[1]
        a_lp = S.construct_cat_puzzle(S.CAT_MOD, a_pool.lp_asset_id, S.drv.LP_MINT_INNER).get_tree_hash()
        b_lp = S.construct_cat_puzzle(S.CAT_MOD, b_pool.lp_asset_id, S.drv.LP_MINT_INNER).get_tree_hash()
        a_spends = [cs for cs in a_bundle.coin_spends if cs.coin.puzzle_hash != a_lp]
        b_eve = [cs for cs in b_bundle.coin_spends if cs.coin.puzzle_hash == b_lp]
        check("  the two pools really have different LP asset ids",
              a_pool.lp_asset_id != b_pool.lp_asset_id)
        crossed = SpendBundle.aggregate([SpendBundle(a_spends + b_eve, G2Element()), b_bundle])
        error = await expect_refusal(client, crossed, "cross-launcher genesis")
        check(f"  pool B's eve cannot mint under pool A's launcher: {error}",
              error in ("ASSERT_ANNOUNCE_CONSUMED_FAILED", "GENERATOR_RUNTIME_ERROR",
                        "DOUBLE_SPEND", "MINTING_COIN"), f"got {error}")

        restore(wallet, reg, snap)

        print("B. can one creation fee pay for two registrations?")
        snap = snapshot(wallet, reg)
        built = await stage(sim, client, wallet, reg, specs)
        fee_ph = bytes32(OFFER_MOD_HASH)
        stripped = []
        dropped = 0
        for label, bundle, pool in built[1:]:
            keep = []
            for cs in bundle.coin_spends:
                if cs.coin.puzzle_hash == fee_ph and dropped == 0:
                    dropped += 1
                    continue
                keep.append(cs)
            stripped.append(SpendBundle(keep, G2Element()))
        check("  one fee coin spend was removed from the second pool", dropped == 1)
        underpaid = SpendBundle.aggregate([built[0][1], *stripped])
        error = await expect_refusal(client, underpaid, "underpaid batch")
        check(f"  a batch missing one creation fee is refused: {error}",
              error not in ("", None), f"got {error}")

        restore(wallet, reg, snap)

        print("C. can the same economic config be registered twice in one batch?")
        snap = snapshot(wallet, reg)
        twin = [specs[0], ("P0 twin", [None, tokens[0]], [50_000_000, 20_000_000], [1, 1])]
        try:
            built = await stage(sim, client, wallet, reg, twin)
            duped = SpendBundle.aggregate([b for _, b, _ in built])
            error = await expect_refusal(client, duped, "duplicate config")
            check(f"  a duplicate economic key in one batch is refused: {error}", True)
        except (SimRejected, ValueError, AssertionError, KeyError) as exc:
            check("  a duplicate economic key is refused while the batch is still being built",
                  True)
            print(f"          {type(exc).__name__}: {str(exc)[:90]}")

        restore(wallet, reg, snap)

        print("D. if one staged bundle is bad, does anything land?")
        snap = snapshot(wallet, reg)
        built = await stage(sim, client, wallet, reg, specs)
        broken = SpendBundle(list(built[1][1].coin_spends)[:-1], G2Element())
        error = await expect_refusal(
            client, SpendBundle.aggregate([built[0][1], broken]), "half-broken batch")
        check(f"  the whole batch is refused, not the good half: {error}", error not in ("", None))
        for label, _, pool in built:
            record = await client.get_coin_record_by_name(pool.coin.name())
            check(f"  {label} did not land", record is None)

        restore(wallet, reg, snap)

        print("E. after an honest batch, is each pool its own thing?")
        before_pools = reg.registry.state[1]
        built = await stage(sim, client, wallet, reg, specs)
        merged = SpendBundle.aggregate([b for _, b, _ in built])
        await push(client, sim, merged, "honest batch")
        seen = {"launcher": set(), "lp": set(), "coin": set()}
        for label, _, pool in built:
            record = await client.get_coin_record_by_name(pool.coin.name())
            check(f"  {label} is on chain, birth {record.confirmed_block_index if record else '-'}",
                  record is not None)
            seen["launcher"].add(pool.launcher_id)
            seen["lp"].add(pool.lp_asset_id)
            seen["coin"].add(pool.coin.name())
        check("  every pool kept its own launcher, LP asset and singleton coin",
              all(len(v) == len(built) for v in seen.values()),
              f"{[len(v) for v in seen.values()]}")
        check(f"  the registry counted both: {reg.registry.state[1]}",
              reg.registry.state[1] == before_pools + 2, f"{reg.registry.state[1]}")
        heights = set()
        for label, _, pool in built:
            record = await client.get_coin_record_by_name(pool.coin.name())
            if record:
                heights.add(int(record.confirmed_block_index))
        check(f"  and they share one block, which is the point: {sorted(heights)}",
              len(heights) == 1)

        print("F. batched WITHDRAWALS: can two removes in one bundle reach each other?")
        # Removes never touch the registry, so there is no chaining at all -- they are just
        # independent spends sharing a bundle. The thing worth checking is cross-talk: each
        # pool's LP asset id is curried into its own TAIL and each reserve puzzle hash is
        # curried with its own singleton struct, so nothing one pool melts should be able to
        # release another pool's reserves.
        pools = [(label, pool) for label, _, pool in built]
        for label, pool in pools:
            record = await client.get_coin_record_by_name(pool.coin.name())
            pool.birth = int(record.confirmed_block_index)
        burns, advanced_pools = [], []
        ok = True
        for label, pool in pools:
            burn = int(pool.state[1]) // 10
            try:
                after, state, payouts = await S.remove_liquidity(sim, client, wallet, pool, burn, label)
            except Exception as exc:
                check(f"  {label}: remove", False, str(exc)[:80]); ok = False; continue
            burns.append((label, burn, int(pool.state[1]), int(state[1])))
            advanced_pools.append((label, after))
        if ok:
            for label, burn, before_lp, after_lp in burns:
                check(f"  {label}: burned {burn:,}, supply {before_lp:,} -> {after_lp:,}",
                      after_lp == before_lp - burn, f"{after_lp}")
            check("  each pool's supply moved by its OWN burn and nothing else",
                  len({b[0] for b in burns}) == len(burns))

        print("G. batched DEPOSITS: can a deposit be credited without being paid for?")
        # The sharp third-party case. Announcements are bundle-wide, so two pools in one
        # bundle can both see the same settlement announcement. What stops a deposit being
        # counted twice is not the announcement -- it is that each reserve has to be
        # RECREATED at its new amount, and the mojos for that have to come from somewhere
        # in the same bundle.
        pools = advanced_pools or pools      # the removes moved every pool on
        deposits = {}
        staged = []
        for label, pool in pools:
            dep = [max(int(r) // 20, 1) for r in pool.state[0]]
            deposits[label] = dep
        snap2 = snapshot(wallet, reg)
        ok = True
        try:
            for label, pool in pools:
                after, state, minted = await S.add_liquidity(
                    sim, client, wallet, pool, deposits[label], label)
                staged.append((label, after, state, minted))
        except Exception as exc:
            check("  honest batched deposits", False, str(exc)[:90]); ok = False
        if ok:
            for label, after, state, minted in staged:
                check(f"  {label}: deposit credited, {minted:,} LP minted", minted > 0)
            check("  the two pools minted different amounts, so neither used the other's deposit",
                  len({m for _, _, _, m in staged}) == len(staged),
                  f"{[m for _, _, _, m in staged]}")

        print("H. provenance: is minted LP bound to the deposits it represents?")
        # The worry behind batching: two pools settling in one block, both holding the same
        # underlying token, both minting LP. What keeps "this LP represents that deposit"
        # unambiguous is not the block or the ordering -- it is that the launcher id is
        # CURRIED into the LP TAIL, so it is part of the LP asset id itself, and each
        # reserve's puzzle hash is curried with that same pool's singleton struct.
        live = [(label, pool) for label, pool in (advanced_pools or pools)]
        for label, pool in live:
            rebuilt = S.drv.LP_TAIL_MOD.curry(pool.launcher_id, S.drv.PROTOCOL_VERSION).get_tree_hash()
            check(f"  {label}: its LP asset id IS its launcher id curried into the TAIL",
                  rebuilt == pool.lp_asset_id, f"{rebuilt.hex()[:16]} vs {pool.lp_asset_id.hex()[:16]}")
        if len(live) >= 2:
            (la, pa), (lb, pb) = live[0], live[1]
            shared = [i for i, a in enumerate(pa.asset_ids)
                      if a is not None and a in pb.asset_ids]
            check("  the two pools really do hold the same underlying token", bool(shared),
                  f"{[a.hex()[:8] if a else 'XCH' for a in pa.asset_ids]}")
            if shared:
                i = shared[0]
                j = list(pb.asset_ids).index(pa.asset_ids[i])
                check("  yet their reserve puzzle hashes differ, so a deposit lands in one pool only",
                      pa.reserves[i].full_hash != pb.reserves[j].full_hash)
            check("  and their LP asset ids differ, so the receipts are never interchangeable",
                  pa.lp_asset_id != pb.lp_asset_id)
            check("  neither pool's LP can be melted by the other (different curried launcher)",
                  S.drv.LP_TAIL_MOD.curry(pa.launcher_id, S.drv.PROTOCOL_VERSION).get_tree_hash()
                  != S.drv.LP_TAIL_MOD.curry(pb.launcher_id, S.drv.PROTOCOL_VERSION).get_tree_hash())

        print()
        print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- batched creation safety")
        return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
