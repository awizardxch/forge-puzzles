#!/usr/bin/env python3
"""Push the settlement-amount attack at a LIVE V14 pool and let the node answer (spec 1.8).

The attack: a swap whose solution names its settlement as (funding coin, gross) while the
settlement actually created holds ONE mojo. Two things make it the sharpest form:

  * the missing value stays unpaid in the bundle, so conservation holds and MINTING_COIN
    cannot be the thing refusing it -- on V13 this shape was ACCEPTED offline;
  * the one-mojo coin is solved with the honest coin's id as its nonce, so the V10-style
    puzzle announcement the leaf still asserts is satisfied. Only V14's derived-id
    ASSERT_CONCURRENT_SPEND can refuse it.

Then the control: the same swap with an honest settlement, pushed and confirmed, so the
refusal above is the puzzle's and not the pool's state or the wallet's coins.

    python scripts/v14-settlement-probe.py --label H6 --gross 10000000

This probe drives OUR testnet wallet, through scripts/deploy-v14-testnet.py, which is
deliberately not published: it holds wallet paths and picks our coins. So in the public
repository this file is a READING of what was pushed and what the node answered, not
something you can run here -- you could not sign from our wallet in any case.

What is reproducible without a wallet: scripts/sim-v14-review-derivations.py builds the
reserve-launcher constructions against an in-process full node, and the offline suites
under contracts/_test_v14_*.py build every refusal against the compiled puzzles.

"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import forge_math  # noqa: E402
import v14_ops as ops  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

d, drv = ops.deploy, ops.drv
OFFER_PH = bytes32(OFFER_MOD_HASH)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="H6")
    ap.add_argument("--gross", type=int, default=10_000_000)
    args = ap.parse_args()
    state = d.load()
    wallet = d.Wallet()
    record = d.find_pool(state, args.label)
    pool = d.pool_from(record)
    assert pool.asset_ids[0] is None, "the probe trades the native side in"
    gross = args.gross
    r, w = pool.state[0], pool.weights
    honest = forge_math.swap_output(r[0], r[1], gross, pool.fee_bps, w[0], w[1])
    payout = honest - honest * pool.protocol_fee_bps // 10_000 - honest * pool.dao_fee_bps // 10_000
    print(f"{record['label']}: {gross:,} in, curve {honest:,}, payout {payout:,}")

    # ---- the attack -------------------------------------------------------------------------
    funding, funding_puzzle = wallet.xch_coin(gross + d.TX_FEE)
    tiny = Coin(funding.name(), OFFER_PH, uint64(1))
    claimed_id = drv.coin_id(funding.name(), OFFER_PH, gross)          # the coin the leaf will derive
    # the nonce route: the one-mojo coin announces the honest coin's id, so the puzzle
    # announcement the leaf asserts is made -- by a coin that is not that coin
    tiny_spend = make_spend(tiny, OFFER_MOD, d.Program.to([[claimed_id]]))
    change = int(funding.amount) - 1 - d.TX_FEE - (gross - 1)           # gross - 1 stays unpaid: conservation holds
    funding_spend = make_spend(funding, funding_puzzle, drv.p2_delegated_solution(
        [[51, OFFER_PH, 1], [51, wallet.puzzle_hash, change, [wallet.puzzle_hash]]]))
    out_spends, out_cats = d.payout_to_wallet(pool, 1, payout, wallet.puzzle_hash)
    h = d.peak()
    bundle, _ = drv.spend_action(pool, "forge_action_swap", [h, 0, 1, gross, honest, funding.name(), gross],
                                 extra_spends=[funding_spend, tiny_spend, *out_spends], extra_cats=out_cats)
    print(f"attack: settlement holds 1 mojo, named as {gross:,}; {len(bundle.coin_spends)} spends")
    try:
        drv.validate(bundle)
        print("  offline validator: ACCEPTED (unexpected)")
    except drv.Rejected as exc:
        print(f"  offline validator: refused, {exc}  (132 = ASSERT_CONCURRENT_SPEND_FAILED)")
    verdict = d.rpc("push_tx", {"spend_bundle": wallet.sign(list(bundle.coin_spends))})
    err = (verdict.get("structuredError") or {}).get("data", {}).get("error") or verdict.get("error")
    print(f"  node push_tx: success={verdict.get('success')} error={err}")
    refused = not verdict.get("success")
    print("settlement amount on the live chain:",
          "the node REFUSES a settlement short of the amount named" if refused else "ACCEPTED -- a finding")
    if not refused:
        return 1

    # ---- the control -------------------------------------------------------------------------
    wallet._used.clear()          # the attack's funding coin was never spent; let the control pick it
    h = d.peak()
    control, pool2, new_state, info, _ = ops.swap_bundle(wallet, record, 0, 1, gross, h)
    successor = Coin(pool2.coin.name(), pool2.successor_puzzle_hash(drv.state_to_list(new_state)), uint64(1))
    result = d.push_and_wait(wallet, control, successor.name(), f"control swap on {record['label']}")
    result.update(info)
    d.commit_pool(state, record, pool2, new_state, result, "swap")
    print(f"control: the honest swap with a {gross:,} settlement confirmed at {result['height']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
