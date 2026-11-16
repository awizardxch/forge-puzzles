#!/usr/bin/env python3
"""The resync replay reads back a spend exactly, including one that ran a leaf twice.

`forge_v16_resync.replay_spend` rebuilds a pool's state from an on-chain spend, and the
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
  * a selector that is a program, a nested `puzzles`, and quoted selectors over an
    empty `puzzles` all replay to the successor the chain created; a selector that
    resolves to nothing this pool commits to is refused with a typed error

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia_rs.sized_bytes import bytes32

import _v16_testkit as kit
import forge_math
import forge_v16_resync as rs

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
    if not kit.v16_available():
        print("  [skip] V16 build outputs are absent; run scripts/build-v16.py")
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

    # the puzzle RUNS a selector; nothing makes it a path, or
    # `puzzles` a flat list. Each spend below is first accepted by the consensus validator.
    print("selectors the chain accepts and the driver never writes:")
    leaf = pool.leaves["forge_action_observe"]
    proof = pool.leaf_proof("forge_action_observe")
    obs_state, obs_tagged, _base, _eph = kit.run_leaf(pool, "forge_action_observe", [H0])
    obs_solution = kit.with_birth(pool, [H0])
    honest_successor = pool.advance(kit.state_to_list(obs_state)).coin.name()

    def with_inner(bundle, inner_solution):
        """`bundle` with the pool singleton's inner solution replaced."""
        spends = []
        for cs in bundle.coin_spends:
            if cs.coin.name() == pool.coin.name():
                cs = kit.make_spend(pool.coin, kit.puzzle_for_singleton(pool.launcher_id, pool.inner),
                                    kit.solution_for_singleton(pool.lineage, kit.uint64(1), inner_solution))
            spends.append(cs)
        return kit.SpendBundle(spends, kit.G2Element())

    quoted = kit.assemble(pool, leaf, proof, obs_solution, obs_state, obs_tagged, selector=Program.to((1, leaf)))
    kit.validate(quoted)
    replayed, names = rs.replay_spend(pool, singleton_solution(quoted, pool))
    check("a selector that is the program (q . leaf) replays", names == ["forge_action_observe"]
          and replayed.coin.name() == honest_successor, str(names))

    plain = kit.assemble(pool, leaf, proof, obs_solution, obs_state, obs_tagged)
    deep = with_inner(plain, Program.to([[[leaf]], [[4, *proof]], [obs_solution]]))
    kit.validate(deep)
    replayed, names = rs.replay_spend(pool, singleton_solution(deep, pool))
    check("`puzzles` = ((leaf)) with selector 4 replays", names == ["forge_action_observe"]
          and replayed.coin.name() == honest_successor, str(names))

    steps = [("forge_action_swap", [H0, 0, 1, g1, out1, *kit.settlement_ref(s1)]),
             ("forge_action_collect", [H0, [1]])]
    bundle, built_state = kit.spend_actions(pool, steps, extra_spends=[s1_spend])
    inner = list(list(singleton_solution(bundle, pool).as_iter())[2].as_iter())
    order = [pool.leaves[name] for name, _ in reversed(steps)]      # selectors are in reverse execution order
    entries = [Program.to((Program.to((1, lf)), entry.rest())) for lf, entry in zip(order, inner[1].as_iter())]
    mixed = with_inner(bundle, Program.to([0, entries, inner[2]]))  # no `puzzles` at all: every selector is quoted
    kit.validate(mixed)
    replayed, names = rs.replay_spend(pool, singleton_solution(mixed, pool))
    check("two actions, both selectors quoted and `puzzles` empty, replay in execution order",
          names == ["forge_action_swap", "forge_action_collect"]
          and replayed.coin.name() == pool.advance(kit.state_to_list(built_state)).coin.name(), str(names))

    for label, selector in (("a selector that returns a program this pool does not commit to", Program.to((1, Program.to(1)))),
                            ("a selector that cannot run", Program.to([8]))):
        bad = with_inner(plain, Program.to([[leaf], [[selector, *proof]], [obs_solution]]))
        try:
            rs.replay_spend(pool, singleton_solution(bad, pool))
            check(f"{label} is refused", False, "replayed anyway")
        except rs.ResyncError as exc:
            check(f"{label} is refused with a typed error", exc.code == "UNKNOWN_LEAF", exc.code)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} replay checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
