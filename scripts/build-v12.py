#!/usr/bin/env python3
"""Build the Forge V12 rue project and check it against the upstream pins.

Three things come out of contracts/v12/compiled/:

  * every vendored upstream puzzle, recompiled from its verbatim source and
    required to hash to the value pinned in contracts/v12/pins.json;
  * each exported function of forge_curve.rue as a standalone program, so the
    curve can be driven in isolation by the equivalence suite;
  * the same functions cut out of the V10 pool puzzle, compiled with the V10
    bodies untouched, as the reference the V12 module is compared against.

A manifest records every output's tree hash and its source's sha256, so the
integrity suite can tell a stale hex from a fresh one without rue installed.

    python scripts/build-v12.py
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"
V12 = CONTRACTS / "v12"
PUZZLES = V12 / "puzzles"
COMPILED = V12 / "compiled"
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(CONTRACTS))
from chia.types.blockchain_format.program import Program  # noqa: E402

UPSTREAM = ("action", "finalizer", "reserve_finalizer", "p2_delegated_by_singleton", "slot")

# The curve module's public surface. The build refuses to complete if any of
# these is missing, so a rename cannot silently drop a function the actions use.
CURVE_EXPORTS = (
    "exact_swap_output", "exact_invariant_lp_mint", "exact_withdrawal",
    "effective_product", "valid_protocol_fees", "protocol_fee_owed", "pow_int",
    "sum_weights", "product_of", "min_deposit_ratio", "vault_fee_bps",
)

# What is lifted out of pool_singleton_FORGE.rue as the V10 reference. The
# bodies are not touched: the source is read, `export ` is prefixed to these
# definitions, and the result is compiled. valid_protocol_fees is plan-shaped in
# V10 and is compared through the corpus instead.
V10_PROBE_FNS = (
    "exact_swap_output", "exact_invariant_lp_mint", "exact_withdrawal",
    "effective_product", "pow_int", "sum_weights", "product_of",
    "min_deposit_ratio", "vault_fee_bps",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hash_hex(hex_blob: str) -> str:
    return Program.from_bytes(bytes.fromhex(hex_blob.strip())).get_tree_hash().hex()


def rue(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["rue", *args], cwd=cwd, capture_output=True, text=True, timeout=600)


def export_program(source: Path, name: str) -> tuple[str, str]:
    """(hex, tree_hash) of one exported function compiled standalone."""
    out = rue("build", "--hex", "--hash", "--export", name, str(source), cwd=source.parent)
    if out.returncode != 0:
        raise RuntimeError(f"rue failed exporting {name} from {source.name}:\n{out.stderr}\n{out.stdout}")
    lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    hex_blob, hashed = lines[0], lines[1]
    assert tree_hash_hex(hex_blob) == hashed, f"rue's hash disagrees with the hex for {name}"
    return hex_blob, hashed


def main() -> int:
    if shutil.which("rue") is None:
        print("rue compiler not on PATH; nothing built")
        return 2

    pins = json.loads((V12 / "pins.json").read_text(encoding="utf-8"))
    COMPILED.mkdir(exist_ok=True)
    manifest = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "rue": "0.8.4", "outputs": {}}
    ok = True

    # 1. The whole project: every module with a main() lands in compiled/ as
    #    <stem>.rue.hex + .rue.hash (dist_dir in Rue.toml).
    out = rue("build", "--hex", "--hash", "--all", ".", cwd=V12)
    if out.returncode != 0:
        print(out.stderr or out.stdout)
        print("rue build --all failed")
        return 1
    for line in (out.stderr or "").splitlines():
        if line.strip():
            print("  rue:", line.strip())

    print("upstream puzzles, recompiled from vendored source:")
    for name in UPSTREAM:
        hex_path = COMPILED / f"{name}.rue.hex"
        built = tree_hash_hex(hex_path.read_text())
        pinned = pins["puzzles"][name]
        good = built == pinned
        ok &= good
        print(f"  [{'PASS' if good else 'FAIL'}] {name:28s} {built}")
        manifest["outputs"][f"{name}.rue.hex"] = {
            "tree_hash": built, "pinned": pinned,
            "source": f"puzzles/upstream/{name}.rue",
            "source_sha256": sha256_file(PUZZLES / "upstream" / f"{name}.rue"),
        }

    # 1b. Every other module with a main(): Forge puzzles and test leaves.
    print("forge puzzles and test leaves:")
    sources = {q.stem: q for q in PUZZLES.rglob("*.rue")}
    for hex_path in sorted(COMPILED.glob("*.rue.hex")):
        name = hex_path.name[: -len(".rue.hex")]
        if name in UPSTREAM or "." in name:
            continue
        src = sources.get(name)
        if src is None:
            print(f"  [FAIL] {name}: compiled output with no source under puzzles/")
            ok = False
            continue
        built = tree_hash_hex(hex_path.read_text())
        manifest["outputs"][hex_path.name] = {
            "tree_hash": built, "source": src.relative_to(V12).as_posix(),
            "source_sha256": sha256_file(src),
            "test_only": src.parent.name == "testing",
        }
        print(f"  [PASS] {name:28s} {built}{'  (test leaf)' if src.parent.name == 'testing' else ''}")

    # 1c. The LP inners: the melt inner is V10's hex carried unchanged, the mint
    #     inner is V12's clsp (hint added), compiled here with clvm_tools.
    print("LP inners:")
    from clvm_tools.clvmc import compile_clvm_text
    melt_src = CONTRACTS / "compiled" / "forge_lp_melt_inner_FORGE.clvm.hex"
    melt_hex = melt_src.read_text().strip()
    (COMPILED / "forge_lp_melt_inner.clvm.hex").write_text(melt_hex + "\n")
    melt_hash = tree_hash_hex(melt_hex)
    good = melt_hash == pins["lp_inners"]["forge_lp_melt_inner"]
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] {'forge_lp_melt_inner (V10)':28s} {melt_hash}")
    manifest["outputs"]["forge_lp_melt_inner.clvm.hex"] = {
        "tree_hash": melt_hash, "pinned": pins["lp_inners"]["forge_lp_melt_inner"],
        "source": "../compiled/forge_lp_melt_inner_FORGE.clvm.hex", "source_sha256": sha256_file(melt_src)}
    mint_src = V12 / "clsp" / "forge_lp_mint_inner.clsp"
    mint = Program.to(compile_clvm_text(mint_src.read_text(encoding="utf-8"), search_paths=[]))
    mint_hex, mint_hash = bytes(mint).hex(), mint.get_tree_hash().hex()
    (COMPILED / "forge_lp_mint_inner.clvm.hex").write_text(mint_hex + "\n")
    good = mint_hash == pins["lp_inners"]["forge_lp_mint_inner"]
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] {'forge_lp_mint_inner (V12)':28s} {mint_hash}")
    manifest["outputs"]["forge_lp_mint_inner.clvm.hex"] = {
        "tree_hash": mint_hash, "pinned": pins["lp_inners"]["forge_lp_mint_inner"],
        "source": "clsp/forge_lp_mint_inner.clsp", "source_sha256": sha256_file(mint_src)}
    # The actions bake both hashes; they must be the ones just built.
    common = (PUZZLES / "forge_action_common.rue").read_text(encoding="utf-8")
    for const, value in (("LP_MELT_INNER_HASH", melt_hash), ("LP_MINT_INNER_HASH", mint_hash)):
        baked = f"inline const {const}: Bytes32 = 0x{value};" in common
        ok &= baked
        print(f"  [{'PASS' if baked else 'FAIL'}] forge_action_common bakes {const}")

    # 1d. The merkle root over exactly the six Forge leaves, in driver order (V12.1 added dao_fee).
    from forge_merkle import LEAF_ORDER, MerkleTree, verify
    leaf_hashes = [bytes.fromhex(tree_hash_hex((COMPILED / f"{name}.rue.hex").read_text())) for name in LEAF_ORDER]
    tree = MerkleTree(leaf_hashes)
    assert all(verify(tree.root, h, tree.proof(h)) for h in leaf_hashes)
    (COMPILED / "merkle.json").write_text(json.dumps({
        "leaves": {name: h.hex() for name, h in zip(LEAF_ORDER, leaf_hashes)},
        "root": tree.root.hex(),
        "proofs": {name: {"path": tree.proof(h).path, "hashes": [x.hex() for x in tree.proof(h).hashes]}
                   for name, h in zip(LEAF_ORDER, leaf_hashes)},
        "shape": "chia-wallet-sdk MerkleTree: split at (n+1)//2, leaf sha256(1||h), node sha256(2||l||r)",
        "registry": {
            "leaves": {name: tree_hash_hex((COMPILED / f"{name}.rue.hex").read_text())
                       for name in ("forge_registry_init", "forge_registry_register")},
            "root": MerkleTree([bytes.fromhex(tree_hash_hex((COMPILED / f"{name}.rue.hex").read_text()))
                                for name in ("forge_registry_init", "forge_registry_register")]).root.hex(),
        },
    }, indent=1) + "\n", encoding="utf-8")
    print(f"merkle root over {len(LEAF_ORDER)} leaves: {tree.root.hex()}")

    # 2. forge_curve exports.
    print("forge_curve.rue exports:")
    curve_src = PUZZLES / "forge_curve.rue"
    for fn in CURVE_EXPORTS:
        hex_blob, hashed = export_program(curve_src, fn)
        (COMPILED / f"forge_curve.{fn}.rue.hex").write_text(hex_blob + "\n")
        manifest["outputs"][f"forge_curve.{fn}.rue.hex"] = {
            "tree_hash": hashed, "source": "puzzles/forge_curve.rue",
            "export": fn, "source_sha256": sha256_file(curve_src),
        }
        print(f"  [PASS] {fn:28s} {hashed}")

    # 3. The V10 reference: same functions, V10 bodies, compiled standalone.
    print("V10 reference functions, lifted from pool_singleton_FORGE.rue:")
    v10_src = CONTRACTS / "pool_singleton_FORGE.rue"
    text = v10_src.read_text(encoding="utf-8")
    for fn in V10_PROBE_FNS:
        text, n = re.subn(rf"^fn {fn}\(", f"export fn {fn}(", text, count=1, flags=re.M)
        if n != 1:
            print(f"  [FAIL] {fn} not found in the V10 source")
            ok = False
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "v10_probe.rue"
        probe.write_text(text, encoding="utf-8")
        for fn in V10_PROBE_FNS:
            hex_blob, hashed = export_program(probe, fn)
            (COMPILED / f"v10_probe.{fn}.rue.hex").write_text(hex_blob + "\n")
            manifest["outputs"][f"v10_probe.{fn}.rue.hex"] = {
                "tree_hash": hashed, "source": "../pool_singleton_FORGE.rue",
                "export": fn, "source_sha256": sha256_file(v10_src),
            }
            print(f"  [PASS] {fn:28s} {hashed}")

    (COMPILED / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"\nmanifest: {COMPILED / 'manifest.json'}")
    print("upstream pins " + ("hold" if ok else "DO NOT HOLD"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
