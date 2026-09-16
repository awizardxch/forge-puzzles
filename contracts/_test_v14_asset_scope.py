#!/usr/bin/env python3
"""Asset scope (spec 5): a reserve behind a revocation or fee layer is refused, by a test.

V14 reserves are plain CATs (or XCH) at p2_delegated_by_singleton. CHIP-0038 rCATs and
CHIP-0056 fee CATs wrap the inner puzzle in a layer the registry does not compute, so
their full puzzle hashes differ from what `register` rebuilds -- and the launcher's
announcement, which names the created inner hash, differs too. That refusal was an
accident of hash arithmetic; this suite makes it a test, so a future change that taught
the registry a layer would have to change a check on purpose.

The layer here is a stand-in: any wrapper around the reserve inner that hashes
differently plays the same role, and the registry knows none of them.

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs.sized_bytes import bytes32

import _v14_testkit as kit
import _test_v14_registry as regsuite

results = []
CAT = bytes32(b"\xd0" * 32)
RECIPIENT = bytes32(b"\x55" * 32)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def verdict(thunk) -> str:
    try:
        thunk()
        return "ACCEPTED"
    except kit.Rejected as exc:
        return "refused " + str(exc).split(": ")[-1].split(" ")[0]
    except Exception as exc:
        return f"refused locally ({type(exc).__name__})"


def layered(inner_hash: bytes32) -> bytes32:
    """A stand-in for a revocation or fee layer around the reserve inner: a wrapper whose
    hash commits to the inner and is not the inner."""
    return bytes32(kit.Program.to(1).curry(inner_hash).get_tree_hash())


def main() -> int:
    if not (kit.v14_available() and kit.registry_available()):
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py")
        return 2
    reg0 = kit.make_registry(salt=0x21)
    kit.validate(kit.registry_spend(reg0, "forge_registry_init", [])[0])
    reg = reg0.advance([1, 0])
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    left, right = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50)

    print("the plain reserve, as the registry computes it:")
    v = verdict(lambda: kit.validate(regsuite.registration(reg, pool, left, right, slots)[0]))
    check(f"a plain CAT reserve registers: {v}", v == "ACCEPTED")

    print("a reserve behind a layer the registry does not know:")
    wrapped_inner = layered(pool.reserves[1].inner_hash)
    check("the layered inner hashes differently from the plain inner", wrapped_inner != pool.reserves[1].inner_hash)
    # 1. The launcher creates the layered coin and says so: its announcement names a created
    #    puzzle hash the registry never computed.
    base = regsuite.registration(reg, pool, left, right, slots, with_reserves=False)[0]
    spends = [*base.coin_spends, *kit.reserve_launcher_spends(pool, created={1: wrapped_inner})]
    v = verdict(lambda: kit.validate(kit.SpendBundle(spends, kit.G2Element())))
    check(f"a launcher creating the CAT reserve behind a layer is {v} (12 = the announcement asserted is never made)",
          v == "refused 12")
    # 2. A pool whose finalizer is curried with the layered full hash: the pool coin's puzzle
    #    hash differs from the registry's recompute, so the singleton launcher's announcement
    #    is not the one `register` asserts.
    layered_full = bytes32(kit.construct_cat_puzzle(kit.CAT_MOD, CAT, kit.Program.to(1).curry(pool.reserves[1].inner)).get_tree_hash())
    twisted = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50,
                            reserve_full_hashes=[pool.reserves[0].full_hash, layered_full])
    check("  a pool built over a layered reserve has a different puzzle hash from the plain one",
          twisted.coin.puzzle_hash != pool.coin.puzzle_hash)
    v = verdict(lambda: kit.validate(regsuite.registration(reg, twisted, left, right, slots)[0]))
    check(f"  and cannot register: {v}", v.startswith("refused"))
    # 3. Nor can it stand in for the registered pool's own coin: the launcher announced the
    #    plain pool's hash, and `register` recomputes that one.
    v = verdict(lambda: kit.validate(regsuite.registration(reg, pool, left, right, slots, launcher_pool=twisted)[0]))
    check(f"  a launcher that minted the layered pool under the plain pool's registration: {v}", v.startswith("refused"))

    print("what the registry computes, stated:")
    check("reserve inners are p2_delegated_by_singleton(nonce = asset index) and nothing wraps them but the CAT layer",
          all(r.full_hash == (r.inner_hash if r.asset_id is None else
                              bytes32(kit.construct_cat_puzzle(kit.CAT_MOD, r.asset_id, r.inner).get_tree_hash()))
              for r in pool.reserves))
    check("the launcher hash is bare for XCH and CAT-wrapped for a CAT, with no third form",
          kit.reserve_launcher_full_hash(None) == kit.RESERVE_LAUNCHER_HASH
          and kit.reserve_launcher_full_hash(CAT) == bytes32(kit.construct_cat_puzzle(kit.CAT_MOD, CAT, kit.RESERVE_LAUNCHER).get_tree_hash()))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} asset-scope checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
