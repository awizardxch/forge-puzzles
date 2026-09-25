#!/usr/bin/env python3
"""The rCAT imprint on an in-process node: real coins, real lineage, real mempool.

contracts/_sim_v14_rcat_imprint.py answers the AMA question (2026-09-24) offline. The
offline validator cannot see whether a coin exists or whether its lineage is true, so
every claim that turns on those is re-run here against `chia._tests.util.spend_sim`:

  1. a genuine rCAT, issued with its layer, and a plain CAT          (real TAIL runs)
  2. the imprint, the keyless imprint and the nested imprint         (pushed and confirmed)
  3. the hidden path: the issuer strips a coin, a stranger takes a keyless one
  4. a V14 pool opened through the real registry lane, then:
       control   a swap paid with a plain settlement            -> SUCCESS
       attack    the same swap paid with an imprinted settlement -> refused by the leaf
  5. a fake riding with a real one: a twin planted at the reserve's exact puzzle hash, and
     a swap whose ring carries the real settlement AND an imprinted coin
  6. what separates genuine from fake: walk a coin back to the spend that ran its TAIL,
     and compare the H that issuance created with the H the coin wears

    python scripts/sim-v14-rcat-imprint.py

Exit 0 if every probe behaved as reported, 1 otherwise.
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
from chia.wallet.cat_wallet.cat_utils import (  # noqa: E402
    CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH  # noqa: E402
from chia.wallet.uncurried_puzzle import uncurry_puzzle  # noqa: E402
from chia.wallet.vc_wallet.vc_drivers import REVOCATION_LAYER, create_revocation_layer, match_revocation_layer  # noqa: E402
from chia_rs import Coin, G1Element, G2Element, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_math  # noqa: E402
import forge_v14_driver as drv  # noqa: E402
from _sim_harness import (CREATE_COIN, IDENTITY, IDENTITY_HASH, SimRejected, Wallet,  # noqa: E402
                          expect_refusal, farm_to_identity, issue_cat, push, quoted_nil_tail,
                          sim_and_client, split_xch)

_spec = importlib.util.spec_from_file_location("sim_v14", ROOT / "scripts" / "sim-v14.py")
sim_v14 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim_v14)

FAILED = 0
OFFER_PH = bytes32(OFFER_MOD_HASH)


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    FAILED += 0 if ok else 1
    return ok


def hidden_puzzle(tag: bytes) -> Program:
    """A key-gated hidden puzzle: AGG_SIG_UNSAFE on a key, then the solution's conditions.
    The simulator's mempool DOES check signatures, so a hidden spend of this one would need
    the key's signature; the probes below use it only as an H value, and the spends that
    exercise the hidden path use the keyless ANYONE, or ISSUER signed by nobody -> refused."""
    return Program.to([4, (1, [49, bytes(G1Element.generator()), tag]), 1])


ISSUER, ATTACKER, ANYONE = hidden_puzzle(b"issuer"), hidden_puzzle(b"attacker"), IDENTITY
H_ISSUER, H_ATTACKER, H_ANYONE = (bytes32(p.get_tree_hash()) for p in (ISSUER, ATTACKER, ANYONE))
NAMES = {H_ISSUER: "issuer", H_ATTACKER: "attacker", H_ANYONE: "anyone"}


def rev(h: bytes32, inner_hash: bytes32) -> Program:
    return create_revocation_layer(h, inner_hash)


def cat_ph(asset: bytes32, inner: Program) -> bytes32:
    return bytes32(construct_cat_puzzle(CAT_MOD, asset, inner).get_tree_hash())


def cat_bundle(*spendables) -> SpendBundle:
    return SpendBundle(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, list(spendables)).coin_spends, G2Element())


def issue_rcat(funding: Coin, amount: int, salt: int, h: bytes32):
    """A genuine rCAT: the TAIL-running eve spend creates the minted coin INSIDE
    revocation(h, IDENTITY). The only difference from issue_cat is that one CREATE_COIN."""
    tail = quoted_nil_tail(salt)
    asset_id = bytes32(tail.get_tree_hash())
    eve_ph = cat_ph(asset_id, IDENTITY)
    change = int(funding.amount) - 1 - amount
    funding_spend = make_spend(funding, IDENTITY, Program.to([[CREATE_COIN, eve_ph, 1], [CREATE_COIN, IDENTITY_HASH, change]]))
    eve = Coin(funding.name(), eve_ph, uint64(1))
    layered = rev(h, IDENTITY_HASH)
    inner_solution = Program.to([[CREATE_COIN, layered.get_tree_hash(), amount, [IDENTITY_HASH]],
                                 [CREATE_COIN, 0, -113, tail, Program.to(0)]])
    ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        eve, asset_id, IDENTITY, inner_solution, lineage_proof=LineageProof(), extra_delta=amount - 1,
        limitations_program_reveal=tail, limitations_solution=Program.to(0))]).coin_spends
    minted = Coin(eve.name(), cat_ph(asset_id, layered), uint64(amount))
    return asset_id, minted, LineageProof(eve.parent_coin_info, IDENTITY_HASH, uint64(1)), layered, \
        SpendBundle([funding_spend, *ring], G2Element()), Coin(funding.name(), IDENTITY_HASH, uint64(change))


def classify(puzzle: Program):
    """chia's RCATWallet classifier, as wallet_state_manager runs it: H comes from the coin."""
    u = uncurry_puzzle(puzzle)
    inner = uncurry_puzzle(u.args.at("rrf"))
    return ("rCAT", bytes32(match_revocation_layer(inner)[0])) if inner.mod == REVOCATION_LAYER else ("CAT", None)


async def issuance_h(client, coin: Coin):
    """The genuineness test: walk a coin's ancestry back to the spend that REVEALED ITS TAIL
    (the CREATE_COIN -113 in a CAT inner solution), and return the revocation H of the coins
    that issuance created -- None if it created plain coins. Also returns the hops walked.
    This is chain truth: it reads spends from the coin store, not what the coin claims."""
    hops, current = 0, coin
    while True:
        parent_rec = await client.get_coin_record_by_name(current.parent_coin_info)
        if parent_rec is None:
            raise SimRejected("ancestry leaves the chain before an issuance was found")
        spend = await client.get_puzzle_and_solution(current.parent_coin_info, parent_rec.spent_block_index)
        hops += 1
        u = uncurry_puzzle(Program.from_bytes(bytes(spend.puzzle_reveal)))
        if u.mod == CAT_MOD:
            inner_solution = Program.from_bytes(bytes(spend.solution)).first()
            inner = u.args.at("rrf")
            conds = inner.run(inner_solution) if uncurry_puzzle(inner).mod != REVOCATION_LAYER else None
            if conds is not None and any(c.first().as_int() == CREATE_COIN and c.rest().rest().first().as_int() == -113
                                         for c in conds.as_iter() if c.first().atom is not None):
                created = [c for c in conds.as_iter() if c.first().as_int() == CREATE_COIN
                           and c.rest().rest().first().as_int() > 0]
                hs = set()
                for c in created:
                    ph = bytes32(c.rest().first().as_atom())
                    # is ph revocation(H, IDENTITY) for some H we can name? The issuer publishes H;
                    # here we test the published candidates, exactly as a creation gate would.
                    hs.add(next((h for h in NAMES if rev(h, IDENTITY_HASH).get_tree_hash() == ph), None))
                return hs, hops
        current = parent_rec.coin


async def main() -> int:
    async with sim_and_client() as (sim, client):
        print("== setup: farm, issue a plain CAT X and a genuine rCAT R (issuer's H at issuance)")
        coins = await farm_to_identity(sim, client, blocks=6)
        wallet = Wallet(xch=list(coins), cats={})
        pieces, bundle = split_xch(wallet.take_xch(1_000_000_000), [200_000_000, 200_000_000])
        await push(client, sim, bundle, "split")
        X, x_coin, x_lin, b = issue_cat(pieces[0], 60_000_000, salt=0xC1)
        await push(client, sim, b, "issue X")
        R, r_coin, r_lin, r_layer, b, change = issue_rcat(pieces[1], 1_000_000, salt=0xC2, h=H_ISSUER)
        await push(client, sim, b, "issue R")
        wallet.xch.append(change)
        check("X issued plain; R issued inside revocation(H_issuer, p2) by its own TAIL spend",
              (await client.get_coin_record_by_name(r_coin.name())) is not None)

        # split X: one coin for the pool, one to imprint, one keyless, one for the settlements
        conds = [[CREATE_COIN, IDENTITY_HASH, a] for a in (40_000_000, 1_000_001, 1_000_002, 17_999_997)]
        b = cat_bundle(SpendableCAT(x_coin, X, IDENTITY, Program.to(conds), lineage_proof=x_lin))
        await push(client, sim, b, "split X")
        x_lin2 = LineageProof(x_coin.parent_coin_info, IDENTITY_HASH, x_coin.amount)
        xs = [Coin(x_coin.name(), cat_ph(X, IDENTITY), uint64(a)) for a in (40_000_000, 1_000_001, 1_000_002, 17_999_997)]
        wallet.cats[X] = [(xs[0], x_lin2)]

        print("\n== 2. the imprint, confirmed on a real coin store")
        imprint = rev(H_ATTACKER, IDENTITY_HASH)
        b = cat_bundle(SpendableCAT(xs[1], X, IDENTITY, Program.to([[CREATE_COIN, imprint.get_tree_hash(), 1_000_001]]),
                                    lineage_proof=x_lin2))
        await push(client, sim, b, "imprint")
        fake = Coin(xs[1].name(), cat_ph(X, imprint), uint64(1_000_001))
        fake_lin = LineageProof(xs[1].parent_coin_info, IDENTITY_HASH, xs[1].amount)
        check("a plain X holder wraps their coin in revocation(H_attacker): CONFIRMED",
              (await client.get_coin_record_by_name(fake.name())) is not None)
        c = classify(construct_cat_puzzle(CAT_MOD, X, imprint))
        check(f"chia's classifier: {c[0]} revocable by {NAMES.get(c[1])}", c == ("rCAT", H_ATTACKER))

        keyless = rev(H_ANYONE, IDENTITY_HASH)
        b = cat_bundle(SpendableCAT(xs[2], X, IDENTITY, Program.to([[CREATE_COIN, keyless.get_tree_hash(), 1_000_002]]),
                                    lineage_proof=x_lin2))
        await push(client, sim, b, "keyless imprint")
        kcoin = Coin(xs[2].name(), cat_ph(X, keyless), uint64(1_000_002))
        k_lin = LineageProof(xs[2].parent_coin_info, IDENTITY_HASH, xs[2].amount)

        nested = rev(H_ATTACKER, IDENTITY_HASH)
        b = cat_bundle(SpendableCAT(r_coin, R, r_layer, Program.to([0, IDENTITY, [[CREATE_COIN, nested.get_tree_hash(), 1_000_000]]]),
                                    lineage_proof=r_lin))
        await push(client, sim, b, "nested imprint")
        stacked = rev(H_ISSUER, nested.get_tree_hash())
        ncoin = Coin(r_coin.name(), cat_ph(R, stacked), uint64(1_000_000))
        check("the holder of a GENUINE R coin nests a second revoker inside it: CONFIRMED",
              (await client.get_coin_record_by_name(ncoin.name())) is not None)
        c = classify(construct_cat_puzzle(CAT_MOD, R, stacked))
        check(f"chia's classifier: {c[0]} revocable by {NAMES.get(c[1])} -- the inner revoker does not show", c == ("rCAT", H_ISSUER))

        print("\n== 3. the hidden path, with coin existence enforced")
        thief = bytes32(b"\x7e" * 32)
        b = cat_bundle(SpendableCAT(kcoin, X, keyless, Program.to([1, ANYONE, [[CREATE_COIN, thief, 1_000_002]]]),
                                    lineage_proof=k_lin))
        await push(client, sim, b, "keyless revoke")
        check("a stranger takes the keyless imprint with no key: CONFIRMED",
              (await client.get_coin_record_by_name(Coin(kcoin.name(), _cat_hash_of(X, thief), uint64(1_000_002)).name()))
              is not None)
        b = cat_bundle(SpendableCAT(fake, X, imprint, Program.to([1, ATTACKER, [[CREATE_COIN, IDENTITY_HASH, 1_000_001]]]),
                                    lineage_proof=fake_lin))
        err = await expect_refusal(client, b, "attacker hidden path, unsigned")
        check(f"the key-gated H needs its key: the unsigned hidden spend is refused {err}", err == "BAD_AGGREGATE_SIGNATURE")
        b = cat_bundle(SpendableCAT(fake, X, imprint, Program.to([0, IDENTITY, [[CREATE_COIN, IDENTITY_HASH, 1_000_001]]]),
                                    lineage_proof=fake_lin))
        await push(client, sim, b, "inner-path strip attempt")
        rewrapped = Coin(fake.name(), cat_ph(X, imprint), uint64(1_000_001))
        check("the owner's inner path asks for a plain child and gets the layer back: CONFIRMED at the layered id",
              (await client.get_coin_record_by_name(rewrapped.name())) is not None)
        fake, fake_lin = rewrapped, LineageProof(fake.parent_coin_info, imprint.get_tree_hash(), fake.amount)

        print("\n== 4. a V14 pool through the real registry lane, then the imprinted settlement")
        reg = await sim_v14.mint_registry(sim, client, wallet)
        assets, reserves = [None, X], [30_000_000, 40_000_000]
        pool = await sim_v14.create_pool(sim, client, wallet, reg, assets, reserves, [1, 1], label="XCH/X")
        check(f"XCH/X created and registered, birth {pool.birth}", pool.birth > 0)
        gross = 1_000_001
        r, w = pool.state[0], pool.weights
        out = forge_math.swap_output(r[1], r[0], gross, pool.fee_bps, w[1], w[0])

        def swap(settlement: Coin, spendable) -> SpendBundle:
            return drv.spend_action(pool, "forge_action_swap",
                                    [sim.block_height, 1, 0, gross, out, *drv.settlement_ref(settlement)],
                                    extra_cats={X: [spendable]})[0]

        # attack: the imprinted coin makes an offer settlement the only way it can -- its inner
        # path creates OFFER_MOD, and the layer re-wraps it as revocation(H_attacker, OFFER_MOD)
        b = cat_bundle(SpendableCAT(fake, X, imprint, Program.to([0, IDENTITY, [[CREATE_COIN, OFFER_PH, gross]]]),
                                    lineage_proof=fake_lin))
        await push(client, sim, b, "imprinted settlement")
        s_inner = rev(H_ATTACKER, OFFER_PH)
        s_coin = Coin(fake.name(), cat_ph(X, s_inner), uint64(gross))
        s_spend = SpendableCAT(s_coin, X, s_inner, Program.to([0, OFFER_MOD, [[s_coin.name()]]]),
                               lineage_proof=LineageProof(fake.parent_coin_info, imprint.get_tree_hash(), fake.amount))
        err = await expect_refusal(client, swap(s_coin, s_spend), "swap paid by the imprint")
        check(f"attack: a swap paid with the imprinted settlement is refused {err}", err == "ASSERT_CONCURRENT_SPEND_FAILED")

        # control: the same swap, same amount, paid with a plain X settlement
        b = cat_bundle(SpendableCAT(xs[3], X, IDENTITY, Program.to([[CREATE_COIN, OFFER_PH, gross],
                                                                    [CREATE_COIN, IDENTITY_HASH, int(xs[3].amount) - gross]]),
                                    lineage_proof=x_lin2))
        await push(client, sim, b, "plain settlement")
        p_coin = Coin(xs[3].name(), cat_ph(X, OFFER_MOD), uint64(gross))
        p_spend = SpendableCAT(p_coin, X, OFFER_MOD, Program.to([[p_coin.name()]]),
                               lineage_proof=LineageProof(xs[3].parent_coin_info, IDENTITY_HASH, xs[3].amount))
        control, new_state = drv.spend_action(pool, "forge_action_swap",
                                              [sim.block_height, 1, 0, gross, out, *drv.settlement_ref(p_coin)],
                                              extra_cats={X: [p_spend]})
        try:
            await push(client, sim, control, "control swap")
            check("control: the identical swap paid with a plain settlement: SUCCESS, confirmed", True)
        except SimRejected as exc:
            check("control: the identical swap paid with a plain settlement", False, str(exc))

        async def advanced(p, st):
            nxt = p.advance(drv.state_to_list(st))
            rec = await client.get_coin_record_by_name(nxt.coin.name())
            nxt.birth = int(rec.confirmed_block_index)
            return nxt

        print("\n== 6. a fake riding with a real one (the AMA's combination concern)")
        pool = await advanced(pool, new_state)
        rx = pool.reserves[1]
        change = Coin(xs[3].name(), cat_ph(X, IDENTITY), uint64(int(xs[3].amount) - gross))
        change_lin = LineageProof(xs[3].parent_coin_info, IDENTITY_HASH, xs[3].amount)
        rest = int(change.amount) - 1_000 - gross
        # 6a. a TWIN: anyone can create a coin at the reserve's exact puzzle hash -- it is a public hash
        b = cat_bundle(SpendableCAT(change, X, IDENTITY, Program.to([[CREATE_COIN, rx.inner_hash, 1_000],
                                                                     [CREATE_COIN, OFFER_PH, gross],
                                                                     [CREATE_COIN, IDENTITY_HASH, rest]]),
                                    lineage_proof=change_lin))
        await push(client, sim, b, "twin at the reserve puzzle hash")
        twin = Coin(change.name(), rx.full_hash, uint64(1_000))
        check("an outsider plants a twin coin at the X reserve's exact puzzle hash: CONFIRMED",
              twin.puzzle_hash == rx.coin.puzzle_hash and (await client.get_coin_record_by_name(twin.name())) is not None)
        # 6b. the same swap again, its X ring carrying the honest settlement AND the imprinted
        #     settlement coin from part 4, which pays itself back out through its own layer
        settle2 = Coin(change.name(), cat_ph(X, OFFER_MOD), uint64(gross))
        settle2_spend = SpendableCAT(settle2, X, OFFER_MOD, Program.to([[settle2.name()]]),
                                     lineage_proof=LineageProof(change.parent_coin_info, IDENTITY_HASH, change.amount))
        fake_rider = SpendableCAT(s_coin, X, s_inner,
                                  Program.to([0, OFFER_MOD, [[s_coin.name(), [IDENTITY_HASH, gross, [IDENTITY_HASH]]]]]),
                                  lineage_proof=LineageProof(fake.parent_coin_info, imprint.get_tree_hash(), fake.amount))
        r, w = pool.state[0], pool.weights
        out2 = forge_math.swap_output(r[1], r[0], gross, pool.fee_bps, w[1], w[0])
        mixed, new_state = drv.spend_action(pool, "forge_action_swap",
                                            [sim.block_height, 1, 0, gross, out2, *drv.settlement_ref(settle2)],
                                            extra_cats={X: [settle2_spend, fake_rider]})
        try:
            await push(client, sim, mixed, "mixed swap")
            check("a swap whose X ring carries a real settlement AND an imprinted coin: SUCCESS, confirmed", True)
        except SimRejected as exc:
            check("a swap whose X ring carries a real settlement AND an imprinted coin", False, str(exc))
        pool = await advanced(pool, new_state)
        rx2 = pool.reserves[1]
        check("  the new X reserve is plain -- nothing about the fake reached it, there is no flag to trip",
              rx2.coin.puzzle_hash == rx.full_hash and (await client.get_coin_record_by_name(rx2.coin.name())) is not None)
        rider_child = Coin(s_coin.name(), cat_ph(X, rev(H_ATTACKER, IDENTITY_HASH)), uint64(gross))
        check("  the imprinted coin's value left wearing its OWN layer, to its own owner",
              (await client.get_coin_record_by_name(rider_child.name())) is not None)
        check("  the twin sits unspent and ignored: the pool names its reserve by parent, not by puzzle hash",
              not (await client.get_coin_record_by_name(twin.name())).spent)
        # 6c. the twin cannot be pulled into the pool, nor taken back by whoever planted it
        grab = cat_bundle(SpendableCAT(twin, X, rx.inner, Program.to([pool.inner_hash, Program.to((1, [[CREATE_COIN, IDENTITY_HASH, 1_000]]))]),
                                       lineage_proof=LineageProof(change.parent_coin_info, IDENTITY_HASH, change.amount)))
        err = await expect_refusal(client, grab, "take the twin back")
        check(f"  its planter cannot spend it back without the pool singleton: refused {err}", err == "MESSAGE_NOT_SENT_OR_RECEIVED")
        # 6d. and the pool carries on
        b, _ = drv.spend_action(pool, "forge_action_observe", [sim.block_height])
        try:
            await push(client, sim, b, "observe after the combination")
            check("the pool spends normally afterwards (observe): SUCCESS, confirmed", True)
        except SimRejected as exc:
            check("the pool spends normally afterwards", False, str(exc))

        print("\n== 5. genuine or fake: walk each coin back to the spend that ran its TAIL")
        for label, coin, wears in (("the imprinted X coin", fake, H_ATTACKER),
                                   ("the genuine R coin, nested", ncoin, H_ISSUER)):
            hs, hops = await issuance_h(client, coin)
            issued = ", ".join(NAMES.get(h, "plain") if h else "plain" for h in hs)
            genuine = wears in hs
            check(f"{label}: wears H={NAMES[wears]}, its issuance created [{issued}] ({hops} hops) -> "
                  f"{'GENUINE outer layer' if genuine else 'FAKE: the layer was added after issuance'}",
                  genuine == (label.startswith("the genuine")))
        print("""
   The coin cannot say which H is right; its issuance can. Walking the ancestry to the spend
   that revealed the TAIL is chain truth, and it separates the imprint (issuance made plain
   coins) from the genuine article (issuance made revocation(H_issuer) coins). It does NOT
   see the revoker nested inside a genuine coin -- only an exact-hash reserve does that.""")

    print(f"\n{'all probes behaved as reported' if FAILED == 0 else f'{FAILED} probe(s) did not'}")
    return 0 if FAILED == 0 else 1


def _cat_hash_of(asset: bytes32, inner_hash: bytes32) -> bytes32:
    from chia.wallet.util.curry_and_treehash import curry_and_treehash, shatree_atom
    from chia.wallet.cat_wallet.cat_utils import CAT_MOD_HASH_HASH, QUOTED_CAT_MOD_HASH
    return bytes32(curry_and_treehash(QUOTED_CAT_MOD_HASH, CAT_MOD_HASH_HASH, shatree_atom(asset), inner_hash))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
