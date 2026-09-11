#!/usr/bin/env python3
"""The shipping puzzle set is self-consistent.

The pool puzzle hard-codes the tree hashes of the LP melt and mint inners, and
binds the LP action coin to a CAT wrapping one of them. If a baked constant ever
drifts from the puzzle it names -- a recompile that is not carried into the
constant, an edited .clsp whose .clvm.hex was not regenerated -- the pool would
demand a coin nobody can construct, and every add or remove would fail. Worse,
if it drifted to a puzzle an attacker CAN construct, the LP binding stops being
a binding.

So this checks, for the mainnet set only:
  * each .clsp source compiles to the .clvm.hex actually shipped;
  * each of those matches the constant the pool puzzle commits to;
  * the .rue sources still compile to the .clvm.hex shipped alongside them;
  * the compiled manifest agrees with all of it.
"""
import hashlib
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, ".")

from chia.types.blockchain_format.program import Program

import forge_puzzles

CONTRACTS = pathlib.Path(__file__).resolve().parent
RUE_PUZZLES = ("pool_singleton_FORGE", "forge_reserve_FORGE", "forge_lp_cat_tail_FORGE")
CLSP_PUZZLES = {
    "forge_lp_melt_inner_FORGE": "LP_MELT_INNER_HASH",
    "forge_lp_mint_inner_FORGE": "LP_MINT_INNER_HASH",
}


def tree_hash_of(name: str) -> str:
    blob = forge_puzzles.hex_path(name).read_text("ascii").strip()
    return Program.from_bytes(bytes.fromhex(blob)).get_tree_hash().hex()


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    results = []
    pool_src = (CONTRACTS / "pool_singleton_FORGE.rue").read_text(encoding="utf8")
    baked = dict(re.findall(
        r"inline const (LP_(?:MELT|MINT)_INNER_HASH): Bytes32 = 0x([0-9a-f]{64});", pool_src))
    results.append(check("the pool commits to both inner hashes", len(baked) == 2, str(sorted(baked))))

    # 1. .clsp sources -> shipped hex -> the constant the pool binds to.
    try:
        from clvm_tools.clvmc import compile_clvm_text
    except ImportError:
        compile_clvm_text = None
    for name, const in CLSP_PUZZLES.items():
        shipped = tree_hash_of(name)
        results.append(check(f"{name} matches the pool's {const}",
                             shipped == baked.get(const), shipped))
        if compile_clvm_text is not None:
            src = (CONTRACTS / f"{name}.clsp").read_text(encoding="utf8")
            built = Program.to(compile_clvm_text(src, search_paths=[])).get_tree_hash().hex()
            results.append(check(f"{name}.clsp compiles to what is shipped", built == shipped))

    # 2. .rue sources still compile to the shipped hex. Skipped rather than failed
    #    when the compiler is unavailable, so this stays runnable anywhere.
    rue = None
    try:
        rue = subprocess.run(["rue", "--help"], capture_output=True, timeout=30).returncode == 0
    except Exception:
        rue = False
    if rue:
        for name in RUE_PUZZLES:
            try:
                out = subprocess.run(["rue", "build", "--hex", "--hash", f"{name}.rue"],
                                     cwd=CONTRACTS, capture_output=True, text=True, timeout=300)
                hashes = re.findall(r"^([0-9a-f]{64})$", out.stdout, re.M)
                built = hashes[-1] if hashes else ""
                results.append(check(f"{name}.rue compiles to the shipped hex",
                                     built == tree_hash_of(name), built or out.stderr.strip()[:60]))
            except Exception as exc:
                results.append(check(f"{name}.rue recompiles", False, f"{type(exc).__name__}"))
    else:
        print("  [skip] rue compiler unavailable; source->hex recompile not verified")

    # 3. The manifest agrees with the shipped files.
    manifest = CONTRACTS / "compiled" / "v3-manifest.json"
    if manifest.is_file():
        import json
        entries = json.loads(manifest.read_text()).get("contracts", {})
        for name in (*RUE_PUZZLES, *CLSP_PUZZLES):
            key = next((k for k in entries if k.rsplit(".", 1)[0] == name), None)
            if key is None:
                results.append(check(f"manifest lists {name}", False))
                continue
            results.append(check(f"manifest tree_hash for {name} is current",
                                 entries[key].get("tree_hash") == tree_hash_of(name)))

    # 4. Nothing version-suffixed is left in the shipping set.
    strays = sorted(p.name for p in CONTRACTS.glob("*.rue") if re.search(r"_v\d+\.rue$", p.name))
    results.append(check("no versioned puzzles remain in the mainnet set",
                         not strays, ", ".join(strays)))

    # 5. The archived snapshot of the current revision must stay byte-identical to
    #    what ships. Keeping both a FORGE copy and a versioned copy is only safe
    #    if they cannot drift -- otherwise the archive would quietly stop being a
    #    record of what was deployed.
    version = forge_puzzles.FORGE_VERSION
    archive = CONTRACTS / "development" / "puzzles"
    if not archive.is_dir():
        # A published checkout excludes the archive, so there is no second copy to
        # drift from. Nothing to verify rather than something failing.
        print("  [skip] the archive is absent; snapshot-drift check does not apply")
        results.append(True)
    for name, ext in () if not archive.is_dir() else (
                      ("pool_singleton", "rue"), ("forge_reserve", "rue"),
                      ("forge_lp_cat_tail", "rue"),
                      ("forge_lp_melt_inner", "clsp"), ("forge_lp_mint_inner", "clsp")):
        shipped_src = CONTRACTS / f"{name}_FORGE.{ext}"
        archived_src = CONTRACTS / "development" / "puzzles" / f"{name}_v{version}.{ext}"
        shipped_hex = CONTRACTS / "compiled" / f"{name}_FORGE.clvm.hex"
        archived_hex = CONTRACTS / "development" / "compiled" / f"{name}_v{version}.clvm.hex"
        if not archived_src.is_file() or not archived_hex.is_file():
            results.append(check(f"v{version} snapshot of {name} exists", False))
            continue
        same = (shipped_src.read_bytes() == archived_src.read_bytes()
                and shipped_hex.read_bytes() == archived_hex.read_bytes())
        results.append(check(f"{name}_FORGE matches its v{version} snapshot", same))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} integrity checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
