#!/usr/bin/env python3
"""V13 oracle: every block is credited once, at the price that was in force.

V12 measured elapsed time from the pool coin's birth height (`h - birth`), which closed
the review's same-bundle backfill: `birth` is a consensus fact, so a solver can only
understate the interval. The second audit (2026-09-14) found what understating costs.
Spend k claims `h_k` and is included at block `M_k`; its successor is born at `M_k` and
credits its pre-spend price over `h_{k+1} - M_k`. The blocks `(h_k, M_k]` -- during
which the PREVIOUS state was still in force -- were credited to nobody, and V12 wrote
`last_height = h_k`, so they never could be. Claiming `h = birth` every time stood the
accumulator still while real blocks passed; an honest pool spent in every transaction
block did the same by accident. Ten permissionless `observe`s drove a TWAP to zero.

V13 keeps the spot it last credited in state (`last_spot`) and credits two intervals:

    cums' = cums + last_spot * (birth - last_height) + spot * (h - birth)

so the tail the previous spend could not see is credited by the next spend, at the price
that was actually in force. Understating `h` defers credit; nothing deletes it; nothing
fabricates it, because `birth` is asserted (ASSERT_MY_BIRTH_HEIGHT) and `h` cannot
exceed the block that includes the spend. The prologue also asserts `birth > last_height`,
which consensus guarantees for every real chain of spends (a claimed height is checked
against the PREVIOUS transaction block).

What this file proves, and where the rest is. The offline validator has no coin records,
so it passes any birth assert through. This file therefore proves the ARITHMETIC over
the births the chain would give: the auditors' two scenarios credit every block, and a
chain of `h = birth` claims ends with the same total as one honest observe. That a
LYING birth is refused is consensus's job: `_test_v13_consensus_timelocks.py` runs
`chia.consensus.check_time_locks` over the same bundles.
"""
import sys

sys.path.insert(0, ".")

from chia_rs.sized_bytes import bytes32

import _v13_testkit as kit
import forge_math

FAILED = 0
CATS = [bytes32(bytes([0xD0 + i]) * 32) for i in range(2)]
H = 6_999_990                      # inside (VALIDATION_HEIGHT - window, VALIDATION_HEIGHT]


def check(label, ok, detail=""):
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILED += 1


def refuses(label, thunk):
    try:
        thunk()
    except (kit.Rejected, ValueError, TypeError) as exc:
        check(label, True)
        print(f"          refused: {str(exc)[:80]}")
        return
    check(label, False, "ACCEPTED")


def observe(pool, h, birth):
    """One observe by a coin born at `birth`, claiming `h`; returns the successor pool and its oracle."""
    p = kit.replace(pool, birth=birth)
    bundle, new_state = kit.spend_action(p, "forge_action_observe", [h])
    kit.validate(bundle)
    st = kit.state_to_list(new_state)
    return p.advance(st), st[3]


def main():
    base = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x40, last_height=H - 100)
    spot0 = kit.spots(base.state[0], base.weights)

    print("the two intervals:")
    # born at H-60 (the previous spend was included there, having claimed H-100), observed at H
    _, (last, cums, last_spot) = observe(base, H, H - 60)
    check("last_height advances to h", last == H)
    check("the tail (last_height, birth] is credited at last_spot (zero at genesis) and [birth, h] at the spot",
          cums == [0 * 40 + spot0[0] * 60], f"{cums}")
    check("the spot credited is recorded for the next spend", last_spot == spot0)
    check("expected_oracle agrees", [last, cums, last_spot] == kit.expected_oracle(base.state, base.weights, H, birth=H - 60))

    print("the auditors' first scenario: honest spends in consecutive blocks:")
    # each spend claims the current height and is included in the next block; V12 credited 0 every time
    pool, total = base, 0
    h, birth = H - 60, H - 61
    for _ in range(8):
        pool, (_, cums, _) = observe(pool, h, birth)
        birth, h = h + 1, h + 1
    # the price never moved, so the accumulator must equal spot0 x (blocks from the first coin's birth to the last
    # claim): the first spend credits [H-61, H-60], each later one credits the one-block tail its predecessor left
    first_birth, last_claim = H - 61, h - 1
    check("eight honest spends credit every block from the first birth to the last claim, once",
          cums == [spot0[0] * (last_claim - first_birth)], f"cums {cums}, spot {spot0[0]}, span {last_claim - first_birth}")
    check("  (V12 credited one block for the first and zero for the other seven: h == birth every time)",
          last_claim - first_birth == 8)

    print("the auditors' second scenario: claiming h = birth every time:")
    # ten observes, each claiming exactly its birth: V12 accumulated nothing over 10 real blocks
    pool = base
    birth = H - 60
    for i in range(10):
        pool, (_, cums_claimed, _) = observe(pool, birth, birth)
        birth += 1
    # then one honest observe at H by the coin born at `birth`
    _, (_, cums_end, _) = observe(pool, H, birth)
    # the same chain observed once, honestly, at H by a coin born at H-60
    _, (_, cums_once, _) = observe(base, H, H - 60)
    check("a chain of h = birth claims credits exactly what one honest observe credits", cums_end == cums_once,
          f"{cums_end} vs {cums_once}")
    check("  nothing was deleted along the way: each claim deferred its tail to the next spend", cums_claimed != cums_end)

    print("a price move is credited from the block it took effect:")
    # swap at h = H-60 (born H-61): the pre-swap spot is credited over [H-61, H-60]; the post-swap spot
    # applies from H-60's inclusion block onward and is credited by the next spend over its tail
    pool = kit.replace(base, birth=H - 61)
    settle, settle_spend = kit.offer_settlement_xch(250_000, salt=0xC5)
    honest = forge_math.swap_output(pool.state[0][0], pool.state[0][1], 250_000, pool.fee_bps, 1, 1)
    bundle, new_state = kit.spend_action(pool, "forge_action_swap",
                                         [H - 60, 0, 1, 250_000, honest, settle.name()], extra_spends=[settle_spend])
    kit.validate(bundle)
    moved = pool.advance(kit.state_to_list(new_state))
    spot1 = kit.spots(moved.state[0], moved.weights)
    check("the swap moved the spot", spot1 != spot0)
    _, (_, cums_after, last_spot_after) = observe(moved, H, H - 59)   # included at H-59
    # spelled out: swap credited spot0 over [H-61, H-60] (1 block) plus the genesis tail of zero; observe credits
    # spot0 (the swap's recorded spot, in force until its inclusion at H-59) over (H-60, H-59] and spot1 over [H-59, H]
    expected = [spot0[0] * 1 + spot0[0] * 1 + spot1[0] * 59]
    check("the old price is credited up to the swap's inclusion block, the new price from it", cums_after == expected,
          f"{cums_after} vs {expected}")
    check("  and the new spot is what the next spend will carry", last_spot_after == spot1)

    print("bounds:")
    refuses("h below birth is refused", lambda: kit.spend_action(kit.replace(base, birth=H + 1), "forge_action_observe", [H]))
    refuses("a birth at the last claimed height is refused (a successor is born after the block its predecessor was checked against)",
            lambda: kit.spend_action(kit.replace(base, birth=H - 100), "forge_action_observe", [H]))
    refuses("a birth below the last claimed height is refused",
            lambda: kit.spend_action(kit.replace(base, birth=H - 120), "forge_action_observe", [H]))
    refuses("a state whose last_spot has the wrong length is refused",
            lambda: kit.spend_action(kit.replace(base, birth=H - 60, state=[*base.state[:3], [H - 100, [0], [0, 0]], *base.state[4:]]),
                                     "forge_action_observe", [H]))

    print()
    print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- V13 oracle checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
