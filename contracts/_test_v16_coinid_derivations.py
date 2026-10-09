#!/usr/bin/env python3
"""Audit F4 (2026-10-07): the two coin ids that were sha256 over solution bytes are
derived with `coinid`, and this file pins WHERE the refusal of a malformed input happens.

Before, `lp_action_coin_id` (forge_action_common.rue) and the launcher id in
forge_registry_register.rue were `sha256(parent + puzzle_hash + (amount as Bytes))`. A
parent of 31 or 33 bytes, or a `burn` carrying a redundant leading zero byte, hashed to an
id no coin has, and the spend failed downstream: `remove` on message pairing
(MESSAGE_NOT_SENT_OR_RECEIVED, 147), `register` on `valid_pool`'s TAIL binding (the LP
TAIL is curried with the launcher id, so a wrong id names a TAIL the config does not
claim). Fail-closed both times, but refused by something other than the width itself,
which is the class-15 shape the audit asked to be finished off.

After, the derivation is the `coinid` operator, as the settlement id and the reserve
parents already were: it refuses a parent that is not 32 bytes and an amount that is not a
canonical non-negative integer, in the leaf, before any hash is compared. For every valid
input the result is byte-identical to the old sha256, so the honest controls here are
accepted on both builds and every other suite is unchanged.

Two lanes per probe, each beside its honest control:

  local      the driver runs the leaf first, as every lane does; the interpreter raises
             naming `coinid` (after) / the leaf runs and consensus refuses (before)
  consensus  the malformed atom is substituted into an otherwise honest bundle's singleton
             solution, so the leaf runs under consensus validation: GENERATOR_RUNTIME_ERROR
             (117, after) / the downstream code (before)

Audit F7 rides in the same file (canonical_state_lane): `h` and the DAO rate `new_bps` are
solution atoms the prologue and the dao_fee leaf used to place in the hashed state as they
arrived, so a redundant leading zero made tree_hash(state) diverge from every canonical
mirror. After, both are stored after arithmetic (`+ 0`). The lane feeds a leading-zero `h`
to `observe` and a leading-zero `new_bps` to `dao_fee`, beside the canonical controls, and
pins: after, the resulting state hashes to the canonical state's hash (and the lane records
whether consensus accepts the spend); before, the hashes differ.

Run against the shipped build it pins the "after" column; `--before <compiled dir>` also
runs it, in a subprocess, against an older build and pins the "before" column, which is how
the pull request carries both. Exit 0 all pass, 1 a failure, 2 nothing exercised: the build
is absent, or compiled/manifest.json records sources other than the ones in the tree (a
stale build cannot pin anything about these sources).
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32

import _v16_testkit as kit
import forge_math

FAILED = 0
CAT = bytes32(b"\xd0" * 32)
H = 6_999_990
GENERATOR_RUNTIME_ERROR = 117
MESSAGE_NOT_SENT_OR_RECEIVED = 147
V16 = pathlib.Path(__file__).resolve().parent / "v16"
WIDTHS = {"31 bytes": b"\x11" * 31, "33 bytes": b"\x11" * 33}


def check(label, ok, detail=""):
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILED += 1


def outcome(thunk):
    """('accepted', ''), ('refused', reason) or ('broken', text) -- a harness crash never
    reads as a refusal (QA-2)."""
    try:
        thunk()
        return "accepted", ""
    except Exception as exc:  # noqa: BLE001
        reason = kit.refusal_reason(exc)
        if reason is not None:
            return "refused", reason
        return "broken", f"{type(exc).__name__}: {str(exc)[:80]}"


def code_of(reason: str):
    """The consensus code at the end of a `Rejected` reason, else None."""
    m = re.search(r":\s*(\d+)\s*$", reason)
    return int(m.group(1)) if m else None


def substitute(bundle: SpendBundle, coin, old: bytes, new: bytes) -> SpendBundle:
    """The same bundle with every atom equal to `old` in `coin`'s solution replaced by `new`."""
    def walk(p):
        if p.atom is not None:
            return Program.to(new) if bytes(p.atom) == old else p
        return Program.to((walk(p.first()), walk(p.rest())))
    spends = []
    for cs in bundle.coin_spends:
        if cs.coin == coin:
            cs = make_spend(cs.coin, Program.from_bytes(bytes(cs.puzzle_reveal)),
                            walk(Program.from_bytes(bytes(cs.solution))))
        spends.append(cs)
    return SpendBundle(spends, G2Element())


def expect(label, verdict, reason, build, downstream_code, coinid_text):
    """Pin the refusal to the build: after -> the operator itself; before -> downstream."""
    ok = verdict == "refused"
    check(f"{label}: refused", ok, f"{verdict} {reason}" if not ok else "")
    if not ok:
        return
    code = code_of(reason)
    if build == "after":
        names_coinid = "coinid" in reason and coinid_text in reason
        in_leaf = names_coinid or code == GENERATOR_RUNTIME_ERROR
        check(f"  by the leaf itself (coinid, or GENERATOR_RUNTIME_ERROR {GENERATOR_RUNTIME_ERROR})", in_leaf, reason[:90])
        check("  and not by message pairing or an announcement downstream",
              code not in (MESSAGE_NOT_SENT_OR_RECEIVED, 12), reason[:90])
    else:
        check("  not by coinid (the old build never ran it)", "coinid" not in reason, reason[:90])
        if downstream_code is not None:
            check(f"  downstream, code {downstream_code}", code == downstream_code, reason[:90])
        else:
            check("  by an assert the leaf reached after deriving the id (clvm raise 80 / 117)",
                  "clvm raise" in reason or code == GENERATOR_RUNTIME_ERROR, reason[:90])


# ---- remove: lp_parent_id and burn --------------------------------------------------------

def remove_lane(build):
    print("remove leaf, lp_parent_id and burn -> lp_action_coin_id:")
    pool = kit.replace(kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                                     leaves="forge", salt=0x64, last_height=H - 40), birth=H - 39)
    burn = 1_000_000
    vf = forge_math.vault_fee_bps(2, 10, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)
    probe_state, _, _, _ = kit.run_leaf(pool, "forge_action_remove", [H, burn, bytes32(b"\x01" * 32), payouts])
    lp_parent, lp_spends = kit.lp_melt_spend(pool, burn, pool.state[1] - burn, probe_state.get_tree_hash(), salt=0x91)
    honest, _ = kit.spend_action(pool, "forge_action_remove", [H, burn, lp_parent, payouts], extra_spends=lp_spends)
    verdict, reason = outcome(lambda: kit.validate(honest))
    check("the honest remove is accepted (control)", verdict == "accepted", reason)

    def local(solution):
        return lambda: kit.validate(kit.spend_action(pool, "forge_action_remove", solution, extra_spends=lp_spends)[0])

    for name, parent in WIDTHS.items():
        v, r = outcome(local([H, burn, parent, payouts]))
        expect(f"lp_parent_id of {name}, local lane", v, r, build, MESSAGE_NOT_SENT_OR_RECEIVED, "32 bytes")
        v, r = outcome(lambda p=parent: kit.validate(substitute(honest, pool.coin, bytes(lp_parent), p)))
        expect(f"lp_parent_id of {name}, consensus lane", v, r, build, MESSAGE_NOT_SENT_OR_RECEIVED, "32 bytes")
    noncanonical = b"\x00" + burn.to_bytes(3, "big")
    assert int.from_bytes(noncanonical, "big") == burn
    v, r = outcome(local([H, noncanonical, lp_parent, payouts]))
    expect("burn encoded with a redundant leading zero, local lane", v, r, build, MESSAGE_NOT_SENT_OR_RECEIVED, "leading zero")
    v, r = outcome(lambda: kit.validate(substitute(honest, pool.coin, kit.amt(burn), noncanonical)))
    expect("burn encoded with a redundant leading zero, consensus lane", v, r, build, MESSAGE_NOT_SENT_OR_RECEIVED, "leading zero")


# ---- register: launcher_parent_id ----------------------------------------------------------

def register_lane(build):
    print()
    print("register leaf, launcher_parent_id -> launcher_id:")
    import _test_v16_registry as regsuite
    reg0 = kit.make_registry(salt=0x21)
    kit.validate(kit.registry_spend(reg0, "forge_registry_init", [])[0])
    reg1 = reg0.advance([1, 0, 1_000_000])
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    left = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY)
    right = (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50)
    honest, _ = regsuite.registration(reg1, pool, left, right, slots)
    verdict, reason = outcome(lambda: kit.validate(honest))
    check("the honest registration is accepted (control)", verdict == "accepted", reason)

    for name, parent in WIDTHS.items():
        solution = kit.register_solution(pool, left, right)
        solution[0] = parent
        v, r = outcome(lambda s=solution: kit.validate(kit.registry_spend(reg1, "forge_registry_register", s)[0]))
        # Before: no downstream code -- valid_pool's TAIL binding raised in the leaf once the
        # wrong id was derived. After: coinid refuses the width before anything is derived.
        expect(f"launcher_parent_id of {name}, local lane", v, r, build, None, "32 bytes")
        v, r = outcome(lambda p=parent: kit.validate(substitute(honest, reg1.coin, bytes(pool.launcher_parent), p)))
        expect(f"launcher_parent_id of {name}, consensus lane", v, r, build, None, "32 bytes")


# ---- F7: h and new_bps in the hashed state are canonical ----------------------------------

ASSERT_HEIGHT_ABSOLUTE = 83


def canonical_state_lane(build):
    print()
    print("hashed state, h and new_bps spelled with a redundant leading zero:")
    import _test_v16_dao_fee as daosuite
    pool = kit.replace(kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                                     leaves="forge", salt=0x67, last_height=H - 40,
                                     protocol_ph=daosuite.PROTOCOL_PH, dao_ph=daosuite.DAO_PH, dao_fee_bps=50),
                      birth=H - 39)
    h_nc = b"\x00" + H.to_bytes((H.bit_length() + 8) // 8, "big")
    assert int.from_bytes(h_nc, "big") == H and len(h_nc) == len(kit.amt(H)) + 1

    def states(name, honest_solution, probe_solution, **knobs):
        honest_state, _, honest_base, honest_eph = kit.run_leaf(pool, name, honest_solution)
        try:
            probe_state, _, probe_base, probe_eph = kit.run_leaf(pool, name, probe_solution)
        except Exception as exc:  # noqa: BLE001
            reason = kit.refusal_reason(exc)
            print(f"  [info] {name}: the leaf itself refuses the leading zero: {reason or exc}")
            return honest_state, None, None, None, None
        return honest_state, probe_state, probe_base, honest_eph, probe_eph

    def pin(name, honest_state, probe_state, extra):
        if probe_state is None:
            check(f"{name}: a leading-zero atom is refused by the leaf" + (" (after: expected a canonical state instead)" if build == "after" else ""),
                  build == "before")
            return
        same = bytes(probe_state.get_tree_hash()) == bytes(honest_state.get_tree_hash())
        if build == "after":
            check(f"{name}: the state the leaf returns hashes to the canonical state's hash", same,
                  f"{probe_state.get_tree_hash().hex()[:16]} vs {honest_state.get_tree_hash().hex()[:16]}")
        else:
            check(f"{name}: before, the raw atom is stored and the state hash DIVERGES from the canonical one", not same,
                  "" if not same else "the hashes agree: this build already normalises")
        for label, ok in extra:
            check(f"  {label}", ok)

    # observe: the prologue alone writes the state (last_height) and the truth's height.
    honest_state, probe_state, probe_base, honest_eph, probe_eph = states("forge_action_observe", [H], [h_nc])
    extra = []
    if probe_state is not None:
        height_conds = [bytes(c.rest().first().as_atom()) for c in probe_base
                        if c.first().atom is not None and c.first().as_int() == ASSERT_HEIGHT_ABSOLUTE]
        canonical = bytes(probe_eph.as_atom()) == kit.amt(H) if probe_eph.atom is not None else False
        if build == "after":
            extra = [("the truth's height (ephemeral state) is canonical", canonical),
                     ("ASSERT_HEIGHT_ABSOLUTE carries the canonical height", height_conds == [kit.amt(H)])]
        else:
            extra = [("before, the truth's height and the height condition carry the raw atom",
                      not canonical and height_conds == [h_nc])]
    pin("observe with h = 0x00||h", honest_state, probe_state, extra)
    if probe_state is not None:
        v, r = outcome(lambda: kit.validate(kit.spend_action(pool, "forge_action_observe", [h_nc])[0]))
        check("  the probe spend is not a harness fault", v != "broken", r)
        print(f"  [info] observe with the leading-zero h under consensus validation: {v} {r[:60]}")
    v, r = outcome(lambda: kit.validate(kit.spend_action(pool, "forge_action_observe", [H])[0]))
    check("  the canonical observe is accepted (control)", v == "accepted", r)

    # dao_fee: the leaf writes new_bps into the state and names it in the DAO's message.
    bps_nc = b"\x00" + (20).to_bytes(1, "big")
    honest_state, probe_state, _, _, _ = states("forge_action_dao_fee", [H, 20], [H, bps_nc])
    pin("dao_fee with new_bps = 0x00||20", honest_state, probe_state, [])
    if probe_state is not None:
        v, r = outcome(lambda: kit.validate(kit.spend_action(pool, "forge_action_dao_fee", [H, bps_nc],
                                                             extra_spends=[daosuite.dao_coin_spend(pool, 20)])[0]))
        check("  the probe spend is not a harness fault", v != "broken", r)
        print(f"  [info] dao_fee with the leading-zero rate, DAO message naming canonical 20, under consensus: {v} {r[:60]}")
    v, r = outcome(lambda: kit.validate(kit.spend_action(pool, "forge_action_dao_fee", [H, 20],
                                                         extra_spends=[daosuite.dao_coin_spend(pool, 20)])[0]))
    check("  the canonical dao_fee is accepted (control)", v == "accepted", r)


# ---- the build under test -------------------------------------------------------------------

def lf_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def build_matches_sources() -> tuple[bool, str]:
    """The shipped manifest must record the sources in the tree for the modules this file
    is about; a build of other sources pins nothing about these."""
    manifest_path = kit.V16_COMPILED / "manifest.json"
    if not manifest_path.is_file():
        return False, "compiled/manifest.json is absent"
    outputs = json.loads(manifest_path.read_text(encoding="utf-8"))["outputs"]
    for module in ("forge_action_remove", "forge_action_add", "forge_registry_register",
                   "forge_action_observe", "forge_action_dao_fee"):
        entry = outputs.get(f"{module}.rue.hex")
        if entry is None:
            return False, f"{module} is not in the manifest"
        if entry["source_sha256"] != lf_sha256(V16 / entry["source"]):
            return False, f"{entry['source']} differs from the source the build recorded"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expect", choices=("after", "before"), default="after",
                    help="which column this build must match (default after: the coinid derivation)")
    ap.add_argument("--before", metavar="DIR", help="also run, in a subprocess, against this older compiled dir")
    args = ap.parse_args()

    if not (kit.v16_available() and kit.registry_available()):
        print("  [skip] V16 build outputs are absent; run scripts/build-v16.py")
        return 2
    if args.expect == "after" and not os.environ.get("FORGE_V16_COMPILED"):
        ok, why = build_matches_sources()
        if not ok:
            print(f"  [skip] the build does not match the sources ({why}); run scripts/build-v16.py")
            return 2

    print(f"build: {kit.V16_COMPILED}  (expecting the '{args.expect}' column)")
    remove_lane(args.expect)
    register_lane(args.expect)
    canonical_state_lane(args.expect)
    print()
    print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- coinid derivations, '{args.expect}' column")
    rc = 1 if FAILED else 0

    if args.before:
        print()
        print(f"---- the same probes against the older build at {args.before} ----")
        env = {**os.environ, "FORGE_V16_COMPILED": args.before, "PYTHONIOENCODING": "utf-8"}
        out = subprocess.run([sys.executable, __file__, "--expect", "before"], env=env,
                             cwd=pathlib.Path(__file__).resolve().parent, capture_output=True, text=True)
        for line in (out.stdout + out.stderr).splitlines():
            print(f"  {line}")
        rc = rc or out.returncode
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
