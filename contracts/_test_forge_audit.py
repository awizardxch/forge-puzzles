#!/usr/bin/env python3
"""Adversarial probes against the shipping (FORGE) puzzles.

Each case is an attack someone would actually try, run against the real compiled
puzzle. They are grouped by the layer that is supposed to stop them, so a
regression tells you which guarantee broke rather than just that something did.

The LP-authorization bug that produced V9 is covered by _test_forge_lp_binding and
_test_forge_exploit_closed; this file covers the layer *underneath* that binding.

The one that matters most here: binding the LP action coin to a pinned melt
puzzle is only worth anything if that coin cannot be conjured. A CAT coin with no
valid lineage can still be spent when the TAIL authorizes it, so an attacker can
CREATE_COIN a coin at the melt puzzle hash out of thin air and try to melt it --
destroying LP that was never real. The TAIL's parent_is_cat branch is what stops
that, and nothing else does, so it is tested directly.
"""
import sys

sys.path.insert(0, ".")

from chia.types.blockchain_format.program import Program
from chia_rs.sized_bytes import bytes32

import forge_puzzles
from forge_offer import compiled_program

LAUNCHER = bytes32(b"\x11" * 32)
POOL_COIN = bytes32(b"\x22" * 32)
LP_COIN = bytes32(b"\x33" * 32)
PARENT = bytes32(b"\x44" * 32)
FULL_PH = bytes32(b"\x55" * 32)
INNER_PH = bytes32(b"\x66" * 32)
STATE_ROOT = bytes32(b"\x77" * 32)
FORGE_VERSION = forge_puzzles.FORGE_VERSION


def truths(my_id: bytes32, amount: int) -> Program:
    """CAT2 Truths: ((inner_ph . cat_struct) . (my_id . (parent full_ph amount)))."""
    return Program.to(((INNER_PH, (LAUNCHER, 0)), (my_id, [PARENT, FULL_PH, amount])))


def run_tail(*, amount, parent_is_cat, delta, expected_delta,
             my_id=LP_COIN, action_coin=LP_COIN, pool_coin=POOL_COIN,
             new_total_lp=1000, launcher=LAUNCHER, version=FORGE_VERSION):
    tail = compiled_program("forge_lp_cat_tail_FORGE").curry(launcher, version)
    action = [pool_coin, action_coin, expected_delta, new_total_lp, STATE_ROOT]
    solution = Program.to([truths(my_id, amount), parent_is_cat, 0, delta, 0, action])
    return tail.run(solution)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def refuses(label, **kwargs):
    try:
        run_tail(**kwargs)
        return check(label, False, "the TAIL ACCEPTED it")
    except Exception:
        return check(label, True)


def accepts(label, **kwargs):
    try:
        run_tail(**kwargs)
        return check(label, True)
    except Exception as exc:
        return check(label, False, f"{type(exc).__name__}: {str(exc)[:60]}")


def main() -> int:
    results = []
    burn = 500

    print("LP TAIL -- a burn must come from real supply:")
    # The honest burn: the melt coin's parent IS a CAT (the LP settlement it was
    # split from), so the ring's delta is the whole story.
    results.append(accepts("a genuine burn (CAT parent, delta = -burn) is authorized",
                           amount=burn, parent_is_cat=1, delta=-burn, expected_delta=-burn))

    # The attack this branch exists to stop. An attacker CREATE_COINs a coin at
    # the melt puzzle hash from an ordinary coin they own -- no lineage, so the
    # amount was never real LP -- and melts it. Melting all of a coin means the
    # ring reports delta = -amount, and for a non-CAT parent the TAIL scores that
    # as amount + delta = 0, which can never equal the -burn the pool demanded.
    results.append(refuses("a FABRICATED melt coin (no CAT parent) is refused",
                           amount=burn, parent_is_cat=0, delta=-burn, expected_delta=-burn))
    # ...and it cannot be rescued by over-claiming the melt either.
    results.append(refuses("over-claiming the melt on a fabricated coin is refused",
                           amount=burn, parent_is_cat=0, delta=-2 * burn, expected_delta=-burn))

    print()
    print("LP TAIL -- the pool's authorization cannot be reinterpreted:")
    results.append(refuses("a delta the pool did not authorize is refused",
                           amount=burn, parent_is_cat=1, delta=-burn, expected_delta=-burn + 1))
    results.append(refuses("naming a different LP action coin is refused",
                           amount=burn, parent_is_cat=1, delta=-burn, expected_delta=-burn,
                           action_coin=bytes32(b"\x99" * 32)))
    results.append(refuses("a zero delta (no supply change) is refused",
                           amount=burn, parent_is_cat=1, delta=0, expected_delta=0))
    results.append(refuses("a zero-amount action coin is refused",
                           amount=0, parent_is_cat=1, delta=-burn, expected_delta=-burn))
    results.append(refuses("a zero launcher id is refused",
                           amount=burn, parent_is_cat=1, delta=-burn, expected_delta=-burn,
                           launcher=bytes32(b"\x00" * 32)))
    results.append(refuses("a zero pool coin id is refused",
                           amount=burn, parent_is_cat=1, delta=-burn, expected_delta=-burn,
                           pool_coin=bytes32(b"\x00" * 32)))
    results.append(refuses("a negative total supply is refused",
                           amount=burn, parent_is_cat=1, delta=-burn, expected_delta=-burn,
                           new_total_lp=-1))
    results.append(refuses("a TAIL curried for another protocol version is refused",
                           amount=burn, parent_is_cat=1, delta=-burn, expected_delta=-burn,
                           version=8))

    print()
    print("LP TAIL -- a mint is authorized the same way:")
    mint = 250
    # A genesis eve carries one mojo and the TAIL authorizes the rest, so the
    # non-CAT-parent branch scores amount + delta -- here 1 + 249.
    results.append(accepts("a genuine genesis mint is authorized",
                           amount=1, parent_is_cat=0, delta=mint - 1, expected_delta=mint))
    results.append(refuses("a mint larger than the pool authorized is refused",
                           amount=1, parent_is_cat=0, delta=mint, expected_delta=mint))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} FORGE audit probes passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
