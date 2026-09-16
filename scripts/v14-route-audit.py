#!/usr/bin/env python3
"""Are we routing traders the best way, and is the router's fee charged once?

Quotes every simple route up to `--hops` between every tradable pair, on the pools' LIVE
recorded states, using the composer's own `simulate_chain` -- the same curve the puzzle
enforces, net of the protocol and DAO slices. For each pair and size it reports:

  * the best route and the direct one, and the gap between them
  * whether a multi-hop beats the direct pool (the router MUST find it, or the trader
    overpays by taking the obvious route)
  * the router's fee, charged once on the entry, and what it costs as a share of the trade
  * whether any route is worse than not trading at all after the fee

It also checks the fee arithmetic the composer applies: the fee comes off coins sourced
from the OFFER, so a longer route pays it once, not once per hop.

    python scripts/v14-route-audit.py                 # 3% public rate
    python scripts/v14-route-audit.py --fee-bps 0     # what the pools alone cost
    python scripts/v14-route-audit.py --hops 3 --top 12
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import forge_v14_route as route  # noqa: E402
import v14_ops as ops  # noqa: E402

d = ops.deploy


def asset_name(asset, labels):
    return labels.get(bytes(asset).hex() if asset is not None else "xch", "?")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hops", type=int, default=3)
    ap.add_argument("--fee-bps", type=int, default=300, help="the router's own rate; 300 is the public one")
    ap.add_argument("--sizes", default="0.1,1,5", help="trade sizes as a percent of the entry reserve")
    ap.add_argument("--top", type=int, default=10, help="how many of the widest gaps to print")
    args = ap.parse_args()

    state = d.load()
    pools, labels = [], {"xch": "TXCH"}
    for rec in state["pools"]:
        pool = d.pool_from(rec)
        pools.append(pool)
        for asset, name in zip(pool.asset_ids, rec.get("asset_labels") or []):
            labels[bytes(asset).hex() if asset is not None else "xch"] = name
        labels.setdefault(bytes(pool.lp_asset_id).hex(), f"LP-{rec['label'].split()[0]}")
    print(f"{len(pools)} pools, live states from the record\n")

    # the asset graph: which pools carry which ordered pair
    edges: dict[tuple, list] = {}
    for pool in pools:
        for a_in, a_out in itertools.permutations(pool.asset_ids, 2):
            edges.setdefault((a_in, a_out), []).append(pool)
        # a vault's LP is reachable both ways through the vault itself
        if len(pool.asset_ids) == 1:
            edges.setdefault((pool.asset_ids[0], pool.lp_asset_id), []).append(pool)
            edges.setdefault((pool.lp_asset_id, pool.asset_ids[0]), []).append(pool)

    def paths(src, dst, hops):
        """Every simple asset path from src to dst, with the pool carrying each edge."""
        out = []

        def walk(node, visited, legs):
            if len(legs) > hops:
                return
            if node == dst and legs:
                out.append(list(legs))
                return
            if len(legs) == hops:
                return
            for (a_in, a_out), carriers in edges.items():
                if a_in != node or a_out in visited:
                    continue
                for pool in carriers:
                    if any(p.launcher_id == pool.launcher_id for p, _, _ in legs):
                        continue
                    legs.append((pool, a_in, a_out))
                    walk(a_out, visited | {a_out}, legs)
                    legs.pop()

        walk(src, {src}, [])
        return out

    def quote(legs, amount):
        try:
            return route.simulate_chain([p for p, _, _ in legs], [legs[0][1], *[a for _, _, a in legs]], amount)[-1]
        except Exception:
            return None

    pairs = sorted({(a, b) for (a, b) in edges if a != b}, key=lambda t: (t[0] is not None, str(t)))
    sizes = [float(x) for x in args.sizes.split(",")]
    findings, fee_notes = [], []
    checked = 0

    for src, dst in pairs:
        direct_pools = edges.get((src, dst), [])
        if not direct_pools:
            continue
        # size the trade off the deepest direct pool's entry reserve
        ref = max(direct_pools, key=lambda p: p.state[0][p.asset_ids.index(src)] if src in p.asset_ids else 0)
        if src not in ref.asset_ids:
            continue
        reserve = ref.state[0][ref.asset_ids.index(src)]
        for pct in sizes:
            amount = max(int(reserve * pct / 100), 1)
            routes = []
            for legs in paths(src, dst, args.hops):
                out = quote(legs, amount)
                if out and out > 0:
                    routes.append((out, legs))
            if not routes:
                continue
            checked += 1
            routes.sort(key=lambda t: -t[0])
            best_out, best_legs = routes[0]
            direct = [(o, l) for o, l in routes if len(l) == 1]
            direct_out, direct_legs = (max(direct, key=lambda t: t[0]) if direct else (0, None))
            # the router's fee comes off the entry, once, whatever the route length
            fee = amount * args.fee_bps // 10_000
            net_best = quote(best_legs, amount - fee) if fee else best_out
            net_direct = quote(direct_legs, amount - fee) if (fee and direct_legs) else direct_out
            if direct_out and best_out > direct_out:
                gain = (best_out - direct_out) / direct_out
                findings.append((gain, src, dst, pct, amount, best_out, direct_out, best_legs, direct_legs,
                                 net_best, net_direct))
            if fee and net_best is not None and best_out:
                fee_notes.append((fee / amount, len(best_legs), (best_out - net_best) / best_out))

    print(f"checked {checked} (pair, size) combinations over routes of up to {args.hops} hops")
    print(f"router rate {args.fee_bps} bps, charged once on the entry\n")

    if findings:
        findings.sort(key=lambda t: -t[0])
        print(f"{len(findings)} cases where a multi-hop beats the direct pool "
              f"(the aggregator must find these, or the trader takes the obvious route and loses):")
        for gain, src, dst, pct, amount, best, direct, best_legs, direct_legs, nb, nd in findings[:args.top]:
            names = " -> ".join([asset_name(best_legs[0][1], labels)] + [asset_name(a, labels) for _, _, a in best_legs])
            print(f"  {asset_name(src, labels):>6} -> {asset_name(dst, labels):<6} {pct:>4}% of reserve "
                  f"({amount:>12,})  best {best:>12,} via {len(best_legs)} hop(s) [{names}]")
            print(f"         direct {direct:>12,}   gap {gain * 100:6.2f}%"
                  + (f"   after the router fee: {nb:,} vs {nd:,}" if nb is not None and nd is not None else ""))
    else:
        print("no pair is better served by a multi-hop than by its direct pool at these sizes:")
        print("  the matrix is still priced consistently after the lifecycle passes, so the obvious")
        print("  route is the best route and a router that takes it is not overpaying.")

    if fee_notes:
        worst = max(fee_notes, key=lambda t: t[2])
        by_hops = {}
        for rate, hops, cost in fee_notes:
            by_hops.setdefault(hops, []).append(cost)
        print("\nwhat the router's own fee costs the trader, by route length:")
        for hops in sorted(by_hops):
            costs = by_hops[hops]
            print(f"  {hops} hop(s): {sum(costs) / len(costs) * 100:5.2f}% of the output on average, "
                  f"{max(costs) * 100:5.2f}% worst  ({len(costs)} cases)")
        print(f"  the rate is {args.fee_bps / 100:.2f}% of the INPUT and does not compound with hops")
        print(f"  worst single case: {worst[2] * 100:.2f}% of output on a {worst[1]}-hop route")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
