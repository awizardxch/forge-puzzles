#!/usr/bin/env python3
"""Mutation testing for the V14 leaves: which assertions are actually load-bearing?

A refusal test proves that a bad solution is refused. It does not prove WHICH
line refused it. Those are different facts, and the difference is not academic:
on 2026-09-10, eight fresh sign-refusal cases were written for the swap leaf and
all eight passed -- and then passed again against a leaf recompiled with both
`assert gross_input > 0` and `assert claimed_output > 0` deleted. The curve was
doing the work; the assertions were decoration. Forge was not vulnerable either
way, but "not vulnerable" and "defended by the line I think defends it" are not
the same claim, and only one of them survives a refactor.

So this deletes each assertion in turn, rebuilds, and runs the suite against the
result:

  KILLED     the suite fails -> something tests that line. Good.
  UNREACHED  the suite passes -> no probe reaches the line. It never means the line
             is redundant (fourth review, R-2: a "survived" verdict was read as
             "redundant" and the line was load-bearing). Every UNREACHED line needs
             a bracket-level probe or a written argument in
             contracts/v14/mutation-arguments.json, and the run FAILS if any line
             is both UNREACHED and unargued.
  UNBUILDABLE the mutant does not compile -> the line is structural, not a
             check; not interesting, and not a gap.

An unreached mutant is a question with two honest answers -- another check
covers it (write the argument down), or nothing does (write the probe) -- and
one dishonest one: "it must be redundant". The rule makes the third answer fail.

    python scripts/mutate-v14.py                      # every Forge leaf
    python scripts/mutate-v14.py --leaf forge_action_swap
    python scripts/mutate-v14.py --suite _test_v14_manipulation.py

Needs `rue` on PATH. Nothing is ever written into contracts/v14/compiled: each
mutant is built in a temporary copy and the suite is pointed at it with
FORGE_V14_COMPILED.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
# --project picks the rue project. V14 is the shipping revision and the default;
# v11 stays selectable while contracts/v11 is still in the tree.
PROJECT = "v14"
PROJECT_DIR = ROOT / "contracts" / PROJECT
ARGUMENTS = PROJECT_DIR / "mutation-arguments.json"
PUZZLES = PROJECT_DIR / "puzzles"
CONTRACTS = ROOT / "contracts"

# Forge's own leaves. Upstream puzzles are vendored verbatim and pinned by hash;
# mutating them would only prove that the pins work.
DEFAULT_LEAVES = [
    "forge_action_swap.rue",
    "forge_action_add.rue",
    "forge_action_remove.rue",
    "forge_action_collect.rue",
    "forge_action_observe.rue",
    "forge_action_dao_fee.rue",
    "forge_action_common.rue",
    # V14: the registry's admission rule and the reserve launcher are load-bearing too
    "forge_registry_register.rue",
    "forge_registry_common.rue",
    "forge_reserve_launcher.rue",
]

ASSERT = re.compile(r"^\s*assert\s+.*;\s*$")

# A mutant is killed if ANY of these fails. Running one suite answers a narrower
# question than it looks: the DAO fee's monotonic-decrease assertion survives the
# actions suite and is killed by its own, so a single-suite run would have filed
# a real guarantee as unpinned.
DEFAULT_SUITES = [
    "_test_v14_actions.py",
    "_test_v14_dao_fee.py",
    "_test_v14_registry.py",
    # The only suite that reaches the TAIL's two melt-side locks, the CAT-parent lock
    # (`parent_is_cat || expected_delta > 0`) and the delta lock
    # (`effective_delta == expected_delta`): without it a TAIL run reports both as survivors.
    "_test_v14_lp_receive_forgery.py",
]


def assertions(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every single-line `assert ...;` in a file, as (line number, text)."""
    found = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if ASSERT.match(line):
            found.append((number, line.strip()))
    return found


def build(directory: pathlib.Path) -> tuple[bool, str]:
    try:
        out = subprocess.run(["rue", "build", "--hex", "--hash", "--all", "."],
                             cwd=directory, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        print("  rue is not on PATH. Install it or add ~/.cargo/bin.", file=sys.stderr)
        raise SystemExit(2)
    return out.returncode == 0, (out.stderr or out.stdout)[-400:]


def run_suite(suite: str, compiled: pathlib.Path) -> bool:
    """True when the suite PASSES against this build."""
    env = {**os.environ, f"FORGE_{PROJECT.upper()}_COMPILED": str(compiled), "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, suite], cwd=CONTRACTS, env=env,
                         capture_output=True, text=True, timeout=1800)
    return out.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", choices=("v14", "v13", "v11"), default="v14", help="which rue project to mutate")
    parser.add_argument("--leaf", action="append", help="file name under contracts/<project>/puzzles; repeatable")
    parser.add_argument("--suite", action="append",
                        help="suite to run against each mutant; repeatable. A mutant is "
                             "killed if any of them fails. Defaults to the V11 set.")
    parser.add_argument("--list", action="store_true", help="list the assertions and stop")
    args = parser.parse_args()
    global PROJECT, PROJECT_DIR, PUZZLES, DEFAULT_SUITES
    PROJECT = args.project
    V11 = ROOT / "contracts" / PROJECT
    PUZZLES = V11 / "puzzles"
    DEFAULT_SUITES = [x.replace("_v14_", f"_{PROJECT}_") for x in DEFAULT_SUITES]
    if PROJECT in ("v13", "v14"):
        DEFAULT_SUITES += [f"_test_{PROJECT}_genesis.py", f"_test_{PROJECT}_oracle.py", f"_test_{PROJECT}_finalizer.py"]
    if PROJECT == "v14":
        DEFAULT_SUITES += ["_test_v14_second_review.py", "_test_v14_reserves_proved.py",
                           "_test_v14_settlement_amount.py", "_test_v14_action_binding.py"]

    leaves = args.leaf or DEFAULT_LEAVES
    suites = args.suite or DEFAULT_SUITES
    targets: list[tuple[pathlib.Path, int, str]] = []
    for name in leaves:
        path = PUZZLES / name
        if not path.exists():
            print(f"  ! no such leaf: {name}")
            continue
        for number, text in assertions(path):
            targets.append((path, number, text))

    if not targets:
        print("No assertions found.")
        return 1

    print(f"{len(targets)} assertions across {len(leaves)} files")
    print("suites: " + ", ".join(suites))
    print()
    if args.list:
        for path, number, text in targets:
            print(f"  {path.name}:{number + 1}  {text}")
        return 0

    # The baseline has to pass, or every mutant "kills" and the run means nothing.
    print("baseline: ", end="", flush=True)
    for suite in suites:
        if not run_suite(suite, V11 / "compiled"):
            print(suite + " FAILING -- fix it before mutating, or results are noise.")
            return 1
    print("passes\n")

    survived, killed, unbuildable = [], 0, 0
    for path, number, text in targets:
        label = f"{path.name}:{number + 1}"
        print(f"  {label:<44} {text[:44]:<46}", end="", flush=True)
        with tempfile.TemporaryDirectory(prefix="forge-mutant-") as tmp:
            work = pathlib.Path(tmp) / PROJECT
            shutil.copytree(PROJECT_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
            target = work / "puzzles" / path.name
            lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
            del lines[number]
            target.write_text("".join(lines), encoding="utf-8")

            ok, detail = build(work)
            if not ok:
                unbuildable += 1
                print("unbuildable")
                continue
            # Killed by the first suite that notices; the rest need not run.
            killer = next((suite for suite in suites
                           if not run_suite(suite, work / "compiled")), None)
            if killer is None:
                survived.append((label, text))
                print("UNREACHED")
            else:
                killed += 1
                print("killed (" + killer.replace("_test_v11_", "").replace(".py", "") + ")")

    total = killed + len(survived) + unbuildable
    print(f"\n{killed}/{total} killed, {len(survived)} unreached, {unbuildable} unbuildable")

    if survived:
        import json
        arguments = json.loads(ARGUMENTS.read_text(encoding="utf-8")) if ARGUMENTS.is_file() else {}
        unargued = []
        print("\nUnreached -- deleting these changed nothing the suites could see:\n")
        for label, text in survived:
            argument = arguments.get(text) or arguments.get(label)
            if argument:
                print(f"  {label}\n    {text}\n    argued: {argument}")
            else:
                unargued.append((label, text))
                print(f"  {label}\n    {text}\n    UNARGUED")
        if unargued:
            print(f"\n{len(unargued)} unreached line(s) with no probe and no argument. Either write a")
            print("bracket-level probe that reaches the line, or record why another check covers it in")
            print(f"{ARGUMENTS.relative_to(ROOT).as_posix()} under the assertion's text. \"Survived\" is not \"redundant\".")
            return 1
        print("\nEvery unreached line carries a written argument.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
