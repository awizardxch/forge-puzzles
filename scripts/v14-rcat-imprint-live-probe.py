#!/usr/bin/env python3
"""Push the rCAT imprint at LIVE V14 pools on testnet11 and let the node answer.

No wallet, no signature, nothing that can be accepted. Two facts make that possible:

  * Forge's pool spends ask for no signature, and neither does an offer settlement.
  * A node checks CONDITIONS (message pairing, ASSERT_CONCURRENT_SPEND) during
    pre-validation, BEFORE it looks the removals up in its coin store.

So every probe is a pair, built from the pool's real on-chain tip:

  attack    the layered coin, fabricated  -> refused at pre-validation, by the guard
  control   the same bundle with the plain coin, also fabricated
                                          -> gets PAST the guard and is refused only
                                             for UNKNOWN_UNSPENT, the coin store

The control's answer is what makes the attack's meaningful: the honest shape of the same
bundle, against the same live pool at the same height, is not refused by anything in the
puzzles. Both bundles name a coin that does not exist, so neither can ever be included.

Pool tips are rebuilt in memory by replaying the chain (forge_v14_resync); the local
record file is read, never written.

    python scripts/v14-rcat-imprint-live-probe.py --pools all
In the public repository this file is a READING of what was pushed and what the node
answered: it reaches the chain and the pool records through scripts/deploy-v14-testnet.py,
which is deliberately not published (it holds wallet paths and picks our coins). What runs
without it: contracts/_sim_v14_rcat_imprint.py builds every one of these bundles offline
against the compiled puzzles, and scripts/sim-v14-rcat-imprint.py pushes them at an
in-process node. The run itself is recorded in docs/FORGE_AUDIT_RUN_V14_2026-09-24.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import forge_math  # noqa: E402
import v14_ops as ops  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import SpendableCAT  # noqa: E402
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH  # noqa: E402
from chia_rs import Coin, G2Element, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v14_resync as resync_mod  # noqa: E402
from forge_v14_offer import pool_to_snapshot, snapshot_to_pool  # noqa: E402
import _sim_v14_rcat_imprint as imp  # noqa: E402  the offline sim's layer, hidden puzzles and spend_layered

d, drv = ops.deploy, ops.drv
IDENTITY = Program.to(1)

# The public node rate-limits (HTTP 429). Every call in this probe -- the resync walk
# included, which goes through forge_v14_resync's own _rpc -- backs off and retries.
import time  # noqa: E402
import urllib.error  # noqa: E402


def _patient(call):
    def wrapped(*args, **kwargs):
        for attempt in range(8):
            try:
                result = call(*args, **kwargs)
                time.sleep(1.5)
                return result
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 7:
                    raise
                time.sleep(15 * (attempt + 1))
    return wrapped


d.rpc = _patient(d.rpc)
resync_mod._rpc = _patient(resync_mod._rpc)
LAYERS = [("issuer-shaped H", imp.H_ISSUER), ("imprinted attacker H", imp.H_ATTACKER), ("keyless H", imp.H_ANYONE)]
FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    FAILED += 0 if ok else 1
    return ok


def salt32(*parts) -> bytes32:
    return bytes32(hashlib.sha256("|".join(map(str, parts)).encode()).digest())


def to_json(bundle: SpendBundle) -> dict:
    return {"coin_spends": [{"coin": {"parent_coin_info": "0x" + cs.coin.parent_coin_info.hex(),
                                      "puzzle_hash": "0x" + cs.coin.puzzle_hash.hex(), "amount": int(cs.coin.amount)},
                             "puzzle_reveal": "0x" + bytes(cs.puzzle_reveal).hex(),
                             "solution": "0x" + bytes(cs.solution).hex()} for cs in bundle.coin_spends],
            "aggregated_signature": "0x" + bytes(G2Element()).hex()}


def push(bundle: SpendBundle) -> tuple[str, dict]:
    """The node's verdict, read from the BODY: (error name or 'ACCEPTED', raw body)."""
    body = d.rpc("push_tx", {"spend_bundle": to_json(bundle)})
    if body.get("success"):
        return "ACCEPTED", body
    err = (body.get("structuredError") or {}).get("data", {}).get("error") or body.get("error") or json.dumps(body)[:160]
    for name in ("ASSERT_CONCURRENT_SPEND_FAILED", "MESSAGE_NOT_SENT_OR_RECEIVED", "UNKNOWN_UNSPENT",
                 "ASSERT_ANNOUNCE_CONSUMED_FAILED", "DOUBLE_SPEND", "GENERATOR_RUNTIME_ERROR"):
        if name in str(err):
            return name, body
    return str(err)[:120], body


def live_pool(record: dict):
    """The pool at its on-chain tip, rebuilt by replay and proven against the live puzzle hash."""
    stale = d.pool_from(record)
    out = resync_mod.resync({"launcher_id": stale.launcher_id.hex(), "pool": pool_to_snapshot(stale)})
    pool = replace(snapshot_to_pool(out["snapshot"]), birth=int(out["birth"]))
    # every coin the probes name as existing must exist and be unspent at the tip
    for c in [pool.coin, *[r.coin for r in pool.reserves]]:
        rec = d.rpc("get_coin_record_by_name", {"name": "0x" + c.name().hex()}).get("coin_record")
        if not rec or rec.get("spent"):
            raise SystemExit(f"{record['label']}: {c.name().hex()[:12]} is not unspent at the tip; rerun")
    return pool, len(out.get("steps") or [])


def fabricated_settlement(asset: bytes32, inner: Program, amount: int, tag: str):
    """A settlement coin that does not exist, with a lineage the CAT layer's own check accepts."""
    grand = salt32("rcat-probe", tag)
    parent = drv.coin_id(grand, imp.construct_cat_puzzle(imp.CAT_MOD, asset, IDENTITY).get_tree_hash(), amount)
    coin = Coin(parent, imp.construct_cat_puzzle(imp.CAT_MOD, asset, inner).get_tree_hash(), uint64(amount))
    return coin, LineageProof(grand, IDENTITY.get_tree_hash(), uint64(amount))


def probe_pool(record: dict, h: int):
    label = record["label"]
    pool, replayed = live_pool(record)
    cat_idx = next(i for i, a in enumerate(pool.asset_ids) if a is not None)
    out_idx = next((i for i in range(len(pool.asset_ids)) if i != cat_idx), None)   # None: a one-asset vault
    asset = pool.asset_ids[cat_idx]
    hh = ops.claim_height(pool, h)
    print(f"\n== {label}: {len(pool.asset_ids)} assets, tip {pool.coin.name().hex()[:16]} "
          f"(replayed {replayed} spend(s)), born {pool.birth}, claiming h={hh}")
    r, w = pool.state[0], pool.weights
    if out_idx is not None:
        swap_lane(pool, label, hh, cat_idx, out_idx, asset, r, w)
    else:
        print("  swap lane: a one-asset vault has no swap; reserve lane only")
    reserve_lane(pool, label, hh, cat_idx)


def swap_lane(pool, label, hh, cat_idx, out_idx, asset, r, w):
    gross = max(1_000, r[cat_idx] // 1_000)
    out = forge_math.swap_output(r[cat_idx], r[out_idx], gross, pool.fee_bps, w[cat_idx], w[out_idx])

    def swap(coin: Coin, spendable) -> SpendBundle:
        return drv.spend_action(pool, "forge_action_swap", [hh, cat_idx, out_idx, gross, out, *drv.settlement_ref(coin)],
                                extra_cats={asset: [spendable]})[0]

    print(f"  swap lane: {gross:,} of reserve {cat_idx} in, {out:,} of reserve {out_idx} out")
    coin, lin = fabricated_settlement(asset, OFFER_MOD, gross, f"{label}-plain")
    v, _ = push(swap(coin, SpendableCAT(coin, asset, OFFER_MOD, Program.to([[coin.name()]]), lineage_proof=lin)))
    control_ok = check(f"control, plain settlement: node says {v} -- past every puzzle guard, stopped only by the coin store",
                       v == "UNKNOWN_UNSPENT")
    for name, h_ in LAYERS:
        inner = imp.rev(h_, bytes32(OFFER_MOD_HASH))
        coin, lin = fabricated_settlement(asset, inner, gross, f"{label}-{name}")
        spendable = SpendableCAT(coin, asset, inner, Program.to([0, OFFER_MOD, [[coin.name()]]]), lineage_proof=lin)
        v, _ = push(swap(coin, spendable))
        check(f"attack, settlement behind the {name}: node says {v}",
              control_ok and v == "ASSERT_CONCURRENT_SPEND_FAILED")



def reserve_lane(pool, label, hh, cat_idx):
    print(f"  reserve lane: observe, reserve {cat_idx} replaced by a layered coin (same parent, same amount)")
    ghost = Coin(salt32("rcat-probe-ghost", label), IDENTITY.get_tree_hash(), uint64(1))
    ghost_spend = make_spend(ghost, IDENTITY, Program.to([]))
    b, _ = drv.spend_action(pool, "forge_action_observe", [hh], extra_spends=[ghost_spend])
    v, _ = push(b)
    control_ok = check(f"control, honest observe + one nonexistent 1-mojo coin: node says {v}", v == "UNKNOWN_UNSPENT")
    for name, h_ in LAYERS:
        b, _ = imp.spend_layered(pool, "forge_action_observe", [hh], {cat_idx: (h_, None)})
        v, _ = push(b)
        check(f"attack, reserve behind the {name}: node says {v}", control_ok and v == "MESSAGE_NOT_SENT_OR_RECEIVED")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", default="A1 txch t6 v14,D2 five assets v14",
                    help="comma-separated labels, or 'all' for every pool in the deploy record")
    args = ap.parse_args()
    state = d.load()
    chain = d.rpc("get_blockchain_state", {})["blockchain_state"]
    h = int(chain["peak"]["height"])
    fp = subprocess.run([sys.executable, str(ROOT / "scripts" / "revision-fingerprint.py")], capture_output=True, text=True)
    print(f"network testnet11 via {d.NODE}, peak {h}, sync {chain.get('sync', {}).get('synced')}")
    print(f"revision: {fp.stdout.strip() or fp.stderr.strip()[:120]}")
    records = state["pools"] if args.pools == "all" else [d.find_pool(state, l.strip()) for l in args.pools.split(",")]
    probed, not_probed = 0, []
    for record in records:
        if not any(a is not None for a in record["asset_ids"]):
            not_probed.append((record["label"], "no CAT reserve"))
            continue
        try:
            probe_pool(record, h)
            probed += 1
        except (SystemExit, resync_mod.ResyncError) as exc:
            # a record too far behind the chain, or a tip that moved mid-run: a fact about the
            # checkout, not about the puzzles (runbook rule 9) -- named, never counted as a pass
            not_probed.append((record["label"], str(exc)[:90]))
    print(f"\ncoverage: {probed} of {len(records)} pools probed")
    for label, why in not_probed:
        print(f"  not probed: {label}: {why}")
    print(f"{'every probe behaved as reported' if FAILED == 0 else f'{FAILED} probe(s) did not'}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
