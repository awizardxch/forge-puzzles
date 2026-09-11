#!/usr/bin/env python3
"""Every pool shape in the launch matrix, built at the shipping revision.

docs/FORGE_LAUNCH_MATRIX.md lists the pools to create on testnet for V10. A
matrix nobody can build is a to-do list, not a plan -- so each row is deployed
here through the real creation path before anyone spends real coins on it.

Two shapes are worth calling out because nothing else covered them:

  * a **native XCH reserve**. The single XCH settlement in a creation offer does
    double duty -- it backs the genesis LP mint AND seeds the XCH reserve -- and
    the testkit assumed every reserve was a CAT, so no V10 suite could build a
    pool holding XCH. Nearly every real pool holds XCH.

  * a **weighted N-asset pool**. Weighted pools were covered at two assets and
    N-asset pools at equal weights; the corner where both are true was not, and
    that is exactly where the weight-blind maths of findings 5, 7 and 8 lived.
"""
import sys

sys.path.insert(0, ".")

from chia_rs.sized_bytes import bytes32

from _forge_testkit import make_pool
from forge_offer import ZERO_32

# The testnet assets these pools are built from. Real ids, so the matrix in the
# doc and the shapes proved here are the same thing.
ASSET = {
    "TXCH": ZERO_32,
    "T6":   bytes32.fromhex("969289f721bed5723aa03c73efd7886c204e50ff4e80a8eacfb352b74e7305a7"),
    "T11":  bytes32.fromhex("1341d936632cdc5bf3d6675cfcade6f3c75f51cfc09f19b1e53b96e4c364c653"),
    "t8":   bytes32.fromhex("7f4c27d7ca229468059d6b74827dfd037482d51d55077823ec2c45845ea888fb"),
    "A1":   bytes32.fromhex("32bcad65145bc2301a6c4b19e3df8082a9accecfc06be0c0a0939c690b399443"),
    "A4":   bytes32.fromhex("38ae3d2bfac9f42cab1a10269aa95ff821022c0d99d9b1adc3c88cd0604ae610"),
    "A5":   bytes32.fromhex("c3b49d131c8989fbc7f9e0081eed8c2c2914ab0e78176a65ddf0b2d998724695"),
}

# id, assets, weights, swap fee bps, protocol fee bps, what the row proves.
# "@LP1"/"@LP2" are the LP CATs of vaults B1 and F1, only known once those
# exist -- the same ordering the live deployment has to follow.
ROWS = [
    ("A1", ["TXCH", "T6"],   [1, 1], 30,  5,   "native XCH reserve, the baseline pair"),
    ("A2", ["TXCH", "T11"],  [1, 1], 30,  5,   "a second XCH pair, so multi-hop has a route"),
    ("A3", ["T6", "T11"],    [1, 1], 30,  5,   "all-CAT pair, closing the triangle"),
    ("A4", ["TXCH", "t8"],   [1, 1], 30,  5,   "third XCH pair"),
    ("B1", ["t8"],           [1],    30,  0,   "a vault: one asset, cannot swap"),
    ("B2", ["TXCH", "@LP1"], [1, 1], 30,  5,   "a pool holding a vault's LP as a reserve"),
    ("B3", ["t8", "@LP1"],   [1, 1], 30,  5,   "LP against its own underlying"),
    ("C1", ["TXCH", "T6"],   [4, 1], 30,  5,   "80/20 weighted"),
    ("C2", ["TXCH", "t8"],   [3, 1], 30,  5,   "75/25 weighted"),
    ("D1", ["TXCH", "T6", "T11"],             [1, 1, 1],       30, 5, "3 assets, equal"),
    ("D2", ["TXCH", "T6", "T11", "t8", "A1"], [1, 1, 1, 1, 1], 30, 5, "5 assets"),
    ("D3", ["TXCH", "T6", "T11"],             [4, 1, 1],       30, 5, "3 assets AND weighted"),
    ("E1", ["TXCH", "A4"],   [1, 1], 0,   0,   "no fee at all"),
    ("E2", ["TXCH", "A5"],   [1, 1], 200, 100, "both caps at once"),
    ("F1", ["T6"],           [1],    30,  0,   "a second vault"),
    ("F2", ["TXCH", "@LP2"], [1, 1], 30,  5,   "reaches the second vault"),
]

BOOT = 2_000_000

# The puzzle's own limits, mirrored from pool_singleton_FORGE.rue. A row outside
# these is rejected at creation, so the matrix must stay inside them.
MAX_WEIGHT_UNITS = 8
MAX_TOTAL_WEIGHT = 20
MAX_FEE_BPS = 200
MAX_PROTOCOL_FEE_BPS = 100
MAX_ASSETS = 10


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    results = []

    # The vaults first: two later rows hold their LP, and an LP asset id does
    # not exist until the vault that issues it does.
    vault_t8 = make_pool([ASSET["t8"]], {ASSET["t8"]: BOOT}, 0x51, protocol_bps=0)
    vault_t6 = make_pool([ASSET["T6"]], {ASSET["T6"]: BOOT}, 0x52, protocol_bps=0)
    lp = {"@LP1": bytes32(vault_t8.lp_asset_id), "@LP2": bytes32(vault_t6.lp_asset_id)}

    print("every launch-matrix row, deployed through the real creation path:")
    built = {}
    for salt, (pool_id, names, weights, fee, proto, why) in enumerate(ROWS, start=0x60):
        ids = [ASSET.get(n) or lp[n] for n in names]
        order = sorted(range(len(ids)), key=lambda i: ids[i])
        assets = [ids[i] for i in order]
        sorted_weights = [weights[i] for i in order]
        sorted_names = [names[i] for i in order]

        within = (len(assets) <= MAX_ASSETS
                  and all(1 <= w <= MAX_WEIGHT_UNITS for w in sorted_weights)
                  and sum(sorted_weights) <= MAX_TOTAL_WEIGHT
                  and fee <= MAX_FEE_BPS and proto <= MAX_PROTOCOL_FEE_BPS)
        if not within:
            results.append(check(f"{pool_id} is inside the puzzle's limits", False))
            continue

        try:
            pool = make_pool(assets, {a: BOOT for a in assets}, salt,
                             protocol_bps=proto, fee_bps=fee, weights=sorted_weights)
            built[pool_id] = pool
            total = sum(sorted_weights)
            shape = " / ".join(f"{n} {w * 100 // total}%"
                               for n, w in zip(sorted_names, sorted_weights))
            results.append(check(f"{pool_id}  {shape}", True,
                                 f"fee {fee}bps proto {proto}bps -- {why}"))
        except Exception as exc:
            results.append(check(f"{pool_id}  {'/'.join(sorted_names)}: "
                                 f"{type(exc).__name__}: {str(exc)[:70]}", False))

    print()
    print("the properties the matrix exists to cover:")

    # Each of these is a shape, not a row: if the matrix is ever trimmed, these
    # say which coverage would be lost with it.
    def holds(pool, predicate):
        return pool is not None and predicate(pool)

    results.append(check("some pool holds a native XCH reserve",
                         any(ZERO_32 in [bytes32(a) for a in p.config[2]]
                             for p in built.values())))
    results.append(check("some pool holds no XCH at all",
                         any(ZERO_32 not in [bytes32(a) for a in p.config[2]]
                             for p in built.values())))
    results.append(check("some pool is a vault (one asset, cannot swap)",
                         any(len(p.config[2]) == 1 for p in built.values())))
    results.append(check("some pool holds another pool's LP as a reserve",
                         any(lp["@LP1"] in [bytes32(a) for a in p.config[2]]
                             or lp["@LP2"] in [bytes32(a) for a in p.config[2]]
                             for p in built.values())))
    results.append(check("some pool has unequal weights",
                         any(len(set(int(w) for w in p.config[3])) > 1
                             for p in built.values())))
    results.append(check("some pool is BOTH N-asset and weighted",
                         any(len(p.config[2]) > 2 and len(set(int(w) for w in p.config[3])) > 1
                             for p in built.values()),
                         "the corner the weight-blind maths lived in"))
    results.append(check("some pool charges no fee whatsoever",
                         any(int(p.config[4]) == 0 and int(p.config[5]) == 0
                             for p in built.values())))
    results.append(check("some pool sits on both fee caps",
                         any(int(p.config[4]) == MAX_FEE_BPS
                             and int(p.config[5]) == MAX_PROTOCOL_FEE_BPS
                             for p in built.values())))
    results.append(check("two pools quote the same pair differently",
                         holds(built.get("A1"), lambda a: built.get("C1") is not None
                               and list(a.config[3]) != list(built["C1"].config[3])),
                         "A1 vs C1 -- what the Balancer needs to have anything to do"))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} launch-matrix checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
