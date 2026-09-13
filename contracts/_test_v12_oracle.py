#!/usr/bin/env python3
"""V12 oracle: elapsed time binds to the pool coin's birth height (CHIP-0062 review P1).

The V11 hole: the prologue accepted any `h` inside the window and accumulated
price x (h - last_height), both solver-chosen. Spend generation N at h = H - 31 with
a price-moving swap, then the ephemeral successor at h = H in the SAME bundle: 31
blocks of the manipulated price, no block elapsed. CNI reproduced it with an
accepted bundle.

V12 measures from `birth`, which the pool coin asserts with ASSERT_MY_BIRTH_HEIGHT:
elapsed = h - birth. A solver can understate h (by at most the window) and so
understate elapsed; nothing can be fabricated, because birth is a consensus fact.

What this file proves, and where the rest is. The offline validator
(chia_rs.get_conditions_from_spendbundle) has no coin records, so it passes any
birth assert through. This file therefore proves the ARITHMETIC: with the birth a
same-block successor really has (the block it was created in), the successor
accumulates nothing, and a claimed h below birth is refused.

That a LYING birth is refused is consensus's job, and consensus decides it in a
pure function -- `chia.consensus.check_time_locks`, the one the mempool itself
calls. `_test_v12_consensus_timelocks.py` runs it over these same bundles with
the coin records the chain would have, so the lying-birth half is proved offline
too, with the node's own error codes; `scripts/live_v12_probe.py` then confirms
it against a real node. That suite also shows the stronger fact this one only
approximates: a V12 pool coin cannot be spent in the bundle that created it at
all, because consensus forbids a birth condition on an ephemeral coin.
"""
import sys

sys.path.insert(0, ".")

from chia_rs.sized_bytes import bytes32

import _v12_testkit as kit
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
        check(label, True); print(f"          refused: {str(exc)[:80]}"); return
    check(label, False, "ACCEPTED")


def cums_after(pool, h, birth):
    p = kit.replace(pool, birth=birth)
    bundle, new_state = kit.spend_action(p, "forge_action_observe", [h])
    kit.validate(bundle)
    return kit.state_to_list(new_state)[3]          # [last_height, cums]


def main():
    # born at H-31, observed at H: 31 blocks accumulate at the pre-spend price
    pool = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x40, last_height=H - 40)
    print("elapsed = h - birth:")
    last, cums = cums_after(pool, H, H - 31)
    price = forge_math.oracle_price(pool.state[0], pool.weights, pool.price_scale) if hasattr(forge_math, "oracle_price") else None
    expected = kit.expected_cums(pool.state, pool.weights, H - 31 + 0)  # helper accumulates from last_height; recompute below
    check("last_height advances to h", last == H)
    # accumulate() adds price*elapsed; compare against elapsed=31 vs the kit's own helper for elapsed=31
    ref = kit.expected_cums(kit.forge_state(pool.state[0], pool.state[1], last_height=H - 31), pool.weights, H)
    check("31 blocks accumulate exactly as if the previous height were the birth", cums == ref, f"{cums} vs {ref}")

    print("a same-block successor accumulates nothing:")
    # generation 1 at h = H-31 (its birth earlier), then its successor -- born in THIS block (H) -- at h = H
    gen1 = kit.replace(pool, birth=H - 40)
    b1, s1 = kit.spend_action(gen1, "forge_action_observe", [H - 31])
    kit.validate(b1)
    gen2 = gen1.advance(kit.state_to_list(s1))
    gen2 = kit.replace(gen2, birth=H)                       # the truth: created in block H
    b2, s2 = kit.spend_action(gen2, "forge_action_observe", [H])
    kit.validate(b2)
    check("successor born at H, spent at h = H, adds zero to the accumulator",
          kit.state_to_list(s2)[3][1] == kit.state_to_list(s1)[3][1])
    check("  ...while its last_height still advances (timestamps stay monotone)", kit.state_to_list(s2)[3][0] == H)

    print("what the reviewer's bundle now needs, and cannot have:")
    # To backfill 31 blocks the successor must CLAIM birth = H-31. Offline this builds (the
    # validator cannot see coin records); the node refuses it with ASSERT_MY_BIRTH_HEIGHT_FAILED.
    lying = kit.replace(gen2, birth=H - 31)
    b3, s3 = kit.spend_action(lying, "forge_action_observe", [H])
    check("the backfill requires asserting a false birth height (refused in _test_v12_consensus_timelocks.py)",
          kit.state_to_list(s3)[3][1] != kit.state_to_list(s1)[3][1])

    print("bounds:")
    refuses("h below birth is refused", lambda: kit.spend_action(kit.replace(pool, birth=H + 1), "forge_action_observe", [H]))
    refuses("h not above last_height is refused (kept from V11)",
            lambda: kit.spend_action(kit.replace(pool, birth=H - 50), "forge_action_observe", [H - 40]))

    print()
    print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- V12 oracle checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
