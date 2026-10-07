#!/usr/bin/env python3
"""Settle a trader's signed Offer against a V11 pool, with no key of our own.

This is `scripts/v11_offer_router.py` made into a library the responder can call
through forge_stdin: the same three shapes (swap, add, remove) that ran on
testnet11 on 2026-09-05, minus the router's own funding coin. Everything the
bundle needs comes out of the offer:

  * the offer's OFFER_MOD settlement coins are the leaf's inputs, spent under
    their own coin id with no payments; the leaf asserts that announcement;
  * the pool's payout coin (the reserve's OFFER_MOD child; on an add, the LP
    eve's mint to OFFER_MOD) pays the offer's *requested* notarized payments --
    the nonces the trader's spends assert -- plus one more group paying the
    surplus above the request to `surplus_ph`, the router's fee;
  * on an add the offered XCH carries the LP mint's backing (every CAT mojo is
    an XCH mojo) and is the parent of the one-mojo LP eve, which the settlement
    creates through a second payment group nobody has to assert;
  * on a remove the offered LP settlement pays the pinned melt inner, so the
    melt coin has a real CAT parent (finding 4 stays closed here too);
  * the network fee is the trader's: whatever their offer leaves unspent.

Also here: the V11 snapshot the responder round-trips through the deployment
index (`pool_to_snapshot` / `snapshot_to_pool`). It keeps every key the V10
snapshot had that the JS side reads (pool_coin_id, pool_coin, reserves[].coin,
total_lp, lp_asset_id, asset_ids, weights, fee_bps, protocol_*), and adds the
V11 state. Large integers (oracle cumulatives, state amounts) are strings, since
the snapshot passes through JSON.parse in node on its way back.

No RPC, no signing, no submission: the caller supplies the current height.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forge_math  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import (  # noqa: E402
    CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH, Offer  # noqa: E402
from chia_rs import Coin, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v15_driver as drv  # noqa: E402
from forge_offer import ZERO_32, find_offer_settlements, requested_amounts, requested_solution  # noqa: E402

PROTOCOL_VERSION = drv.PROTOCOL_VERSION      # 11
OFFER_PH = bytes32(OFFER_MOD_HASH)
# The frontend keys its fee mode and unbalanced-join support on the join rule;
# V11's mint is V10's (forge_math version 10), so it declares the same rule.
JOIN_RULE = "geometric-invariant-v3"
MATH_VERSION = 10


class OfferRejected(ValueError):
    """The offer cannot settle against this pool at this state."""


@dataclass(frozen=True)
class SettleResult:
    bundle: SpendBundle
    pool: drv.V15Pool          # the pool after the spend
    details: dict[str, Any]


# ---- snapshot -------------------------------------------------------------------------------------

def _hex(value: bytes) -> str:
    return bytes(value).hex()


def _b32(value: Any) -> bytes32:
    text = str(value).removeprefix("0x")
    if len(text) != 64:
        raise ValueError(f"expected a 32-byte hex value, got {value!r}")
    return bytes32.fromhex(text)


def _coin_json(coin: Coin) -> dict[str, Any]:
    return {"parent_coin_info": _hex(coin.parent_coin_info), "puzzle_hash": _hex(coin.puzzle_hash), "amount": int(coin.amount)}


def _coin(value: dict[str, Any]) -> Coin:
    return Coin(_b32(value["parent_coin_info"]), _b32(value["puzzle_hash"]), uint64(int(value["amount"])))


def _lineage_json(lineage: LineageProof | None) -> dict[str, Any] | None:
    if lineage is None or lineage.parent_name is None:
        return None
    return {
        "parent_name": _hex(lineage.parent_name),
        "inner_puzzle_hash": None if lineage.inner_puzzle_hash is None else _hex(lineage.inner_puzzle_hash),
        "amount": int(lineage.amount),
    }


def _lineage(value: dict[str, Any] | None) -> LineageProof | None:
    if not value:
        return None
    inner = value.get("inner_puzzle_hash")
    return LineageProof(_b32(value["parent_name"]), None if inner is None else _b32(inner), uint64(int(value["amount"])))


def is_pool_snapshot(value: dict[str, Any] | None) -> bool:
    try:
        return int((value or {}).get("protocol_version") or 0) == PROTOCOL_VERSION
    except (TypeError, ValueError):
        return False


def pool_to_snapshot(pool: drv.V15Pool) -> dict[str, Any]:
    reserves, total_lp, fees_owed, (last_height, cums, last_spot), prev_root, dao_fee_bps, dao_owed, reserve_parents = pool.state   # V15
    return {
        "protocol_version": PROTOCOL_VERSION,
        "join_rule": JOIN_RULE,
        # What V10 called the pool module: the puzzle every V11 pool's inner is an
        # instance of. The frontend only asks that these two are strings.
        "pool_module_hash": _hex(drv.ACTION_LAYER.get_tree_hash()),
        # The action layer is shared by V14 and V15, so the index tells revisions apart by the
        # LP TAIL mod hash, which every revision changes (its PROTOCOL_VERSION is curried
        # into every LP coin). api/_deploymentIndex.js believes the (action, tail) pair.
        "revision_hash": _hex(drv.LP_TAIL_MOD.get_tree_hash()),
        "reserve_inner_puzzle_hash": _hex(pool.reserves[0].inner_hash),
        "reserve_inner_puzzle_hashes": [_hex(r.inner_hash) for r in pool.reserves],
        "launcher_id": _hex(pool.launcher_id),
        "launcher_parent": _hex(pool.launcher_parent),
        "pool_coin_id": _hex(pool.coin.name()),
        "pool_coin": _coin_json(pool.coin),
        "pool_lineage_parent_name": _hex(pool.lineage.parent_name),
        "parent_inner_puzzle_hash": None if pool.lineage.inner_puzzle_hash is None else _hex(pool.lineage.inner_puzzle_hash),
        "inner_puzzle_hash": _hex(pool.inner_hash),
        "merkle_root": _hex(pool.merkle_root),
        "asset_ids": [_hex(ZERO_32 if a is None else a) for a in pool.asset_ids],
        "weights": [int(w) for w in pool.weights],
        "fee_bps": int(pool.fee_bps),
        "protocol_fee_bps": int(pool.protocol_fee_bps),
        "protocol_puzzle_hash": _hex(pool.protocol_ph),
        # V11.1: the DAO recipient is config; the rate is state and may only fall
        "dao_puzzle_hash": _hex(pool.dao_ph),
        "dao_fee_bps": int(dao_fee_bps),
        "total_lp": int(total_lp),
        "lp_asset_id": _hex(pool.lp_asset_id),
        "reserves": [{"coin": _coin_json(r.coin), "lineage_proof": _lineage_json(r.lineage)} for r in pool.reserves],
        "state": {
            "reserves": [str(int(x)) for x in reserves],
            "total_lp": str(int(total_lp)),
            "fees_owed": [str(int(x)) for x in fees_owed],
            "oracle": {"last_height": int(last_height), "cums": [str(int(c)) for c in cums],
                       "last_spot": [str(int(c)) for c in last_spot]},   # V15
            "prev_root": _hex(prev_root),
            "dao_fee_bps": int(dao_fee_bps),
            "dao_owed": [str(int(x)) for x in dao_owed],
            "reserve_parents": [_hex(p) for p in reserve_parents],   # V15
            "birth": int(pool.birth),   # V15
        },
    }


def snapshot_to_pool(value: dict[str, Any]) -> drv.V15Pool:
    """Rebuild the pool from its snapshot and prove the snapshot is authentic: the
    re-curried inner must hash to the recorded pool coin's puzzle hash, which is
    V11's equivalent of V10's module- and reserve-hash checks."""
    if not is_pool_snapshot(value):
        raise ValueError(f"not a V{PROTOCOL_VERSION} snapshot")
    assets = [None if _b32(a) == ZERO_32 else _b32(a) for a in value["asset_ids"]]
    st = value["state"]
    state = [
        [int(x) for x in st["reserves"]],
        int(st["total_lp"]),
        [int(x) for x in st["fees_owed"]],
        [int(st["oracle"]["last_height"]), [int(c) for c in st["oracle"]["cums"]],
         [int(c) for c in st["oracle"]["last_spot"]]],   # V15
        _b32(st["prev_root"]),
        int(st.get("dao_fee_bps") or 0),
        [int(x) for x in (st.get("dao_owed") or [0] * len(assets))],
        [_b32(x) for x in st["reserve_parents"]],   # V15: exact, so the re-curried inner hashes to the recorded coin
    ]
    if len(state[0]) != len(assets) or len(state[2]) != len(assets) or len(state[3][1]) != max(0, len(assets) - 1) \
            or len(state[3][2]) != max(0, len(assets) - 1) \
            or len(state[6]) != len(assets) or len(state[7]) != len(assets):
        raise ValueError("V11 snapshot state does not match its asset count")
    reserve_coins = [(_coin(r["coin"]), _lineage(r.get("lineage_proof"))) for r in value["reserves"]]
    pool = drv.make_pool(
        assets, state[0], total_lp=state[1], fees=state[2], leaves="forge",
        weights=[int(w) for w in value["weights"]], fee_bps=int(value["fee_bps"]),
        protocol_fee_bps=int(value.get("protocol_fee_bps") or 0),
        protocol_ph=_b32(value["protocol_puzzle_hash"]),
        launcher_parent=_b32(value["launcher_parent"]), reserve_coins=reserve_coins, state=state,
        dao_ph=_b32(value.get("dao_puzzle_hash") or ZERO_32.hex()),
    )
    coin = _coin(value["pool_coin"])
    parent_inner = value.get("parent_inner_puzzle_hash")
    lineage = LineageProof(_b32(value["pool_lineage_parent_name"]),
                           None if parent_inner is None else _b32(parent_inner), uint64(1))
    # V15: the coin's birth height. `pool_to_snapshot` writes this INSIDE `state`, so
    # reading it from the snapshot root silently produced 0 on every round trip -- and a
    # pool rebuilt with birth 0 asserts ASSERT_MY_BIRTH_HEIGHT against zero and is refused
    # by consensus every time. The deploy script never noticed because it holds the pool
    # object in memory and never round-trips; the website only ever round-trips, so every
    # offer-lane swap, add and remove failed from the moment V15 went live. Read where it
    # is written, and keep accepting a root-level `birth` for snapshots written elsewhere.
    state_birth = (value.get("state") or {}).get("birth")
    birth = state_birth if state_birth is not None else value.get("birth")
    pool = replace(pool, coin=coin, lineage=lineage, birth=int(birth or 0))
    if pool.launcher_id != _b32(value["launcher_id"]):
        raise ValueError("V11 snapshot launcher id does not follow from its launcher parent")
    expected = puzzle_for_singleton(pool.launcher_id, pool.inner).get_tree_hash()
    if bytes(coin.puzzle_hash) != bytes(expected):
        raise ValueError("V11 snapshot state does not hash to its pool coin's puzzle")
    if coin.name().hex() != str(value["pool_coin_id"]).removeprefix("0x"):
        raise ValueError("V11 snapshot pool_coin_id does not name pool_coin")
    return pool


# ---- shared pieces --------------------------------------------------------------------------------

def settle_ph(asset: bytes32 | None) -> bytes32:
    return OFFER_PH if asset is None else bytes32(construct_cat_puzzle(CAT_MOD, asset, OFFER_MOD).get_tree_hash())


def _own_group(coin: Coin, extra_groups: list | None = None) -> Program:
    """An OFFER_MOD input: one group under the coin's own id with no payments (the
    announcement the leaf asserts), plus any extra groups the caller needs."""
    return Program.to([[coin.name()], *(extra_groups or [])])


CREATE_COIN = 51
ASSERT_PUZZLE_ANNOUNCEMENT = 63


# A maker's puzzle reveal is untrusted input: it arrived in an offer string. It runs under the
# cost cap forge_offer applies to the same reveals, so a reveal that never halts is cut off
# instead of stalling the responder in here (F2, 2026-10-07 review).
MAX_REVEAL_COST = 11_000_000_000


def _reveal_conditions(spend, max_cost: int = MAX_REVEAL_COST) -> Program | None:
    """What an untrusted spend's reveal produces, or None when it fails or exceeds `max_cost`."""
    try:
        _, conditions = Program.from_bytes(bytes(spend.puzzle_reveal)).run_with_cost(
            max_cost, Program.from_bytes(bytes(spend.solution)))
        return conditions
    except Exception:  # noqa: BLE001 -- a spend that does not run asserts nothing
        return None


def _asserted_announcements(offer: Offer) -> set[bytes]:
    """Every puzzle announcement the maker's SIGNED spends assert."""
    asserted: set[bytes] = set()
    for spend in _trader_spends(offer):
        conditions = _reveal_conditions(spend)
        if conditions is None:
            continue
        for condition in conditions.as_iter():
            try:
                items = list(condition.as_iter())
                if len(items) >= 2 and items[0].as_int() == ASSERT_PUZZLE_ANNOUNCEMENT:
                    asserted.add(bytes(items[1].as_atom()))
            except Exception:  # noqa: BLE001 -- not a condition shape we read
                continue
    return asserted


def _asset_name(asset: bytes32 | None) -> str:
    return "XCH" if asset is None else asset.hex()[:8]


def bound_groups(offer: Offer, asset: bytes32 | None) -> list[Program]:
    """The offer's requested groups of `asset`, every one of them asserted by the maker's
    signed spends: the settlement solution nobody but the trader could have written.

    Requested payment groups are unsigned offer metadata (2026-09-26 external review,
    finding 14): anyone relaying the offer can add a group of their own. What a relayer
    cannot forge is the maker's ASSERT_PUZZLE_ANNOUNCEMENT of a group. Since 2026-10-07
    a pool pays only the groups the maker asserts, so an added group is refused here
    rather than paid, and an offer that asks for the asset but binds none of its groups
    is a gift, not an offer, and is refused too.
    """
    payments = offer.get_requested_payments().get(asset, [])
    if not payments:
        return []
    asserted = _asserted_announcements(offer)
    settlement_ph = settle_ph(asset)
    groups: dict[bytes32, list] = {}
    for payment in payments:
        groups.setdefault(payment.nonce, []).append(payment)
    for nonce, group in groups.items():
        message = Program.to((nonce, [p.as_condition_args() for p in group])).get_tree_hash()
        if hashlib.sha256(bytes(settlement_ph) + bytes(message)).digest() not in asserted:
            raise OfferRejected(f"a requested payment group of {_asset_name(asset)} is not asserted by the maker's "
                                "signed spends; a pool pays only what the trader signed for")
    return list(requested_solution(offer, asset))


def exact_payout_solution(offer: Offer, asset: bytes32 | None, amount: int) -> Program:
    """The settlement solution paying out `amount` of `asset`: the maker's bound groups, which
    must add up to exactly `amount`.

    A pool pays exactly what it releases, to exactly whom the trader signed for, and
    nothing is left over. It used to pay the trader's notarised minimum and split the
    overage into a router-fee group and a refund group that no signature covered -- so
    whoever put the bundle in a block could point both at itself with every other spend
    byte for byte the same (F3, 2026-10-07 review, confirmed on the simulator). An offer
    that asks for less than the pool pays is not a bargain for anyone; it is a stale
    quote, and the trader signs again at the current price.
    """
    groups = bound_groups(offer, asset)
    requested = sum(int(p.amount) for p in offer.get_requested_payments().get(asset, []))
    if requested != amount:
        raise OfferRejected(f"the pool pays {amount} of {_asset_name(asset)}, the offer asks {requested}: an offer asks "
                            "for exactly what the pool pays at this state; quote again")
    return Program.to(groups)


def maker_payment_to(offer: Offer, asset: bytes32 | None, puzzle_hash: bytes32) -> int:
    """What the maker's own signed spends pay to `puzzle_hash` in `asset` (CAT-wrapped for a
    CAT), read off the spends as they run."""
    target = puzzle_hash if asset is None else \
        construct_cat_puzzle(CAT_MOD, asset, Program.to(puzzle_hash)).get_tree_hash_precalc(puzzle_hash)
    total = 0
    for spend in _trader_spends(offer):
        conditions = _reveal_conditions(spend)
        if conditions is None:
            continue
        for condition in conditions.as_iter():
            try:
                items = list(condition.as_iter())
                if len(items) >= 3 and items[0].as_int() == CREATE_COIN and bytes32(items[1].as_atom()) == target:
                    total += items[2].as_int()
            except Exception:  # noqa: BLE001 -- not a condition shape we read
                continue
    return total


def requested_to(offer: Offer, asset: bytes32 | None, puzzle_hash: bytes32) -> int:
    """What the offer's requested payments of `asset` pay to `puzzle_hash`."""
    return sum(int(p.amount) for p in offer.get_requested_payments().get(asset, []) if bytes32(p.puzzle_hash) == puzzle_hash)


def minimum_router_fee(settled: int, fee_bps: int) -> int:
    """The smallest fee that covers the rate on an entry whose settlement holds `settled`:
    what a quote tells the trader to pay beside the settlement coin (see router_fee_paid)."""
    if fee_bps <= 0:
        return 0
    if fee_bps >= 10_000:
        raise OfferRejected("the router's rate would consume the whole entry")
    fee = settled * int(fee_bps) // (10_000 - int(fee_bps))
    while fee < (settled + fee) * int(fee_bps) // 10_000:
        fee += 1
    while fee > 0 and fee - 1 >= (settled + fee - 1) * int(fee_bps) // 10_000:
        fee -= 1
    return fee


def router_fee_paid(offer: Offer, asset: bytes32 | None, fee_ph: bytes32, settled: int, fee_bps: int) -> int:
    """The router's rate on an entry, as the trader's own spend paid it.

    The fee is no longer carved out of the settlement coin by the router: the trader's
    signed spend creates the fee coin itself, beside the settlement coin that holds the
    net (the lane the Dexie-only swap already used, PR 73). It is therefore inside the
    trader's signature, where a farmer cannot redirect it, and it exists exactly when
    the swap does. The rate is on what the trader put in -- the net that settles plus
    the fee itself -- so an offer that pays less than that is refused.
    """
    if fee_bps <= 0:
        return 0
    paid = maker_payment_to(offer, asset, fee_ph)
    required = (settled + paid) * int(fee_bps) // 10_000
    if paid < required:
        raise OfferRejected(f"the router's fee is {fee_bps} bps of the {settled + paid} {_asset_name(asset)} put in, "
                            f"{required}; the offer's own spends pay {paid}: rebuild the offer with the fee payment")
    return paid


def _trader_spends(offer: Offer) -> list:
    return [s for s in offer.to_spend_bundle().coin_spends if s.coin.parent_coin_info != ZERO_32]


def _finish(offer: Offer, pool: drv.V15Pool, pool_bundle: SpendBundle, new_state, details: dict[str, Any]) -> SettleResult:
    bundle = SpendBundle([*_trader_spends(offer), *pool_bundle.coin_spends], offer.to_spend_bundle().aggregated_signature)
    try:
        conds, _ = drv.validate(bundle)
    except drv.Rejected as exc:
        raise OfferRejected(f"bundle fails consensus validation: {exc}") from exc
    after = pool.advance(drv.state_to_list(new_state))
    details = {**details, "cost": int(conds.cost), "successor_coin_id": _hex(after.coin.name())}
    return SettleResult(bundle, after, details)


def _pool_asset_settlements(pool: drv.V15Pool, offer: Offer):
    offered = find_offer_settlements(offer, [a for a in pool.asset_ids if a is not None])
    wanted = requested_amounts(offer)
    return offered, wanted


# ---- swap -----------------------------------------------------------------------------------------

def router_fee_side(asset_in: bytes32 | None, asset_out: bytes32 | None) -> str:
    """Which leg of a swap the router's own fee is taken from.

    XCH has priority wherever it sits: the fee is charged in XCH whenever XCH is one
    of the two legs, so the router is paid in the asset it can always use and never
    accumulates dust in a CAT nobody quotes.

      * paying XCH   -> 'input'  (XCH is `None` on the offered side)
      * receiving XCH -> 'output'
      * CAT for CAT  -> 'input'

    CAT-for-CAT lands on the input: an input fee is an explicit payment by the trader's
    own spend out of a coin they have already given up. On either side the fee is
    inside the trader's signature -- a maker-side payment on the input, a requested
    payment to the router on the output -- never a carve by the router (F3, 2026-10-07).

    Both sides of the wire derive this from the pool's assets rather than negotiating
    it, so a crafted request cannot move the fee to the cheaper leg: the front end's
    `getSwapDevFeeMode` is this same rule.
    """
    return "output" if asset_out is None else "input"


def settle_swap(pool: drv.V15Pool, offer: Offer, height: int, surplus_ph: bytes32,
                fee_bps: int = 0) -> SettleResult:
    offered, wanted = _pool_asset_settlements(pool, offer)
    ins = [a for a in offered if a in pool.asset_ids]
    if len(ins) != 1:
        raise OfferRejected(f"a swap offer offers exactly one of the pool's assets, got {len(ins)}")
    outs = [a for a in wanted if a in pool.asset_ids]
    if len(outs) != 1:
        raise OfferRejected(f"a swap offer requests exactly one of the pool's assets, got {len(outs)}")
    asset_in, asset_out = ins[0], outs[0]
    if asset_in == asset_out:
        raise OfferRejected("a swap offer cannot request the asset it offers")
    i_in, i_out = pool.asset_ids.index(asset_in), pool.asset_ids.index(asset_out)
    settlement = offered[asset_in]
    gross = int(settlement.coin.amount)
    # The router's fee comes off ONE leg, never both, and the router never carves it: on
    # the input leg the trader's own signed spend pays it beside the settlement coin, which
    # holds the net the curve sees; on the output leg it is one of the payments in the
    # trader's own requested group. Either way it exists only inside the trader's
    # signature, where a farmer cannot redirect it (F3, 2026-10-07 review). `in_fee` and
    # `out_fee` are mutually exclusive by construction.
    side = router_fee_side(asset_in, asset_out)
    in_fee = router_fee_paid(offer, asset_in, surplus_ph, gross, int(fee_bps)) if side == "input" else 0
    r, w = pool.state[0], pool.weights
    honest = forge_math.swap_output(r[i_in], r[i_out], gross, pool.fee_bps, w[i_in], w[i_out])
    pfee = honest * pool.protocol_fee_bps // 10_000 + honest * pool.dao_fee_bps // 10_000   # protocol + DAO slices
    payout = honest - pfee
    out_fee = payout * int(fee_bps) // 10_000 if side == "output" else 0
    if out_fee > 0 and requested_to(offer, asset_out, surplus_ph) < out_fee:
        raise OfferRejected(f"the router's fee is {fee_bps} bps of the {payout} {_asset_name(asset_out)} paid out, {out_fee}; "
                            f"the offer's requested payments pay the router {requested_to(offer, asset_out, surplus_ph)}: "
                            "rebuild the offer with the fee payment")
    requested = wanted[asset_out]

    extra_spends, extra_cats = [], {}
    # The leaf asserts the settlement coin's OWN group -- nonce = its coin id, no payments.
    in_solution = _own_group(settlement.coin)
    if asset_in is None:
        extra_spends.append(make_spend(settlement.coin, OFFER_MOD, in_solution))
    else:
        extra_cats.setdefault(asset_in, []).append(SpendableCAT(
            settlement.coin, asset_in, OFFER_MOD, in_solution, lineage_proof=settlement.lineage_proof))
    reserve = pool.reserves[i_out]
    payout_coin = Coin(reserve.coin.name(), settle_ph(asset_out), uint64(payout))
    sol = exact_payout_solution(offer, asset_out, payout)
    if asset_out is None:
        extra_spends.append(make_spend(payout_coin, OFFER_MOD, sol))
    else:
        extra_cats.setdefault(asset_out, []).append(SpendableCAT(
            payout_coin, asset_out, OFFER_MOD, sol,
            lineage_proof=LineageProof(reserve.coin.parent_coin_info, reserve.inner_hash, reserve.coin.amount)))

    solution = [int(height), i_in, i_out, gross, honest, *drv.settlement_ref(settlement.coin)]   # V15: parent + amount
    pool_bundle, new_state = drv.spend_action(pool, "forge_action_swap", solution, extra_spends=extra_spends, extra_cats=extra_cats)
    return _finish(offer, pool, pool_bundle, new_state, {
        "asset_in": _hex(ZERO_32 if asset_in is None else asset_in), "asset_out": _hex(ZERO_32 if asset_out is None else asset_out),
        "gross": gross, "out": honest, "protocol_fee": pfee, "requested": requested,
        # Exact settlement: nothing above the request exists, so there is no surplus
        # and no refund. Both keys are kept at zero for the readers of this record.
        "surplus": 0, "h": int(height),
        # What the ROUTER was paid, by the trader's own spend, and on which leg.
        "router_fee": in_fee + out_fee,
        "router_fee_side": side, "router_fee_bps": int(fee_bps),
        "refund": 0,
        "offered": gross + in_fee,
    })


# ---- add ------------------------------------------------------------------------------------------

def _mint(pool: drv.V15Pool, deposits: list[int]) -> int:
    return forge_math.invariant_lp_mint(pool.state[0], deposits, pool.state[1], pool.fee_bps, pool.weights, version=MATH_VERSION)


def settle_add(pool: drv.V15Pool, offer: Offer, height: int, surplus_ph: bytes32) -> SettleResult:
    offered, wanted = _pool_asset_settlements(pool, offer)
    if list(wanted) != [pool.lp_asset_id]:
        raise OfferRejected("an add offer requests this pool's LP and nothing else")
    requested = wanted[pool.lp_asset_id]
    xch = offered.get(None)
    if xch is None:
        raise OfferRejected("an add offer must include XCH: it backs the LP mint (every CAT mojo is an XCH mojo) "
                            "and is the parent of the LP eve")
    for asset in offered:
        if asset is not None and asset not in pool.asset_ids:
            raise OfferRejected("an add offer offers only the pool's assets")

    deposits = [int(offered[a].coin.amount) if a in offered else 0 for a in pool.asset_ids]
    honest = _mint(pool, deposits)
    xi = pool.asset_ids.index(None) if None in pool.asset_ids else -1
    offered_xch = int(xch.coin.amount)
    if xi >= 0:
        # The backing comes out of the offered XCH, which lowers the XCH deposit,
        # which lowers the mint: iterate to the fixed point (a few rounds).
        for _ in range(12):
            deposits[xi] = offered_xch - honest
            if deposits[xi] < 0:
                raise OfferRejected(f"offered XCH {offered_xch} does not cover the LP backing {honest}")
            new_honest = _mint(pool, deposits)
            if new_honest == honest:
                break
            honest = new_honest
        excess = 0
    else:
        excess = offered_xch - honest
        if excess < 0:
            raise OfferRejected(f"offered XCH {offered_xch} does not cover the LP backing {honest}")
    if honest <= 0:
        raise OfferRejected("the deposit mints no LP")
    if honest != requested:
        raise OfferRejected(f"the pool mints {honest} LP, the offer asks {requested}: a deposit asks for exactly what "
                            "the pool mints at this state; quote again")
    if excess > 0:
        # XCH above the backing used to come back to the depositor through a group nothing
        # signed (F3). A deposit now offers exactly the backing, and the quote says what it is.
        raise OfferRejected(f"the offer's XCH exceeds the mint's backing by {excess}: a deposit offers exactly "
                            "the backing; quote again")
    new_total = pool.state[1] + honest

    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
    # The XCH settlement creates the one-mojo eve through a second group; its first group
    # is the no-payment announcement the leaf asserts when XCH is a pool asset.
    eve_nonce = bytes32(hashlib.sha256(bytes(xch.coin.name()) + b"forge-lp-eve").digest())
    extra_spends = [make_spend(xch.coin, OFFER_MOD, _own_group(xch.coin, [(eve_nonce, [[eve_ph, 1]])]))]
    extra_cats, parents, amounts = {}, [], []
    for asset in pool.asset_ids:
        s = offered.get(asset)
        if not s:
            parents.append(ZERO_32); amounts.append(0)
            continue
        parents.append(s.coin.parent_coin_info); amounts.append(int(s.coin.amount))
        if asset is not None:
            extra_cats.setdefault(asset, []).append(SpendableCAT(
                s.coin, asset, OFFER_MOD, _own_group(s.coin), lineage_proof=s.lineage_proof))

    solution = [int(height), deposits, honest, xch.coin.name(), parents, amounts]   # V15
    probe_state, _, _, _ = drv.run_leaf(pool, "forge_action_add", solution)
    eve = Coin(xch.coin.name(), eve_ph, uint64(1))
    action = [honest, new_total, probe_state.get_tree_hash(), pool.inner_hash, ZERO_32]
    eve_spends = drv.lp_eve_ring(pool, eve, OFFER_PH, honest, action)
    lp_settlement = Coin(eve.name(), settle_ph(pool.lp_asset_id), uint64(honest))
    # A deposit is not a trade, so the router has no claim on it: the LP goes to the
    # depositor, all of it, in the groups they signed for.
    sol = exact_payout_solution(offer, pool.lp_asset_id, honest)
    lp_settle_spends = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        lp_settlement, pool.lp_asset_id, OFFER_MOD, sol,
        lineage_proof=LineageProof(eve.parent_coin_info, drv.LP_MINT_INNER.get_tree_hash(), eve.amount))]).coin_spends
    pool_bundle, new_state = drv.spend_action(
        pool, "forge_action_add", solution,
        extra_spends=[*extra_spends, *eve_spends, *lp_settle_spends], extra_cats=extra_cats)
    return _finish(offer, pool, pool_bundle, new_state, {
        "deposits": deposits, "lp_minted": honest, "requested": requested, "surplus": 0,
        "backing": honest, "excess_xch": 0, "h": int(height),
    })


# ---- remove ---------------------------------------------------------------------------------------

def settle_remove(pool: drv.V15Pool, offer: Offer, height: int, surplus_ph: bytes32) -> SettleResult:
    offered = find_offer_settlements(offer, [pool.lp_asset_id])
    if list(offered) != [pool.lp_asset_id]:
        raise OfferRejected("a remove offer offers this pool's LP and nothing else")
    lp_settlement = offered[pool.lp_asset_id]
    burn = int(lp_settlement.coin.amount)
    wanted = requested_amounts(offer)
    for asset in wanted:
        if asset not in pool.asset_ids:
            raise OfferRejected("a remove offer requests only the pool's assets")
    vf = forge_math.vault_fee_bps(len(pool.state[0]), MATH_VERSION, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)
    for asset, pay in zip(pool.asset_ids, payouts):
        if pay != wanted.get(asset, 0):
            raise OfferRejected(f"the pool pays {pay} of {_asset_name(asset)}, the offer asks {wanted.get(asset, 0)}: "
                                "a withdrawal asks for exactly what the pool pays at this state; quote again")
    if burn <= 0 or burn > pool.state[1]:
        raise OfferRejected("burn must be positive and at most the LP supply")
    new_total = pool.state[1] - burn

    # The offered LP settlement becomes the melt coin's parent: its spend pays the
    # pinned melt inner, so the melt has a real CAT parent.
    melt_inner_hash = drv.LP_MELT_INNER.get_tree_hash()
    lp_spends = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        lp_settlement.coin, pool.lp_asset_id, OFFER_MOD,
        Program.to([(lp_settlement.coin.name(), [[melt_inner_hash, burn]])]),
        lineage_proof=lp_settlement.lineage_proof)]).coin_spends
    solution = [int(height), burn, lp_settlement.coin.name(), payouts]
    probe_state, _, _, _ = drv.run_leaf(pool, "forge_action_remove", solution)
    melt = Coin(lp_settlement.coin.name(), construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MELT_INNER).get_tree_hash(), uint64(burn))
    action = [-burn, new_total, probe_state.get_tree_hash(), pool.inner_hash, ZERO_32]
    melt_spends = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        melt, pool.lp_asset_id, drv.LP_MELT_INNER, Program.to([pool.lp_tail, action]),
        lineage_proof=LineageProof(lp_settlement.coin.parent_coin_info, OFFER_PH, lp_settlement.coin.amount),
        extra_delta=-burn, limitations_program_reveal=pool.lp_tail, limitations_solution=Program.to(action))]).coin_spends
    extra_spends, extra_cats = [*lp_spends, *melt_spends], {}
    for reserve, asset, pay in zip(pool.reserves, pool.asset_ids, payouts):
        if pay <= 0:
            continue
        payout_coin = Coin(reserve.coin.name(), settle_ph(asset), uint64(pay))
        # As with a deposit: a withdrawal is not a trade, and every mojo the pool releases
        # goes to the withdrawer in the groups they signed for.
        sol = exact_payout_solution(offer, asset, pay)
        if asset is None:
            extra_spends.append(make_spend(payout_coin, OFFER_MOD, sol))
        else:
            extra_cats.setdefault(asset, []).append(SpendableCAT(
                payout_coin, asset, OFFER_MOD, sol,
                lineage_proof=LineageProof(reserve.coin.parent_coin_info, reserve.inner_hash, reserve.coin.amount)))
    pool_bundle, new_state = drv.spend_action(pool, "forge_action_remove", solution, extra_spends=extra_spends, extra_cats=extra_cats)
    return _finish(offer, pool, pool_bundle, new_state, {
        "burn": burn, "payouts": [int(p) for p in payouts], "requested": [int(wanted.get(a, 0)) for a in pool.asset_ids],
        "surplus": [0 for _ in pool.asset_ids], "h": int(height),
    })


SETTLERS = {"swap": settle_swap, "add": settle_add, "remove": settle_remove}


def settle(action: str, pool: drv.V15Pool, offer: Offer, height: int, surplus_ph: bytes32 | None = None,
           fee_bps: int = 0) -> SettleResult:
    """Dispatch by action name; the surplus defaults to the pool's protocol recipient.

    `fee_bps` is the router's own rate and applies to swaps only. A deposit or a
    withdrawal has no router fee: its overage is rounding on the trader's own
    liquidity, and those two settlers still pay it where they always did.
    """
    if action not in SETTLERS:
        raise ValueError(f"V{PROTOCOL_VERSION} settles swap, add and remove; got {action!r}")
    recipient = surplus_ph if surplus_ph is not None else pool.protocol_ph
    if action == "swap":
        return settle_swap(pool, offer, int(height), recipient, int(fee_bps))
    return SETTLERS[action](pool, offer, int(height), recipient)
