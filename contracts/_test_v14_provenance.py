#!/usr/bin/env python3
"""Provenance (spec 4): what is published is what was built, and every citation resolves.

Three checks the fifth review asked for:

  * manifest hashes against the COMMITTED sources, not the working copy: each source's
    sha256 in compiled/manifest.json must equal `git show HEAD:<path>` in the repository
    the tree is committed to. Until contracts/v14 is committed this cannot be verified,
    and the suite says so (exit 2) rather than passing on the working copy.
  * every contracts/, scripts/ and docs/ path cited in the V14 documents resolves inside
    the project tree.
  * every shipped hex equals a fresh recompile, and the compiler's absence is exit 2, never
    0 -- delegated to _test_v14_integrity, which is where that check lives.

    FORGE_REPO=<path to a clone that tracks projects/chia-cfmm> overrides the git lookup.

Exit 0 all pass, 1 a failure, 2 nothing could be exercised.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

CONTRACTS = pathlib.Path(__file__).resolve().parent
PROJECT = CONTRACTS.parent
V14 = CONTRACTS / "v14"
DOCS = [PROJECT / "docs" / "FORGE_PUZZLE_V14_SPEC.md", *sorted((PROJECT / "docs").glob("FORGE_*V14*.md"))]
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def git_repo_for(path: pathlib.Path):
    """(repo root, path of `path` inside it) for a clone that TRACKS the file, else None."""
    candidates = [os.environ.get("FORGE_REPO")] if os.environ.get("FORGE_REPO") else []
    candidates += [str(PROJECT), str(PROJECT.parent), str(PROJECT.parent.parent)]
    workspace = PROJECT.parent.parent          # the monorepo root; a clone mirrors its layout
    for root in candidates:
        root = pathlib.Path(root)
        if not (root / ".git").exists():
            continue
        inside = str(path.resolve()).startswith(str(root.resolve()))
        rel = os.path.relpath(path, root if inside else workspace).replace(os.sep, "/")
        out = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=root, capture_output=True, text=True)
        if out.returncode == 0:
            return root, rel
    return None


def main() -> int:
    manifest_path = V14 / "compiled" / "manifest.json"
    if not manifest_path.is_file():
        print("  [skip] contracts/v14/compiled/manifest.json is absent; run scripts/build-v14.py")
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["outputs"]

    print("citations in the V14 documents resolve inside the project tree:")
    cited = set()
    for doc in DOCS:
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8")
        for m in re.finditer(r"`((?:contracts|scripts|docs)/[A-Za-z0-9_./\-]+?)(?::\d+)?`", text):
            cited.add((doc.name, m.group(1)))
    missing = sorted((d, p) for d, p in cited if not (PROJECT / p).exists())
    check(f"{len(cited)} cited paths across {len([d for d in DOCS if d.is_file()])} documents", bool(cited))
    check("every cited path exists", not missing, "; ".join(f"{d}: {p}" for d, p in missing[:6]))

    print("the build's sources against the committed tree:")
    located = git_repo_for(V14 / "puzzles" / "forge_action_common.rue")
    if located is None:
        print("  [skip] contracts/v14 is not tracked by any git clone found (set FORGE_REPO); "
              "the manifest can only be checked against the working copy, which proves nothing")
        verdict_git = None
    else:
        root, _ = located
        verdict_git = True
        for out, entry in manifest.items():
            src = (V14 / entry["source"]).resolve()
            inside = str(src).startswith(str(root.resolve()))
            rel = os.path.relpath(src, root if inside else PROJECT.parent.parent).replace(os.sep, "/")
            show = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root, capture_output=True)
            if show.returncode != 0:
                ok = False; detail = "not committed"
            else:
                ok = hashlib.sha256(show.stdout).hexdigest() == entry["source_sha256"]; detail = "" if ok else "committed source differs from the built one"
            verdict_git &= ok
            results.append(check(f"{out} <- {rel}", ok, detail))

    print("every shipped hex equals a fresh recompile (delegated to _test_v14_integrity):")
    if shutil.which("rue") is None:
        print("  [skip] rue is not on PATH: the build cannot be verified")
        verdict_build = None
    else:
        out = subprocess.run([sys.executable, str(CONTRACTS / "_test_v14_integrity.py")], cwd=CONTRACTS,
                             capture_output=True, text=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        verdict_build = out.returncode == 0
        tail = [l for l in out.stdout.splitlines() if "integrity checks passed" in l]
        results.append(check("the integrity suite passes", verdict_build, tail[-1].strip() if tail else out.stdout[-120:]))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} provenance checks passed")
    if verdict_git is None or verdict_build is None:
        print("exit 2: at least one provenance check could not be exercised (see the [skip] lines above)")
        return 2 if passed == len(results) else 1
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
