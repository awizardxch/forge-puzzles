#!/usr/bin/env python3
"""Multi-pool bundles: no pool is ever the poorer for sharing a bundle (phase 5, the last lane).

A single-pool sequence cannot enrich the actor (`_test_v11_manipulation.py`). Across pools
the picture changes in one honest way: when two pools disagree on a price, a route that
crosses both collects the gap -- that is arbitrage, priced by the disagreement, and it is
exactly what the Balancer exists to collect first. So the pin here is not on the actor but
on the pools: for EVERY pool touched by a multi-pool bundle, the value behind one LP unit
(K^(1/sum w) / total_lp, K the weighted product of reserves) never falls. A pool can be
brought back toward the market's price, never drained below its own invariant, whatever
else sits in the bundle.

Two views:
  * the composer's lanes -- multihop, split, flow, wrap, revisit, routed deposit, zap --
    built and consensus-validated by forge_stdin, successor against predecessor per pool;
  * the mirrors -- random sequences of swaps, adds and removes across pools that share
    assets, hundreds of them, the same invariant checked after every action.

Exits 0 when every check passes, 1 otherwise, 2 when the V11 build is absent.
"""
from __future__ import annotations

import json
import random
import sys

sys.path.insert(0, ".")

import forge_math  # noqa: E402
import forge_stdin  # noqa: E402
import forge_v14_driver as drv  # noqa: E402
import forge_v14_offer as v14  # noqa: E402
import forge_v14_route as v14r  # noqa: E402
from _test_v14_offer_lane import ROUTER_PH, T_A, T_B, TRADER_PH, H, fabricate_offer, make  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from forge_offer import ZERO_32  # noqa: E402

T_C = bytes32(b"\xc3" * 32)
results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def value_key(reserves, weights, total_lp):
    """(K, L, sum w): value per LP = K^(1/sum w) / L, so compare K1*L0^s >= K0*L1^s exactly."""
    k = 1
    for r, w in zip(reserves, weights):
        k *= r ** w
    return k, total_lp, sum(weights)


def never_poorer(before, after, weights) -> bool:
    k0, l0, s = value_key(before[0], weights, before[1])
    k1, l1, _ = value_key(after[0], weights, after[1])
    return k1 * (l0 ** s) >= k0 * (l1 ** s)


def hx(asset):
    return (ZERO_32 if asset is None else asset).hex()


def snap(pool):
    return v14.pool_to_snapshot(pool)


def lane(label, payload, pools):
    """Build through the stdin lane (consensus-validated) and pin every pool's value per LP."""
    try:
        out = forge_stdin.build(json.loads(json.dumps(payload)))
    except Exception as exc:  # noqa: BLE001
        check(f"{label}: builds and validates", False, f"{type(exc).__name__}: {str(exc)[:100]}"); return None
    check(f"{label}: builds and validates", True, f"{len(out['pools'])} pool(s) touched")
    by_id = {p.launcher_id.hex(): p for p in pools}
    for succ in out["pools"]:
        pool = by_id[succ["launcher_id"]]
        after = ([int(x) for x in succ["state"]["reserves"]], int(succ["state"]["total_lp"]))
        check(f"  {label}: pool {succ['launcher_id'][:8]} value per LP did not fall", never_poorer((pool.state[0], pool.state[1]), after, pool.weights),
              f"{pool.state[0]} -> {after[0]}")
    return out


def main() -> int:
    if not drv.v14_available():
        print("  [skip] V11 puzzles not built"); return 2
    fee = {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}
    xa = make([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x91)
    ab = make([T_A, T_B], [800_000, 600_000], [1, 1], salt=0x92)
    xb = make([None, T_B], [8_000_000_000, 700_000], [1, 1], salt=0x93)          # disagrees with xa*ab: an arbitrage exists
    xa2 = make([None, T_A], [30_000_000_000, 1_400_000], [1, 1], salt=0x94)
    vault = make([T_A], [2_000_000], [1], salt=0x96)
    xlp = make([None, vault.lp_asset_id], [5_000_000_000, 400_000], [1, 1], salt=0x97)
    triple = make([None, T_A, T_B], [20_000_000_000, 400_000, 300_000], [2, 1, 1], salt=0x98)

    print("the composer's lanes, every pool never poorer:")
    g = 200_000_000
    lane("multihop XCH -> A -> B", {"action": "multihop-swap", "offer": fabricate_offer({None: g}, {T_B: 1}, salt=0x71).to_bech32(),
                                    "pools": [snap(xa), snap(ab)], "path": [hx(None), hx(T_A), hx(T_B)], "current_height": H, "dev_fee": fee}, [xa, ab])
    lane("split XCH -> B two ways", {"action": "split-swap", "offer": fabricate_offer({None: g}, {T_B: 1}, salt=0x72).to_bech32(), "current_height": H, "dev_fee": fee,
                                     "branches": [{"pools": [snap(xa), snap(ab)], "path": [hx(None), hx(T_A), hx(T_B)], "amountIn": 1},
                                                  {"pools": [snap(xb)], "path": [hx(None), hx(T_B)], "amountIn": 1}]}, [xa, ab, xb])
    # the triangle: XCH -> A -> B -> XCH collects the disagreement between xb and xa*ab
    fa = v14r.simulate_chain([xa], [hx(None), hx(T_A)], g)[0]
    fb = v14r.simulate_chain([ab], [hx(T_A), hx(T_B)], fa)[0]
    out = lane("flow triangle XCH -> A -> B -> XCH", {"action": "flow-balance", "offer": fabricate_offer({None: g}, {None: 1}, salt=0x73).to_bech32(),
                                                      "current_height": H, "dev_fee": fee, "startAsset": hx(None),
                                                      "legs": [{"pool": snap(xa), "assetIn": hx(None), "assetOut": hx(T_A), "amountIn": g},
                                                               {"pool": snap(ab), "assetIn": hx(T_A), "assetOut": hx(T_B), "amountIn": fa},
                                                               {"pool": xb and snap(xb), "assetIn": hx(T_B), "assetOut": hx(None), "amountIn": fb}]}, [xa, ab, xb])
    if out:
        back = int(out["total_out"])
        print(f"          the triangle returned {back} for {g}: {'a gain, the disagreement collected' if back > g else 'a loss to fees'} -- and every pool kept its value per LP")
    offered = 300_000_000
    lane("wrap XCH -> A -> vault LP -> XCH", {"action": "multihop-swap", "offer": fabricate_offer({None: offered}, {None: 1}, salt=0x74).to_bech32(),
                                              "pools": [snap(xa), snap(vault), snap(xlp)], "path": [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None)],
                                              "current_height": H, "dev_fee": fee}, [xa, vault, xlp])
    lane("revisit XCH -> A -> LP -> XCH -> A", {"action": "multihop-swap", "offer": fabricate_offer({None: 400_000_000}, {T_A: 1}, salt=0x75).to_bech32(),
                                                "pools": [snap(xa), snap(vault), snap(xlp), snap(xa2)],
                                                "path": [hx(None), hx(T_A), hx(vault.lp_asset_id), hx(None), hx(T_A)], "current_height": H, "dev_fee": fee},
         [xa, vault, xlp, xa2])
    dep_x, dep_a, sell_a = 2_000_000_000, 80_000, 30_000
    b_bought = v14r.simulate_chain([ab], [hx(T_A), hx(T_B)], sell_a)[0]
    mint = forge_math.invariant_lp_mint(triple.state[0], [dep_x, dep_a - sell_a, b_bought], triple.state[1], triple.fee_bps, triple.weights, version=10)
    lane("routed deposit with a sale through another pool", {"action": "routed-deposit", "offer": fabricate_offer({None: dep_x + mint, T_A: dep_a}, {triple.lp_asset_id: 1}, salt=0x76).to_bech32(),
                                                             "current_height": H, "pool": snap(triple), "dev_fee": fee,
                                                             "sales": [{"pools": [snap(ab)], "path": [hx(T_A), hx(T_B)], "amountIn": sell_a}]}, [triple, ab])
    lane("zap through the target", {"action": "routed-deposit", "offer": fabricate_offer({None: 3_000_000_000}, {xa.lp_asset_id: 1}, salt=0x77).to_bech32(),
                                    "current_height": H, "pool": snap(xa), "dev_fee": fee,
                                    "sales": [{"pools": [snap(xa)], "path": [hx(None), hx(T_A)], "amountIn": 1_000_000_000}]}, [xa])

    print("random cross-pool sequences on the mirrors:")
    rng = random.Random(0xA11CE)
    drops, sequences, actions = 0, 0, 0
    for _ in range(300):
        assets = list(range(rng.randint(3, 5)))                      # shared asset ids
        pools = []
        for _ in range(rng.randint(2, 4)):
            n = rng.randint(1, min(3, len(assets)))
            ids = sorted(rng.sample(assets, n))
            pools.append({"ids": ids, "r": [rng.randint(500_000, 50_000_000) for _ in ids], "w": [rng.randint(1, 4) for _ in ids],
                          "lp": rng.randint(300_000, 3_000_000), "fee": rng.choice([0, 5, 30, 100, 200]), "pfee": rng.choice([0, 5, 50])})
        for _ in range(rng.randint(3, 8)):
            p = rng.choice(pools)
            k0, l0, s = value_key(p["r"], p["w"], p["lp"])
            kind = rng.choice(["swap", "swap", "swap", "add", "remove"]) if len(p["ids"]) > 1 else rng.choice(["add", "remove"])
            if kind == "swap":
                i, j = rng.sample(range(len(p["ids"])), 2)
                gross = max(1, p["r"][i] * rng.randint(1, 40) // 100)
                honest = forge_math.swap_output(p["r"][i], p["r"][j], gross, p["fee"], p["w"][i], p["w"][j])
                p["r"][i] += gross; p["r"][j] -= honest
            elif kind == "add":
                deposits = [max(0, p["r"][i] * rng.randint(0, 25) // 100) for i in range(len(p["ids"]))]
                if sum(deposits) == 0:
                    continue
                minted = forge_math.invariant_lp_mint(p["r"], deposits, p["lp"], p["fee"], p["w"], version=10)
                p["r"] = [a + d for a, d in zip(p["r"], deposits)]; p["lp"] += minted
            else:
                burn = max(1, p["lp"] * rng.randint(5, 60) // 100)
                if burn >= p["lp"]:
                    continue
                vf = forge_math.vault_fee_bps(len(p["r"]), 10, p["fee"])
                payouts = forge_math.withdrawal_amounts(p["r"], burn, p["lp"], vf)
                p["r"] = [a - x for a, x in zip(p["r"], payouts)]; p["lp"] -= burn
            actions += 1
            k1, l1, _ = value_key(p["r"], p["w"], p["lp"])
            if k1 * l0 ** s < k0 * l1 ** s:
                drops += 1
        sequences += 1
    check(f"{sequences} cross-pool sequences, {actions} actions: no pool's value per LP ever fell", drops == 0, f"drops {drops}")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} multi-pool checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
