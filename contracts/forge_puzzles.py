#!/usr/bin/env python3
"""Where compiled Forge puzzles live, and which of them is the mainnet one.

Two things are resolved here so nothing else has to know about either:

**The FORGE alias.** The puzzles we intend to ship carry no version in their
filename -- they are `pool_singleton_FORGE`, `forge_reserve_FORGE` and so on,
always the latest revision. Everything on chain still identifies a pool by the
`protocol_version` in its config, and callers naturally ask for
`pool_singleton_v9`, so that request is aliased here to the FORGE file. When the
mainnet puzzle is revised, the superseded copy is archived under development/
with its version name and FORGE_VERSION moves up; call sites never change.

**The archive.** Superseded revisions live in development/compiled/ rather than
alongside the shipping set, so the mainnet surface is small enough to audit at a
glance. They are still loadable, because pools minted under them remain on chain
and their tests are the regression evidence that the maths never silently moved.
"""
from __future__ import annotations

from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent
DEVELOPMENT_DIR = CONTRACTS_DIR / "development"

# Shipping puzzles first: a name present in both places resolves to the mainnet
# one, so an archived copy can never shadow what we actually deploy.
COMPILED_DIRS = (
    CONTRACTS_DIR / "compiled",
    DEVELOPMENT_DIR / "compiled",
)

# The revision the FORGE puzzles currently are. Bump this in the same commit that
# archives the previous FORGE files under development/.
FORGE_VERSION = 10

# Puzzles that track the pool revision. Anything else (the melt/mint inners, the
# launch guard) is named FORGE outright and never carried a version suffix.
_VERSIONED_STEMS = (
    "pool_singleton",
    "forge_reserve",
    "forge_lp_cat_tail",
)


def forge_name(stem: str) -> str:
    """The shipping filename for a puzzle family."""
    return f"{stem}_FORGE"


def resolve_name(name: str) -> str:
    """Map a request for the current revision onto its FORGE filename.

    `pool_singleton_v9` -> `pool_singleton_FORGE` while FORGE_VERSION is 9.
    Older versions, and names that already say FORGE, are returned unchanged.
    """
    for stem in _VERSIONED_STEMS:
        if name == f"{stem}_v{FORGE_VERSION}":
            return forge_name(stem)
    return name


def hex_path(name: str) -> Path:
    """Locate a compiled puzzle, shipping set first, then the archive."""
    resolved = resolve_name(name)
    for directory in COMPILED_DIRS:
        candidate = directory / f"{resolved}.clvm.hex"
        if candidate.is_file():
            return candidate
    # Report the name that was asked for as well as the one looked up, or an
    # alias mistake reads as a missing file.
    searched = ", ".join(str(d) for d in COMPILED_DIRS)
    extra = "" if resolved == name else f" (resolved to {resolved!r})"
    raise FileNotFoundError(f"compiled puzzle {name!r}{extra} not found in: {searched}")


def source_path(name: str) -> Path:
    """Locate a puzzle's .rue source, shipping set first, then the archive."""
    resolved = resolve_name(name)
    for directory in (CONTRACTS_DIR, DEVELOPMENT_DIR / "puzzles"):
        candidate = directory / f"{resolved}.rue"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"puzzle source {name!r} not found")


def available(name: str) -> bool:
    """Whether a compiled puzzle is present in this checkout.

    Superseded revisions live under development/, which is not published: they
    carry critical authorization bugs, and shipping ready-to-run copies would
    hand anyone a working attack against any pool still running one.

    So in a fresh clone the archive is simply absent. Suites that exercise an
    older revision use this to skip rather than fail -- a missing archive is an
    expected state, not a broken checkout. A skip is not a pass; it means that
    revision was not exercised here.
    """
    try:
        hex_path(name)
        return True
    except FileNotFoundError:
        return False
