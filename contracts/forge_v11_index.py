#!/usr/bin/env python3
"""Emit the V11 pools as deployment-index entries for api/pools.js.

The deployment index (.awizard/deployment-index.json) is what the API lists
from and what the responder reads its pool snapshots out of. V11 pools are
created by the registry driver (scripts/deploy-v11-testnet.py), which keeps its
own record in .awizard/v11-testnet.json; this script turns that record into the
index's shape -- one plan per pool, one batch, `poolSnapshot` in the V11 format
forge_v11_offer round-trips -- so the same responder and listing code serves
V11 with no second index. scripts/import-v11-pools.mjs merges the output
through mergeDeploymentIndex, which is the only writer the index has.

Every snapshot is rebuilt through `snapshot_to_pool` before it is emitted, so an
entry that does not hash to its recorded pool coin is never written.

stdout: JSON {plans: {planKey: {summary, batches}}, count, skipped: [...]}
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "contracts"))
sys.stdout.reconfigure(encoding="utf-8")

from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v11_driver as drv  # noqa: E402
import forge_v11_offer as v11  # noqa: E402

RECORD = ROOT / ".awizard" / "v11-testnet.json"
ZERO_HEX = "00" * 32
# Forge is word-minimal: the emoji IS the market. The forge glyph (hammer and
# pick) marks an LP. A one-asset pool is just a pool: no glyph or word of its own,
# and its deployer may rename it to anything, a new token name included.
# src/lib/tokenRegistry.json is the single source for a token's glyph, ticker and
# name (the frontend and the Sage label sync read the same file).
_REGISTRY = json.loads((ROOT / "src" / "lib" / "tokenRegistry.json").read_text(encoding="utf-8"))["tokens"]
EMOJI = {t["assetId"].lower(): t["emoji"] for t in _REGISTRY if t.get("emoji") and t["assetId"] != "txch"}
TICKERS = {t["assetId"].lower(): t["symbol"] for t in _REGISTRY if t.get("emoji") and t["assetId"] != "txch"}
# The base asset is named, never a glyph: a reader has to know what the pool is priced in.
EMOJI[ZERO_HEX] = "TXCH"
TICKERS[ZERO_HEX] = "TXCH"
FORGE = "\u2692\uFE0F"      # hammer and pick: an LP of a Forge pool
SYMBOLS = dict(TICKERS)


def _order(asset_ids: list, weights: list) -> list:
    return sorted(range(len(asset_ids)), key=lambda i: (-int(weights[i]), i))


def emoji_name(asset_ids: list, weights: list, fee_bps: int = 0, state: dict | None = None) -> str:
    """The pool's NAME: its assets' glyphs and nothing else, heaviest first, TXCH as
    text; a one-asset pool is simply its asset's glyph. Weights and fees are not part
    of a name; they are the label (`pool_label`). An LP asset reads as the forge glyph
    plus its pool's name."""
    def glyph(asset_hex: str) -> str:
        if asset_hex in EMOJI:
            return EMOJI[asset_hex]
        for other in (state or {}).get("pools", []):
            if other["lp_asset_id"] == asset_hex:
                return FORGE + (other.get("emoji") or emoji_name([ZERO_HEX if a is None else a for a in other["asset_ids"]],
                                                                 other["weights"], other["fee_bps"], state))
        return "\u2753"          # question mark: an asset this record has never seen
    order = _order(asset_ids, weights)
    # glyphs run together; the named base asset gets a space after it (TXCH 🍕, TXCH 🛸⚡💵🍕)
    return "".join(glyph(asset_ids[i]) + (" " if asset_ids[i] == ZERO_HEX else "") for i in order).strip()


def pool_symbol(asset_ids: list, weights: list, state: dict | None = None) -> str:
    """The pool's SYMBOL: tickers heaviest first and the weight ratio when unequal
    ('TXCH/T6 4:1'). Set at mint and updatable by the deployer like the name; the fee is
    not part of it because the fee is the puzzle's and cannot change."""
    return pool_label(asset_ids, weights, 0, state, with_fee=False)


def pool_label(asset_ids: list, weights: list, fee_bps: int, state: dict | None = None, with_fee: bool = True) -> str:
    """The pool's LABEL, everything a name should not carry: tickers heaviest first
    ('TXCH/T6'), the weight ratio when unequal ('4:1'), the fee ('0.30%'); a one-asset
    pool is labelled by its one ticker. An LP asset reads 'LP ' plus its pool's label
    without the fee (the fee belongs to the pool being labelled, not to the asset
    inside it)."""
    def ticker(asset_hex: str) -> str:
        if asset_hex in TICKERS:
            return TICKERS[asset_hex]
        for other in (state or {}).get("pools", []):
            if other["lp_asset_id"] == asset_hex:
                return "LP " + pool_label([ZERO_HEX if a is None else a for a in other["asset_ids"]],
                                          other["weights"], other["fee_bps"], state, with_fee=False)
        return asset_hex[:6] + "\u2026"
    order = _order(asset_ids, weights)
    label = "/".join(ticker(asset_ids[i]) for i in order)
    if len(set(weights)) > 1:
        label += " " + ":".join(str(int(weights[i])) for i in order)
    if with_fee:
        label += f" \u00B7 {fee_bps / 100:.2f}%"
    return label


def asset_symbol(asset_hex: str, state: dict) -> str:
    """'seedling TXCH' style, as the V10 index labelled assets; an LP is the forge glyph
    plus its pool's emoji name, then 'LP'."""
    if asset_hex == ZERO_HEX:
        return "TXCH"
    if asset_hex in EMOJI:
        return f"{EMOJI[asset_hex]} {TICKERS[asset_hex]}"
    for other in state.get("pools", []):
        if other["lp_asset_id"] == asset_hex:
            return f"{FORGE}{other.get('emoji') or ''} LP".replace("  ", " ")
    return asset_hex[:6] + "\u2026"


def _coin(d: dict) -> Coin:
    return Coin(bytes32.fromhex(d["parent_coin_info"]), bytes32.fromhex(d["puzzle_hash"]), uint64(int(d["amount"])))


def _lineage(d: dict | None) -> LineageProof | None:
    if not d:
        return None
    inner = d.get("inner_puzzle_hash")
    return LineageProof(bytes32.fromhex(d["parent_name"]), None if inner is None else bytes32.fromhex(inner), uint64(int(d["amount"])))


def pool_from_record(record: dict) -> drv.V11Pool:
    """The deploy script's `pool_from`, duplicated so this file does not import a
    script by path."""
    assets = [None if a is None else bytes32.fromhex(a) for a in record["asset_ids"]]
    st = record["state"]
    # V11.1 records carry [reserves, total_lp, fees_owed, oracle, prev_root, dao_fee_bps, dao_owed]
    state = [st[0], st[1], st[2], st[3], bytes32.fromhex(st[4]),
             int(st[5]) if len(st) > 5 else 0, list(st[6]) if len(st) > 6 else [0] * len(st[0])]
    reserve_coins = [(_coin(r["coin"]), _lineage(r["lineage"])) for r in record["reserves"]]
    pool = drv.make_pool(assets, st[0], total_lp=st[1], fees=st[2], leaves="forge", weights=record["weights"],
                         fee_bps=record["fee_bps"], protocol_fee_bps=record["protocol_fee_bps"],
                         protocol_ph=bytes32.fromhex(record["protocol_ph"]),
                         dao_ph=bytes32.fromhex(record["dao_ph"]) if record.get("dao_ph") else None,
                         launcher_parent=bytes32.fromhex(record["launcher_parent"]), reserve_coins=reserve_coins, state=state)
    return replace(pool, coin=_coin(record["coin"]), lineage=_lineage(record["lineage"]))


def chain_names(state: dict) -> dict[str, dict]:
    """launcher id -> what the chain says the pool is called (forge_names.resolve)."""
    import forge_names as names
    out = {}
    for record in state["pools"]:
        try:
            out[record["launcher_id"]] = names.resolve(record["launcher_id"], record["launcher_parent"])
        except Exception as exc:  # noqa: BLE001 -- the record's name stands in when the node is unreachable
            out[record["launcher_id"]] = {"name": None, "source": None, "error": f"{type(exc).__name__}: {exc}"}
    return out


def entries(state: dict, resolve: bool = False) -> tuple[dict, list]:
    registry_id = state["registry"]["launcher_id"]
    on_chain = chain_names(state) if resolve else {}
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    plans, skipped = {}, []
    for record in state["pools"]:
        try:
            pool = pool_from_record(record)
            snapshot = v11.pool_to_snapshot(pool)
            v11.snapshot_to_pool(json.loads(json.dumps(snapshot)))    # proves it, through JSON as node holds it
        except Exception as exc:  # noqa: BLE001
            skipped.append({"label": record.get("label"), "error": f"{type(exc).__name__}: {exc}"})
            continue
        asset_ids = ["txch" if a is None else a for a in record["asset_ids"]]
        hex_ids = [ZERO_HEX if a is None else a for a in record["asset_ids"]]
        symbols = [asset_symbol(a, state) for a in hex_ids]
        derived = emoji_name(hex_ids, record["weights"], record["fee_bps"], state)
        chain = on_chain.get(record["launcher_id"]) or {}
        # The chain's name and symbol (the deployer's latest rename, else the genesis
        # memos) are the authority for what to DISPLAY; the record stands in for pools
        # that never carried them. What the pool IS -- assets, weights, fee, its asset
        # ids -- comes from the puzzle through the snapshot, never from a memo.
        emoji = chain.get("name") or record.get("emoji") or derived
        symbol = chain.get("symbol") or record.get("symbol") or pool_symbol(hex_ids, record["weights"], state)
        label_text = f"{symbol} \u00B7 {int(pool.fee_bps) / 100:.2f}%"
        reserves = [str(int(x)) for x in pool.state[0]]
        created = record.get("created") or {}
        last = record["history"][-1] if record.get("history") else {}
        launcher = pool.launcher_id.hex()
        plan_key = f"v11:{launcher}"
        pool_state_json = {
            "assets": [{"asset_id": ZERO_HEX if a is None else a, "reserve": r, "weight": int(w)}
                       for a, r, w in zip(record["asset_ids"], reserves, pool.weights)],
            "fee_bps": int(pool.fee_bps),
            "total_lp": str(int(pool.state[1])),
            "lp_tail_hash": pool.lp_asset_id.hex(),
        }
        plans[plan_key] = {
            "summary": {
                "planKey": plan_key,
                "poolName": emoji,
                "operatorLabel": record["label"],
                "description": label_text,
                "mode": "single",
                "totalAssets": len(asset_ids),
                "batchCount": 1,
                "assetSymbols": symbols,
                "updatedAt": now,
            },
            "batches": {
                "0": {
                    "status": "confirmed",
                    "poolVersion": v11.PROTOCOL_VERSION,
                    # The names live on the batch under their own keys: a browser posting a
                    # stale copy of the index back only overwrites keys it carries, and the
                    # merge unions arrays, so summary.poolName and execution.assetSymbols
                    # can both be dragged backwards; these two cannot.
                    "emojiName": emoji,
                    "emojiSymbols": symbols,
                    "poolSymbol": symbol,
                    "displayLabel": label_text,
                    "nameSource": chain.get("source") or "record",
                    "deployerPuzzleHash": chain.get("deployer_ph"),
                    "launchLane": "forge-v11-registry",
                    "registryLauncherId": registry_id,
                    "registryKey": record.get("key"),
                    "launcherCoinId": launcher,
                    "launcherParentCoinInfo": pool.launcher_parent.hex(),
                    "launcherParent": pool.launcher_parent.hex(),
                    "currentCoinId": snapshot["pool_coin_id"],
                    "currentPuzzleHash": snapshot["pool_coin"]["puzzle_hash"],
                    "bootstrapTargetCoinId": snapshot["pool_coin_id"],
                    "bootstrapTargetPuzzleHash": snapshot["pool_coin"]["puzzle_hash"],
                    "lpCatAssetId": pool.lp_asset_id.hex(),
                    "lpTailHash": pool.lp_asset_id.hex(),
                    "lpOut": str(int(pool.state[1])),
                    "txId": str(created.get("tx_id") or "").removeprefix("0x"),
                    "bootstrapTxId": str((last.get("tx_id") if last else created.get("tx_id")) or "").removeprefix("0x"),
                    "error": "",
                    "updatedAt": now,
                    "bootstrapStatus": "confirmed",
                    "bootstrapUpdatedAt": now,
                    "poolStateJson": json.dumps(pool_state_json),
                    "poolSnapshot": snapshot,
                    "execution": {
                        "assetIds": asset_ids,
                        "assetSymbols": symbols,
                        "weights": [int(w) for w in pool.weights],
                        "bootstrapAmounts": reserves,
                        "swapFeeBps": int(pool.fee_bps),
                        "daoFeeBps": int(pool.state[5]),
                        "daoPuzzleHash": pool.dao_ph.hex(),
                        "daoPuzzleHash": "",
                        "adminPuzzleHash": "",
                        "protocolFeeBps": int(pool.protocol_fee_bps),
                        "protocolFeePpm": int(pool.protocol_fee_bps) * 100,
                        "protocolPuzzleHash": pool.protocol_ph.hex(),
                        "lpRecipientPuzzleHash": record.get("lp_recipient_ph", ""),
                    },
                },
            },
        }
    return plans, skipped


def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = Path(positional[0]) if positional else RECORD
    if not path.is_file():
        print(json.dumps({"success": False, "error": f"no V11 record at {path}"}))
        return 1
    state = json.loads(path.read_text(encoding="utf-8"))
    plans, skipped = entries(state, resolve="--resolve" in sys.argv)
    print(json.dumps({"success": True, "plans": plans, "count": len(plans), "skipped": skipped,
                      "registry": state["registry"]["launcher_id"], "network": state.get("network")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
