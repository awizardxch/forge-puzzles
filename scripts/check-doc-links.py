#!/usr/bin/env python3
"""Every link and every cited path in the documents and skills resolves.

Two failures this catches, and both have happened here: a document that tells a reader to
start at a file the publish slice no longer ships, and a skill that names
`contracts/v11/...` long after that tree stopped being the live one.

It checks, for each markdown file:
  * `[text](target)` links -- relative targets must exist; http(s) are listed, not fetched
  * inline `code` spans that look like repository paths (contracts/, scripts/, docs/, api/,
    src/) -- the file must exist, with an optional `:line` suffix ignored
  * whether a cited path is excluded from the public publish slice, for the documents that
    slice ships (a citation that resolves here but not there is the failure mode that left
    four published documents pointing at nothing)

    python scripts/check-doc-links.py                 # the whole tree
    python scripts/check-doc-links.py --publish-only  # just what forge-puzzles ships
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # projects/chia-cfmm
WORKSPACE = ROOT.parent.parent                          # the monorepo root
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.S | re.M)


def prose_only(text: str) -> str:
    """The document with fenced blocks blanked out, line count preserved.

    A `[text](target)` inside a fence renders as literal characters: nobody can follow
    it, so checking it reports a template as a broken link. questManagement.md is mostly
    worked examples of quest files, and every one of its "broken" links was sample text
    showing the format. Blanking rather than deleting keeps any later line arithmetic
    honest.
    """
    return FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def project_context(text: str, upto: int) -> str | None:
    """The project a heading has put the reader in, for a skill that documents another one.

    `## Module Map -- awizard-gui` means the paths under it are relative to that project.
    """
    names = {d.name for d in (WORKSPACE / "projects").iterdir() if d.is_dir()}         if (WORKSPACE / "projects").is_dir() else set()
    current = None
    for m in re.finditer(r"^#{1,6} +(.+)$", text[:upto], re.M):
        heading = m.group(1).lower()
        for name in names:
            if name.lower() in heading:
                current = name
    return current
CODEPATH = re.compile(r"`((?:contracts|scripts|docs|api|src)/[A-Za-z0-9_./\-]+?)(?::\d+)?`")

# What the public slice ships -- read from sync-subrepos.ps1, not restated here.
#
# This used to be a hand-kept copy: a tuple of excluded directories, a set of
# published document names, a tuple of published script names. Two sources of
# truth for one fact, with nothing making them agree. Adding the 2026-09-20 audit
# record and its fingerprint tool to the slice left this file still believing they
# were unpublished, so it flagged a citation that was about to be perfectly valid.
# The same drift the other way is the one that matters: a checker that thinks a
# path publishes when it does not says nothing about the citation that will point
# at a 404 -- which is the failure this whole check exists to catch.
#
# So the slice file is the source and this parses it. A sub-repo's Slices block is
# a list of `@{ From = "..."; To = "..." }` entries, some of them directories
# carrying `Except` and `ExceptFiles`. That is enough structure to answer the only
# question asked here: would the public repository contain this path?
SLICE_TARGET = "forge-puzzles"


def _slice_file():
    """sync-subrepos.ps1, if this checkout has it.

    It is absent from the published repository by design -- the public clone gets
    the puzzles and the audit tooling, not the machinery that publishes them. So
    the publish gate is skipped there rather than failed, and says that it skipped.
    """
    override = os.environ.get("FORGE_SYNC_MANIFEST")
    if override:
        p = Path(override)
        return p.resolve() if p.is_file() else None
    candidates = [ROOT / ".." / ".." / "sync-subrepos.ps1",
                   ROOT / ".." / ".." / "sync" / "sync-subrepos.ps1",
                   ROOT / "sync-subrepos.ps1"]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return None


def _parse_slice(text, target):
    """(exact file map, directory slices) for one sub-repo's Slices block."""
    block = re.search('"' + re.escape(target) + r'"\s*=\s*@\{(.*?)\n    \}', text, re.S)
    if not block:
        return {}, []
    slices = re.search(r"Slices\s*=\s*@\((.*)", block.group(1), re.S)
    if not slices:
        return {}, []

    files, dirs = {}, []
    for chunk in slices.group(1).split("@{")[1:]:
        chunk = chunk.split("}", 1)[0]
        frm = re.search(r'From\s*=\s*"([^"]+)"', chunk)
        to = re.search(r'To\s*=\s*"([^"]+)"', chunk)
        if not frm or not to:
            continue
        src = frm.group(1).replace("\\", "/")
        dst = to.group(1).replace("\\", "/")
        # Every slice is rooted at the project; strip that for a repo-relative path.
        src = re.sub(r"^projects/[^/]+/", "", src)

        def _arr(name):
            m = re.search(name + r'\s*=\s*@\(([^)]*)\)', chunk)
            return re.findall(r'"([^"]+)"', m.group(1)) if m else []

        if (ROOT / src).is_dir():
            dirs.append((src, dst, _arr("Except"), _arr("ExceptFiles")))
        else:
            files[src] = dst
    return files, dirs


_SLICE_PATH = _slice_file()
_SLICE_TEXT = _SLICE_PATH.read_text(encoding="utf-8", errors="replace") if _SLICE_PATH else ""
SLICE_FILES, SLICE_DIRS = _parse_slice(_SLICE_TEXT, SLICE_TARGET)
SLICE_KNOWN = bool(SLICE_FILES or SLICE_DIRS)


# A few files publish under a different name than they carry here, because the slice
# renames them. A document written FOR the public repository cites the published name,
# which resolves nowhere in the monorepo -- so the citation is correct for its reader and
# broken for this checker. Map them rather than making the document wrong for its audience.
# Paths a document names BECAUSE they were removed. A history that cannot name what it
# replaced is not a history -- so a removed path is allowed only where the same document
# also names what took its place, and that replacement must itself exist. `None` is a file
# removed outright, with nothing to point at.
HISTORICAL = {
    "api/forge-v11-create.js": "api/forge-create.js",
    "api/forge-v12-create.js": "api/forge-create.js",
    "api/forge-v13-create.js": "api/forge-create.js",
    "api/forge-v14-create.js": "api/forge-create.js",
    "src/lib/forgeV11Create.ts": "src/lib/forgeCreate.ts",
    "src/lib/forgeV12Create.ts": "src/lib/forgeCreate.ts",
    "src/lib/forgeV13Create.ts": "src/lib/forgeCreate.ts",
    "src/lib/forgeV14Create.ts": "src/lib/forgeCreate.ts",
    "src/lib/contracts.generated.ts": None,
    "src/lib/spendBundles.ts": None,
    "api/_forgeV4Responder.js": "api/_forgeResponder.js",
    "contracts/gen_parity_vectors.py": "contracts/_test_v14_curve_equivalence.py",
    # The deploy lane FORGE_ROADMAP.md narrates the removal of: nothing took its place,
    # the story is why it went.
    "api/sage-deploy.js": None,
    "contracts/deploy_pool.py": None,
    # Named by the "an archive is not neutral" lesson precisely because it is gone.
    "contracts/sync_pool_from_chain.py": None,
}

# Published name -> the file it is published FROM, for the entries the slice
# renames. A document written for the public repository cites the published name,
# which resolves nowhere here -- correct for its reader, broken for this checker.
#
# Derived from the slice rather than listed, for the same reason as everything
# above it: the rename is already stated once, in the `From`/`To` pair that
# performs it. Empty when no slice file is present, which is exactly when the
# publish gate is skipped anyway.
PUBLISHED_ALIASES = {dst: src for src, dst in SLICE_FILES.items() if dst != src}

# Without a slice file the renames are unknown, and a document citing its published
# name would be reported as a broken link. In the public repository that never
# happens -- the file is there under that name, so the citation simply resolves. It
# happens in a partial checkout of this project without the workspace around it,
# where the citation is correct and the checker merely cannot see why.
#
# These two are a statement about the public repository's layout rather than a copy
# of the slice, and the derived map above wins whenever the slice can be read.
if not SLICE_KNOWN:
    PUBLISHED_ALIASES = {
        "docs/FORGE_SECURITY.md": "docs/subrepo/forge-puzzles.SECURITY.md",
        "README.md": "docs/subrepo/forge-puzzles.README.md",
    }


def published(rel):
    """Would the public slice carry this path?"""
    rel = rel.replace("\\", "/")
    if not SLICE_KNOWN:
        return True           # no slice file in this checkout; skipped, not guessed
    if rel in PUBLISHED_ALIASES:
        return True           # published under exactly this name, from a renamed source
    if rel in SLICE_FILES:
        return True
    for src, _dst, except_dirs, except_files in SLICE_DIRS:
        prefix = src.rstrip("/") + "/"
        if not rel.startswith(prefix):
            continue
        head = rel[len(prefix):].split("/", 1)[0]
        if head in except_dirs:
            return False
        name = rel.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(name, pat) for pat in except_files):
            return False
        return True
    return False


def is_published_doc(path) -> bool:
    """Is this document one the public repository carries?

    Asked of the document's own repo-relative path, through the same slice the
    citations are gated against -- so "which documents are public" and "which
    paths are public" can no longer disagree with each other.
    """
    try:
        rel = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return False
    return published(rel)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish-only", action="store_true")
    args = ap.parse_args()

    docs = sorted((ROOT / "docs").glob("*.md"))
    # The audit runbook sits with the puzzles rather than under docs/, and publishes.
    docs += sorted((ROOT / "skills").glob("*/SKILL.md"))
    skills = sorted((WORKSPACE / "docs" / "skills").glob("*.md"))
    targets = [d for d in docs if (not args.publish_only or is_published_doc(d))]
    if not args.publish_only:
        targets += skills

    broken, slice_gaps, external, unchecked = [], [], set(), []
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="replace")
        text = prose_only(text)
        base = path.parent
        for target in LINK.findall(text):
            if target.startswith(("http://", "https://")):
                external.add(target)
                continue
            if target.startswith("#"):
                continue
            resolved = (base / target.split("#")[0]).resolve()
            if not resolved.exists():
                broken.append((path.name, target, "link"))
        # a document under projects/chia-cfmm cites paths relative to it; a SKILL lives in the
        # workspace and cites paths relative to the workspace root (docs/skills/..., src/...).
        # Resolving a skill's citation against chia-cfmm reports the whole skill set as broken.
        # a document cites its own project's paths, and may also cite a workspace skill
        # (docs/skills/...); a skill cites the workspace. Both roots are legitimate.
        roots = [ROOT, WORKSPACE] if ROOT in path.parents else [WORKSPACE, ROOT]
        for cited in CODEPATH.findall(text):
            # Resolve an alias for EXISTENCE only: the cited name is the published one, and
            # rebinding `cited` would then slice-check the source file under a name the
            # slice does not carry.
            # A document may cite a file in ANOTHER repository and say so -- the
            # Nightspire theme's source of truth is one. Naming the repo on the same line
            # is the author telling the reader where to look; there is nothing local to
            # resolve, and flagging it would push them to delete a true citation.
            line = next((l for l in text.splitlines() if "`" + cited + "`" in l), "")
            if "github.com/" in line:
                continue
            if cited in HISTORICAL:
                replacement = HISTORICAL[cited]
                if replacement is None:
                    continue
                if ("`" + replacement + "`") in text and (ROOT / replacement).exists():
                    continue
                broken.append((path.name, cited,
                               "removed; name " + replacement))
                continue
            # In the public clone the published name is the one on disk; the alias only
            # helps in the monorepo, where the file still carries its source name.
            on_disk = any((root / cited).exists() for root in roots)
            lookup = cited if on_disk else PUBLISHED_ALIASES.get(cited, cited)
            if cited in PUBLISHED_ALIASES and (WORKSPACE / lookup).exists():
                continue          # published under exactly the name the document cites
            if cited.startswith(("api/", "src/")) and "interface repository" in text:
                continue      # declared external; there is nothing here to resolve
            if not any((root / lookup).exists() for root in roots):
                # A skill that documents another project cites paths relative to THAT
                # project. Several of them are nested clones or are simply not checked out
                # here, and reporting an absent checkout as a documentation defect trains
                # people to ignore this script.
                proj = project_context(text, text.find("`" + cited + "`"))
                if proj is not None:
                    where = WORKSPACE / "projects" / proj
                    if (where / lookup).exists():
                        continue
                    checked_out = where.is_dir() and any(
                        c.is_dir() and c.name in ("src", "api", "contracts")
                        for c in where.iterdir())
                    if not checked_out:
                        unchecked.append((path.name, cited, proj))
                        continue
                broken.append((path.name, cited, "cited path"))
            elif is_published_doc(path) and (ROOT / cited).exists() and not published(cited):
                # `api/` and `src/` are the interface repository's, and naming them is fine so
                # long as the document says so -- otherwise an external reader follows a path
                # that does not exist in the repository they are holding.
                if cited.startswith(("api/", "src/")) and "interface repository" in text:
                    continue
                slice_gaps.append((path.name, cited))

    print(f"checked {len(targets)} files: {len(docs)} documents, {len(skills) if not args.publish_only else 0} skills")
    print(f"  {len(external)} external links (not fetched)")

    if unchecked:
        names = sorted({proj for _, _, proj in unchecked})
        print(f"  {len(set(unchecked))} citation(s) not checked: "
              f"{', '.join(names)} not checked out here")

    if broken:
        print(f"\n{len(broken)} reference(s) that do not resolve:")
        for name, target, kind in sorted(set(broken)):
            print(f"  {name:<34} {kind:<11} {target}")
    else:
        print("\nevery relative link and cited path resolves")

    if slice_gaps:
        print(f"\n{len(set(slice_gaps))} citation(s) in PUBLISHED documents that the public slice does not ship:")
        for name, cited in sorted(set(slice_gaps)):
            print(f"  {name:<34} {cited}")
        print("  (each of these resolves for us and points at nothing for an external reader)")
    else:
        if SLICE_KNOWN:
            print("every citation in a published document is also published")
        else:
            # Say that the gate did not run. "Passed" and "was not checked" are
            # different answers and a reader acts differently on each; printing the
            # first for the second is how a check quietly stops being one.
            print("publish gate SKIPPED: no sync-subrepos.ps1 in this checkout, so which "
                  "paths are public is unknown here")

    return 1 if (broken or slice_gaps) else 0


if __name__ == "__main__":
    raise SystemExit(main())
