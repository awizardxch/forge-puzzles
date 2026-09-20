#!/usr/bin/env python3
"""One number for "which compiled revision is this", reproducible by anyone.

The audit record has carried a revision fingerprint since 2026-09-19, described
in prose as "sha256 over every .rue, .hex, .hash and the manifest under
contracts/v14, sorted". That sentence has more than one reading -- over the file
bytes or over their digests, raw or LF-normalised, including pins.json or not --
and the recorded value reproduces under none of the obvious ones. The number
appears nowhere in this repository except the record that quotes it, so there was
nothing to check it against.

That matters more than it looks. The runbook's completion gate asks that the
report "identifies the exact compiled revision tested", and an outside auditor is
asked to say which build they tested. A fingerprint only its author can compute
identifies the revision to nobody.

So: the recipe is code now, not prose.

  digest = sha256 of one line per file, sorted by path:
      <path relative to contracts/v14>:<sha256 of the file's LF-normalised bytes>

LF-normalised because the same tree checks out CRLF on Windows and LF elsewhere,
and a fingerprint that changes with the checkout is a fingerprint that says
nothing about the puzzle. Per-file digests rather than concatenated bytes so that
a mismatch can name the file that moved, which is the question anyone actually
has when two fingerprints disagree.

    python scripts/revision-fingerprint.py              # the digest
    python scripts/revision-fingerprint.py --files      # and every file's line
    python scripts/revision-fingerprint.py --expect <hex>   # exit 1 on mismatch

This is provenance, not integrity: it says the artefacts are the same bytes as
last time, not that they match their sources. `_test_v14_integrity.py` recompiles
every source and compares, and `_test_v14_provenance.py` checks the manifest
against git. Run those for "is this build honest"; run this for "is this the same
build".
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUFFIXES = {".rue", ".hex", ".hash", ".json"}


def revision_files(base: Path) -> list[Path]:
    return sorted(
        (p for p in base.rglob("*") if p.is_file() and p.suffix in SUFFIXES),
        key=lambda p: p.relative_to(base).as_posix(),
    )


def file_line(base: Path, path: Path) -> str:
    body = path.read_bytes().replace(b"\r\n", b"\n")
    return f"{path.relative_to(base).as_posix()}:{hashlib.sha256(body).hexdigest()}"


def fingerprint(base: Path) -> tuple[str, list[str]]:
    lines = [file_line(base, p) for p in revision_files(base)]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest(), lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revision", default="contracts/v14",
                    help="directory to fingerprint, relative to the repo root")
    ap.add_argument("--files", action="store_true", help="print every file's line too")
    ap.add_argument("--expect", help="fail if the digest is not this")
    args = ap.parse_args()

    base = (ROOT / args.revision).resolve()
    if not base.is_dir():
        print(f"no such revision directory: {base}", file=sys.stderr)
        return 2

    digest, lines = fingerprint(base)
    if args.files:
        for line in lines:
            print(f"  {line}")
    print(f"{args.revision}: {len(lines)} files")
    print(f"fingerprint: {digest}")

    if args.expect and args.expect.lower() != digest:
        print(f"MISMATCH: expected {args.expect.lower()}", file=sys.stderr)
        print("run with --files against both trees to see which file moved", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
