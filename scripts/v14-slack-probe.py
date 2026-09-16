#!/usr/bin/env python3
"""Two swaps naming ONE settlement: refused without slack, accepted with it.

Found on testnet11 on 2026-09-16, and this is the reproduction. Our documents said the
shape is "refused by conservation". Conservation is a property of the WHOLE BUNDLE, not
of an action, so it only refuses when the bundle has nothing spare. A network fee is
something spare: on H6 at 4,693,721 the second swap was funded out of a 5 XCH fee, the
fee actually paid came to 4,995,000,000, and the pool received full value for both swaps.

Nothing was stolen -- the trader overpaid -- but "refused by conservation" is true only
of a bundle with no slack, and that is worth pinning rather than asserting.

  A. zero network fee  -> the bundle has no slack, conservation refuses (MINTING_COIN)
  B. a fee of `--slack` -> the second swap is funded out of it and the bundle confirms

B is a real spend and moves the pool honestly (it pays for what it takes). A is a refused
push. Run A alone with --only a.

    python scripts/v14-slack-probe.py --label "H2 txch t8 8-1 v14" --gross 5000000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import forge_math  # noqa: E402
import v14_ops as ops  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

d, drv = ops.deploy, ops.drv


def build_double(wallet, record, gross, fee):
    """Two swaps in one spend, both naming the same settlement, with `fee` of slack."""
    pool = d.pool_from(record)
    r, w = pool.state[0], pool.weights
    out1 = forge_math.swap_output(r[0], r[1], gross, pool.fee_bps, w[0], w[1])
    after = [r[0] + gross, r[1] - out1]
    out2 = forge_math.swap_output(after[0], after[1], gross, pool.fee_bps, w[0], w[1])
    b = ops.Bundle(wallet)
    b.xch_needed = fee + gross                      # the ONLY slack is `fee`
    b.reserve_funding()
    b.xch_needed -= gross
    settlement = b.settlement_in(None, gross)
    for out in (out1, out2):
        pay = out - out * pool.protocol_fee_bps // 10_000 - out * pool.dao_fee_bps // 10_000
        b.payout_to_wallet(pool.reserves[1], pool.asset_ids[1], pay)
    b.finish_funding()
    h = d.peak()
    ref = drv.settlement_ref(settlement)
    bundle, new_state = drv.spend_actions(pool, [("forge_action_swap", [h, 0, 1, gross, out1, *ref]),
                                                 ("forge_action_swap", [h, 0, 1, gross, out2, *ref])],
                                          extra_spends=b.extra_spends, extra_cats=b.extra_cats)
    return pool, bundle, new_state, out1, out2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="H2 txch t8 8-1 v14")
    ap.add_argument("--gross", type=int, default=5_000_000)
    ap.add_argument("--slack", type=int, default=5_000_000_000)
    ap.add_argument("--only", default="ab")
    args = ap.parse_args()
    state = d.load()
    wallet = d.Wallet()
    record = d.find_pool(state, args.label)
    results = []

    if "a" in args.only:
        print("A. two swaps, one settlement, ZERO network fee: the bundle has no slack")
        pool, bundle, _, o1, o2 = build_double(wallet, record, args.gross, 0)
        try:
            drv.validate(bundle)
            print("   offline validator: ACCEPTED (unexpected)")
        except drv.Rejected as exc:
            print(f"   offline validator: refused, {exc}")
        verdict = d.rpc("push_tx", {"spend_bundle": wallet.sign(list(bundle.coin_spends))})
        err = (verdict.get("structuredError") or {}).get("data", {}).get("error") or verdict.get("error")
        status = verdict.get("status")
        ok = not verdict.get("success")
        print(f"   node: success={verdict.get('success')} status={status} error={err}")
        print(f"   [{'PASS' if ok else 'FAIL'}] with no slack the duplicate is refused")
        results.append(ok)
        wallet._used.clear()

    if "b" in args.only:
        print(f"B. the same pair with {args.slack:,} of network fee as slack")
        pool, bundle, new_state, o1, o2 = build_double(wallet, record, args.gross, args.slack)
        try:
            drv.validate(bundle)
            print("   offline validator: ACCEPTED -- the slack funds the second swap")
        except drv.Rejected as exc:
            print(f"   offline validator: refused, {exc}")
            return 1
        successor = Coin(pool.coin.name(), pool.successor_puzzle_hash(drv.state_to_list(new_state)), uint64(1))
        result = d.push_and_wait(wallet, bundle, successor.name(), f"double-settlement swap on {record['label']}")
        result.update({"gross": args.gross * 2, "out": o1 + o2, "h": result["height"], "note": "two swaps, one settlement, funded from the fee"})
        d.commit_pool(state, record, pool, new_state, result, "swap")
        after = d.pool_from(d.find_pool(state, args.label))
        grew = after.state[0][0] - pool.state[0][0]
        print(f"   confirmed at {result['height']}: reserve 0 grew {grew:+,} for a settlement of {args.gross:,}")
        print(f"   [{'PASS' if grew == args.gross * 2 else 'FAIL'}] the pool received full value; the fee absorbed the difference")
        results.append(grew == args.gross * 2)

    print(f"\n{sum(results)}/{len(results)} slack probes behaved as described")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
