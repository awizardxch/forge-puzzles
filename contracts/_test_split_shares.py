#!/usr/bin/env python3
"""Equal split shares come out distinct (2026-09-26 external review, finding 13).

Two children of one hub coin with the same amount are the same coin, so the
composer nudges equal shares apart. The single-pass version re-collided at
four or more equal shares and refused the route after the trader had signed;
two 1-mojo shares skipped the guard and collided outright. The helper now
converges, holds the total, and refuses instead of colliding.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from forge_v15_offer import OfferRejected  # noqa: E402
from forge_v15_route import nudge_equal_shares  # noqa: E402

FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        FAILED += 1


def distinct_and_conserved(shares: list[int]) -> tuple[bool, list[int]]:
    out = nudge_equal_shares(shares)
    return len(set(out)) == len(out) and sum(out) == sum(shares) and all(g > 0 for g in out), out


print("the review's worked example and its neighbours:")
x = 2_425_000_000
for n in (2, 3, 4, 5, 6, 8, 12):
    ok, out = distinct_and_conserved([x] * n)
    check(f"{n} equal shares of {x} come out distinct with the total held", ok, str(out[:6]))

print("\nshapes that used to slip through:")
try:
    nudge_equal_shares([1, 1])
    check("two 1-mojo shares are refused, not collided", False, "no refusal")
except OfferRejected as exc:
    check("two 1-mojo shares are refused, not collided", "cannot be told apart" in str(exc), str(exc))
ok, out = distinct_and_conserved([5, 5, 5, 5])
check("four small equal shares converge", ok, str(out))
ok, out = distinct_and_conserved([3, 2, 2, 3])
check("interleaved duplicates converge", ok, str(out))
ok, out = distinct_and_conserved([7, 3, 9])
check("already-distinct shares are untouched", ok and out == [7, 3, 9], str(out))

print("\nrandom shapes:")
rng = random.Random(2026_09_26)
bad = 0
refused_feasible = []
for _ in range(2_000):
    n = rng.randint(2, 9)
    base = rng.choice([1, 2, 3, 10, 999, 10**9])
    shares = [base + rng.choice([0, 0, 0, 1, 2]) for _ in range(n)]
    # n distinct positive integers sum to at least 1+2+...+n; below that no
    # assignment exists and a refusal is the only honest answer.
    feasible = sum(shares) >= n * (n + 1) // 2
    try:
        ok, _ = distinct_and_conserved(shares)
        if not ok:
            bad += 1
    except OfferRejected:
        if feasible:
            bad += 1
            refused_feasible.append(shares)
check("2,000 random duplicate-heavy shapes are distinct, conserved and positive, or refused only when no such assignment exists",
      bad == 0, f"{bad} bad; refused-but-feasible: {refused_feasible[:3]}")

print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    raise SystemExit(1)
print("ALL PASSED -- equal split shares are told apart")
