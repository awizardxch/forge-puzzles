#!/usr/bin/env python3
"""A live path may not reach into a retired revision's module without saying why.

Forge has retired four revisions now, and each retirement left the same residue: a
helper that never depended on a protocol at all, sitting under the version it happened
to be written for, quietly imported by every revision since.

Two were found on 2026-09-16, both on paths a user touches:

  * `forge_v11_names.py` was the only pool-name resolver there has ever been, and all
    four index modules called it -- including V14's, the live one.
  * `v11_create_bridge.py` is the wallet side of a keyless creation, and the live V14
    creation API called it at three sites. Worse, it loaded `deploy-v11-testnet.py`
    outright for its `Wallet`, so a creation on the live protocol picked the creator's
    coins with a wallet three revisions old, and no fix made to the newer wallet ever
    reached the path a creator actually takes.

Neither was a bug anyone could see: both worked, because the helpers were genuinely
version-agnostic. That is exactly why this needs a check rather than a reading. The
rule is not "never import across revisions" -- a before/after control has to. The rule
is that doing it is a **decision**, written down here with its reason, the same way
`mutation-arguments.json` makes an UNREACHED line an argument rather than a shrug.

Exit 1 on an unargued crossing. Exit 0 when every crossing is listed below.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"

# (importing file, imported module) -> why this crossing is allowed to exist.
# A crossing not listed here fails the run. Adding a line is cheap; the point is that
# it has to be a sentence someone wrote, not something that accumulated.
LIVE_SECONDARY = ("V14 and V15 are not retired: the owner kept each live beside its successor "
                  "(2026-10-07, 2026-10-09). Their pools stay on chain undrained, their lanes ship in "
                  "every slice and are imported unconditionally; a creation takes the newest lane")

ARGUED: dict[tuple[str, str], str] = {
    ("forge_stdin.py", "forge_v15_offer"): LIVE_SECONDARY,
    ("forge_stdin.py", "forge_v15_route"): LIVE_SECONDARY,
    ("forge_stdin.py", "forge_v15_create"): LIVE_SECONDARY,
    ("forge_stdin.py", "forge_v15_driver"): LIVE_SECONDARY,
    ("forge_stdin.py", "forge_v14_offer"): LIVE_SECONDARY,
    ("forge_stdin.py", "forge_v14_route"): LIVE_SECONDARY,
    ("forge_stdin.py", "forge_v14_create"): LIVE_SECONDARY,
    ("forge_stdin.py", "forge_v14_driver"): LIVE_SECONDARY,
    ("forge_v16_route.py", "forge_v15_driver"): LIVE_SECONDARY + "; the composer spends a V15 pool on a route with its own driver",
    ("forge_v16_route.py", "forge_v14_driver"): LIVE_SECONDARY + "; the composer spends a V14 pool on a route with its own driver",
    ("_test_v16_route_lane.py", "_v14_testkit"): LIVE_SECONDARY + "; the mixed-revision route case builds a V14 pool",
    ("_test_v16_route_lane.py", "forge_v14_offer"): LIVE_SECONDARY + "; its snapshot is the V14 lane's",
    ("forge_stdin.py", "forge_v13_offer"):
        "the retired lane is optional and guarded by try/ImportError; it serves pools "
        "whose record is still read back, and its absence is reported by _lane",
    ("forge_stdin.py", "forge_v13_route"): "same optional lane",
    ("forge_stdin.py", "forge_v13_create"): "same optional lane",
    ("forge_stdin.py", "forge_v13_driver"): "same optional lane",
    ("_test_v16_chip0062_audit.py", "_v11_testkit"):
        "the CHIP-0062 audit read contracts/v11, so every finding is built against V11 "
        "first: a finding that cannot be demonstrated where the auditors found it is as "
        "much a failure as one V14 still allows",
    ("_test_v16_consensus_timelocks.py", "_v11_testkit"):
        "the before/after control: it proves V11 asserted no birth height where V16 "
        "does, which cannot be shown without building a V11 pool",
    ("_test_v16_before_after.py", "_v13_testkit"):
        "the before/after control: every V14 change, carried into V15 and V16, is run against the V13 build too",
}

IMPORT = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
VERSIONED = re.compile(r"^(?:_|forge_)?(?:.*?)v(\d+)(?:_|$)")


def module_version(name: str) -> int | None:
    """The revision a module name is filed under, or None if it names no revision."""
    m = re.search(r"(?:^|_)v(\d+)(?:_|$)", name)
    return int(m.group(1)) if m else None


def live_revision() -> int:
    """The highest revision with a puzzle tree, which is the one that ships."""
    found = [int(m.group(1)) for p in CONTRACTS.iterdir()
             if p.is_dir() and (m := re.fullmatch(r"v(\d+)", p.name))]
    if not found:
        raise SystemExit("[aWizard] no contracts/v<N> tree; cannot tell which revision is live")
    return max(found)


def main() -> int:
    live = live_revision()
    print(f"live revision: v{live}; anything older is retired\n")
    failures, crossings, unused = [], 0, set(ARGUED)

    for path in sorted(CONTRACTS.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        owner = module_version(path.stem)
        # Only LIVE and UNVERSIONED files are held to this. A retired file importing its
        # own retired siblings is just that revision, intact.
        if owner is not None and owner < live:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for imported in sorted(set(IMPORT.findall(text))):
            target = module_version(imported)
            if target is None or target >= live:
                continue
            crossings += 1
            key = (path.name, imported)
            if key in ARGUED:
                unused.discard(key)
                print(f"  [argued] {path.name} -> {imported}")
                print(f"           {ARGUED[key]}")
            else:
                failures.append(key)
                print(f"  [UNARGUED] {path.name} imports {imported}, a v{target} module")

    print(f"\n{crossings} crossing(s) from live or unversioned code into a retired revision")
    for key in sorted(unused):
        print(f"  [stale argument] {key[0]} no longer imports {key[1]}; drop the entry")
    if failures:
        print(f"\n{len(failures)} UNARGUED crossing(s).")
        print("Either the helper is version-agnostic and belongs under an unversioned name")
        print("(forge_names.py, forge_create_bridge.py were both fixed that way), or the")
        print("crossing is deliberate and belongs in ARGUED with the reason.")
        return 1
    print("\nevery crossing is argued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
