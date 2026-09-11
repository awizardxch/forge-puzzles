#!/usr/bin/env python3
"""The V11 lane of forge_stdin, end to end and keyless, in the simulator.

A trader's Offer is fabricated exactly as a wallet would shape it: their coins
(here, plain `(1)` puzzles nobody has to sign) create the OFFER_MOD settlement
coins and assert the notarized requested payments, and the Offer object carries
those requested payments with their nonces. The builder gets the snapshot the
responder would hold, the bech32 offer and a height, and must return a bundle
that passes consensus validation with the trader paid exactly their request,
the surplus paid to the router, and a successor snapshot that rebuilds to the
successor pool.

Runs on the V10 corpus of shapes: XCH/CAT pair, CAT-only pair, three-asset
weighted, a vault. Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.conditions import CreateCoin
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH, Offer
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin
import forge_v11_driver as drv
import forge_v11_offer as v11
from forge_offer import ZERO_32

IDENTITY = Program.to(1)
TRADER_PH = bytes32(b"\x77" * 32)
ROUTER_PH = bytes32(b"\x66" * 32)
T_A = bytes32(b"\xa1" * 32)
T_B = bytes32(b"\xb2" * 32)
H = 4_700_000
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


# ---- a wallet's offer, without a wallet -------------------------------------------------------------

def fabricate_offer(offered: dict, requested: dict, salt: int) -> Offer:
    """`offered`: asset -> amount the trader puts in; `requested`: asset -> amount
    they must receive. Trader coins are `(1)` puzzles: an XCH coin creates the XCH
    settlement, a CAT coin (with a fabricated CAT parent) creates the CAT
    settlement; each asserts the requested payments' announcements, so the
    offer only settles when the trader is paid."""
    coins, spends, cats = [], [], []
    for i, (asset, amount) in enumerate(offered.items()):
        if asset is None:
            coin = Coin(bytes32(bytes([salt, i]) * 16), IDENTITY.get_tree_hash(), uint64(amount))
        else:
            outer = construct_cat_puzzle(CAT_MOD, asset, IDENTITY).get_tree_hash()
            grand = bytes32(bytes([salt, 0x10 + i]) * 16)
            coin = Coin(drv.coin_id(grand, outer, amount), outer, uint64(amount))
        coins.append((asset, coin))
    notarized = Offer.notarize_payments(
        {asset: [CreateCoin(TRADER_PH, uint64(amount), [TRADER_PH])] for asset, amount in requested.items()},
        [c for _, c in coins])
    announcements = []
    for asset, payments in notarized.items():
        settle = OFFER_MOD_HASH if asset is None else construct_cat_puzzle(CAT_MOD, asset, OFFER_MOD).get_tree_hash()
        for p in payments:
            msg = Program.to((p.nonce, [p.as_condition_args()])).get_tree_hash()
            announcements.append([63, bytes32(drv.hashlib.sha256(bytes(settle) + bytes(msg)).digest())])
    for asset, coin in coins:
        # Inside a CAT the inner puzzle pays the INNER settlement hash; the CAT
        # layer wraps it into the CAT-settlement outer hash the announcements use.
        conditions = [[51, OFFER_MOD_HASH, int(coin.amount)], *announcements]
        if asset is None:
            spends.append(make_spend(coin, IDENTITY, Program.to(conditions)))
        else:
            grand = bytes32(bytes([salt, 0x10 + list(offered).index(asset)]) * 16)
            cats.append(SpendableCAT(coin, asset, IDENTITY, Program.to(conditions),
                                     lineage_proof=LineageProof(grand, IDENTITY.get_tree_hash(), coin.amount)))
    # One CAT ring per asset: a ring is the coins of a single TAIL.
    by_asset = {}
    for sc in cats:
        by_asset.setdefault(bytes(sc.limitations_program_hash), []).append(sc)
    for ring in by_asset.values():
        spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, ring).coin_spends)
    driver = {asset: PuzzleInfo({"type": "CAT", "tail": "0x" + asset.hex()}) for asset in requested if asset is not None}
    return Offer(notarized, SpendBundle(spends, G2Element()), driver)


def paid_to(bundle: SpendBundle, ph: bytes32, cat_asset=None) -> int:
    """Mojos created for `ph` (as an XCH coin, or wrapped in the CAT of `cat_asset`)."""
    target = ph if cat_asset is None else construct_cat_puzzle(CAT_MOD, cat_asset, Program.to(ph)).get_tree_hash_precalc(ph)
    total = 0
    for cs in bundle.coin_spends:
        puzzle = Program.from_bytes(bytes(cs.puzzle_reveal))
        for c in puzzle.run(Program.from_bytes(bytes(cs.solution))).as_iter():
            if c.first().atom is None or c.first().as_int() != 51:
                continue
            if bytes32(c.rest().first().as_atom()) == target:
                total += c.rest().rest().first().as_int()
    return total


def run(payload: dict) -> dict:
    return forge_stdin.build(json.loads(json.dumps(payload)))     # through JSON, as node would send it


def make(assets, reserves, weights, salt, fee_bps=30, protocol_fee_bps=5, total_lp=1_000_000):
    return drv.make_pool(assets, reserves, total_lp=total_lp, weights=weights, fee_bps=fee_bps,
                         protocol_fee_bps=protocol_fee_bps, protocol_ph=bytes32(b"\x55" * 32), salt=salt, leaves="forge",
                         last_height=H - 10)


def snapshot_roundtrip(pool):
    snap = v11.pool_to_snapshot(pool)
    back = v11.snapshot_to_pool(json.loads(json.dumps(snap)))
    return snap, back


def swap_case(label, pool, i_in, i_out, gross, ask_bps=9_900, expect_ok=True):
    print(f"swap on {label}: asset {i_in} -> {i_out}, gross {gross}")
    snap, _ = snapshot_roundtrip(pool)
    a_in, a_out = pool.asset_ids[i_in], pool.asset_ids[i_out]
    r, w = pool.state[0], pool.weights
    honest = v11.forge_math.swap_output(r[i_in], r[i_out], gross, pool.fee_bps, w[i_in], w[i_out])
    payout = honest - honest * pool.protocol_fee_bps // 10_000
    want = payout * ask_bps // 10_000
    offer = fabricate_offer({a_in: gross}, {a_out: want}, salt=0x31)
    payload = {"action": "swap", "offer": offer.to_bech32(), "pool": snap, "current_height": H,
               "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}}
    try:
        out = run(payload)
    except Exception as exc:
        check("  builder accepted the offer", not expect_ok, f"{type(exc).__name__}: {str(exc)[:90]}")
        return None
    if not check("  builder accepted the offer", expect_ok):
        return None
    bundle = SpendBundle.from_json_dict(out["bundle"])
    check("  trader is paid exactly the request", paid_to(bundle, TRADER_PH, a_out) == want)
    check("  router gets the surplus", paid_to(bundle, ROUTER_PH, a_out) == payout - want, f"{payout - want}")
    check("  successor snapshot is V11 and rebuilds to the successor pool",
          out["pool"]["protocol_version"] == drv.PROTOCOL_VERSION and v11.snapshot_to_pool(out["pool"]).coin.name().hex() == out["v11"]["successor_coin_id"])
    check("  successor state moved the reserves", [int(x) for x in out["pool"]["state"]["reserves"]][i_in] == r[i_in] + gross)
    check("  details name the protocol fee and surplus", out["v11"]["protocol_fee"] == honest - payout and out["v11"]["surplus"] == payout - want)
    return out


def add_case(label, pool, pct, extra_xch=0, expect_ok=True):
    print(f"add on {label}: {pct}% of each reserve")
    snap, _ = snapshot_roundtrip(pool)
    deposits = [max(1, x * pct // 100) for x in pool.state[0]]
    honest = v11.forge_math.invariant_lp_mint(pool.state[0], deposits, pool.state[1], pool.fee_bps, pool.weights, version=10)
    offered = {}
    for a, d in zip(pool.asset_ids, deposits):
        offered[a] = d + (honest if a is None else 0)
    if None not in pool.asset_ids:
        offered[None] = honest + extra_xch
    want = honest * 99 // 100
    offer = fabricate_offer(offered, {pool.lp_asset_id: want}, salt=0x32)
    payload = {"action": "add", "offer": offer.to_bech32(), "pool": snap, "current_height": H,
               "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}}
    try:
        out = run(payload)
    except Exception as exc:
        check("  builder accepted the offer", not expect_ok, f"{type(exc).__name__}: {str(exc)[:90]}")
        return None
    if not check("  builder accepted the offer", expect_ok):
        return None
    bundle = SpendBundle.from_json_dict(out["bundle"])
    minted = out["v11"]["lp_minted"]
    check("  mint equals the honest quote", minted == honest, f"{minted}")
    check("  trader receives exactly the requested LP", paid_to(bundle, TRADER_PH, pool.lp_asset_id) == want)
    check("  router gets the LP surplus", paid_to(bundle, ROUTER_PH, pool.lp_asset_id) == minted - want)
    if None not in pool.asset_ids:
        check("  excess XCH above the backing returns to the router", paid_to(bundle, ROUTER_PH) == extra_xch, f"{extra_xch}")
    check("  total_lp grew by the mint", int(out["pool"]["state"]["total_lp"]) == pool.state[1] + minted)
    check("  successor snapshot rebuilds", v11.snapshot_to_pool(out["pool"]).coin.name().hex() == out["v11"]["successor_coin_id"])
    return out


def remove_case(label, pool, burn, ask_bps=9_900, expect_ok=True):
    print(f"remove on {label}: burn {burn}")
    snap, _ = snapshot_roundtrip(pool)
    vf = v11.forge_math.vault_fee_bps(len(pool.state[0]), 10, pool.fee_bps)
    payouts = v11.forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)
    wants = {a: p * ask_bps // 10_000 for a, p in zip(pool.asset_ids, payouts) if p * ask_bps // 10_000 > 0}
    # the trader's LP coin: a CAT of the pool's LP asset, fabricated with a CAT parent
    offer = fabricate_offer({pool.lp_asset_id: burn}, wants, salt=0x33)
    payload = {"action": "remove", "offer": offer.to_bech32(), "pool": snap, "current_height": H,
               "dev_fee": {"puzzle_hash": ROUTER_PH.hex(), "bps": 0}}
    try:
        out = run(payload)
    except Exception as exc:
        check("  builder accepted the offer", not expect_ok, f"{type(exc).__name__}: {str(exc)[:90]}")
        return None
    if not check("  builder accepted the offer", expect_ok):
        return None
    bundle = SpendBundle.from_json_dict(out["bundle"])
    for a, p in zip(pool.asset_ids, payouts):
        w = wants.get(a, 0)
        check(f"  trader paid exactly the request for asset {'XCH' if a is None else a.hex()[:6]}", paid_to(bundle, TRADER_PH, a) == w)
        check("  ...and the router the surplus", paid_to(bundle, ROUTER_PH, a) == p - w, f"{p - w}")
    check("  total_lp fell by the burn", int(out["pool"]["state"]["total_lp"]) == pool.state[1] - burn)
    check("  the melt coin is the offered LP settlement's child",
          any(cs.coin.puzzle_hash == construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MELT_INNER).get_tree_hash()
              for cs in bundle.coin_spends))
    return out


def main() -> int:
    if not drv.v11_available():
        print("  [skip] V11 puzzles not built"); return 2

    pair = make([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x41)
    cats = make([T_A, T_B], [800_000, 600_000], [1, 1], salt=0x42)
    triple = make([None, T_A, T_B], [20_000_000_000, 400_000, 300_000], [2, 1, 1], salt=0x43)
    vault = make([T_A], [1_000_000], [1], salt=0x44)

    print("snapshot round trip:")
    for label, pool in (("pair", pair), ("cats", cats), ("triple", triple), ("vault", vault)):
        snap, back = snapshot_roundtrip(pool)
        check(f"  {label}: snapshot rebuilds the same pool coin and inner", back.coin == pool.coin and back.inner_hash == pool.inner_hash)
        check(f"  {label}: state integers are strings in the snapshot", all(isinstance(x, str) for x in snap["state"]["reserves"]))
    bad = dict(v11.pool_to_snapshot(pair)); bad["state"] = dict(bad["state"]); bad["state"]["total_lp"] = "999"
    try:
        v11.snapshot_to_pool(bad); check("  a snapshot with altered state is refused", False)
    except ValueError as exc:
        check("  a snapshot with altered state is refused", "does not hash" in str(exc))
    try:
        forge_stdin._pool(v11.pool_to_snapshot(pair)); check("  the V3Pool reader refuses a V11 snapshot", False)
    except ValueError as exc:
        check("  the V3Pool reader refuses a V11 snapshot", "forge_v11_offer" in str(exc))

    swap_case("pair (XCH in)", pair, 0, 1, 50_000_000)
    swap_case("pair (CAT in)", pair, 1, 0, 2_000)
    swap_case("cats", cats, 0, 1, 10_000)
    swap_case("triple (weighted)", triple, 1, 2, 5_000)
    swap_case("pair, greedy trader", pair, 0, 1, 50_000_000, ask_bps=10_100, expect_ok=False)
    swap_case("pair, trader asks exactly the payout", pair, 0, 1, 50_000_000, ask_bps=10_000)

    add_case("pair", pair, 5)
    add_case("cats (XCH rides only as backing)", cats, 5, extra_xch=1234)
    add_case("triple", triple, 3)
    add_case("vault", vault, 10)

    remove_case("pair", pair, 20_000)
    remove_case("cats", cats, 5_000)
    remove_case("triple", triple, 7_000)
    remove_case("vault", vault, 10_000)
    remove_case("pair, greedy trader", pair, 20_000, ask_bps=10_050, expect_ok=False)

    print("lane guards:")
    snap = v11.pool_to_snapshot(pair)
    offer = fabricate_offer({None: 1_000_000}, {T_A: 1}, salt=0x39)
    for action, payload in (("multihop-swap", {"action": "multihop-swap", "offer": offer.to_bech32(), "pools": [snap], "path": [ZERO_32.hex(), T_A.hex()]}),
                            ("swap without height", {"action": "swap", "offer": offer.to_bech32(), "pool": snap})):
        try:
            run(payload); check(f"  {action} is refused with a clear reason", False)
        except ValueError as exc:
            check(f"  {action} is refused with a clear reason", "V11" in str(exc), str(exc)[:80])

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} V11 offer-lane checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
