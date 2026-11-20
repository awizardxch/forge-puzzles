#!/usr/bin/env python3
"""V20 rCAT issuer lanes: `rebase` (the issuer) and `refund` (anyone). Owner decision D4,
2026-11-11 (Reading C): the rCAT reserve stays under the layer, the LP stays a plain CAT, the
issuer can re-denominate its reserve, and a reserve the issuer took by its hidden path can be
replaced so the pool does not freeze.

Every case runs through full consensus; every accepted bundle is checked with the per-reserve
leak oracle (coin amount minus its CREATE_COINs, from consensus output). Refusals are a leaf
raise (the same CLVM as on chain) or consensus `Rejected`; anything else is a harness error.

  REBASE   down: dev + DAO fees paid first (as rCATs), surplus to CAT(R, rev(H, H)) -- a coin
           only the issuer can spend. up: funded by the issuer. Refused: unfunded up, no
           message, a message from another coin, naming another amount, a plain asset, replayed
           on the successor, a trade placed BEFORE it, zero, negative, a redirected surplus,
           any rebase in a plain pool.
  REFUND   a replacement at the reserve's own layered hash and recorded amount becomes the
           reserve, and the pool trades on. Refused: another amount, a stripped layer, another
           H, a plain asset, two refunds of one reserve, a replacement used WITHOUT refund, an
           index out of range, any refund in a plain pool.

Exit 0 all pass, 1 a failure.
"""
from __future__ import annotations

import sys
from dataclasses import replace

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_math  # noqa: E402
import forge_v20_driver as kit  # noqa: E402
from chia.types.blockchain_format.coin import Coin  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, SpendableCAT, construct_cat_puzzle  # noqa: E402
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.vc_wallet.vc_drivers import REVOCATION_LAYER, REVOCATION_LAYER_HASH  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

Program = kit.Program
PLAIN, R = bytes32(b"\xd0" * 32), bytes32(b"\xd5" * 32)
ISSUER_PUZZLE = Program.to([5, [4, 1, []]])           # the issuer's hidden puzzle: runs its solution
H = bytes32(ISSUER_PUZZLE.get_tree_hash())
OTHER_PUZZLE = Program.to([6, [4, [], 1]])            # (r (c () 1)): also runs its solution, another hash
H_OTHER = bytes32(b"\x4e" * 32)
PROTOCOL_PH, DAO_PH = bytes32(b"\x7a" * 32), bytes32(b"\x66" * 32)
ATTACKER = bytes32(b"\xa7" * 32)
H0 = 6_999_990
results: list[bool] = []


def check(label, good, detail=""):
    # An accepted result is a (conditions, additions) tuple, not text; never crash on it (twice a
    # mutant's ACCEPTED result crashed this print instead of being reported).
    detail = detail if isinstance(detail, str) else "ACCEPTED"
    results.append(bool(good))
    print(f"  [{'PASS' if good else 'FAIL'}] {label}{'  -- ' + detail if detail else ''}")


def cat_ph(asset, inner_ph):
    return bytes32(construct_cat_puzzle(CAT_MOD, asset, Program.to(inner_ph)).get_tree_hash_precalc(inner_ph))


def layered_cat_ph(asset, hidden, inner_ph):
    return cat_ph(asset, bytes32(REVOCATION_LAYER.curry(REVOCATION_LAYER_HASH, hidden, inner_ph).get_tree_hash()))


class HarnessError(Exception):
    """A bug in this file. Never caught by `attempt`, so it can never be counted as a refusal."""


def valid(bundle):
    """kit.validate, but only for a real SpendBundle. kit.validate wraps EVERY exception as
    Rejected, so handing it anything else (e.g. spend_actions' (bundle, state) tuple) would look
    exactly like a consensus refusal -- the first run of this suite passed a check that way."""
    if not isinstance(bundle, kit.SpendBundle):
        raise HarnessError(f"not a SpendBundle: {type(bundle).__name__}")
    return kit.validate(bundle)


def attempt(thunk):
    try:
        return "accepted", thunk()
    except kit.Rejected as exc:
        return "refused", f"consensus {str(exc)[:55]}"
    except ValueError as exc:   # a leaf's CLVM raising, run locally while building
        return "refused", f"leaf {str(exc)[:55]}"


def leaks(pool, conds):
    ids = {bytes(r.coin.name()): r.index for r in pool.reserves}
    return {ids[bytes(s.coin_id)]: int(s.coin_amount) - sum(int(c[1]) for c in s.create_coin)
            for s in conds.spends if bytes(s.coin_id) in ids}


def outputs_of(pool, conds, index):
    rid = bytes(pool.reserves[index].coin.name())
    return [(bytes32(c[0]), int(c[1])) for s in conds.spends if bytes(s.coin_id) == rid for c in s.create_coin]


# ── coins ───────────────────────────────────────────────────────────────────


def issuer_message(pool, index, amount, puzzle=ISSUER_PUZZLE, receiver=None, parent=b"\xe1"):
    """A standalone coin AT `puzzle`'s hash sending the rebase message (mode 23) to the pool coin."""
    msg = kit.rebase_message(index, amount) if hasattr(kit, "rebase_message") else \
        bytes32(Program.to([b"forge-rebase-v20", index, amount]).get_tree_hash())
    coin = Coin(bytes32(parent * 32), bytes32(puzzle.get_tree_hash()), uint64(1))
    return make_spend(coin, puzzle, Program.to([[66, 0b010111, msg, receiver or pool.coin.name()]]))


def layered_r_coin(amount, salt, outputs=(), hidden=H):
    """A coin of R under the layer with `hidden`, spent through the owner path (identity inner)."""
    inner = kit.layered(hidden, kit.IDENTITY)
    outer = construct_cat_puzzle(CAT_MOD, R, inner).get_tree_hash()
    gp = bytes32(bytes([salt]) * 32)
    coin = Coin(kit.coin_id(gp, outer, amount), outer, uint64(amount))
    return coin, SpendableCAT(coin, R, inner, kit.layered_solution(hidden, kit.IDENTITY, list(outputs)),
                              lineage_proof=LineageProof(gp, inner.get_tree_hash(), uint64(amount)))


def rcat_pool(salt=0x51, hidden=None):
    return kit.make_pool([None, PLAIN, R], [10_000_000, 20_000_000, 30_000_000], total_lp=5_000_000,
                         leaves="forge", salt=salt, protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=50,
                         hidden_puzzle_hashes=hidden if hidden is not None else [kit.ZERO_32, kit.ZERO_32, H])


def with_fees(pool):
    """The pool after a swap into R, so dev and DAO fees are owed on the rCAT reserve."""
    r, w = pool.state[0], pool.weights
    out = forge_math.swap_output(r[0], r[2], 600_000, pool.fee_bps, w[0], w[2])
    coin, spend = kit.offer_settlement_xch(600_000, salt=0xC1)
    bundle, st = kit.spend_action(pool, "forge_action_swap", [H0, 0, 2, 600_000, out, *kit.settlement_ref(coin)], extra_spends=[spend])
    valid(bundle)
    return pool.advance(kit.state_to_list(st))


def rebase(pool, index, amount, spelled_index=None, spelled_amount=None, message=True, extra_spends=(), extra_cats=None,
           reserve_delegated=None, h=H0 + 5):
    sol = [h, spelled_index if spelled_index is not None else index, spelled_amount if spelled_amount is not None else amount]
    es = list(extra_spends) + ([issuer_message(pool, index, amount)] if message is True else ([message] if message else []))
    if reserve_delegated is None:
        return valid(kit.spend_action(pool, "forge_action_rebase", sol, extra_spends=es, extra_cats=extra_cats)[0])
    new_state, tagged, _b, _e = kit.run_leaf(pool, "forge_action_rebase", sol)
    return valid(kit.assemble(pool, pool.leaves["forge_action_rebase"], pool.leaf_proof("forge_action_rebase"),
                                     kit.with_birth(pool, sol), new_state, tagged, reserve_delegated=reserve_delegated,
                                     extra_spends=es, extra_cats=extra_cats))


# ── rebase ──────────────────────────────────────────────────────────────────


def rebase_section():
    pool = with_fees(rcat_pool())
    old, fee, dao = pool.state[0][2], pool.state[2][2], pool.state[6][2]
    print(f"\nREBASE  rCAT reserve {old:,}, fees owed {fee} dev + {dao} DAO")

    new = old // 2   # a 1:2 reverse split
    v, res = attempt(lambda: rebase(pool, 2, new))
    check("down (1:2 reverse split): accepted", v == "accepted", res if v != "accepted" else "")
    if v == "accepted":
        conds, adds = res
        outs = outputs_of(pool, conds, 2)
        check("  the dev fee is paid first, as an rCAT, exactly owed", (layered_cat_ph(R, H, PROTOCOL_PH), fee) in outs)
        check("  the DAO fee is paid first, as an rCAT, exactly owed", (layered_cat_ph(R, H, DAO_PH), dao) in outs)
        check("  the surplus goes to CAT(R, rev(H, H)), a coin only the issuer can spend",
              (layered_cat_ph(R, H, H), old - new) in outs, f"surplus {old - new:,}")
        check("  no plain coin of R is created", not any(ph == cat_ph(R, x) for ph, _ in outs for x in (H, PROTOCOL_PH, DAO_PH)))
        check("  leak 0 on every reserve", all(x == 0 for x in leaks(pool, conds).values()), f"{leaks(pool, conds)}")
        st = kit.state_to_list(kit.spend_action(pool, "forge_action_rebase", [H0 + 5, 2, new], extra_spends=[issuer_message(pool, 2, new)])[1])
        check("  state: the reserve is the new amount, its fees zeroed, LP supply unchanged",
              st[0][2] == new and st[2][2] == 0 and st[6][2] == 0 and st[1] == pool.state[1])

    up = old * 2     # a 2:1 split
    fund_coin, fund = layered_r_coin(up - old, 0xF1)
    v, res = attempt(lambda: rebase(pool, 2, up, extra_cats={R: [fund]}))
    check("up (2:1 split), the issuer funds the difference: accepted", v == "accepted", res if v != "accepted" else "")
    if v == "accepted":
        lk = leaks(pool, res[0])
        check("  the reserve grows by exactly the funding (leak = -(new - old))", lk.get(2) == -(up - old), f"leak {lk}")
    v, why = attempt(lambda: rebase(pool, 2, up))
    check("up, UNFUNDED: refused", v == "refused", why)

    print(" REBASE refusals:")
    cases = [
        ("no issuer message", lambda: rebase(pool, 2, new, message=None)),
        ("a message from a coin at another puzzle hash", lambda: rebase(pool, 2, new, message=issuer_message(pool, 2, new, puzzle=OTHER_PUZZLE))),
        ("a message naming another amount", lambda: rebase(pool, 2, new, message=issuer_message(pool, 2, new - 1))),
        ("a plain asset (index 1, zero H)", lambda: rebase(pool, 1, 10_000_000)),
        ("rebase to zero", lambda: rebase(pool, 2, 0)),
        ("a negative amount", lambda: rebase(pool, 2, -5)),
        ("an index out of range", lambda: rebase(pool, 3, new)),
        ("the surplus redirected to an attacker", lambda: rebase(pool, 2, new, reserve_delegated={2: Program.to((1, [
            [51, pool.reserves[2].inner_hash, new, [pool.hint]], [51, ATTACKER, old - new + fee + dao]]))})),
    ]
    for label, thunk in cases:
        v, why = attempt(thunk)
        check(f"{label}: refused", v == "refused", why if v == "refused" else "ACCEPTED")

    # Replay: the issuer's message to THIS pool coin, presented to the successor.
    v, res = attempt(lambda: rebase(pool, 2, new))
    if v == "accepted":
        nxt = pool.advance(kit.state_to_list(kit.spend_action(pool, "forge_action_rebase", [H0 + 5, 2, new],
                                                              extra_spends=[issuer_message(pool, 2, new)])[1]))
        # A LATER height, so the only thing wrong is the message's receiver (the first run of this
        # case was refused by the height check instead, which proved nothing about replay).
        v, why = attempt(lambda: rebase(nxt, 2, new // 2, h=H0 + 6))
        check("CONTROL the successor accepts a fresh rebase message addressed to it", v == "accepted", why if v != "accepted" else "")
        v, why = attempt(lambda: rebase(nxt, 2, new // 2, h=H0 + 6,
                                        message=issuer_message(pool, 2, new // 2, receiver=pool.coin.name())))
        check("the issuer's message to the previous pool coin, replayed on the successor: refused", v == "refused", why)

    print(" REBASE ordering (the sandwich):")
    r, w = pool.state[0], pool.weights
    out = forge_math.swap_output(r[0], r[1], 100_000, pool.fee_bps, w[0], w[1])
    coin, spend = kit.offer_settlement_xch(100_000, salt=0xC5)
    swap_step = ("forge_action_swap", [H0 + 5, 0, 1, 100_000, out, *kit.settlement_ref(coin)])
    reb_step = ("forge_action_rebase", [H0 + 5, 2, new])
    # The CONTROL first: the same two steps, rebase leading, must be accepted, or the refusal
    # below could be any failure of this construction.
    v, why = attempt(lambda: valid(kit.spend_actions(pool, [reb_step, swap_step], extra_spends=[spend, issuer_message(pool, 2, new)])[0]))
    check("CONTROL the rebase first, then a trade at the new price: accepted", v == "accepted", why if v != "accepted" else "")
    v, why = attempt(lambda: valid(kit.spend_actions(pool, [swap_step, reb_step], extra_spends=[spend, issuer_message(pool, 2, new)])[0]))
    check("a trade BEFORE the rebase in one spend: refused", v == "refused", why if v == "refused" else "ACCEPTED")

    print(" REBASE canonical:")
    v, res = attempt(lambda: rebase(pool, 2, new, spelled_index=b"\x00\x02"))
    check("an index spelled 0x0002 rebases reserve 2 (the message names canonical 2)", v == "accepted", res if v != "accepted" else "")

    plain = rcat_pool(salt=0x52, hidden=[kit.ZERO_32] * 3)
    for i in (1, 2):
        v, why = attempt(lambda: rebase(plain, i, 1_000))
        check(f"a plain pool: rebase of reserve {i} refused (the leaf is inert)", v == "refused", why)


# ── refund ──────────────────────────────────────────────────────────────────


def replacement(pool, index, amount=None, under="layered", salt=0xA9):
    """A coin standing in for reserve `index` after the issuer took it, parented by a coin of R
    under H, holding `amount`. `under` is the H the replacement's OWN puzzle carries: "layered"
    (the pool's H -- the honest replacement), ZERO_32 (the layer stripped) or another H. The
    replacement is spent with ITS OWN puzzle, so a refusal can only come from the pool's binding;
    spending it with the layered reveal would fail as WRONG_PUZZLE_HASH and prove nothing."""
    r = pool.reserves[index]
    asset = r.asset_id
    amount = int(r.coin.amount) if amount is None else amount
    hidden = r.hidden if under == "layered" else under
    own = replace(r, hidden=hidden)
    full = bytes32(construct_cat_puzzle(CAT_MOD, asset, own.cat_inner).get_tree_hash())
    # The funder is a coin of the same asset under the reserve's own H (plain for a plain reserve).
    funder_inner = kit.layered(r.hidden, kit.IDENTITY)
    funder_outer = construct_cat_puzzle(CAT_MOD, asset, funder_inner).get_tree_hash()
    gp = bytes32(bytes([salt]) * 32)
    parent = kit.coin_id(gp, funder_outer, amount)
    coin = Coin(parent, full, uint64(amount))
    lin = LineageProof(gp, funder_inner.get_tree_hash(), uint64(amount))
    return replace(pool, reserves=[replace(own, coin=coin, lineage=lin) if x.index == index else x for x in pool.reserves]), parent


def refund_section():
    pool = rcat_pool(salt=0x53)
    print(f"\nREFUND  the issuer took reserve 2 ({int(pool.reserves[2].coin.amount):,}) by its hidden path")

    swapped, parent = replacement(pool, 2)
    v, res = attempt(lambda: valid(kit.spend_action(swapped, "forge_action_refund", [H0, 2, parent])[0]))
    check("a replacement at the reserve's layered hash, recorded amount: accepted", v == "accepted", res if v != "accepted" else "")
    if v == "accepted":
        conds, adds = res
        check("  leak 0 on every reserve", all(x == 0 for x in leaks(swapped, conds).values()))
        st = kit.spend_action(swapped, "forge_action_refund", [H0, 2, parent])[1]
        after = swapped.advance(kit.state_to_list(st))
        check("  the successor pool coin is the one consensus created", (after.coin.puzzle_hash, 1) in adds)
        # Every action advances the oracle (field 3) and its prev_root (4); the money fields must not move.
        money = lambda s: [s[0], s[1], s[2], s[5], s[6]]  # noqa: E731  reserves, LP, fees, DAO rate, DAO owed
        check("  reserves, LP supply, fees and DAO owed are unchanged",
              money(kit.state_to_list(st)) == money(list(pool.state)))
        check("  the successor records the replacement as reserve 2's coin",
              list(after.state)[7][2] == swapped.reserves[2].coin.name())
        r, w = after.state[0], after.weights
        out = forge_math.swap_output(r[0], r[2], 100_000, after.fee_bps, w[0], w[2])
        coin, spend = kit.offer_settlement_xch(100_000, salt=0xC7)
        v2, why = attempt(lambda: valid(kit.spend_action(after, "forge_action_swap", [H0 + 1, 0, 2, 100_000, out, *kit.settlement_ref(coin)],
                                                                extra_spends=[spend])[0]))
        check("  and the pool trades on: a swap out of the refunded reserve is accepted", v2 == "accepted", why if v2 != "accepted" else "")

    print(" REFUND refusals:")
    amt = int(pool.reserves[2].coin.amount)
    plain_inner = pool.reserves[2].inner_hash
    # Each replacement is spent with its own (valid) puzzle and, when it is short, the ring is
    # balanced by one more mojo of R -- so the ONLY thing that can refuse it is the finalizer
    # addressing reserve 2's message to coinid(parent, the layered full hash, the recorded amount).
    _bal_coin, balance = layered_r_coin(1, 0xB7)
    cases = [
        ("a replacement one mojo short (ring balanced by another mojo of R)", lambda: replacement(pool, 2, amount=amt - 1), {R: [balance]}),
        ("a replacement with the layer stripped (a plain CAT of R)", lambda: replacement(pool, 2, under=kit.ZERO_32), None),
        ("a replacement under another H", lambda: replacement(pool, 2, under=H_OTHER), None),
    ]
    for label, make, cats in cases:
        def go(make=make, cats=cats):
            p, par = make()
            return valid(kit.spend_action(p, "forge_action_refund", [H0, 2, par], extra_cats=cats)[0])
        v, why = attempt(go)
        check(f"{label}: refused", v == "refused", why if v == "refused" else "ACCEPTED")

    v, why = attempt(lambda: valid(kit.spend_action(swapped, "forge_action_refund", [H0, 1, parent])[0]))
    check("a refund of the plain CAT's reserve with a coin that is not one: refused", v == "refused", why)
    # A GENUINE replacement of the plain reserve -- at its own hash, the recorded amount. Only the
    # leaf's zero-H assert refuses this: a plain reserve can never be taken, so swapping in another
    # coin could only abandon the original forever (a plain coin has no hidden path back).
    plain_swapped, plain_parent = replacement(pool, 1)
    v, why = attempt(lambda: valid(kit.spend_action(plain_swapped, "forge_action_refund", [H0, 1, plain_parent])[0]))
    check("a GENUINE replacement of the plain CAT's reserve: refused", v == "refused", why)
    v, why = attempt(lambda: valid(kit.spend_action(swapped, "forge_action_refund", [H0, 3, parent])[0]))
    check("an index out of range: refused", v == "refused", why)
    v, why = attempt(lambda: valid(kit.spend_actions(swapped, [("forge_action_refund", [H0, 2, parent]),
                                                                ("forge_action_refund", [H0, 2, parent])])[0]))
    check("two refunds of one reserve in one spend: refused", v == "refused", why)
    v, why = attempt(lambda: valid(kit.spend_action(swapped, "forge_action_observe", [H0])[0]))
    check("the replacement used WITHOUT a refund (any other action): refused", v == "refused", why)
    v, res = attempt(lambda: valid(kit.spend_action(swapped, "forge_action_refund", [H0, b"\x00\x02", parent])[0]))
    check("an index spelled 0x0002 refunds reserve 2 (canonical)", v == "accepted", res if v != "accepted" else "")

    plain = rcat_pool(salt=0x54, hidden=[kit.ZERO_32] * 3)
    p2, par2 = replacement(plain, 2)
    v, why = attempt(lambda: valid(kit.spend_action(p2, "forge_action_refund", [H0, 2, par2])[0]))
    check("a plain pool: refund refused (the leaf is inert)", v == "refused", why)


def main() -> int:
    if not kit.v20_available():
        print("  [skip] V20 build outputs are absent")
        return 2
    rebase_section()
    refund_section()
    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} rCAT issuer-lane checks passed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
