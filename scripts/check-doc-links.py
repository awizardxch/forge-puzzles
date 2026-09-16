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
}


def published(rel: str) -> bool:
    """Would the public slice carry this path?"""
    rel = rel.replace("\\", "/")
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
                        "v14-route-audit.py", "check-doc-links.py")
    if rel.startswith(("api/", "src/")):
        return False          # the interface lives in forge-ui
    return rel.startswith("contracts/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish-only", action="store_true")
    args = ap.parse_args()

    docs = sorted((ROOT / "docs").glob("*.md"))
    skills = sorted((WORKSPACE / "docs" / "skills").glob("*.md"))
    targets = [d for d in docs if (not args.publish_only or d.name in PUBLISHED_DOCS)]
    if not args.publish_only:
        targets += skills

    broken, slice_gaps, external = [], [], set()
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="replace")
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
            if not any((root / cited).exists() for root in roots):
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
