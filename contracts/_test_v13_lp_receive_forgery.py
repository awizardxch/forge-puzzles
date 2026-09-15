#!/usr/bin/env python3
"""Can an LP coin satisfy `remove` by *looking* burnt instead of running the TAIL?

The concern, raised against TibetSwap V2 and re-raised against Forge on
2026-09-10: a CAT's inner puzzle chooses its own conditions, and CAT2 passes
anything that is not a CREATE_COIN straight through. So an LP holder could spend
their LP with an inner puzzle that recreates the LP *and* emits a RECEIVE_MESSAGE
shaped exactly like the one the TAIL would emit for a melt -- the pool's SEND is
answered, the CAT ring balances, no supply is destroyed, and the reserves pay out.

This file establishes two facts by running the puzzles, not by reading them:

  1. The pass-through is REAL. An attacker inner's bare RECEIVE_MESSAGE comes out
     of the CAT layer untouched (case A's probe prints it). Any pool whose receiver
     is not pinned to a specific inner puzzle is exposed to exactly this.

  2. Forge is not, and for one reason only: `remove` derives the receiver coin id
     from the PINNED melt inner's hash (a curried constant), and mode 23 makes
     consensus require that coin -- not any coin -- to answer. The attacker's coin
     wraps a different inner, so its id is different, and its perfect forgery is
     an unmatched receive on the wrong coin. (case A: REJECTED)

  A second, subtler shape is closed by the TAIL rather than by the derivation:
  a coin at the pinned melt hash spent with extra_delta = 0 and its ring balanced
  by a sibling LP coin that grows by `burn`. The pinned inner still runs the TAIL,
  and the TAIL's `effective_delta == expected_delta` refuses a zero delta against
  a message that authorized -burn. (case B: REJECTED; the two-coin ring itself is
  proven sound by case B0, which is the same ring done honestly and ACCEPTED.)

Delete that TAIL assert and rebuild (scripts/mutate-v13.py) and B flips to
accepted while A stays refused -- which is the point: two different layers close
two different attacks, and each is pinned by its own test.

  A third case (C) pins the TAIL's other melt-side lock, the CAT-parent lock
  `parent_is_cat || expected_delta > 0` (finding 4). The existing finding-4 probe
  in _test_v11_actions.py fabricates the melt coin with extra_delta = -burn, which
  the delta lock refuses before the CAT-parent lock is ever reached -- so a
  mutation run showed the CAT-parent lock surviving every suite. Reaching it
  takes extra_delta = -2*burn (so that amount + delta == -burn) and a real sibling
  shrinking by `burn` to balance the ring. Run against a TAIL with the CAT-parent
  lock deleted, that bundle is ACCEPTED, and the outcome is an honest burn: real
  supply falls by exactly `burn`, because CAT2's ring charges for the fabricated
  coin's delta. So the CAT-parent lock is defence in depth, not the sole lock: it
  removes Forge's dependence on the ring's sign convention and makes the messaged
  melt coin have to hold real supply. Case C asserts that policy so the assert is
  pinned. (case C: REJECTED)

  Four more shapes, added 2026-09-11 when the concern was re-raised with "a fake
  TAIL, or a burned TAIL of a different asset, or the method splitXCH found":

  D. Create the coin AT the pinned melt hash (so the pool's derived receiver is
     that coin) but reveal CAT(attacker inner) when spending it. Consensus checks
     sha256tree(reveal) == puzzle_hash before any puzzle runs. (REJECTED)
  E. A CAT under an attacker TAIL that emits the pool's receive for free, with
     the pinned melt inner, the same parent and the same amount. The receiver is
     derived from the CURRIED lp_tail_hash, so a different TAIL is a different
     coin id. (REJECTED)
  F. Another pool's LP, genuinely melted through its own TAIL, offered against
     this pool's remove. Different asset id, different receiver. (REJECTED)
  G. The TibetSwap V2 shape itself: take the payouts and spend no LP at all.
     The handshake is emitted unconditionally, so the SEND is unanswered.
     (REJECTED)
"""
import sys

sys.path.insert(0, ".")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import (CAT_MOD, SpendableCAT, construct_cat_puzzle,
                                              unsigned_spend_bundle_for_spendable_cats)
from chia.wallet.lineage_proof import LineageProof
from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import _v13_testkit as kit
import forge_math

FAILED = 0
CATS = [bytes32(bytes([0xD0 + i]) * 32) for i in range(4)]
H0 = 6_999_990
MODE_23 = 23                       # SENDER_PUZZLE | RECEIVER_COIN, as the TAIL and the leaf use
RECEIVE_MESSAGE = 67


def check(label, ok, detail=""):
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILED += 1


def refuses(label, thunk):
    try:
        thunk()
    except kit.Rejected as exc:
        check(label, True)
        print(f"          refused: {str(exc)[:110]}")
        return
    check(label, False, "ACCEPTED -- this is the exploit")


def refuses_in_puzzle(label, thunk):
    """Like refuses(), but the refusal we want is the LEAF ITSELF raising (a clvm `x`
    from a failed assert) -- the puzzle is the judge, not consensus. A CLVM raise
    surfaces as ValueError/TypeError out of run_leaf."""
    try:
        thunk()
    except (kit.Rejected, ValueError, TypeError) as exc:
        check(label, True)
        print(f"          puzzle raised: {str(exc)[:90]}")
        return
    check(label, False, "ACCEPTED -- the puzzle emitted the wrong burn message")


def accepts(label, thunk):
    try:
        out = thunk()
    except kit.Rejected as exc:
        check(label, False, f"refused: {exc}")
        return None
    check(label, True)
    return out


def lp_message(lp_delta: int, new_total_lp: int, next_state_root: bytes32) -> bytes32:
    """The rue mirror: tree_hash(["forge-lp-v13", lp_delta, new_total_lp, next_state_root])."""
    return Program.to(["forge-lp-v13", lp_delta, new_total_lp, next_state_root]).get_tree_hash()


def holder_coin(pool, amount: int, salt: int, inner: Program = kit.IDENTITY):
    """A real LP coin: CAT(lp, inner) with a CAT parent, so lineage validates."""
    grandparent = bytes32(bytes([salt]) * 32)
    parent = kit.coin_id(grandparent, construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, kit.IDENTITY).get_tree_hash(), amount)
    coin = Coin(parent, construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, inner).get_tree_hash(), uint64(amount))
    return coin, LineageProof(grandparent, kit.IDENTITY.get_tree_hash(), uint64(amount))


def remove_probe(pool, burn):
    """Run the leaf once to learn the successor root the message will commit to."""
    vf = forge_math.vault_fee_bps(len(pool.state[0]), 10, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)
    new_total = pool.state[1] - burn
    probe_state, _, _, _ = kit.run_leaf(pool, "forge_action_remove", [H0, burn, bytes32(b"\x01" * 32), payouts])
    return payouts, new_total, probe_state.get_tree_hash()


def main():
    pool = kit.make_pool([None, CATS[0], CATS[1]], [3_000_000, 4_000_000, 5_000_000], total_lp=3_000_000,
                         leaves="forge", salt=0x71)
    burn = 300_000
    payouts, new_total, root = remove_probe(pool, burn)
    action = [-burn, new_total, root, pool.inner_hash, kit.ZERO_32]

    print("control:")
    lp_parent, lp_spends = kit.lp_melt_spend(pool, burn, new_total, root, salt=0x72)
    bundle, _ = kit.spend_action(pool, "forge_action_remove", [H0, burn, lp_parent, payouts], extra_spends=lp_spends)
    accepts("honest remove through the pinned melt inner is accepted", lambda: kit.validate(bundle))

    # ------------------------------------------------------------------ A
    print("A. an attacker inner forges the TAIL's receive instead of running the TAIL:")
    msg = lp_message(-burn, new_total, root)
    attacker, lineage = holder_coin(pool, burn, salt=0x73)      # inner = IDENTITY: conditions come from the solution
    forged = [[kit.CREATE_COIN, kit.IDENTITY.get_tree_hash(), burn],          # keep the LP
              [RECEIVE_MESSAGE, MODE_23, msg, pool.coin.puzzle_hash]]           # and "answer" the pool
    spendable = SpendableCAT(attacker, pool.lp_asset_id, kit.IDENTITY, Program.to(forged),
                             lineage_proof=lineage, extra_delta=0)
    ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends

    # First the probe: does CAT2 let that receive out? (This is the finding.)
    cs = ring[0]
    _, out = Program.from_bytes(bytes(cs.puzzle_reveal)).run_with_cost(10**9, Program.from_bytes(bytes(cs.solution)))
    ops = [c.first().as_int() for c in out.as_iter()]
    check("CAT2 passes the inner's RECEIVE_MESSAGE straight through (the pass-through is real)",
          RECEIVE_MESSAGE in ops, f"opcodes: {ops}")
    check("  and the TAIL did not run (no -113 was emitted, supply unchanged)",
          ops.count(60) <= 1)   # one ring announcement only; the TAIL's own marker is absent

    # Then the pool: the receiver it names is coin_id(parent, CAT(lp, MELT_INNER), burn) -- not this coin.
    derived = kit.coin_id(attacker.parent_coin_info,
                          construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, kit.LP_MELT_INNER).get_tree_hash(), burn)
    check("the pool's derived receiver is a different coin id from the attacker's", derived != attacker.name())
    bundle_a, _ = kit.spend_action(pool, "forge_action_remove", [H0, burn, attacker.parent_coin_info, payouts],
                                   extra_spends=ring)
    refuses("a byte-perfect forged receive on the attacker's own LP coin is REJECTED", lambda: kit.validate(bundle_a))

    # ------------------------------------------------------------------ B0 / B
    print("B. a coin at the pinned melt hash, spent with extra_delta = 0, ring balanced by a sibling:")
    melt_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, kit.LP_MELT_INNER).get_tree_hash()

    def two_coin_ring(melt_extra: int, sibling_out: int, salt: int):
        gp = bytes32(bytes([salt]) * 32)
        melt_parent = kit.coin_id(gp, construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, kit.IDENTITY).get_tree_hash(), burn)
        melt = Coin(melt_parent, melt_ph, uint64(burn))
        melt_sc = SpendableCAT(melt, pool.lp_asset_id, kit.LP_MELT_INNER, Program.to([pool.lp_tail, action]),
                               lineage_proof=LineageProof(gp, kit.IDENTITY.get_tree_hash(), uint64(burn)),
                               extra_delta=melt_extra, limitations_program_reveal=pool.lp_tail,
                               limitations_solution=Program.to(action))
        sib, sib_lineage = holder_coin(pool, 1_000_000, salt=salt + 1)
        sib_sc = SpendableCAT(sib, pool.lp_asset_id, kit.IDENTITY,
                              Program.to([[kit.CREATE_COIN, kit.IDENTITY.get_tree_hash(), sibling_out]]),
                              lineage_proof=sib_lineage, extra_delta=0)
        try:
            spends = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [melt_sc, sib_sc]).coin_spends
        except ValueError as exc:
            raise kit.Rejected(f"ring: {exc}") from exc
        return melt_parent, spends

    # B0: the same two-coin ring, done honestly -- proves the harness, so B's refusal is the assert.
    mp, spends = two_coin_ring(melt_extra=-burn, sibling_out=1_000_000, salt=0x80)
    b0, _ = kit.spend_action(pool, "forge_action_remove", [H0, burn, mp, payouts], extra_spends=spends)
    accepts("B0 control: melt with extra_delta = -burn beside an unchanged sibling is accepted", lambda: kit.validate(b0))

    # B: zero delta on the melt coin, the sibling grows by `burn`, the ring balances -- supply unchanged.
    mp, spends = two_coin_ring(melt_extra=0, sibling_out=1_000_000 + burn, salt=0x84)
    b1, _ = kit.spend_action(pool, "forge_action_remove", [H0, burn, mp, payouts], extra_spends=spends)
    refuses("melt coin with extra_delta = 0 (ring balanced by the sibling) is REJECTED by the TAIL",
            lambda: kit.validate(b1))

    # ------------------------------------------------------------------ C
    print("C. a melt coin fabricated from plain mojos, delta shaped to satisfy line 77, ring balanced by a real burn:")
    funding = Coin(bytes32(b"\xA1" * 32), kit.IDENTITY.get_tree_hash(), uint64(burn))
    funding_spend = kit.make_spend(funding, kit.IDENTITY, Program.to([[kit.CREATE_COIN, melt_ph, burn]]))
    fabricated = Coin(funding.name(), melt_ph, uint64(burn))                   # no CAT parent
    fab_sc = SpendableCAT(fabricated, pool.lp_asset_id, kit.LP_MELT_INNER, Program.to([pool.lp_tail, action]),
                          lineage_proof=LineageProof(), extra_delta=-2 * burn,   # amount + delta == -burn
                          limitations_program_reveal=pool.lp_tail, limitations_solution=Program.to(action))
    sib, sib_lineage = holder_coin(pool, 1_000_000, salt=0xA2)
    sib_sc = SpendableCAT(sib, pool.lp_asset_id, kit.IDENTITY,
                          Program.to([[kit.CREATE_COIN, kit.IDENTITY.get_tree_hash(), 1_000_000 - burn]]),
                          lineage_proof=sib_lineage, extra_delta=0)
    spends = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [fab_sc, sib_sc]).coin_spends
    c1, _ = kit.spend_action(pool, "forge_action_remove", [H0, burn, fabricated.parent_coin_info, payouts],
                             extra_spends=[funding_spend, *spends])
    refuses("a fabricated melt coin is REJECTED even when the ring makes the burn honest (the CAT-parent lock is policy)",
            lambda: kit.validate(c1))

    # ------------------------------------------------------------------ D
    print("D. reveal the attacker inner while CLAIMING the pinned melt outer puzzle hash:")
    # The coin is created at the pinned melt hash (so the pool's derived receiver IS this coin), but
    # the spend reveals CAT(lp, IDENTITY) and forges the receive. Consensus checks
    # sha256tree(reveal) == coin.puzzle_hash before anything runs.
    gp = bytes32(b"\xB0" * 32)
    parent_d = kit.coin_id(gp, construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, kit.IDENTITY).get_tree_hash(), burn)
    claimed = Coin(parent_d, melt_ph, uint64(burn))                       # id matches the pool's derivation
    check("the claimed coin id IS the pool's derived receiver",
          claimed.name() == kit.coin_id(parent_d, melt_ph, burn))
    reveal_sc = SpendableCAT(Coin(parent_d, construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, kit.IDENTITY).get_tree_hash(), uint64(burn)),
                             pool.lp_asset_id, kit.IDENTITY, Program.to(forged),
                             lineage_proof=LineageProof(gp, kit.IDENTITY.get_tree_hash(), uint64(burn)), extra_delta=0)
    honest_cs = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [reveal_sc]).coin_spends[0]
    mismatched = kit.make_spend(claimed, Program.from_bytes(bytes(honest_cs.puzzle_reveal)), Program.from_bytes(bytes(honest_cs.solution)))
    d1, _ = kit.spend_action(pool, "forge_action_remove", [H0, burn, parent_d, payouts], extra_spends=[mismatched])
    refuses("a reveal that does not hash to the claimed puzzle hash is REJECTED before any puzzle runs",
            lambda: kit.validate(d1))

    # ------------------------------------------------------------------ E
    print("E. a fake TAIL: a CAT under an attacker TAIL that hands out the pool's receive for free:")
    fake_tail = Program.to((1, [[RECEIVE_MESSAGE, MODE_23, msg, pool.coin.puzzle_hash]]))   # emits exactly the receive
    fake_id = fake_tail.get_tree_hash()
    fake_melt_ph = construct_cat_puzzle(CAT_MOD, fake_id, kit.LP_MELT_INNER).get_tree_hash()
    gp = bytes32(b"\xC0" * 32)
    fparent = kit.coin_id(gp, construct_cat_puzzle(CAT_MOD, fake_id, kit.IDENTITY).get_tree_hash(), burn)
    fcoin = Coin(fparent, fake_melt_ph, uint64(burn))
    check("same parent, same pinned inner, same amount -- but a different TAIL is a different coin id",
          fcoin.name() != kit.coin_id(fparent, melt_ph, burn))
    fake_sc = SpendableCAT(fcoin, fake_id, kit.LP_MELT_INNER, Program.to([fake_tail, action]),
                           lineage_proof=LineageProof(gp, kit.IDENTITY.get_tree_hash(), uint64(burn)), extra_delta=-burn,
                           limitations_program_reveal=fake_tail, limitations_solution=Program.to(action))
    fake_ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [fake_sc]).coin_spends
    _, fout = Program.from_bytes(bytes(fake_ring[0].puzzle_reveal)).run_with_cost(10**9, Program.from_bytes(bytes(fake_ring[0].solution)))
    check("  the fake TAIL really does emit the pool's receive", RECEIVE_MESSAGE in [c.first().as_int() for c in fout.as_iter()])
    e1, _ = kit.spend_action(pool, "forge_action_remove", [H0, burn, fparent, payouts], extra_spends=fake_ring)
    refuses("a coin under a fake TAIL is REJECTED: the receiver is derived from the curried lp_tail_hash", lambda: kit.validate(e1))

    # ------------------------------------------------------------------ E2
    print("E2. the second audit's shape (M-4): the remove leaf ITSELF curried with the fake TAIL, beside five honest leaves:")
    # Case E kept one consistent config and forged the coin. The audit changed the config of one
    # leaf instead: nothing in the action layer says the six leaves agree, so a `remove` curried
    # with lp_tail_hash = fake_id sits behind a valid root and derives the attacker's coin as its
    # receiver. V12's finalizer accepted that (99.98% of both reserves, chia_rs-verified); V13's
    # asserts the root is the six-leaf root of ONE configuration. The full before-and-after is
    # _test_v12_v13_review_findings.py; this pins the V13 refusal beside the honest control above.
    from clvm_tools.clvmc import compile_clvm_text
    fake_tail2 = Program.to(compile_clvm_text(
        "(mod (TRUTHS PARENT_IS_CAT LINEAGE DELTA INNER_CONDS SOL) (list (list 67 23 (f SOL) (f (r SOL)))))", []))
    fake_id2 = fake_tail2.get_tree_hash()
    cfg_fake = list(pool.config()); cfg_fake[5] = fake_id2
    cp, cf = Program.to(pool.config()), Program.to(cfg_fake)
    mixed = [kit.LEAF_MODS["forge_action_swap"].curry(cp), kit.LEAF_MODS["forge_action_add"].curry(cp),
             kit.LEAF_MODS["forge_action_remove"].curry(cf),
             kit.LEAF_MODS["forge_action_observe"].curry(cp, pool.slot_first_curry_hash),
             kit.LEAF_MODS["forge_action_collect"].curry(cp), kit.LEAF_MODS["forge_action_dao_fee"].curry(cp)]
    mixed_pool = kit.make_pool([None, CATS[0], CATS[1]], [3_000_000, 4_000_000, 5_000_000], total_lp=3_000_000, leaves=mixed, salt=0x71)
    check("  six leaves of two configurations still make a valid merkle root", mixed_pool.merkle_root != pool.merkle_root)
    m_state, _, _, _ = kit.run_leaf(mixed_pool, "leaf2", [H0, burn, bytes32(b"" * 32), payouts])
    m_msg = lp_message(-burn, new_total, m_state.get_tree_hash())
    m_sol = [m_msg, mixed_pool.coin.puzzle_hash]
    gp2 = bytes32(bytes([0xC1]) * 32)
    m_parent = kit.coin_id(gp2, construct_cat_puzzle(CAT_MOD, fake_id2, kit.IDENTITY).get_tree_hash(), burn)
    m_coin = Coin(m_parent, construct_cat_puzzle(CAT_MOD, fake_id2, kit.LP_MELT_INNER).get_tree_hash(), uint64(burn))
    m_sc = SpendableCAT(m_coin, fake_id2, kit.LP_MELT_INNER, Program.to([fake_tail2, m_sol]),
                        lineage_proof=LineageProof(gp2, kit.IDENTITY.get_tree_hash(), uint64(burn)), extra_delta=-burn,
                        limitations_program_reveal=fake_tail2, limitations_solution=Program.to(m_sol))
    m_ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [m_sc]).coin_spends
    e2, _ = kit.spend_action(mixed_pool, "leaf2", [H0, burn, m_parent, payouts], extra_spends=m_ring)
    refuses("a remove curried with the fake TAIL behind an otherwise honest root is REJECTED by the finalizer's config binding",
            lambda: kit.validate(e2))

    # ------------------------------------------------------------------ F
    print("F. another pool's LP, genuinely melted through its own TAIL, offered against this pool's remove:")
    other = kit.make_pool([None, CATS[0], CATS[1]], [3_000_000, 4_000_000, 5_000_000], total_lp=3_000_000,
                          leaves="forge", salt=0x99)
    check("  a second pool over the same assets has a different LP asset id", other.lp_asset_id != pool.lp_asset_id)
    o_payouts, o_total, o_root = remove_probe(other, burn)
    o_parent, o_spends = kit.lp_melt_spend(other, burn, o_total, o_root, salt=0x9A)    # a real melt of OTHER's LP
    f1, _ = kit.spend_action(pool, "forge_action_remove", [H0, burn, o_parent, payouts], extra_spends=o_spends)
    refuses("another pool's LP cannot answer this pool's message (different asset id, different receiver)",
            lambda: kit.validate(f1))

    # ------------------------------------------------------------------ G
    print("G. the TibetSwap V2 shape itself: take the payouts and simply never spend any LP:")
    g1, _ = kit.spend_action(pool, "forge_action_remove", [H0, burn, bytes32(b"\xD0" * 32), payouts], extra_spends=[])
    refuses("remove with no LP spend in the bundle is REJECTED: the handshake is unconditional", lambda: kit.validate(g1))

    # ------------------------------------------------------------------ H
    # "Attack the puzzle, not the front end": drive the remove leaf directly with a
    # solution that tries to make the pool SEND a burn message inconsistent with the
    # reserves it releases. No node, no UI -- the leaf itself is the judge here.
    # H1/H2 pin `exact_withdrawal`, the multi-line assert scripts/mutate-v13.py cannot
    # reach (its parser is single-line). Verified by hand on 2026-09-11: rebuilt the leaf
    # with that assert replaced by `assert true`, and both cases flipped to ACCEPTED --
    # the pool paid a larger burn's share while authorizing a smaller one. So this is the
    # line that welds payout to the burn the message carries.
    print("H. make the pool send the wrong burn message (adversarial leaf solution):")
    vf = forge_math.vault_fee_bps(len(pool.state[0]), 10, pool.fee_bps)
    honest_payouts = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)

    # H1: release the payouts a LARGER burn would justify, while authorizing only `burn`.
    big = forge_math.withdrawal_amounts(pool.state[0], burn * 2, pool.state[1], vf)
    refuses_in_puzzle("paying out a larger burn's share while burning less is REJECTED (exact_withdrawal)",
                      lambda: kit.spend_action(pool, "forge_action_remove", [H0, burn, bytes32(b"\x01" * 32), big]))

    # H2: one reserve overpaid by a single mojo -- the message's burn is unchanged, the payout is not.
    over = [p + 1 if i == 0 else p for i, p in enumerate(honest_payouts)]
    refuses_in_puzzle("overpaying one reserve by one mojo is REJECTED (payout welded to burn)",
                      lambda: kit.spend_action(pool, "forge_action_remove", [H0, burn, bytes32(b"\x01" * 32), over]))

    # H3: structural weld -- read the SEND the honest leaf emits and confirm its receiver embeds the
    # SAME burn that exact_withdrawal validated. One variable drives payout, delta and receiver.
    honest_parent, honest_lp = kit.lp_melt_spend(pool, burn, new_total, root, salt=0x88)
    hb, _ = kit.spend_action(pool, "forge_action_remove", [H0, burn, honest_parent, honest_payouts], extra_spends=honest_lp)
    pool_spend = hb.coin_spends[0]
    _, pconds = Program.from_bytes(bytes(pool_spend.puzzle_reveal)).run_with_cost(10**9, Program.from_bytes(bytes(pool_spend.solution)))
    sends = [c for c in pconds.as_iter() if c.first().as_int() == 66]              # SEND_MESSAGE
    want_receiver = kit.coin_id(honest_parent,
                                construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, kit.LP_MELT_INNER).get_tree_hash(), burn)
    # SEND_MESSAGE layout: (66 mode msg . receiver-args); the receiver coin id is the last argument
    got = [bytes(c.as_iter().__next__ and list(c.as_iter())[-1].as_atom()) for c in sends]
    check("the pool's SEND names exactly the melt coin derived from this burn (message welded to payout)",
          any(g == bytes(want_receiver) for g in got), f"receivers={[g.hex()[:12] for g in got]}")

    print()
    print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- LP receive-forgery checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
