#!/usr/bin/env python3
"""V14 genesis binds to exactly one LP eve (CHIP-0062 review P0).

The V11 hole, reproduced by CNI with an accepted bundle: the TAIL's genesis branch
asserted the launcher's one coin announcement, and ANY coin can assert an
announcement. Two independently funded eves, each asserting it, each minted the
genesis supply -- state said 5,000,000, 10,000,000 existed, and the hidden half
could be redeemed against later depositors' reserves.

V14 puts the eve's own coin id into the announced list: the launcher announces
(total_lp, eve_coin_id), and the TAIL asserts that list with `my_coin_id` from its
CAT truths. A second eve needs an announcement that was never made.

The offline validator does not check coin existence, so the launcher's own parent
need not be spent here; what it does check is that every asserted announcement was
emitted in the bundle, which is exactly the lock under test.
"""
import sys

sys.path.insert(0, ".")

from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32

import _v14_testkit as kit

FAILED = 0
CATS = [bytes32(bytes([0xD0 + i]) * 32) for i in range(2)]
RECIPIENT = bytes32(b"\x42" * 32)


def check(label, ok, detail=""):
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILED += 1


def refuses(label, thunk):
    try:
        thunk()
    except kit.Rejected as exc:
        check(label, True); print(f"          refused: {str(exc)[:80]}"); return
    check(label, False, "ACCEPTED -- this is the exploit")


def accepts(label, thunk):
    try:
        out = thunk()
    except kit.Rejected as exc:
        check(label, False, f"refused: {exc}"); return None
    check(label, True); return out


def bundle(spends):
    return SpendBundle(list(spends), G2Element())


def main():
    pool = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50)
    total = pool.state[1]
    eve_a = kit.genesis_eve_id(pool, salt=0xA1)
    eve_b = kit.genesis_eve_id(pool, salt=0xB1)
    check("two differently funded eves have different coin ids", eve_a != eve_b)

    print("honest genesis:")
    _, launcher = kit.launcher_spend(pool, kv_list=[total, eve_a])
    ring_a = kit.lp_genesis_mint_spends(pool, RECIPIENT, salt=0xA1)
    out = accepts("the launcher names eve A; eve A mints the genesis supply", lambda: kit.validate(bundle([launcher, *ring_a])))
    if out:
        _, additions = out
        lp_ph = kit.construct_cat_puzzle(kit.CAT_MOD, pool.lp_asset_id, kit.Program.to(RECIPIENT)).get_tree_hash_precalc(RECIPIENT)
        burn_ph = kit.construct_cat_puzzle(kit.CAT_MOD, pool.lp_asset_id, kit.Program.to(kit.ZERO_32)).get_tree_hash_precalc(kit.ZERO_32)
        # V14: the settlement pays the locked floor to the zero puzzle hash and the rest to the creator
        check("  the creator received exactly total_lp - LOCKED_BURN", sum(a for ph, a in additions if ph == lp_ph) == total - kit.LOCKED_BURN)
        check("  and LOCKED_BURN was burned to the zero puzzle hash", sum(a for ph, a in additions if ph == burn_ph) == kit.LOCKED_BURN)

    print("the review's attack:")
    ring_b = kit.lp_genesis_mint_spends(pool, RECIPIENT, salt=0xB1)
    refuses("a SECOND eve asserting the same launcher announcement is refused (V11 accepted this)",
            lambda: kit.validate(bundle([launcher, *ring_a, *ring_b])))
    refuses("eve B alone, against a launcher that named eve A, is refused",
            lambda: kit.validate(bundle([launcher, *ring_b])))

    print("the old announcement shape is dead:")
    _, old_launcher = kit.launcher_spend(pool, kv_list=[total])
    refuses("a launcher announcing only (total_lp), V11's list, authorizes nobody",
            lambda: kit.validate(bundle([old_launcher, *ring_a])))

    print("the registry agrees with the TAIL:")
    # register_solution carries the eve id the TAIL will require; the registry asserts the same list
    sol = kit.register_solution(pool, (bytes32(b"\x00" * 32), bytes32(b"\x00" * 32), bytes32(b"\x00" * 32)),
                                (bytes32(b"\xff" * 32), bytes32(b"\xff" * 32), bytes32(b"\xff" * 32)))
    check("register_solution names the pool's eve coin id", sol[6] == pool.extra.get("eve_coin_id", kit.genesis_eve_id(pool)))
    # V14: the solution carries the GRANDPARENTS; the parents in the eve state are what
    # `register` derives from them and the launcher's hash (spec 1.4)
    check("register_solution carries the reserves' grandparents, not their parents",
          sol[5] == [r.grandparent for r in pool.reserves] and sol[5] != pool.state[7])
    check("  and the eve state's parents are exactly the launchers derived from those grandparents",
          [kit.coin_id(gp, kit.reserve_launcher_full_hash(a), amt) for gp, a, amt in
           zip(sol[5], pool.asset_ids, pool.state[0])] == pool.state[7])

    print()
    print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- V14 genesis checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
