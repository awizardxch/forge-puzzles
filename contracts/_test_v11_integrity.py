#!/usr/bin/env python3
"""The V11 build is what it claims to be: upstream pinned, curve fresh.

Two kinds of drift this catches, and both would be silent otherwise:

  * an upstream puzzle that is not the community-reviewed one. V11 leans on
    CHIP-0050's action layer, finalizers, p2_delegated_by_singleton and slot
    exactly because they were reviewed and are on mainnet; an edited copy
    forfeits that and moves the file into our audit scope without anyone
    noticing. So the vendored sources must hash to what was pinned, and must
    still compile to the tree hashes chia-sdk-types 0.36.0 publishes.

  * a compiled curve export that is stale against its source. The equivalence
    suite drives the compiled programs, so a source edit without a rebuild would
    test yesterday's curve and report today's as equivalent.

Exit codes follow the lifecycle skill: 0 all checks pass, 1 a check failed,
2 nothing could be exercised (build outputs absent).
"""
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program

CONTRACTS = pathlib.Path(__file__).resolve().parent
V11 = CONTRACTS / "v11"
COMPILED = V11 / "compiled"
UPSTREAM = ("action", "finalizer", "reserve_finalizer", "p2_delegated_by_singleton", "slot")
CURVE_EXPORTS = (
    "exact_swap_output", "exact_invariant_lp_mint", "exact_withdrawal",
    "effective_product", "valid_protocol_fees", "protocol_fee_owed", "pow_int",
    "sum_weights", "product_of", "min_deposit_ratio", "vault_fee_bps",
)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return bool(ok)


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hash(path: pathlib.Path) -> str:
    return Program.from_bytes(bytes.fromhex(path.read_text().strip())).get_tree_hash().hex()


def main() -> int:
    if not (COMPILED / "manifest.json").is_file():
        print("  [skip] contracts/v11/compiled/manifest.json is absent; run scripts/build-v11.py")
        return 2
    pins = json.loads((V11 / "pins.json").read_text(encoding="utf-8"))
    manifest = json.loads((COMPILED / "manifest.json").read_text(encoding="utf-8"))["outputs"]
    results = []

    print("upstream sources are the pinned bytes (never edited):")
    for rel, digest in pins["sources"].items():
        path = V11 / rel
        results.append(check(rel, path.is_file() and sha256(path) == digest))

    print("upstream builds hash to the chia-sdk-types 0.36.0 pins:")
    for name in UPSTREAM:
        hex_path, hash_path = COMPILED / f"{name}.rue.hex", COMPILED / f"{name}.rue.hash"
        pinned = pins["puzzles"][name]
        built = tree_hash(hex_path) if hex_path.is_file() else ""
        results.append(check(f"{name} hex", built == pinned, built[:16]))
        results.append(check(f"{name} .hash file", hash_path.is_file()
                             and hash_path.read_text().strip() == pinned))

    print("manifest agrees with the shipped hex and the current sources:")
    for out, entry in manifest.items():
        hex_path = COMPILED / out
        ok = hex_path.is_file() and tree_hash(hex_path) == entry["tree_hash"]
        src = (V11 / entry["source"]).resolve()
        fresh = src.is_file() and sha256(src) == entry["source_sha256"]
        results.append(check(f"{out}", ok and fresh,
                             "" if ok and fresh else ("stale source" if ok else "hash mismatch")))

    print("the curve module exports its whole surface:")
    for fn in CURVE_EXPORTS:
        results.append(check(fn, (COMPILED / f"forge_curve.{fn}.rue.hex").is_file()))

    print("functions the port left byte-identical to V10:")
    for fn in pins["byte_identical_to_v10"]:
        a, b = COMPILED / f"forge_curve.{fn}.rue.hex", COMPILED / f"v10_probe.{fn}.rue.hex"
        results.append(check(fn, a.is_file() and b.is_file() and tree_hash(a) == tree_hash(b)))

    print("the LP inners the actions bake:")
    common = (V11 / "puzzles" / "forge_action_common.rue").read_text(encoding="utf-8")
    for name, const in (("forge_lp_melt_inner", "LP_MELT_INNER_HASH"), ("forge_lp_mint_inner", "LP_MINT_INNER_HASH")):
        hex_path = COMPILED / f"{name}.clvm.hex"
        pinned = pins["lp_inners"][name]
        built = tree_hash(hex_path) if hex_path.is_file() else ""
        results.append(check(f"{name} hashes to its pin", built == pinned, built[:16]))
        results.append(check(f"forge_action_common bakes {const}",
                             f"inline const {const}: Bytes32 = 0x{pinned};" in common))

    print("the merkle root is exactly the six Forge leaves:")
    from forge_v11_merkle import LEAF_ORDER, MerkleTree, verify
    merkle = json.loads((COMPILED / "merkle.json").read_text(encoding="utf-8"))
    leaf_hashes = [bytes.fromhex(tree_hash(COMPILED / f"{name}.rue.hex")) for name in LEAF_ORDER]
    tree = MerkleTree(leaf_hashes)
    results.append(check("root recomputes from the compiled leaves in driver order", tree.root.hex() == merkle["root"]))
    results.append(check("exactly six leaves", len(merkle["leaves"]) == 6 and list(merkle["leaves"]) == list(LEAF_ORDER)))
    passthrough = bytes.fromhex(tree_hash(COMPILED / "passthrough_action.rue.hex"))
    results.append(check("the passthrough test leaf is not in the tree", passthrough not in leaf_hashes))
    results.append(check("every recorded proof verifies against the root",
                         all(verify(tree.root, h, tree.proof(h)) for h in leaf_hashes)
                         and all(merkle["proofs"][n]["path"] == tree.proof(h).path for n, h in zip(LEAF_ORDER, leaf_hashes))))

    print("the registry tree is exactly init and register:")
    reg_order = ("forge_registry_init", "forge_registry_register")
    reg_hashes = [bytes.fromhex(tree_hash(COMPILED / f"{name}.rue.hex")) for name in reg_order]
    reg_tree = MerkleTree(reg_hashes)
    results.append(check("registry root recomputes from the compiled leaves", reg_tree.root.hex() == merkle["registry"]["root"]))
    results.append(check("registry leaves are init and register, in that order", list(merkle["registry"]["leaves"]) == list(reg_order)))
    results.append(check("no pool leaf is in the registry tree", not (set(h.hex() for h in reg_hashes) & set(merkle["leaves"].values()))))

    # A fresh recompile in a scratch copy, so the check cannot pass by reading
    # back what the last build wrote.
    if shutil.which("rue"):
        print("fresh recompile from source:")
        with tempfile.TemporaryDirectory() as tmp:
            scratch = pathlib.Path(tmp) / "v11"
            shutil.copytree(V11, scratch, ignore=shutil.ignore_patterns("compiled"))
            (scratch / "compiled").mkdir()
            out = subprocess.run(["rue", "build", "--hex", "--hash", "--all", "."],
                                 cwd=scratch, capture_output=True, text=True, timeout=600)
            results.append(check("rue build --all succeeds", out.returncode == 0, out.stderr.strip()[:80]))
            for name in UPSTREAM:
                built = scratch / "compiled" / f"{name}.rue.hex"
                results.append(check(f"{name} recompiles to its pin",
                                     built.is_file() and tree_hash(built) == pins["puzzles"][name]))
            for fn in CURVE_EXPORTS:
                out = subprocess.run(["rue", "build", "--hex", "--export", fn, "puzzles/forge_curve.rue"],
                                     cwd=scratch, capture_output=True, text=True, timeout=600)
                fresh = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
                shipped = (COMPILED / f"forge_curve.{fn}.rue.hex").read_text().strip()
                results.append(check(f"forge_curve.{fn} recompiles to the shipped hex", fresh == shipped))
    else:
        print("  [skip] rue compiler unavailable; source->hex recompile not verified")

    strays = sorted(p.name for p in V11.rglob("*_v[0-9]*.rue"))
    results.append(check("no versioned puzzle names in the V11 set", not strays, ", ".join(strays)))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} V11 integrity checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
