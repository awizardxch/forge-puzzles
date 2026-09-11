#!/usr/bin/env python3
"""A reserve can only be moved by the pool that owns it (V10).

Through V9 a reserve was not bound to its pool at all: `launcher_id` arrived in
the solution and was never used, and the sole authorization was a COIN
announcement keyed by a `current_pool_coin_id` the spender also supplied. Any
coin the attacker owned satisfied it, so anyone could drain any pool. See
_poc_reserve_unbound.py for that attack against the old puzzle.

V10 makes three changes, and this checks each one actually holds -- driving the
real compiled puzzle with the attacker's own solution rather than reasoning about
it, because a spend that merely fails on arity would look like a fix without
being one.
"""
import hashlib
import sys

sys.path.insert(0, ".")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.puzzles.singleton_top_layer_v1_1 import (
    SINGLETON_LAUNCHER_HASH, SINGLETON_TOP_LAYER_V1_1_HASH, puzzle_for_singleton,
)
from chia.wallet.trading.offer import OFFER_MOD
from chia_rs.sized_bytes import bytes32

from forge_offer import ZERO_32, compiled_program, reserve_inner_puzzle, reserve_solution

VICTIM_LAUNCHER = bytes32(b"\x10" * 32)
ATTACKER_LAUNCHER = bytes32(b"\xAA" * 32)
POOL_INNER = bytes32(b"\x20" * 32)
ASSERT_MY_PUZZLE_HASH, ASSERT_PUZZLE_ANNOUNCEMENT, ASSERT_COIN_ANNOUNCEMENT = 72, 63, 61


def amt(n: int) -> bytes:
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 8) // 8, "big")


def coin_id(parent: bytes32, ph: bytes32, a: int) -> bytes32:
    return bytes32(hashlib.sha256(bytes(parent) + bytes(ph) + amt(a)).digest())


def singleton_full_hash(launcher: bytes32, inner_hash: bytes32) -> bytes32:
    """The pool coin's puzzle hash, the way the reserve now rebuilds it."""
    return puzzle_for_singleton(launcher, Program.to(inner_hash)).get_tree_hash_precalc(inner_hash)


def run_reserve(launcher_curried, *, asset_id=ZERO_32, reserve_hash=None,
                pool_inner=POOL_INNER, pool_coin=bytes32(b"\xBB" * 32),
                successor_ph=None):
    inner = reserve_inner_puzzle(10, launcher_curried)
    real_hash = inner.get_tree_hash()
    reserve_hash = real_hash if reserve_hash is None else reserve_hash
    successor_ph = reserve_hash if successor_ph is None else successor_ph

    victim_coin = bytes32(b"\x77" * 32)
    current, successor = 10_000_000_000_000, 1
    released = current - successor
    settle_ph = bytes32(OFFER_MOD.get_tree_hash())

    plan = [0, pool_coin, asset_id, victim_coin, current,
            coin_id(victim_coin, settle_ph, released),
            coin_id(victim_coin, successor_ph, successor), successor, 0, ZERO_32]
    solution = reserve_solution(10, launcher_curried, asset_id, reserve_hash, pool_inner, plan)
    return inner.run(solution), real_hash


def conditions(out):
    found = {}
    for cond in out.as_iter():
        items = list(cond.as_iter())
        op = items[0].as_int()
        found.setdefault(op, []).append(items)
    return found


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    results = []

    print("the reserve is now pool-specific:")
    victim_inner = reserve_inner_puzzle(10, VICTIM_LAUNCHER)
    attacker_inner = reserve_inner_puzzle(10, ATTACKER_LAUNCHER)
    results.append(check("a reserve's puzzle hash commits to its launcher",
                         victim_inner.get_tree_hash() != attacker_inner.get_tree_hash(),
                         f"{victim_inner.get_tree_hash().hex()[:16]}… vs {attacker_inner.get_tree_hash().hex()[:16]}…"))

    print()
    print("the authorization is bound to the real pool singleton:")
    # The attacker MUST curry the victim's launcher -- otherwise the puzzle does
    # not hash to the victim reserve coin they are trying to spend.
    out, real_hash = run_reserve(VICTIM_LAUNCHER)
    conds = conditions(out)

    results.append(check("it no longer asserts a forgeable COIN announcement",
                         ASSERT_COIN_ANNOUNCEMENT not in conds))
    puzzle_asserts = [bytes(i[1].as_atom()) for i in conds.get(ASSERT_PUZZLE_ANNOUNCEMENT, [])]
    results.append(check("it asserts a PUZZLE announcement instead", bool(puzzle_asserts)))

    # That announcement must come from a coin carrying the owning pool's
    # singleton puzzle hash, which only genuine lineage back to that launcher can
    # produce. Re-running with a different curried launcher must therefore change
    # every assertion: the key is the pool's identity, not the spender's choice.
    out2, _ = run_reserve(ATTACKER_LAUNCHER)
    attacker_asserts = [bytes(i[1].as_atom()) for i in conditions(out2).get(ASSERT_PUZZLE_ANNOUNCEMENT, [])]
    results.append(check("the assertion changes with the curried launcher",
                         puzzle_asserts != attacker_asserts,
                         "so it is keyed to the owning pool, not the spender"))

    print()
    print("the successor cannot be redirected:")
    pinned = [bytes(i[1].as_atom()) for i in conds.get(ASSERT_MY_PUZZLE_HASH, [])]
    results.append(check("AssertMyPuzzleHash pins the reserve puzzle",
                         bool(pinned) and pinned[0] == bytes(real_hash),
                         pinned[0].hex()[:16] + "…" if pinned else "absent"))

    # The old attack: claim a successor at a puzzle the attacker controls. The
    # plan's successor id is derived from `reserve_hash`, so lying about it makes
    # AssertMyPuzzleHash name a hash this coin does not have, and the spend dies
    # at consensus rather than paying out.
    attacker_ph = bytes32(b"\xCC" * 32)
    out3, real3 = run_reserve(VICTIM_LAUNCHER, reserve_hash=attacker_ph, successor_ph=attacker_ph)
    pinned3 = [bytes(i[1].as_atom()) for i in conditions(out3).get(ASSERT_MY_PUZZLE_HASH, [])]
    results.append(check("redirecting the successor self-destructs the spend",
                         bool(pinned3) and pinned3[0] != bytes(real3),
                         "asserts a puzzle hash the coin does not have"))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} reserve-binding checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
