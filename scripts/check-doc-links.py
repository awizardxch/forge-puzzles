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

# what the public slice ships, from sync-subrepos.ps1: contracts/ minus the retired trees,
# a named set of documents, and two scripts.
PUBLISH_EXCLUDED_DIRS = ("contracts/v11", "contracts/v12", "contracts/v13",
                         "contracts/development", "contracts/greenwood")
PUBLISH_EXCLUDED_GLOBS = ("_test_v11_", "_test_v12_", "_test_v13_", "forge_v12_", "forge_v13_",
                          "_v12_testkit", "_v13_testkit", "_sim_future_", "_sim_v14_vault_",
                          "_sim_v14_rcat_", "_sim_v14_layered_", "_sim_v14_matrix")
PUBLISHED_DOCS = {
    "FORGE_PUZZLE_V14.md", "FORGE_PUZZLE_V14_SPEC.md", "FORGE_V14_ARCHITECTURE.md",
    "FORGE_V14_CLVM_PASS.md", "FORGE_DAO_FEE_V14.md", "FORGE_AUDIT_TIBETSWAP.md",
    "FORGE_CHIP_WORKFLOW.md", "FORGE_ROUTER_PROTOCOL_V1.md", "FORGE_MULTISIG.md",
    "FORGE_LOCK_MIPS.md", "FORGE_LOCK_SAFE_MODEL.md",
    # The audit runbook publishes with the puzzles (skills/ in the forge-puzzles slice),
    # so every path it cites has to exist THERE, not only here. It is the document an
    # outside auditor works from, which makes a citation it cannot follow worse than one
    # in a document only we read.
    "SKILL.md",
    # An audit run's record publishes beside the runbook it followed.
    "FORGE_AUDIT_RUN_V14_2026-09-19.md",
    "FORGE_AUDIT_RUN_V14_2026-09-20.md",
}


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

PUBLISHED_ALIASES = {
    "docs/FORGE_SECURITY.md": "docs/subrepo/forge-puzzles.SECURITY.md",
    "README.md": "docs/subrepo/forge-puzzles.README.md",
}


def published(rel: str) -> bool:
    """Would the public slice carry this path?"""
    rel = rel.replace("\\", "/")
    if rel in PUBLISHED_ALIASES:
        return True           # published under exactly this name, from a renamed source
    if rel.startswith(tuple(PUBLISH_EXCLUDED_DIRS)):
        return False
    name = rel.rsplit("/", 1)[-1]
    if any(g in name for g in PUBLISH_EXCLUDED_GLOBS):
        return False
    if rel.startswith("docs/"):
        return name in PUBLISHED_DOCS or rel.startswith("docs/chip/")
    if rel.startswith("scripts/"):
        return name in ("mutate-v14.py", "build-v14.py", "sim-v14.py", "sim-v14-batch-security.py",
                        "v14-squat-probe.py", "v14-settlement-probe.py", "v14-slack-probe.py",
                        "check-doc-links.py",
                        "sim-v14-review-derivations.py", "sim-v14-chip0062.py",
                        # The runbook cites this one in prose: it is the answer to
                        # "which build did you test", so an outside auditor needs it.
                        "revision-fingerprint.py")
    if rel.startswith(("api/", "src/")):
        return False          # the interface lives in forge-ui
    if rel.startswith("skills/"):
        return True           # the whole directory is a slice
    return rel.startswith("contracts/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish-only", action="store_true")
    args = ap.parse_args()

    docs = sorted((ROOT / "docs").glob("*.md"))
    # The audit runbook sits with the puzzles rather than under docs/, and publishes.
    docs += sorted((ROOT / "skills").glob("*/SKILL.md"))
    skills = sorted((WORKSPACE / "docs" / "skills").glob("*.md"))
    targets = [d for d in docs if (not args.publish_only or d.name in PUBLISHED_DOCS)]
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
            elif path.name in PUBLISHED_DOCS and (ROOT / cited).exists() and not published(cited):
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
        print("every citation in a published document is also published")

    return 1 if (broken or slice_gaps) else 0


if __name__ == "__main__":
    raise SystemExit(main())
