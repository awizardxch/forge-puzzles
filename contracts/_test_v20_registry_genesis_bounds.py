#!/usr/bin/env python3
"""V20 (protocol 21; external review 2026-11-16, finding 09): the genesis supply is bounded by the
reserves it stands on.

V18's `valid_pool` bounded total_lp only from below (MIN_GENESIS_LP = 10^6) and required every
reserve positive. V20 keeps both and adds (owner decision 2026-11-17):

  * total_lp <= MAX_GENESIS_LP_PER_MOJO (10^6) * min(reserves), for every pool (a vault: lpRatio <= 10^6);
  * total_lp <= reserve_xch when XCH (asset id zero) is an asset (an XCH vault: lpRatio <= 1).

No per-asset minimum reserve beyond > 0. Every pool here registers on an empty registry between
the two sentinels -- its OWN neighbors -- so only valid_pool can refuse it (the V18 mutation-rule
pattern in _test_v18_registry.py). The shapes of all 52 live V18 twins (2026-11-16) still register,
and the creation lane refuses an out-of-bounds genesis before anything is signed.

Exits 0 when every check passes, 1 otherwise, 2 when the V20 build is absent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import _v20_testkit as kit  # noqa: E402
import forge_v20_create as cre  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

from _test_v20_registry import registration  # noqa: E402

results: list[bool] = []
LIVE = Path(__file__).resolve().parent.parent / "api" / "__checks__" / "helpers" / "live-pools-2026-11-16.json"
PER_MOJO = 10 ** 6


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def cat(i: int) -> bytes32:
    return bytes32(bytes([0x10 + i % 0xE0]) * 31 + bytes([i // 0xE0 + 1]))


def main() -> int:
    if not (kit.v20_available() and kit.registry_available()):
        print("  [skip] V20 build outputs are absent; run scripts/build-v20.py")
        return 2
    check("the driver mirrors the bound (MAX_GENESIS_LP_PER_MOJO = MIN_GENESIS_LP = 10^6)",
          kit.MAX_GENESIS_LP_PER_MOJO == kit.MIN_GENESIS_LP == PER_MOJO)

    reg0 = kit.make_registry(salt=0x2C)
    kit.validate(kit.registry_spend(reg0, "forge_registry_init", [])[0])
    reg1 = reg0.advance([1, 0, 1_000_000])
    sentinels = ((kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY))
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    salt = [0x40]

    def pool(assets, reserves, total_lp, **kw):
        salt[0] += 1
        return kit.make_pool(assets, reserves, total_lp=total_lp, leaves="forge", salt=salt[0] % 256, **kw)

    def registers(p):
        bundle, _ = registration(reg1, p, *sentinels, slots)
        kit.validate(bundle)
        return True

    def accepted(label, p):
        try:
            registers(p)
            return check(label, True)
        except Exception as exc:  # noqa: BLE001
            return check(label, False, f"{type(exc).__name__}: {str(exc)[:80]}")

    def refused(label, p):
        try:
            registers(p)
            return check(label, False, "ACCEPTED")
        except Exception as exc:  # noqa: BLE001
            return check(label, True, f"{type(exc).__name__}: {str(exc)[:60]}")

    C1, C2 = cat(1), cat(2)
    print("refused by valid_pool (each bracketed by its own neighbors):")
    refused("XCH/CAT reserves [1, 1], total_lp = 10^6 + 1", pool([None, C1], [1, 1], PER_MOJO + 1))
    refused("XCH/CAT reserves [1, 1], total_lp = 10^9 (the affordable squat)", pool([None, C1], [1, 1], 10 ** 9))
    refused("XCH/CAT reserves [1, 1], total_lp = 2^63 (the report's case)", pool([None, C1], [1, 1], 2 ** 63))
    # Each conjunct alone: a pool only the per-mojo bound refuses, and one only xch_backed refuses.
    refused("CAT/CAT [1, 1], total_lp = 10^6 + 1 (no XCH: only the per-mojo bound applies)",
            pool([C1, C2], [1, 1], PER_MOJO + 1))
    refused("CAT/CAT [3, 10^9], total_lp = 3 x 10^6 + 1 (the scarcest reserve sets the bound)",
            pool([C1, C2], [3, 10 ** 9], 3 * PER_MOJO + 1))
    refused("XCH/CAT [999,999, 10^6], total_lp = 10^6 (per-mojo bound holds; the XCH reserve is below total_lp)",
            pool([None, C1], [999_999, PER_MOJO], PER_MOJO))
    refused("a CAT vault with lpRatio above 10^6 ([5], total_lp 5 x 10^6 + 1)", pool([C1], [5], 5 * PER_MOJO + 1))
    refused("an XCH vault with lpRatio above 1 ([10^6], total_lp 10^6 + 1)", pool([None], [PER_MOJO], PER_MOJO + 1))
    refused("an XCH vault at the floor over too little XCH ([999,999], total_lp 10^6)", pool([None], [999_999], PER_MOJO))

    print("accepted at the boundary:")
    accepted("CAT/CAT [1, 1], total_lp = 10^6 exactly (10^6 x min)", pool([C1, C2], [1, 1], PER_MOJO))
    accepted("CAT/CAT [7, 10^12], total_lp = 7 x 10^6 exactly", pool([C1, C2], [7, 10 ** 12], 7 * PER_MOJO))
    accepted("XCH/CAT [10^6, 1], total_lp = 10^6 (both bounds exactly)", pool([None, C1], [PER_MOJO, 1], PER_MOJO))
    accepted("XCH/CAT [5 x 10^6, 9], total_lp = reserve_xch exactly", pool([None, C1], [5 * PER_MOJO, 9], 5 * PER_MOJO))
    accepted("an XCH vault at lpRatio 1 ([10^6], total_lp 10^6)", pool([None], [PER_MOJO], PER_MOJO))
    accepted("an XCH vault below lpRatio 1 ([10^9], total_lp 10^6)", pool([None], [10 ** 9], PER_MOJO))
    accepted("a CAT vault at lpRatio 10^6 ([1], total_lp 10^6)", pool([C1], [1], PER_MOJO))
    accepted("a weighted 4/1 pool and a 3-asset pool inside the bounds",
             pool([None, C1], [4 * 10 ** 9, 10 ** 6], 10 ** 9, weights=[4, 1]))
    accepted("  (3-asset XCH/CAT/CAT [10^9, 2, 10^6], total_lp 2 x 10^6)", pool([None, C1, C2], [10 ** 9, 2, PER_MOJO], 2 * PER_MOJO))
    accepted("an rCAT pool inside the bounds", pool([None, C1], [10 ** 9, 10 ** 6], 10 ** 6,
                                                     hidden_puzzle_hashes=[kit.ZERO_32, bytes32(b"\x4d" * 32)]))

    print("the shapes of the 52 live V18 twins (2026-11-16) register on V20:")
    live = [row for row in json.loads(LIVE.read_text(encoding="utf-8")) if int(row[0]) == 19]
    check("  the fixture holds 52 V18 pools", len(live) == 52, str(len(live)))
    ok, worst, failed = 0, 0.0, []
    for k, (_, kinds, reserves, total) in enumerate(live):
        kinds = kinds.split(",")
        reserves = [int(x) for x in reserves.split(",")]
        total = int(total)
        assets = [None if kind == "xch" else cat(16 + 8 * k + j) for j, kind in enumerate(kinds)]
        worst = max(worst, total / min(reserves))
        why = kit.genesis_bound_reason(assets, reserves, total)
        try:
            registers(pool(assets, reserves, total))
            ok += why is None
            if why is not None:
                failed.append((k, "mirror disagrees: " + why))
        except Exception as exc:  # noqa: BLE001
            failed.append((k, f"{kinds} {reserves} {total}: {type(exc).__name__}: {str(exc)[:60]}"))
    check(f"  all 52 register, and the driver's genesis_bound_reason agrees (largest total_lp / min reserve {worst:,.0f})",
          ok == 52 and not failed, f"{ok}/52 {failed[:2]}")
    nine_two = [row for row in live if row[2].split(",")[-1] == "2" or row[2].split(",")[0] == "2"]
    check("  the [9, 2] / 10^6 twin is among them", any(r[3] == str(10 ** 6) for r in nine_two), str(nine_two[:1]))

    print("the creation lane refuses before signing (forge_v20_create.plan):")

    def plan_refusal(assets, reserves, total):
        config = cre.CreationConfig(assets, reserves, [1] * len(assets), 30, 5, bytes32(b"\x7a" * 32), total)
        try:
            cre.plan(None, {}, config, None, {}, bytes32(b"\x56" * 32), 0)
        except cre.OfferRejected as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 -- got past the bound check into the coin handling
            return f"(passed the bound check: {type(exc).__name__})"
        return "(accepted)"
    msg = plan_refusal([None, C1], [1, 1], 10 ** 9)
    check("  [1, 1] / 10^9 is refused with the reason, naming the reserve it needs", "per mojo" in msg and "1,000" in msg, msg[:120])
    msg = plan_refusal([None, C1], [999_999, PER_MOJO], PER_MOJO)
    check("  an XCH reserve below total_lp is refused, asking for more XCH", "XCH reserve" in msg and "1,000,000" in msg, msg[:120])
    msg = plan_refusal([None, C1], [PER_MOJO, 1], PER_MOJO)
    check("  a genesis at both bounds passes the check (the next refusal, if any, is about the creator's coins)",
          msg.startswith("(passed") or "exceeds" not in msg, msg[:120])

    passed = sum(results)
    print(f"\n{passed}/{len(results)} V20 genesis-bound checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
