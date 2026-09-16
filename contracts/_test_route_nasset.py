#!/usr/bin/env python3
"""Split and multi-hop routes that pass through an N-asset (V6) pool.

The route only trades two of the pool's assets, so the rest are untouched. Every
reserve still has to be named in the plan with a zero settlement id, and nothing
may be paid out of an untouched one. Bundles are audited the way the node would:
announcements satisfied, every spent coin created in-bundle or pre-existing, and
no coin spent twice.
"""
import io
import json
import sys

sys.path.insert(0, ".")

from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin as fs
from _forge_testkit import USER_PH, audit, cat_maker_spend, make_offer, xch_maker_spend
from forge_offer import ZERO_32
from forge_multihop_swap import build_multihop_swap
from forge_split_swap import SplitBranchSpec, build_split_swap

NONCE = bytes32.fromhex("ee" * 32)
T11 = bytes32.fromhex("1341d936632cdc5bf3d6675cfcade6f3c75f51cfc09f19b1e53b96e4c364c653")
T6 = bytes32.fromhex("969289f721bed5723aa03c73efd7886c204e50ff4e80a8eacfb352b74e7305a7")


def load_pools() -> dict:
    """Pools from the live deployment index.

    Every suite built on this tests whatever revision happens to be deployed, NOT
    the shipping puzzles -- which is how a broken vault redemption survived in V9
    and V10 while these all passed against V7 pools. Coverage of the current
    revision belongs in the _test_forge_* suites, which build their pools.

    With the superseded pools retired the index is empty, so these skip rather
    than fail. A skip is not a pass: it means the case was not exercised.
    """
    index = json.load(io.open("../.awizard/deployment-index.json", encoding="utf-8"))
    found = {}
    for entry in index.values():
        for batch in (entry.get("batches") or {}).values():
            snapshot = batch.get("poolSnapshot")
            # V11 snapshots are not V3Pool snapshots (the action layer replaced
            # the pool puzzle); these lanes are V10's, so they skip past them.
            if snapshot and int(snapshot.get("protocol_version") or 0) <= 10:
                found[snapshot["launcher_id"][:8]] = fs._pool(snapshot)
    if not found:
        skip_missing()
    return found


def skip_missing() -> None:
    """Exit as skipped, explaining that these suites test deployed pools."""
    print("SKIP: the pools these suites were written against are not in the")
    print("      deployment index -- superseded revisions were retired.")
    print("      Current-revision coverage: _test_forge_routing.py,")
    print("      _test_forge_cycles.py, _test_forge_transition.py,")
    print("      _test_forge_isolation_audit.py.")
    raise SystemExit(2)   # 2 == skipped; a skip must never read as a pass


def require_pools(pools: dict, *prefixes: str) -> tuple:
    """The named pools, or a skip if the index no longer carries them.

    An empty index is not the only way these suites lose their subject. Creating
    an unrelated pool refills the index without bringing back the specific
    launchers they name, which turned the skip into a KeyError -- a red suite
    that says nothing about the code it was meant to cover.
    """
    missing = [prefix for prefix in prefixes if prefix not in pools]
    if missing:
        print(f"SKIP: deployment index has {sorted(pools)} but this suite needs {list(prefixes)};")
        print(f"      missing {missing}.")
        skip_missing()
    return tuple(pools[prefix] for prefix in prefixes)


def external_for(pools, maker_spends) -> set:
    """Coins that legitimately pre-exist: pool coins, reserves, maker inputs."""
    ids = set()
    for pool in pools:
        ids.add(bytes(pool.pool.coin.name()))
        for reserve in pool.reserves.values():
            ids.add(bytes(reserve.coin.name()))
    for spend in maker_spends:
        ids.add(bytes(spend.coin.name()))
    return ids


def report(label, build, pools, maker_spends, extra=""):
    try:
        result = build()
    except Exception as exc:
        print(f"  [FAIL] {label}: {type(exc).__name__}: {str(exc)[:90]}")
        return False
    problems, checked = audit(result.bundle, external_for(pools, maker_spends))
    ok = not problems
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {len(result.bundle.coin_spends)} spends, "
          f"{checked} assertions{extra}")
    for problem in problems:
        print(f"         - {problem}")
    return ok


def main() -> int:
    pools = load_pools()
    missing = [k for k in ("86c3cd1a", "86d0f02b", "d666d3fe") if k not in pools]
    if missing:
        print(f"missing pool snapshots: {missing}")
        return 2
    v5, v6, v4 = require_pools(pools, "86c3cd1a", "86d0f02b", "d666d3fe")

    results = []

    # ---- split with a multi-hop branch crossing the 3-asset pool ------------
    print("split: 60% TXCH->T11 (V5), 40% TXCH->T6->T11 (V6 then V4):")
    branches = [
        SplitBranchSpec(pools=[v5], path=[ZERO_32, T11], amount_in=600_000_000_000),
        SplitBranchSpec(pools=[v6, v4], path=[ZERO_32, T6, T11], amount_in=400_000_000_000),
    ]
    # Ask the route what it yields rather than hardcoding: these run against the
    # live snapshot, so every real trade moves the answer.
    probe = build_split_swap(branches, make_offer(
        xch_maker_spend(1_000_000_000_000, 0x91),
        {T11: [NotarizedPayment(USER_PH, uint64(1), [], NONCE)]}))
    spends = xch_maker_spend(1_000_000_000_000, 0x91)
    offer = make_offer(spends, {T11: [NotarizedPayment(USER_PH, uint64(probe.total_out), [], NONCE)]})
    results.append(report("split 1-hop + 2-hop", lambda: build_split_swap(branches, offer),
                          [v5, v6, v4], spends))

    # ---- plain multi-hop through the 3-asset pool ---------------------------
    print()
    print("multi-hop: TXCH -> T6 (V6) -> T11 (V4):")
    probe2 = build_multihop_swap([v6, v4], [ZERO_32, T6, T11], make_offer(
        xch_maker_spend(400_000_000_000, 0x92),
        {T11: [NotarizedPayment(USER_PH, uint64(1), [], NONCE)]}))
    spends2 = xch_maker_spend(400_000_000_000, 0x92)
    offer2 = make_offer(spends2, {T11: [NotarizedPayment(USER_PH, uint64(probe2.amounts[-1]), [], NONCE)]})
    results.append(report("multihop via V6", lambda: build_multihop_swap(
        [v6, v4], [ZERO_32, T6, T11], offer2), [v6, v4], spends2))

    # ---- multi-hop entering on a CAT and exiting native ---------------------
    print()
    print("multi-hop: T6 -> T11 (V4) -> TXCH (V5):")
    spends3 = cat_maker_spend(T6, 500, 0x93)
    offer3 = make_offer(spends3, {None: [NotarizedPayment(USER_PH, uint64(1), [], NONCE)]})  # dust minimum: XCH out is large
    results.append(report("multihop CAT in, native out", lambda: build_multihop_swap(
        [v4, v5], [T6, T11, ZERO_32], offer3), [v4, v5], spends3))

    # ---- the untouched reserve must be frozen, not merely present ----------
    print()
    print("untouched V6 reserve is preserved:")
    before = {bytes(a): int(r[2]) for a, r in zip(
        [bytes32(x) for x in v6.config[2]], v6.state[0])}
    spends4 = xch_maker_spend(400_000_000_000, 0x94)
    offer4 = make_offer(spends4, {T11: [NotarizedPayment(USER_PH, uint64(300), [], NONCE)]})
    result4 = build_multihop_swap([v6, v4], [ZERO_32, T6, T11], offer4)
    v6_next = next(p for p in result4.pools if p.launcher_id == v6.launcher_id)
    after = {bytes(a): int(r[2]) for a, r in zip(
        [bytes32(x) for x in v6_next.config[2]], v6_next.state[0])}
    frozen = after[bytes(T11)] == before[bytes(T11)]
    moved_in = after[bytes(ZERO_32)] > before[bytes(ZERO_32)]
    moved_out = after[bytes(T6)] < before[bytes(T6)]
    print(f"  [{'PASS' if frozen else 'FAIL'}] T11 unchanged: {before[bytes(T11)]} -> {after[bytes(T11)]}")
    print(f"  [{'PASS' if moved_in else 'FAIL'}] TXCH grew:    {before[bytes(ZERO_32)]} -> {after[bytes(ZERO_32)]}")
    print(f"  [{'PASS' if moved_out else 'FAIL'}] T6 shrank:    {before[bytes(T6)]} -> {after[bytes(T6)]}")
    results.append(frozen and moved_in and moved_out)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} route checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
