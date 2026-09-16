#!/usr/bin/env python3
"""The resync replay reads back a spend exactly, including one that ran a leaf twice.

`forge_v14_resync.replay_spend` rebuilds a pool's state from an on-chain spend, and the
browser's repair path depends on it: a pool behind the chain is resynced rather than
refused. It read the action layer's solution by zipping `puzzles` against `solutions` --
but `puzzles` lists each DISTINCT leaf once while `solutions` has one entry per action, so
a spend that ran `swap` twice replayed as ONE swap and produced a state that never
existed. Found on testnet11 on 2026-09-16, when the double-settlement spend on H6
(4,693,721) read back as a single swap; the fix resolves each action's leaf through its
selector instead.

What this pins:

  * a two-action spend of DIFFERENT leaves replays as both, in order
  * a two-action spend of the SAME leaf twice replays as both, not one
  * three actions with a repeat in the middle keep their execution order
  * the replayed state equals the state the driver built the spend from
  * a solution whose selector count disagrees with its solutions is refused

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia_rs.sized_bytes import bytes32

import _v14_testkit as kit
import forge_math
import forge_v14_resync as rs

results = []
CAT = bytes32(b"\xd0" * 32)
H0 = 6_999_990


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def singleton_solution(bundle, pool):
    """The pool singleton's solution out of an assembled bundle."""
    for cs in bundle.coin_spends:
        if cs.coin.name() == pool.coin.name():
            return Program.from_bytes(bytes(cs.solution))
    raise AssertionError("the bundle does not spend the pool coin")


def main() -> int:
    if not kit.v14_available():
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py")
        return 2
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x42)
    r, w = pool.state[0], pool.weights
    g1, g2 = 250_000, 100_000
    out1 = forge_math.swap_output(r[0], r[1], g1, pool.fee_bps, w[0], w[1])
    after = [r[0] + g1, r[1] - out1]
    out2 = forge_math.swap_output(after[0], after[1], g2, pool.fee_bps, w[0], w[1])
    s1, s1_spend = kit.offer_settlement_xch(g1, salt=0xE1)
    s2, s2_spend = kit.offer_settlement_xch(g2, salt=0xE2)

    print("one leaf, twice -- the case that read back wrong:")
    steps = [("forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)]),
             ("forge_action_swap", [H0, 0, 1, g2, out2, *kit.settlement_ref(s2)])]
    bundle, built_state = kit.spend_actions(pool, steps, extra_spends=[s1_spend, s2_spend])
    kit.validate(bundle)
    replayed, names = rs.replay_spend(pool, singleton_solution(bundle, pool))
    check("both swaps are reported, not one", names == ["forge_action_swap", "forge_action_swap"], str(names))
    built = kit.state_to_list(built_state)
    check("the replayed reserves equal the ones the driver built", replayed.state[0] == built[0],
          f"{replayed.state[0]} vs {built[0]}")
    check("  which is reserve 0 grown by BOTH inputs", replayed.state[0][0] == r[0] + g1 + g2,
          f"{replayed.state[0][0]} = {r[0]:,} + {g1:,} + {g2:,}")
    check("the replayed successor is the coin the spend creates", replayed.coin.name() == pool.advance(built).coin.name())

    print("two different leaves, in order:")
    steps = [("forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)]),
             ("forge_action_collect", [H0, [1]])]
    bundle, built_state = kit.spend_actions(pool, steps, extra_spends=[s1_spend])
    kit.validate(bundle)
    replayed, names = rs.replay_spend(pool, singleton_solution(bundle, pool))
    check("both leaves are reported in execution order", names == ["forge_action_swap", "forge_action_collect"], str(names))
    check("  and the state matches the driver's", replayed.state[0] == kit.state_to_list(built_state)[0])

    print("three actions with a repeat in the middle:")
    st1, _, _, eph1 = kit.run_leaf(pool, "forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)])
    steps = [("forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)]),
             ("forge_action_observe", [H0]),
             ("forge_action_swap", [H0, 0, 1, g2, out2, *kit.settlement_ref(s2)])]
    bundle, built_state = kit.spend_actions(pool, steps, extra_spends=[s1_spend, s2_spend])
    kit.validate(bundle)
    replayed, names = rs.replay_spend(pool, singleton_solution(bundle, pool))
    check("the order is swap, observe, swap",
          names == ["forge_action_swap", "forge_action_observe", "forge_action_swap"], str(names))
    check("  and the state matches the driver's", replayed.state[0] == kit.state_to_list(built_state)[0],
          f"{replayed.state[0]} vs {kit.state_to_list(built_state)[0]}")

    print("a malformed solution is refused rather than half-replayed:")
    bundle, _ = kit.spend_actions(pool, [("forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)]),
                                         ("forge_action_swap", [H0, 0, 1, g2, out2, *kit.settlement_ref(s2)])],
                                  extra_spends=[s1_spend, s2_spend])
    sol = singleton_solution(bundle, pool)
    parts = list(sol.as_iter())
    inner = list(parts[2].as_iter())
    truncated = Program.to([parts[0], parts[1], Program.to([inner[0], [list(inner[1].as_iter())[0]], inner[2]])])
    try:
        rs.replay_spend(pool, truncated)
        check("a selector count that disagrees with the solutions is refused", False, "replayed anyway")
    except rs.ResyncError as exc:
        check("a selector count that disagrees with the solutions is refused", True, str(exc)[:60])

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} replay checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
