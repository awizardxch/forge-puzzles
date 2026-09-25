#!/usr/bin/env python3
"""The rCAT imprint on testnet11 with real, signed coins -- one stage per run.

    python scripts/v14-rcat-imprint-testnet.py imprint   # T6 -> two layered coins, confirmed
    python scripts/v14-rcat-imprint-testnet.py sage      # how Sage labels T6 now
    python scripts/v14-rcat-imprint-testnet.py attack    # the REAL imprinted coin pays a swap on A1
    python scripts/v14-rcat-imprint-testnet.py keyless   # anyone takes the keyless coin, unsigned
    python scripts/v14-rcat-imprint-testnet.py strip     # our hidden key strips the layer back off

The two imprinted coins (1 T6 each, owned by this wallet's derivation 2):
  A  revocation(H_own, p2)     H_own = this wallet's standard puzzle at derivation 1
  B  revocation(H_anyone, p2)  H_anyone = sha256tree(1): anyone may use the hidden path

Every hidden hash is ours or keyless; nothing here imprints anyone else's token. The
`attack` stage is validated offline first and is pushed ONLY if the local validator already
refuses it, so a guard failure is reported without ever trading against a live pool.
Coin ids are kept in a local state file beside the deploy record, never published.
In the public repository this file is a READING of what was pushed and what the node
answered: it reaches the chain and the pool records through scripts/deploy-v14-testnet.py,
which is deliberately not published (it holds wallet paths and picks our coins). What runs
without it: contracts/_sim_v14_rcat_imprint.py builds every one of these bundles offline
against the compiled puzzles, and scripts/sim-v14-rcat-imprint.py pushes them at an
in-process node. The run itself is recorded in docs/FORGE_AUDIT_RUN_V14_2026-09-24.md.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import forge_math  # noqa: E402
import v14_ops as ops  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats  # noqa: E402
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH  # noqa: E402
from chia.wallet.vc_wallet.vc_drivers import create_revocation_layer  # noqa: E402
from chia_rs import Coin, G2Element, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v14_resync as resync_mod  # noqa: E402
from forge_v14_offer import pool_to_snapshot, snapshot_to_pool  # noqa: E402

d, drv = ops.deploy, ops.drv
T6 = d.ASSETS["T6"]
POOL = "A1 txch t6 v14"
UNIT = 1_000                      # 1 T6 (precision 3)
ANYONE = Program.to(1)
OFFER_PH = bytes32(OFFER_MOD_HASH)
STATE = ROOT / ".awizard" / "rcat-imprint-testnet.json"


def rev(h: bytes32, inner_hash: bytes32) -> Program:
    return create_revocation_layer(h, inner_hash)


def cat_ph(inner: Program) -> bytes32:
    return bytes32(construct_cat_puzzle(CAT_MOD, T6, inner).get_tree_hash())


def load() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.is_file() else {}


def save(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def keys(wallet):
    """(hidden puzzle H_own at derivation 1, owner p2 at derivation 2): both this wallet's."""
    ders = wallet.derivations()
    return wallet.derivation_puzzle(ders[1][0]), wallet.derivation_puzzle(ders[2][0])


def coin_live(coin: Coin) -> dict | None:
    return d.rpc("get_coin_record_by_name", {"name": "0x" + coin.name().hex()}).get("coin_record")


def imprint(wallet) -> int:
    hidden, owner = keys(wallet)
    H_own, H_any = bytes32(hidden.get_tree_hash()), bytes32(ANYONE.get_tree_hash())
    t6_coin, t6_inner, t6_lin = wallet.cat_coin(T6, 2 * UNIT + 1)
    fund, fund_puzzle = wallet.xch_coin(d.TX_FEE)
    a_inner, b_inner = rev(H_own, owner.get_tree_hash()), rev(H_any, owner.get_tree_hash())
    change = int(t6_coin.amount) - (2 * UNIT + 1)
    hint = owner.get_tree_hash()
    conds = [[51, a_inner.get_tree_hash(), UNIT, [hint]], [51, b_inner.get_tree_hash(), UNIT + 1, [hint]]]
    if change:
        conds.append([51, wallet.puzzle_hash, change, [wallet.puzzle_hash]])
    cat_spends = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        t6_coin, T6, t6_inner, drv.p2_delegated_solution(conds), lineage_proof=t6_lin)]).coin_spends
    fund_spend = make_spend(fund, fund_puzzle, drv.p2_delegated_solution(
        [[51, wallet.puzzle_hash, int(fund.amount) - d.TX_FEE, [wallet.puzzle_hash]]]))
    bundle = SpendBundle([*cat_spends, fund_spend], G2Element())
    a = Coin(t6_coin.name(), cat_ph(a_inner), uint64(UNIT))
    b = Coin(t6_coin.name(), cat_ph(b_inner), uint64(UNIT + 1))
    result = d.push_and_wait(wallet, bundle, a.name(), "imprint")
    lin = {"parent_name": t6_coin.parent_coin_info.hex(), "inner_puzzle_hash": t6_inner.get_tree_hash().hex(),
           "amount": int(t6_coin.amount)}
    save({"A": d.coin_to_json(a), "B": d.coin_to_json(b), "lineage": lin, "H_own": H_own.hex(),
          "owner_derivation": 2, "hidden_derivation": 1, "imprint": result})
    print(f"A = {a.name().hex()}  revocation(H_own={H_own.hex()[:12]}..)")
    print(f"B = {b.name().hex()}  revocation(H_anyone)")
    print(f"both confirmed at {result['height']}, parent spend is an ordinary T6 transfer (no TAIL ran)")
    return 0


def sage_view(wallet) -> int:
    t6 = next(c for c in wallet.sage.call("get_cats", {})["cats"] if c["asset_id"] == T6.hex())
    print(f"Sage T6 entry: balance {t6['balance']}, revocation_address {t6['revocation_address']!r}, name {t6['name']!r}")
    s = load()
    coins = wallet.sage.call("get_coins", {"asset_id": T6.hex(), "offset": 0, "limit": 500}) if s else {}
    listed = {c.get("coin_id", "").removeprefix("0x") for c in (coins.get("coins") or [])}
    for tag in ("A", "B"):
        if tag in s:
            cid = d.coin_from_json(s[tag]).name().hex()
            print(f"  coin {tag} {cid[:16]}: {'LISTED' if cid in listed else 'not listed'} in Sage's T6 coins; "
                  f"on chain: {'unspent' if (coin_live(d.coin_from_json(s[tag])) or {}).get('spent') is False else 'spent/absent'}")
    return 0


def attack(wallet) -> int:
    s = load()
    hidden, owner = keys(wallet)
    H_own = bytes32(hidden.get_tree_hash())
    a = d.coin_from_json(s["A"])
    lin = s["lineage"]
    a_lin = LineageProof(bytes32.fromhex(lin["parent_name"]), bytes32.fromhex(lin["inner_puzzle_hash"]), uint64(lin["amount"]))
    rec = coin_live(a)
    assert rec and not rec["spent"], "coin A is not unspent on chain"
    stale = d.pool_from(d.find_pool(d.load(), POOL))
    out = resync_mod.resync({"launcher_id": stale.launcher_id.hex(), "pool": pool_to_snapshot(stale)})
    from dataclasses import replace
    pool = replace(snapshot_to_pool(out["snapshot"]), birth=int(out["birth"]))
    ci = pool.asset_ids.index(T6)
    oi = 1 - ci
    r, w = pool.state[0], pool.weights
    qout = forge_math.swap_output(r[ci], r[oi], UNIT, pool.fee_bps, w[ci], w[oi])
    # The imprinted coin makes its settlement the only way its owner can: the inner path
    # creates OFFER_MOD, and the layer re-wraps it as revocation(H_own, OFFER_MOD). Ephemeral.
    a_inner = rev(H_own, owner.get_tree_hash())
    s_inner = rev(H_own, OFFER_PH)
    settle = Coin(a.name(), cat_ph(s_inner), uint64(UNIT))
    a_spend = SpendableCAT(a, T6, a_inner, Program.to([0, owner, drv.p2_delegated_solution([[51, OFFER_PH, UNIT]])]),
                           lineage_proof=a_lin)
    s_spend = SpendableCAT(settle, T6, s_inner, Program.to([0, OFFER_MOD, [[settle.name()]]]),
                           lineage_proof=LineageProof(a.parent_coin_info, a_inner.get_tree_hash(), a.amount))
    h = ops.claim_height(pool, d.peak())
    bundle, _ = drv.spend_action(pool, "forge_action_swap", [h, ci, oi, UNIT, qout, *drv.settlement_ref(settle)],
                                 extra_cats={T6: [a_spend, s_spend]})
    print(f"{POOL}: tip {pool.coin.name().hex()[:16]}, the real coin A pays {UNIT} T6 for {qout:,} mojos")
    try:
        drv.validate(bundle)
        print("  offline validator: ACCEPTED -- NOT pushing; this would trade against a live pool. A finding.")
        return 1
    except drv.Rejected as exc:
        print(f"  offline validator: refused, {exc}")
    signed = wallet.sign(list(bundle.coin_spends))
    verdict = d.rpc("push_tx", {"spend_bundle": signed})
    err = (verdict.get("structuredError") or {}).get("data", {}).get("error") or verdict.get("error")
    print(f"  node push_tx: success={verdict.get('success')} error={err}")
    print(f"  coin A afterwards: {'unspent -- the refusal cost nothing' if not (coin_live(a) or {}).get('spent') else 'SPENT'}")
    return 0 if not verdict.get("success") else 1


def keyless(wallet) -> int:
    s = load()
    b = d.coin_from_json(s["B"])
    _hidden, owner = keys(wallet)
    b_inner = rev(bytes32(ANYONE.get_tree_hash()), owner.get_tree_hash())
    lin = s["lineage"]
    b_lin = LineageProof(bytes32.fromhex(lin["parent_name"]), bytes32.fromhex(lin["inner_puzzle_hash"]), uint64(lin["amount"]))
    # No key, no signature: the hidden puzzle is `1`, so its solution IS the conditions.
    to = wallet.puzzle_hash
    spend = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        b, T6, b_inner, Program.to([1, ANYONE, [[51, to, int(b.amount), [to]]]]), lineage_proof=b_lin)]).coin_spends
    bundle = SpendBundle(spend, G2Element())
    drv.validate(bundle)
    child = Coin(b.name(), bytes32(construct_cat_puzzle(CAT_MOD, T6, Program.to(to)).get_tree_hash_precalc(to)), b.amount)
    print(f"keyless: spending B with an EMPTY signature, to a plain T6 coin")
    verdict = d.rpc("push_tx", {"spend_bundle": {"coin_spends": [
        {"coin": {k: ("0x" + v if isinstance(v, str) else v) for k, v in d.coin_to_json(cs.coin).items()},
         "puzzle_reveal": "0x" + bytes(cs.puzzle_reveal).hex(), "solution": "0x" + bytes(cs.solution).hex()}
        for cs in bundle.coin_spends], "aggregated_signature": "0x" + bytes(G2Element()).hex()}})
    print(f"  node push_tx: {json.dumps(verdict)[:200]}")
    return wait_for(child, "keyless revoke")


def strip(wallet) -> int:
    s = load()
    hidden, owner = keys(wallet)
    H_own = bytes32(hidden.get_tree_hash())
    a = d.coin_from_json(s["A"])
    lin = s["lineage"]
    a_lin = LineageProof(bytes32.fromhex(lin["parent_name"]), bytes32.fromhex(lin["inner_puzzle_hash"]), uint64(lin["amount"]))
    a_inner = rev(H_own, owner.get_tree_hash())
    to = wallet.puzzle_hash
    # The hidden path runs OUR standard puzzle at derivation 1; its conditions pass through unwrapped.
    spend = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        a, T6, a_inner, Program.to([1, hidden, drv.p2_delegated_solution([[51, to, int(a.amount), [to]]])]),
        lineage_proof=a_lin)]).coin_spends
    child = Coin(a.name(), bytes32(construct_cat_puzzle(CAT_MOD, T6, Program.to(to)).get_tree_hash_precalc(to)), a.amount)
    # a full testnet mempool refuses a zero-fee spend (INVALID_FEE_TOO_CLOSE_TO_ZERO): pay one
    fund, fund_puzzle = wallet.xch_coin(d.TX_FEE)
    fee_spend = make_spend(fund, fund_puzzle, drv.p2_delegated_solution(
        [[51, wallet.puzzle_hash, int(fund.amount) - d.TX_FEE, [wallet.puzzle_hash]]]))
    d.push_and_wait(wallet, SpendBundle([*spend, fee_spend], G2Element()), child.name(), "strip via hidden path")
    print(f"A's child {child.name().hex()[:16]} is a PLAIN T6 coin at the wallet's address")
    return 0


def wait_for(coin: Coin, label: str) -> int:
    import time
    for _ in range(120):
        rec = coin_live(coin)
        if rec and int(rec.get("confirmed_block_index") or 0) > 0:
            print(f"{label}: CONFIRMED at height {rec['confirmed_block_index']}, child {coin.name().hex()[:16]}")
            return 0
        time.sleep(15)
    print(f"{label}: not confirmed yet; check {coin.name().hex()}")
    return 1


def main() -> int:
    stage = sys.argv[1] if len(sys.argv) > 1 else "sage"
    wallet = d.Wallet()
    return {"imprint": imprint, "sage": sage_view, "attack": attack, "keyless": keyless, "strip": strip}[stage](wallet)


if __name__ == "__main__":
    raise SystemExit(main())
