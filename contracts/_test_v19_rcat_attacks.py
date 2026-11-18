#!/usr/bin/env python3
"""V19 rCAT pools (CHIP-0038): adversarial cases against `rebase`, `refund` and the finalizer's
adopt branch that _test_v19_rcat.py and _test_v19_rcat_issuer.py do not cover.

Every bundle runs through full consensus (kit.validate = chia_rs get_conditions_from_spendbundle).
A refusal counts only if it is a leaf CLVM raise (the same program as on chain, run locally while
building) or consensus `Rejected`; anything else is a HarnessError and is never counted. Every
refusal has a CONTROL: the same construction, valid, accepted. Every accepted bundle is checked
with the per-reserve leak oracle (coin amount minus its CREATE_COINs, from consensus output).

  1. rebase to 1 then sell (accepted trust): what is extracted from the other reserves, who can
     do which part of it.
  2. refund while the original reserve coin still exists (a twin at the reserve hash).
  3. refund and a payout from the same reserve in one spend.
  4. message replay: pool A's message on pool B; index i's message on index j.
  5. collect after a rebase; collect after a refund.
  6. clawback and refund twice in a row.
  7. adopt with a malformed parent, refund + rebase in one spend (both orders), two rebases in
     one spend, two refunds of one reserve respelled, a rebase whose fees exceed the new amount,
     a fee-carrying upward rebase, a no-op refund.

Exit 0 all pass, 1 a failure.
"""
from __future__ import annotations

import contextlib
import sys
from dataclasses import replace

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_math  # noqa: E402
import forge_v19_driver as kit  # noqa: E402
from chia.types.blockchain_format.coin import Coin  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import (CAT_MOD, SpendableCAT, construct_cat_puzzle,  # noqa: E402
                                              unsigned_spend_bundle_for_spendable_cats)
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD_HASH  # noqa: E402
from chia.wallet.vc_wallet.vc_drivers import REVOCATION_LAYER, REVOCATION_LAYER_HASH, solve_revocation_layer  # noqa: E402
from chia_rs import G2Element  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

Program = kit.Program
PLAIN, R, R2 = bytes32(b"\xd0" * 32), bytes32(b"\xd5" * 32), bytes32(b"\xd6" * 32)
ISSUER_PUZZLE = Program.to([5, [4, 1, []]])           # the issuer's hidden puzzle: runs its solution
H = bytes32(ISSUER_PUZZLE.get_tree_hash())
OTHER_PUZZLE = Program.to([6, [4, [], 1]])            # also runs its solution, another hash
PROTOCOL_PH, DAO_PH = bytes32(b"\x7a" * 32), bytes32(b"\x66" * 32)
ATTACKER = bytes32(b"\xa7" * 32)
H0 = 6_999_990
results: list[bool] = []


def check(label, good, detail=""):
    detail = detail if isinstance(detail, str) else "ACCEPTED"
    results.append(bool(good))
    print(f"  [{'PASS' if good else 'FAIL'}] {label}{'  -- ' + detail if detail else ''}")


class HarnessError(Exception):
    """A bug in this file. Never caught by `attempt`, so it can never be counted as a refusal."""


def valid(bundle):
    if not isinstance(bundle, kit.SpendBundle):
        raise HarnessError(f"not a SpendBundle: {type(bundle).__name__}")
    return kit.validate(bundle)


def attempt(thunk):
    """('accepted', result) or ('refused', reason). A refusal is consensus Rejected (not the
    driver's own CAT ring builder) or a CLVM failure; anything else raises."""
    try:
        return "accepted", thunk()
    except HarnessError:
        raise
    except kit.Rejected as exc:
        text = str(exc)
        if text.startswith("CAT ring"):
            raise HarnessError(f"the driver's ring builder refused, not consensus: {text}") from exc
        return "refused", f"consensus {text[:70]}"
    except ValueError as exc:
        why = kit.refusal_reason(exc)
        # The interpreter's own (message, node-hex) shape, e.g. strlen of a pair.
        if why is None and len(exc.args) == 2 and isinstance(exc.args[1], str)                 and all(ch in "0123456789abcdef" for ch in exc.args[1]):
            why = f"clvm: {exc.args[0]}"
        if why is None:
            raise HarnessError(f"not a CLVM refusal: {exc!r}") from exc
        return "refused", f"leaf {why[:70]}"


def refused(label, thunk):
    v, why = attempt(thunk)
    check(f"{label}: refused", v == "refused", why if v == "refused" else "ACCEPTED")
    return v


def accepted(label, thunk):
    v, res = attempt(thunk)
    check(f"{label}: accepted", v == "accepted", res if v != "accepted" else "")
    return res if v == "accepted" else None


def cat_ph(asset, inner_ph):
    return bytes32(construct_cat_puzzle(CAT_MOD, asset, Program.to(inner_ph)).get_tree_hash_precalc(inner_ph))


def layered_cat_ph(asset, hidden, inner_ph):
    return cat_ph(asset, bytes32(REVOCATION_LAYER.curry(REVOCATION_LAYER_HASH, hidden, inner_ph).get_tree_hash()))


def leaks(pool, conds):
    ids = {bytes(r.coin.name()): r.index for r in pool.reserves}
    return {ids[bytes(s.coin_id)]: int(s.coin_amount) - sum(int(c[1]) for c in s.create_coin)
            for s in conds.spends if bytes(s.coin_id) in ids}


def outputs_of(pool, conds, index):
    rid = bytes(pool.reserves[index].coin.name())
    return [(bytes32(c[0]), int(c[1])) for s in conds.spends if bytes(s.coin_id) == rid for c in s.create_coin]


def paid(before, after, j, out):
    """What the trader receives from reserve j: the curve's out less the dev and DAO fees the swap
    leaves owed in the reserve (state deltas, so a fee mistake shows as a mismatch)."""
    after = after if isinstance(after, list) else kit.state_to_list(after)
    return out - (after[2][j] - before[2][j]) - (after[6][j] - before[6][j])


@contextlib.contextmanager
def unchecked_ring():
    """The driver builds CAT rings with chia's builder, which refuses an unbalanced ring in Python
    before consensus sees it -- so a refusal there proves nothing about the puzzles. Inside this
    block the same builder runs WITHOUT that check (subtotals from the real deltas), so an
    unbalanced ring reaches consensus and is refused, or not, by the CAT puzzle itself."""
    from chia.wallet.cat_wallet import cat_utils as cu
    from chia.types.condition_opcodes import ConditionOpcode

    def builder(mod_code, spendables):
        deltas = []
        for sp in spendables:
            conds = cu.conditions_dict_for_solution(sp.inner_puzzle, sp.inner_solution, cu.INFINITE_COST)
            total = -sp.extra_delta + sum(Program.to(c.vars[1]).as_int() for c in conds.get(ConditionOpcode.CREATE_COIN, [])
                                          if c.vars[1] != bytes([0x8F]))
            deltas.append(sp.coin.amount - total)
        subtotals = cu.subtotals_for_deltas(deltas)
        ids = [sp.coin.name() for sp in spendables]
        n, out = len(spendables), []
        for i, sp in enumerate(spendables):
            sol = [sp.inner_solution, sp.lineage_proof.to_program(), ids[(i - 1) % n], Program.to(cu.coin_as_list(sp.coin)),
                   cu.next_info_for_spendable_cat(spendables[(i + 1) % n]), subtotals[i], sp.extra_delta]
            out.append(make_spend(sp.coin, construct_cat_puzzle(mod_code, sp.limitations_program_hash, sp.inner_puzzle),
                                  Program.to(sol)))
        return kit.SpendBundle(out, G2Element())

    saved = kit.unsigned_spend_bundle_for_spendable_cats
    kit.unsigned_spend_bundle_for_spendable_cats = builder
    try:
        yield
    finally:
        kit.unsigned_spend_bundle_for_spendable_cats = saved


def spent_ids(bundle):
    return {bytes(cs.coin.name()) for cs in bundle.coin_spends}


# ── coins and pools ─────────────────────────────────────────────────────────


def issuer_message(pool, index, amount, puzzle=ISSUER_PUZZLE, receiver=None, parent=b"\xe1"):
    msg = kit.rebase_message(index, amount) if hasattr(kit, "rebase_message") else \
        bytes32(Program.to([b"forge-rebase-v19", index, amount]).get_tree_hash())
    coin = Coin(bytes32(parent * 32), bytes32(puzzle.get_tree_hash()), uint64(1))
    return make_spend(coin, puzzle, Program.to([[66, 0b010111, msg, receiver or pool.coin.name()]]))


def layered_r_coin(amount, salt, outputs=(), hidden=H, asset=R):
    inner = kit.layered(hidden, kit.IDENTITY)
    outer = construct_cat_puzzle(CAT_MOD, asset, inner).get_tree_hash()
    gp = bytes32(bytes([salt]) * 32)
    coin = Coin(kit.coin_id(gp, outer, amount), outer, uint64(amount))
    return coin, SpendableCAT(coin, asset, inner, kit.layered_solution(hidden, kit.IDENTITY, list(outputs)),
                              lineage_proof=LineageProof(gp, inner.get_tree_hash(), uint64(amount)))


def rcat_pool(salt=0x61, hidden=None, assets=None):
    return kit.make_pool(assets or [None, PLAIN, R], [10_000_000, 20_000_000, 30_000_000], total_lp=5_000_000,
                         leaves="forge", salt=salt, protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=50,
                         hidden_puzzle_hashes=hidden if hidden is not None else [kit.ZERO_32, kit.ZERO_32, H])


def settlement(pool, i, amount, salt):
    asset = pool.asset_ids[i]
    if asset is None:
        coin, spend = kit.offer_settlement_xch(amount, salt=salt)
        return coin, [spend], {}
    coin, spendable = kit.offer_settlement_cat(asset, amount, salt=salt, hidden=pool.hiddens[i])
    return coin, [], {asset: [spendable]}


def swap_step(pool, a_in, a_out, gross, h, state=None):
    r = (state or pool.state)[0]
    out = forge_math.swap_output(r[a_in], r[a_out], gross, pool.fee_bps, pool.weights[a_in], pool.weights[a_out])
    return out


def do_swap(pool, a_in, a_out, gross, h, salt):
    out = swap_step(pool, a_in, a_out, gross, h)
    coin, es, ec = settlement(pool, a_in, gross, salt)
    bundle, st = kit.spend_action(pool, "forge_action_swap", [h, a_in, a_out, gross, out, *kit.settlement_ref(coin)],
                                  extra_spends=es, extra_cats=ec)
    return bundle, st, out


def with_fees(pool, h=H0, salt=0xC1):
    bundle, st, _ = do_swap(pool, 0, 2, 600_000, h, salt)
    valid(bundle)
    return pool.advance(kit.state_to_list(st))


def rebase_bundle(pool, index, amount, h, message=True, extra_cats=None, msg_parent=b"\xe1"):
    es = [issuer_message(pool, index, amount, parent=msg_parent)] if message is True else ([message] if message else [])
    return kit.spend_action(pool, "forge_action_rebase", [h, index, amount], extra_spends=es, extra_cats=extra_cats)


def replacement(pool, index, amount=None, salt=0xA9):
    """A coin standing in for reserve `index` at the reserve's own layered full hash, parented by
    a layered coin of R, holding `amount` (default: the recorded amount). Spent with its own puzzle."""
    r = pool.reserves[index]
    amount = int(r.coin.amount) if amount is None else amount
    full = bytes32(construct_cat_puzzle(CAT_MOD, r.asset_id, r.cat_inner).get_tree_hash())
    funder_inner = kit.layered(r.hidden, kit.IDENTITY)
    funder_outer = construct_cat_puzzle(CAT_MOD, r.asset_id, funder_inner).get_tree_hash()
    gp = bytes32(bytes([salt]) * 32)
    parent = kit.coin_id(gp, funder_outer, amount)
    coin = Coin(parent, full, uint64(amount))
    lin = LineageProof(gp, funder_inner.get_tree_hash(), uint64(amount))
    return replace(pool, reserves=[replace(x, coin=coin, lineage=lin) if x.index == index else x for x in pool.reserves]), parent


def with_reserve_coin(pool, index, coin, lineage):
    return replace(pool, reserves=[replace(x, coin=coin, lineage=lineage) if x.index == index else x for x in pool.reserves])


# ── 1. rebase to 1 then sell ────────────────────────────────────────────────


def case_drain():
    print("\n1. REBASE TO 1 THEN SELL (accepted trust; measured)")
    pool = rcat_pool(salt=0x61)
    before = list(pool.state[0])
    print(f"   reserves before: XCH {before[0]:,}  PLAIN {before[1]:,}  R {before[2]:,}  (weights {pool.weights})")

    # Non-issuer: the same rebase, the message from a coin at another puzzle hash. CONTROL first.
    accepted("CONTROL rebase to 1, the issuer's message", lambda: valid(rebase_bundle(pool, 2, 1, H0)[0]))
    refused("rebase to 1, a NON-issuer's message (coin at another puzzle hash)",
            lambda: valid(rebase_bundle(pool, 2, 1, H0, message=issuer_message(pool, 2, 1, puzzle=OTHER_PUZZLE))[0]))
    refused("rebase to 1 with no message at all", lambda: valid(rebase_bundle(pool, 2, 1, H0, message=None)[0]))

    bundle, st = rebase_bundle(pool, 2, 1, H0)
    conds, _ = valid(bundle)
    surplus = before[2] - 1
    check("the surplus (old - 1) goes to CAT(R, rev(H, H))",
          (layered_cat_ph(R, H, H), surplus) in outputs_of(pool, conds, 2), f"surplus {surplus:,}")
    p1 = pool.advance(kit.state_to_list(st))

    # The sell: R -> XCH, then R -> PLAIN, each in its own spend.
    g1 = 1_000_000
    b2, st2, out_x = do_swap(p1, 2, 0, g1, H0 + 1, 0xD1)
    res = accepted(f"sell {g1:,} R into the 1-unit reserve for XCH", lambda: valid(b2))
    if res:
        check("  leak exact (XCH conserves and pays exactly the quoted out; R grows by the input)",
              leaks(p1, res[0]) == {0: 0, 1: 0, 2: -g1} and (OFFER_MOD_HASH, paid(p1.state, st2, 0, out_x)) in outputs_of(p1, res[0], 0), f"{leaks(p1, res[0])}")
    p2 = p1.advance(kit.state_to_list(st2))
    g2 = surplus - g1
    b3, st3, out_p = do_swap(p2, 2, 1, g2, H0 + 2, 0xD2)
    res = accepted(f"sell the remaining {g2:,} R for PLAIN", lambda: valid(b3))
    if res:
        check("  leak exact (PLAIN conserves and pays exactly the quoted out; R grows by the input)",
              leaks(p2, res[0]) == {0: 0, 1: 0, 2: -g2} and (cat_ph(PLAIN, OFFER_MOD_HASH), paid(p2.state, st3, 1, out_p)) in outputs_of(p2, res[0], 1),
              f"{leaks(p2, res[0])}")
    p3 = p2.advance(kit.state_to_list(st3))
    after = list(p3.state[0])
    net_r = surplus - g1 - g2
    out_x, out_p = paid(p1.state, st2, 0, out_x), paid(p2.state, st3, 1, out_p)   # what the seller receives
    print(f"   extracted: XCH {out_x:,} of {before[0]:,} ({100 * out_x / before[0]:.4f}%), "
          f"PLAIN {out_p:,} of {before[1]:,} ({100 * out_p / before[1]:.4f}%); issuer's net R {net_r:+,}")
    print(f"   reserves after: XCH {after[0]:,}  PLAIN {after[1]:,}  R {after[2]:,}")
    # The seller receives the curve's out less the dev + DAO fees (those stay owed to the fee
    # recipients); the LPs' XCH falls from 10,000,000 to a dust remainder.
    check("documented trust: at net zero R the seller takes > 99% of XCH and > 95% of PLAIN; LPs keep XCH dust",
          out_x * 100 > before[0] * 99 and out_p * 100 > before[1] * 95 and net_r == 0 and after[0] < 100,
          f"XCH {out_x:,}, PLAIN {out_p:,}, LPs' XCH left {after[0]}")
    check("no reserve reaches zero (V19 never-zero)", all(x > 0 for x in after), f"{after}")

    # The same drain in ONE spend: rebase first, then the sell.
    out1 = forge_math.swap_output(1, before[0], g1, pool.fee_bps, pool.weights[2], pool.weights[0])
    coin, es, ec = settlement(pool, 2, g1, 0xD3)
    steps = [("forge_action_rebase", [H0, 2, 1]),
             ("forge_action_swap", [H0, 2, 0, g1, out1, *kit.settlement_ref(coin)])]
    res = accepted("rebase to 1 AND the sell in one spend (rebase first)",
                   lambda: valid(kit.spend_actions(pool, steps, extra_spends=[*es, issuer_message(pool, 2, 1)], extra_cats=ec)[0]))
    if res:
        lk = leaks(pool, res[0])
        _b1, st1 = kit.spend_actions(pool, steps, extra_spends=[*es, issuer_message(pool, 2, 1)], extra_cats=ec)
        outs2 = outputs_of(pool, res[0], 2)
        check("  leak exact: XCH conserves (pays out1), R grows by the input net of the surplus it pays to H",
              lk == {0: 0, 1: 0, 2: -g1} and (OFFER_MOD_HASH, paid(pool.state, st1, 0, out1)) in outputs_of(pool, res[0], 0)
              and (layered_cat_ph(R, H, H), surplus) in outs2, f"{lk}")

    # Who can do the SELL part: anyone holding R. The issuer's message binds the pool coin, the
    # index and the amount, not the rest of the spend: a third party can lift the issuer's message
    # spend from the mempool and put its own sell after the rebase. (The same holder could equally
    # sell in the next block once the rebase confirms.)
    print("   who can sell into the rebased reserve:")
    # Nothing in the pool spend names the seller: the settlement coin is an ordinary offer coin of
    # R under H, which any holder of R can make. This is the same bundle as above, restated as what
    # it proves: the issuer's message spend, lifted, plus anyone's sell.
    third = [("forge_action_rebase", [H0, 2, 1]),
             ("forge_action_swap", [H0, 2, 0, g1, out1, *kit.settlement_ref(coin)])]
    res = accepted("anyone's sell (an ordinary R offer coin) after the issuer's lifted rebase message, one spend",
                   lambda: valid(kit.spend_actions(pool, third, extra_spends=[*es, issuer_message(pool, 2, 1)], extra_cats=ec)[0]))
    out_after = forge_math.swap_output(1, before[0], g1, p1.fee_bps, p1.weights[2], p1.weights[0])
    check("  observation: after a rebase to 1, ANY rCAT holder (not only the issuer) takes the other reserves",
          out_after * 10_000 > before[0] * 9_999, f"{g1:,} R: curve out {out_after:,} XCH")
    # The trade cannot come BEFORE the rebase (the first-action rule): CONTROL is the order above.
    refused("the sell placed BEFORE the rebase in one spend",
            lambda: valid(kit.spend_actions(pool, list(reversed(steps)), extra_spends=[*es, issuer_message(pool, 2, 1)], extra_cats=ec)[0]))


# ── 2. twin coin ────────────────────────────────────────────────────────────


def case_twin():
    print("\n2. REFUND WHILE THE ORIGINAL RESERVE COIN STILL EXISTS (a twin)")
    pool = with_fees(rcat_pool(salt=0x62))
    O = pool.reserves[2].coin
    O_lin = pool.reserves[2].lineage
    swapped, parent = replacement(pool, 2, salt=0xA1)
    T = swapped.reserves[2].coin
    print(f"   original O {O.name().hex()[:12]}..  twin T {T.name().hex()[:12]}..  both {int(O.amount):,} at the reserve hash")
    check("setup: O and T share the puzzle hash and amount, different ids",
          O.puzzle_hash == T.puzzle_hash and O.amount == T.amount and O.name() != T.name())
    bundle, st = kit.spend_action(swapped, "forge_action_refund", [H0 + 1, 2, parent])
    res = accepted("refund adopting T while O is unspent", lambda: valid(bundle))
    if not res:
        return
    conds, adds = res
    check("  O is not spent by the refund (it stays at the reserve hash)", bytes(O.name()) not in spent_ids(bundle))
    check("  T is spent and recreates the reserve, leak 0 everywhere", all(x == 0 for x in leaks(swapped, conds).values()),
          f"{leaks(swapped, conds)}")
    after = swapped.advance(kit.state_to_list(st))
    money = lambda s: [s[0], s[1], s[2], s[5], s[6]]  # noqa: E731
    check("  pool and LPs: reserves, LP supply, fees, DAO owed unchanged", money(list(after.state)) == money(list(pool.state)))
    check("  the successor records T, not O, as reserve 2's coin", list(after.state)[7][2] == T.name())

    # O can no longer act as the reserve: the successor messages T's child only. CONTROL first.
    accepted("CONTROL the successor swaps with T's child as reserve 2", lambda: valid(do_swap(after, 0, 1, 50_000, H0 + 2, 0xD5)[0]))
    o_as_reserve = with_reserve_coin(after, 2, O, O_lin)
    refused("the successor spent with O as reserve 2 (O never receives the pool's message)",
            lambda: valid(do_swap(o_as_reserve, 0, 1, 50_000, H0 + 2, 0xD5)[0]))

    # O is recoverable by the issuer's hidden path alone.
    r2 = pool.reserves[2]
    def hidden_take(puzzle):
        sol = solve_revocation_layer(puzzle, Program.to([[51, ATTACKER, int(O.amount)]]), hidden=True)
        sp = SpendableCAT(O, R, r2.cat_inner, sol, lineage_proof=O_lin)
        return valid(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [sp]))
    accepted("the issuer takes O through the hidden path (its puzzle hashes to H)", lambda: hidden_take(ISSUER_PUZZLE))
    refused("a non-issuer takes O through the hidden path (a puzzle that is not H)", lambda: hidden_take(OTHER_PUZZLE))

    # Flip back: anyone can refund to O again while the recorded amount still equals O's, which
    # strands T's child (the funder's coin) at the reserve hash for the issuer instead.
    flip = with_reserve_coin(after, 2, O, O_lin)
    res = accepted("anyone refunds BACK to O (amount unchanged), stranding T's child",
                   lambda: valid(kit.spend_action(flip, "forge_action_refund", [H0 + 2, 2, O.parent_coin_info])[0]))
    if res:
        check("  leak 0, pool money unchanged by the flip", all(x == 0 for x in leaks(flip, res[0]).values()))
    # After a trade the amount moved, so O can never be re-adopted.
    b, st2, _ = do_swap(after, 0, 2, 50_000, H0 + 2, 0xD6)
    valid(b)
    traded = after.advance(kit.state_to_list(st2))
    # O now holds MORE than the recorded amount; the ring is balanced by a coin of R that absorbs
    # the difference, so the only thing that can refuse is the message aimed at coinid(O's parent,
    # the reserve hash, the RECORDED amount) -- which is not O.
    delta = int(O.amount) - int(traded.reserves[2].coin.amount)
    check("setup: the trade moved reserve 2 below O's amount", delta > 0, f"delta {delta:,}")
    _ac, absorb = layered_r_coin(1_000, 0xB8, outputs=[[51, ATTACKER, 1_000 + delta]])
    refused("after a trade on reserve 2, a refund back to O (amount no longer matches; ring balanced)",
            lambda: valid(kit.spend_action(with_reserve_coin(traded, 2, O, O_lin), "forge_action_refund",
                                           [H0 + 3, 2, O.parent_coin_info], extra_cats={R: [absorb]})[0]))
    accepted("CONTROL after that trade, a refund to a twin of the NEW recorded amount",
             lambda: valid(kit.spend_action(*(lambda p: (p[0], "forge_action_refund", [H0 + 3, 2, p[1]]))(replacement(traded, 2, salt=0xA2)))[0]))


# ── 3. refund plus a payout from the same reserve ───────────────────────────


def case_refund_and_payout():
    print("\n3. REFUND + A SWAP OUT OF THE SAME RESERVE, ONE SPEND")
    pool = with_fees(rcat_pool(salt=0x63))
    swapped, parent = replacement(pool, 2, salt=0xA3)
    g = 200_000
    out = swap_step(swapped, 0, 2, g, H0 + 1)
    coin, es, ec = settlement(swapped, 0, g, 0xD7)
    for order, steps in (("refund first", [("forge_action_refund", [H0 + 1, 2, parent]),
                                           ("forge_action_swap", [H0 + 1, 0, 2, g, out, *kit.settlement_ref(coin)])]),
                         ("swap first", [("forge_action_swap", [H0 + 1, 0, 2, g, out, *kit.settlement_ref(coin)]),
                                         ("forge_action_refund", [H0 + 1, 2, parent])])):
        bundle, st = kit.spend_actions(swapped, steps, extra_spends=es, extra_cats=ec)
        res = accepted(f"{order}", lambda: valid(bundle))
        if res:
            conds, adds = res
            lk = leaks(swapped, conds)
            check(f"  {order}: leak exact -- the replacement conserves (its payout is inside), XCH grows by exactly the input",
                  lk == {0: -g, 1: 0, 2: 0}, f"leak {lk}, out {out:,}")
            check(f"  {order}: the trader's payout is CAT(R, rev(H, settlement_payments))",
                  any(ph == layered_cat_ph(R, H, OFFER_MOD_HASH) and a == paid(swapped.state, st, 2, out) for ph, a in outputs_of(swapped, conds, 2)))
            check(f"  {order}: the original reserve coin is not spent", bytes(pool.reserves[2].coin.name()) not in spent_ids(bundle))
    # The payout cannot come from the absent original: the same swap WITHOUT the refund names the original.
    refused("the same swap without the refund, the replacement offered as reserve 2",
            lambda: valid(kit.spend_action(swapped, "forge_action_swap", [H0 + 1, 0, 2, g, out, *kit.settlement_ref(coin)],
                                           extra_spends=es, extra_cats=ec)[0]))
    # A delegated puzzle that drains the replacement to an attacker.
    new_state, tagged, _b, _e = kit.run_leaf(swapped, "forge_action_refund", [H0 + 1, 2, parent])
    amt = int(swapped.reserves[2].coin.amount)
    drain = Program.to((1, [[51, ATTACKER, amt]]))
    refused("refund with the replacement's delegated puzzle swapped for a drain to an attacker",
            lambda: valid(kit.assemble(swapped, swapped.leaves["forge_action_refund"], swapped.leaf_proof("forge_action_refund"),
                                       kit.with_birth(swapped, [H0 + 1, 2, parent]), new_state, tagged, reserve_delegated={2: drain})))


# ── 4. message replay ───────────────────────────────────────────────────────


def case_replay():
    print("\n4. MESSAGE REPLAY")
    a = with_fees(rcat_pool(salt=0x64))
    b = with_fees(rcat_pool(salt=0x65))
    check("setup: A and B are different coins with the same (R, H) at index 2",
          a.coin.name() != b.coin.name() and a.hiddens == b.hiddens and a.asset_ids == b.asset_ids)
    new = int(b.state[0][2]) // 2
    accepted("CONTROL pool B rebases with a message addressed to B", lambda: valid(rebase_bundle(b, 2, new, H0 + 1)[0]))
    refused("pool B presented pool A's message (same H, asset, index, amount)",
            lambda: valid(rebase_bundle(b, 2, new, H0 + 1, message=issuer_message(a, 2, new))[0]))
    # Both pools in one bundle, ONE message (to A): B cannot share it.
    ba, _ = rebase_bundle(a, 2, new, H0 + 1)
    bb, _ = rebase_bundle(b, 2, new, H0 + 1, message=None)
    two_msgs_b, _ = rebase_bundle(b, 2, new, H0 + 1, msg_parent=b"\xe2")
    accepted("CONTROL A and B both rebased in one bundle, one message each",
             lambda: valid(kit.SpendBundle([*ba.coin_spends, *two_msgs_b.coin_spends], G2Element())))
    refused("A and B both rebased in one bundle, only A's message",
            lambda: valid(kit.SpendBundle([*ba.coin_spends, *bb.coin_spends], G2Element())))

    # Index i for index j: a pool with TWO rCATs under the same H.
    two = kit.make_pool([None, R, R2], [10_000_000, 20_000_000, 30_000_000], total_lp=5_000_000, leaves="forge", salt=0x66,
                        protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=50, hidden_puzzle_hashes=[kit.ZERO_32, H, H])
    amt = 10_000_000
    accepted("CONTROL two-rCAT pool: rebase reserve 2 with a message naming index 2",
             lambda: valid(kit.spend_action(two, "forge_action_rebase", [H0, 2, amt], extra_spends=[issuer_message(two, 2, amt)])[0]))
    refused("two-rCAT pool: rebase reserve 2 with the message naming index 1 (same amount)",
            lambda: valid(kit.spend_action(two, "forge_action_rebase", [H0, 2, amt], extra_spends=[issuer_message(two, 1, amt)])[0]))
    refused("two-rCAT pool: rebase reserve 1 with the message naming index 2",
            lambda: valid(kit.spend_action(two, "forge_action_rebase", [H0, 1, amt], extra_spends=[issuer_message(two, 2, amt)])[0]))
    # Refund across indices: a twin of reserve 2 named under index 1.
    sw, par = replacement(two, 2, salt=0xA4)
    accepted("CONTROL two-rCAT pool: refund reserve 2 with its twin", lambda: valid(kit.spend_action(sw, "forge_action_refund", [H0, 2, par])[0]))
    refused("two-rCAT pool: reserve 2's twin named as the refund of index 1",
            lambda: valid(kit.spend_action(sw, "forge_action_refund", [H0, 1, par])[0]))


# ── 5. collect after rebase / refund ────────────────────────────────────────


def collect_checks(label, pool, h):
    fees, dao = pool.state[2][2], pool.state[6][2]
    check(f"setup ({label}): fees owed on the rCAT reserve", fees > 0 and dao > 0, f"dev {fees}, DAO {dao}")
    bundle, st = kit.spend_action(pool, "forge_action_collect", [h, [2]])
    res = accepted(f"collect [2] {label}", lambda: valid(bundle))
    if res:
        outs = outputs_of(pool, res[0], 2)
        check(f"  {label}: dev and DAO receive CAT(R, rev(H, recipient)), exactly owed",
              (layered_cat_ph(R, H, PROTOCOL_PH), fees) in outs and (layered_cat_ph(R, H, DAO_PH), dao) in outs, f"{outs}")
        check(f"  {label}: no plain coin of R", not any(ph in (cat_ph(R, PROTOCOL_PH), cat_ph(R, DAO_PH)) for ph, _ in outs))
        check(f"  {label}: leak 0 on every reserve", all(x == 0 for x in leaks(pool, res[0]).values()), f"{leaks(pool, res[0])}")
        st = kit.state_to_list(st)
        check(f"  {label}: fees and DAO owed zeroed on 2", st[2][2] == 0 and st[6][2] == 0)


def case_collect():
    print("\n5. COLLECT AFTER A REBASE / AFTER A REFUND")
    pool = with_fees(rcat_pool(salt=0x67))
    new = int(pool.state[0][2]) // 2
    _b, st = rebase_bundle(pool, 2, new, H0 + 1)
    rebased = pool.advance(kit.state_to_list(st))
    check("after the rebase the owed fees are zero (paid by the rebase)", rebased.state[2][2] == 0 and rebased.state[6][2] == 0)
    b, st, _ = do_swap(rebased, 0, 2, 400_000, H0 + 2, 0xD8)
    valid(b)
    collect_checks("after a rebase and a trade", rebased.advance(kit.state_to_list(st)), H0 + 3)

    pool = with_fees(rcat_pool(salt=0x68))
    swapped, parent = replacement(pool, 2, salt=0xA5)
    check("the replacement holds reserve + dev fee + DAO owed",
          int(swapped.reserves[2].coin.amount) == pool.state[0][2] + pool.state[2][2] + pool.state[6][2])
    _b, st = kit.spend_action(swapped, "forge_action_refund", [H0 + 1, 2, parent])
    valid(_b)
    collect_checks("after a refund", swapped.advance(kit.state_to_list(st)), H0 + 2)
    # Refund and collect in one spend.
    steps = [("forge_action_refund", [H0 + 1, 2, parent]), ("forge_action_collect", [H0 + 1, [2]])]
    bundle, _st = kit.spend_actions(swapped, steps)
    res = accepted("refund + collect [2] in one spend", lambda: valid(bundle))
    if res:
        outs = outputs_of(swapped, res[0], 2)
        check("  the replacement pays dev and DAO exactly owed as rCATs, leak 0",
              (layered_cat_ph(R, H, PROTOCOL_PH), pool.state[2][2]) in outs and (layered_cat_ph(R, H, DAO_PH), pool.state[6][2]) in outs
              and all(x == 0 for x in leaks(swapped, res[0]).values()))


# ── 6. clawback + refund twice ─────────────────────────────────────────────


def case_twice():
    print("\n6. CLAWBACK AND REFUND, TWICE IN A ROW")
    pool = with_fees(rcat_pool(salt=0x69))
    cur, h = pool, H0 + 1
    for n, salt in ((1, 0xA6), (2, 0xA7)):
        sw, par = replacement(cur, 2, salt=salt)
        bundle, st = kit.spend_action(sw, "forge_action_refund", [h, 2, par])
        res = accepted(f"refund #{n} at height {h}", lambda: valid(bundle))
        if not res:
            return
        check(f"  refund #{n}: leak 0", all(x == 0 for x in leaks(sw, res[0]).values()))
        cur = sw.advance(kit.state_to_list(st))
        check(f"  refund #{n}: the successor records replacement #{n}", list(cur.state)[7][2] == sw.reserves[2].coin.name())
        h += 1
    money = lambda s: [s[0], s[1], s[2], s[5], s[6]]  # noqa: E731
    check("after two refunds: money fields equal the pre-clawback pool", money(list(cur.state)) == money(list(pool.state)))
    # Reusing the FIRST replacement's parent on the twice-refunded successor: that coin was spent.
    sw1, par1 = replacement(pool, 2, salt=0xA6)
    stale = with_reserve_coin(cur, 2, sw1.reserves[2].coin, sw1.reserves[2].lineage)
    res = attempt(lambda: valid(kit.spend_action(stale, "forge_action_refund", [h, 2, par1])[0]))
    # Offline, consensus does not know that replacement #1 was spent; on chain it would be
    # DOUBLE_SPEND / UNKNOWN_UNSPENT. Reported, not counted either way.
    print(f"   note: re-adopting replacement #1 offline: {res[0]} (on chain: it is spent, so the bundle cannot confirm)")
    b, st2, out = do_swap(cur, 2, 0, 100_000, h, 0xD9)
    res = accepted("the twice-refunded pool trades R -> XCH", lambda: valid(b))
    if res:
        check("  leak exact", leaks(cur, res[0]) == {0: 0, 1: 0, 2: -100_000} and (OFFER_MOD_HASH, paid(cur.state, st2, 0, out)) in outputs_of(cur, res[0], 0),
              f"{leaks(cur, res[0])}")
    # Remove after two refunds: the burner's share of R is layered and exact.
    burn = 100_000
    vf = forge_math.vault_fee_bps(3, 10, cur.fee_bps)
    shares = forge_math.withdrawal_amounts(cur.state[0], burn, cur.state[1], vf)
    probe, _, _, _ = kit.run_leaf(cur, "forge_action_remove", [h, burn, bytes32(b"\x01" * 32), shares])
    lp_parent, lp_spends = kit.lp_melt_spend(cur, burn, cur.state[1] - burn, probe.get_tree_hash(), salt=0xB1)
    res = accepted("LP remove after two refunds", lambda: valid(kit.spend_action(cur, "forge_action_remove", [h, burn, lp_parent, shares],
                                                                                    extra_spends=lp_spends)[0]))
    if res:
        check("  the burner's R share is layered and exact; leak 0",
              (layered_cat_ph(R, H, OFFER_MOD_HASH), shares[2]) in outputs_of(cur, res[0], 2)
              and all(x == 0 for x in leaks(cur, res[0]).values()))


# ── 7. other attacks on rebase / refund / adopt ─────────────────────────────


def case_other():
    print("\n7. OTHER ATTACKS")
    pool = with_fees(rcat_pool(salt=0x6A))
    swapped, parent = replacement(pool, 2, salt=0xA8)

    print(" adopt with a malformed parent (the refund leaf run locally):")
    accepted("CONTROL a 32-byte parent", lambda: valid(kit.spend_action(swapped, "forge_action_refund", [H0 + 1, 2, parent])[0]))
    for label, bad in (("a 31-byte parent", bytes(parent)[:31]), ("a 33-byte parent", bytes(parent) + b"\x00"),
                       ("a nil parent", b""), ("the zero parent", bytes(32)), ("a pair as parent", Program.to([parent]))):
        refused(f"refund with {label}", lambda bad=bad: kit.run_leaf(swapped, "forge_action_refund", [H0 + 1, 2, bad]))

    print(" rebase and refund in one spend:")
    new = int(pool.state[0][2]) // 2
    steps = [("forge_action_rebase", [H0 + 1, 2, new]), ("forge_action_refund", [H0 + 1, 2, parent])]
    bundle, st = kit.spend_actions(swapped, steps, extra_spends=[issuer_message(swapped, 2, new)])
    res = accepted("rebase first, then refund (the issuer re-denominates a reserve it took)", lambda: valid(bundle))
    if res:
        outs = outputs_of(swapped, res[0], 2)
        old, fee, dao = pool.state[0][2], pool.state[2][2], pool.state[6][2]
        check("  the replacement pays dev, DAO and the surplus exactly; leak 0",
              (layered_cat_ph(R, H, PROTOCOL_PH), fee) in outs and (layered_cat_ph(R, H, DAO_PH), dao) in outs
              and (layered_cat_ph(R, H, H), old - new) in outs and all(x == 0 for x in leaks(swapped, res[0]).values()), f"{outs}")
        check("  the successor records the replacement and the new amount",
              kit.state_to_list(st)[0][2] == new)
    refused("refund first, then rebase (rebase must be the first action)",
            lambda: valid(kit.spend_actions(swapped, list(reversed(steps)), extra_spends=[issuer_message(swapped, 2, new)])[0]))

    print(" two rebases in one spend:")
    s2 = [("forge_action_rebase", [H0 + 1, 2, new]), ("forge_action_rebase", [H0 + 1, 2, new // 2])]
    refused("rebase then rebase again (both messages present)",
            lambda: valid(kit.spend_actions(pool, s2, extra_spends=[issuer_message(pool, 2, new), issuer_message(pool, 2, new // 2, parent=b"\xe3")])[0]))

    print(" two refunds of one reserve, the second with a non-canonical index:")
    two = kit.make_pool([None, R, R2], [10_000_000, 20_000_000, 30_000_000], total_lp=5_000_000, leaves="forge", salt=0x6B,
                        protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=50, hidden_puzzle_hashes=[kit.ZERO_32, H, H])
    t1, q1 = replacement(two, 1, salt=0xAC)
    t12, q2 = replacement(t1, 2, salt=0xAD)
    accepted("CONTROL two refunds in one spend, of DIFFERENT reserves (two-rCAT pool, 1 and 0x0002)",
             lambda: valid(kit.spend_actions(t12, [("forge_action_refund", [H0, 1, q1]), ("forge_action_refund", [H0, b"\x00\x02", q2])])[0]))
    _sw2, parent2 = replacement(pool, 2, salt=0xAB)
    for label, second in (("2 then 0x0002, two different parents", [H0 + 1, b"\x00\x02", parent2]),
                          ("2 then 0x0002, the same parent", [H0 + 1, b"\x00\x02", parent])):
        refused(f"refunds {label}",
                lambda second=second: valid(kit.spend_actions(swapped, [("forge_action_refund", [H0 + 1, 2, parent]),
                                                                        ("forge_action_refund", second)])[0]))

    print(" a rebase whose owed fees exceed the new amount:")
    fee, dao = pool.state[2][2], pool.state[6][2]
    check("setup: dev + DAO owed > 1", fee + dao > 1, f"{fee + dao}")
    bundle, st = rebase_bundle(pool, 2, 1, H0 + 1)
    res = accepted("rebase to 1 with fees owed", lambda: valid(bundle))
    if res:
        outs = outputs_of(pool, res[0], 2)
        check("  fees paid in full in pre-rebase units, surplus old - 1, leak 0",
              (layered_cat_ph(R, H, PROTOCOL_PH), fee) in outs and (layered_cat_ph(R, H, DAO_PH), dao) in outs
              and (layered_cat_ph(R, H, H), pool.state[0][2] - 1) in outs and all(x == 0 for x in leaks(pool, res[0]).values()))
    print(" an upward rebase with fees owed:")
    old = pool.state[0][2]
    up = old * 3
    _c, fund = layered_r_coin(up - old, 0xF2)
    bundle, st = rebase_bundle(pool, 2, up, H0 + 1, extra_cats={R: [fund]})
    res = accepted("rebase up x3, issuer funds exactly new - old", lambda: valid(bundle))
    if res:
        outs = outputs_of(pool, res[0], 2)
        check("  fees paid as rCATs, the reserve grows by exactly the funding",
              (layered_cat_ph(R, H, PROTOCOL_PH), fee) in outs and (layered_cat_ph(R, H, DAO_PH), dao) in outs
              and leaks(pool, res[0]).get(2) == -(up - old), f"{leaks(pool, res[0])}")
    # Unbalanced rings, built without the driver's Python check so CONSENSUS decides. CONTROL: the
    # exactly-funded ring through the same unchecked builder.
    _c, short = layered_r_coin(up - old - fee - dao, 0xF3)
    with unchecked_ring():
        accepted("CONTROL (unchecked ring builder) rebase up, exactly funded",
                 lambda: valid(rebase_bundle(pool, 2, up, H0 + 1, extra_cats={R: [fund]})[0]))
        refused("rebase up funded short by exactly the owed fees (the fees cannot fund the upward difference)",
                lambda: valid(rebase_bundle(pool, 2, up, H0 + 1, extra_cats={R: [short]})[0]))
        _c, one = layered_r_coin(1, 0xF4)
        refused("rebase up, UNFUNDED (a 1-mojo R coin keeps a ring; consensus, not the Python builder)",
                lambda: valid(rebase_bundle(pool, 2, up, H0 + 1, extra_cats={R: [one]})[0]))
        # The two refusals above are MINTING_COIN (the bundle's mojos do not cover the CAT outputs).
        # Add enough bare XCH to the bundle that mojo totals balance, so the CAT ring itself must refuse.
        mojo_coin = Coin(bytes32(b"\x3c" * 32), bytes32(Program.to(1).get_tree_hash()), uint64(up))
        mojos = make_spend(mojo_coin, Program.to(1), Program.to([]))
        refused("rebase up, UNFUNDED, mojo totals covered by bare XCH (only the CAT ring can refuse)",
                lambda: valid(kit.SpendBundle([*rebase_bundle(pool, 2, up, H0 + 1, extra_cats={R: [one]})[0].coin_spends, mojos],
                                              G2Element())))
        accepted("CONTROL the exactly-funded ring plus the same bare XCH coin",
                 lambda: valid(kit.SpendBundle([*rebase_bundle(pool, 2, up, H0 + 1, extra_cats={R: [fund]})[0].coin_spends, mojos],
                                               G2Element())))
    print(" rebase to the same amount:")
    res = accepted("rebase to old (no surplus, fees still paid)", lambda: valid(rebase_bundle(pool, 2, old, H0 + 1)[0]))
    if res:
        outs = outputs_of(pool, res[0], 2)
        check("  no surplus coin to H, leak 0", not any(ph == layered_cat_ph(R, H, H) for ph, _ in outs)
              and all(x == 0 for x in leaks(pool, res[0]).values()))
    print(" a no-op refund (new_parent = the recorded parent):")
    res = accepted("refund naming the current reserve's own parent",
                   lambda: valid(kit.spend_action(pool, "forge_action_refund", [H0 + 1, 2, pool.reserves[2].coin.parent_coin_info])[0]))
    if res:
        check("  it is a no-op: leak 0, same coin recorded", all(x == 0 for x in leaks(pool, res[0]).values()))
    print(" a refund aimed at the XCH reserve:")
    xch_twin = with_reserve_coin(pool, 0, Coin(bytes32(b"\x31" * 32), pool.reserves[0].coin.puzzle_hash, pool.reserves[0].coin.amount), None)
    refused("refund index 0 (XCH, zero H) with a twin of the XCH reserve",
            lambda: valid(kit.spend_action(xch_twin, "forge_action_refund", [H0 + 1, 0, bytes32(b"\x31" * 32)])[0]))


def main() -> int:
    if not kit.v19_available():
        print("  [skip] V19 build outputs are absent")
        return 2
    case_drain()
    case_twin()
    case_refund_and_payout()
    case_replay()
    case_collect()
    case_twice()
    case_other()
    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} rCAT attack checks passed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
