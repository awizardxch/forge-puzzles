#!/usr/bin/env python3
"""The V11 route lanes: several pools, one trader's Offer, one atomic bundle.

Multi-hop, split, flow balance, vault route and routed deposit are all the same
shape on V11: a graph of pool legs joined by the coins that carry value between
them. This module composes that graph once, and the five lanes are thin callers.

How value moves between pools (the settlement rule)
---------------------------------------------------
A V11 leaf names ONE settlement coin per input and asserts that coin's
``(id . nil)`` announcement from OFFER_MOD (or CAT-wrapped OFFER_MOD). It binds
the coin, not an amount: the mojos balance through the CAT ring the coin is
spent in, or through bundle value balance for XCH. So a pool's payout coin --
the OFFER_MOD child its reserve creates -- can be the next pool's settlement,
spent under its own id with no payments and never touching the chain unspent.

For every asset the route touches there is a **hub**: the coins holding that
asset (offer settlements, pools' payouts, vault redemptions, LP mints) are its
producers, and the legs that drink it (plus the trader's requested payment) are
its consumers. Legs that enter the route drink from the offer's settlement;
every other leg drinks from the legs before it.

* one producer, one pool consumer: the producer coin IS the consumer's
  settlement (a direct bridge, exactly how the operator multi-hop ran on
  testnet);
* otherwise the producers are spent in one ring and the first pays each pool
  consumer a **child** settlement coin of its amount, the trader's requested
  notarized payments if this asset is what they asked for, and any remainder to
  the surplus recipient; the other producers carry an empty group. A child's id
  is known before its consumer runs, so the consumer's leaf can name it.

Amounts are re-derived here in topological order against the pools' current
snapshots: a hub's consumers get their declared shares of what the hub's
producers actually hold, so conservation holds in mojos. A pool crossed more
than once runs several actions in one spend (the action layer's multi-action
form); the later action prices on the earlier one's state, as the leaves
require.

What is not here yet: a vault crossed forward (an add minting LP on the way),
whose XCH backing would have to be carved out of the entry; such a leg is
refused with a reason. Redeeming through a vault (LP in, underlying out) is.

No RPC, no signing: the caller supplies the height. The network fee is the
trader's, whatever their offer leaves unspent. NOTE: not audited; testnet only.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
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
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH, Offer  # noqa: E402
from chia_rs import Coin, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v11_driver as drv  # noqa: E402
from forge_offer import ZERO_32, find_offer_settlements, requested_amounts, requested_solution  # noqa: E402
from forge_v11_offer import MATH_VERSION, OFFER_PH, OfferRejected, settle_ph  # noqa: E402

Asset = bytes32 | None     # None is XCH


def _asset(value) -> Asset:
    """None, bytes32, or a hex string (with or without 0x); the zero id is XCH."""
    if isinstance(value, str):
        text = value[2:] if value.lower().startswith("0x") else value
        value = None if text.lower() in ("", "txch", "xch") else bytes.fromhex(text)
    return None if value is None or bytes(value) == bytes(ZERO_32) else bytes32(value)


def _hex(asset: Asset) -> str:
    return (ZERO_32 if asset is None else asset).hex()


def _key(asset: Asset) -> bytes:
    return bytes(ZERO_32 if asset is None else asset)


@dataclass(eq=False)
class Leg:
    """One pool action in the route.

    kind "swap":   asset_in -> asset_out on pool `pool`; `amount_in` is the intent
    kind "redeem": burn the vault's LP (asset_in) for its reserve asset (asset_out)
    kind "wrap":   deposit asset_in into a vault mid-route and mint its LP (asset_out) for the
                   next leg; the eve and the backing come from the offer's XCH
    kind "add":    deposit every hub asset the target holds and mint LP (last leg only)
    `entry` legs drink from the offer's settlement; the rest from earlier legs: from the
    one leg named by `feed_from` (a chain, so an asset may appear twice on a route and
    each appearance is its own coin), or from the asset's shared pot when `feed_from`
    is None (a flow, where every producer of an asset feeds every consumer of it).
    """
    pool: int
    kind: str
    asset_in: Asset = None
    asset_out: Asset = None
    amount_in: int = 0
    entry: bool = False
    feed_from: "Leg | None" = None
    # filled by the composer
    gross: int = 0
    out: int = 0
    protocol_fee: int = 0


@dataclass
class Producer:
    coin: Coin
    asset: Asset
    lineage: LineageProof | None       # CATs only
    label: str


@dataclass(frozen=True)
class RouteResult:
    bundle: SpendBundle
    pools: list                        # successors, one per distinct pool in first-use order
    legs: list                         # the Legs with gross/out filled
    details: dict[str, Any]


class _Composer:
    def __init__(self, pools: list, offer: Offer, height: int, surplus_ph: bytes32):
        self.pools = pools
        self.offer = offer
        self.h = int(height)
        self.surplus_ph = surplus_ph
        self.states = [p.state for p in pools]
        self.ephemeral = [None] * len(pools)
        self.steps: list[list] = [[] for _ in pools]
        self.extra_spends: list[list] = [[] for _ in pools]
        self.extra_cats: list[dict] = [{} for _ in pools]
        self.standalone: list = []                  # coin spends outside any pool ring
        self.offer_producers: dict[bytes, Producer] = {}
        self.leg_producers: dict[bytes, list[Producer]] = {}
        self.by_leg: dict[int, list[Producer]] = {}          # id(leg) -> what that leg produced
        self.used_ids: set = set()
        self.surplus: dict[str, int] = {}

    # -- bookkeeping ---------------------------------------------------------------------------------
    def produce(self, p: Producer, leg: Leg | None = None) -> None:
        self.leg_producers.setdefault(_key(p.asset), []).append(p)
        if leg is not None:
            self.by_leg.setdefault(id(leg), []).append(p)

    def take(self, asset: Asset, source) -> list[Producer]:
        """`source`: "offer", "legs", "all", or ("leg", id(leg)) for one leg's own output.
        A coin taken one way is gone the other way too."""
        k = _key(asset)
        out: list[Producer] = []
        if isinstance(source, tuple):
            mine = [p for p in self.by_leg.pop(source[1], []) if _key(p.asset) == k]
            rest = [p for p in self.leg_producers.get(k, []) if all(p is not m for m in mine)]
            if rest:
                self.leg_producers[k] = rest
            else:
                self.leg_producers.pop(k, None)      # an empty pot must not reach pay_trader
            return mine
        if source in ("offer", "all") and k in self.offer_producers:
            out.append(self.offer_producers.pop(k))
        if source in ("legs", "all"):
            taken = self.leg_producers.pop(k, [])
            out.extend(taken)
            for lid in list(self.by_leg):
                self.by_leg[lid] = [p for p in self.by_leg[lid] if all(p is not t for t in taken)]
        return out

    def reserve(self, pool_idx: int, asset: Asset):
        pool = self.pools[pool_idx]
        if asset not in pool.asset_ids:
            raise OfferRejected(f"pool {pool.launcher_id.hex()[:8]} does not hold {_hex(asset)[:8]}")
        return pool.reserves[pool.asset_ids.index(asset)]

    def new_coin(self, coin: Coin) -> Coin:
        if coin.name() in self.used_ids:
            raise OfferRejected("the route would create two identical coins (same parent, asset and amount); "
                                "identical coins cannot coexist in one bundle")
        self.used_ids.add(coin.name())
        return coin

    def payout_coin(self, pool_idx: int, asset: Asset, amount: int, label: str) -> Producer:
        r = self.reserve(pool_idx, asset)
        coin = self.new_coin(Coin(r.coin.name(), settle_ph(asset), uint64(amount)))
        lineage = None if asset is None else LineageProof(r.coin.parent_coin_info, r.inner_hash, r.coin.amount)
        return Producer(coin, asset, lineage, label)

    def run(self, pool_idx: int, name: str, solution: list) -> Program:
        """Queue one action on a pool and return the state it leaves behind."""
        pool = self.pools[pool_idx]
        try:
            new_state, _tagged, _base, eph = drv.run_leaf(pool, name, solution, ephemeral=self.ephemeral[pool_idx],
                                                          state=self.states[pool_idx])
        except Exception as exc:  # the leaf's own asserts: a refusal in the leaf's terms
            raise OfferRejected(f"{name} on pool {pool.launcher_id.hex()[:8]} refused: {exc}") from exc
        self.states[pool_idx] = drv.state_to_list(new_state)
        self.ephemeral[pool_idx] = eph
        self.steps[pool_idx].append((name, solution))
        return new_state

    # -- spending the hub coins ----------------------------------------------------------------------
    def spend_as_input(self, pool_idx: int, p: Producer, extra_groups: list | None = None) -> None:
        """A consumer's settlement: its own empty group, in the consuming pool's ring."""
        sol = Program.to([[p.coin.name()], *(extra_groups or [])])
        if p.asset is None:
            self.extra_spends[pool_idx].append(make_spend(p.coin, OFFER_MOD, sol))
        else:
            self.extra_cats[pool_idx].setdefault(p.asset, []).append(
                SpendableCAT(p.coin, p.asset, OFFER_MOD, sol, lineage_proof=p.lineage))

    def spend_hub(self, producers: list[Producer], payments: list, requested_groups: list, asset: Asset,
                  unpaid: int = 0) -> None:
        """All producers of one asset in one ring; the first carries the payments. `unpaid`
        mojos may stay unpaid (an LP mint's backing, drawn through bundle balance)."""
        total = sum(int(p.coin.amount) for p in producers)
        paid = sum(int(pay[1]) for pay in payments)
        for group in requested_groups:
            for item in group.rest().as_iter():
                paid += item.rest().first().as_int()
        if paid + unpaid > total:
            raise OfferRejected(f"hub for {_hex(asset)[:8]} pays {paid + unpaid} out of {total}")
        first = producers[0]
        groups = list(requested_groups)
        extra = list(payments)
        remainder = total - paid - unpaid
        if remainder > 0:
            extra.append([self.surplus_ph, remainder, [self.surplus_ph]])
            self.surplus[_hex(asset)] = self.surplus.get(_hex(asset), 0) + remainder
        if extra:
            nonce = bytes32(hashlib.sha256(bytes(first.coin.name()) + b"forge-hub").digest())
            groups.append(Program.to((nonce, extra)))
        sols = [Program.to(groups) if groups else Program.to([[first.coin.name()]])]
        sols.extend(Program.to([[p.coin.name()]]) for p in producers[1:])
        if asset is None:
            for p, sol in zip(producers, sols):
                self.standalone.append(make_spend(p.coin, OFFER_MOD, sol))
        else:
            ring = [SpendableCAT(p.coin, asset, OFFER_MOD, sol, lineage_proof=p.lineage) for p, sol in zip(producers, sols)]
            try:
                self.standalone.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, ring).coin_spends)
            except ValueError as exc:
                raise OfferRejected(f"hub ring for {_hex(asset)[:8]}: {exc}") from exc

    def children(self, producers: list[Producer], amounts: list[int], asset: Asset, extra_payments: list | None = None,
                 unpaid: int = 0) -> list[Producer]:
        """Spend the hub paying each amount to a fresh settlement child; return the children."""
        first = producers[0]
        kids = []
        for a in amounts:
            coin = self.new_coin(Coin(first.coin.name(), settle_ph(asset), uint64(a)))
            lineage = None if asset is None else LineageProof(first.coin.parent_coin_info, OFFER_PH, first.coin.amount)
            kids.append(Producer(coin, asset, lineage, f"child of {first.label}"))
        payments = [[OFFER_MOD_HASH, a] for a in amounts] + list(extra_payments or [])
        self.spend_hub(producers, payments, [], asset, unpaid=unpaid)
        return kids

    # -- the legs ------------------------------------------------------------------------------------
    def feed(self, asset: Asset, consumers: list[Leg], source) -> list[Producer]:
        """Resolve one hub: size each consumer and hand each its settlement coin."""
        producers = self.take(asset, source)
        if not producers:
            where = "the offer" if source == "offer" else "the leg before it" if isinstance(source, tuple) else "an earlier leg"
            raise OfferRejected(f"no {_hex(asset)[:8]} from {where} for a leg to consume")
        total = sum(int(p.coin.amount) for p in producers)
        declared = [max(0, int(c.amount_in)) for c in consumers]
        if sum(declared) <= 0:
            raise OfferRejected("a hub's consumers declare no input")
        if len(producers) == 1 and len(consumers) == 1:
            consumers[0].gross = total
            return producers
        # the declared shares of what the producers actually hold
        grosses = [d * total // sum(declared) for d in declared]
        # Two children of one parent with the same amount would be the same coin
        # (a 50/50 split is exactly that), so equal shares are nudged apart by a
        # mojo, moved from the later share to the earlier one; the total holds.
        for i in range(len(grosses)):
            for j in range(i + 1, len(grosses)):
                if grosses[i] == grosses[j] and grosses[j] > 1:
                    grosses[i] += 1
                    grosses[j] -= 1
        for c, g in zip(consumers, grosses):
            c.gross = g
            if g <= 0:
                raise OfferRejected(f"a leg on pool {c.pool} would receive nothing")
        return self.children(producers, grosses, asset)

    def swap(self, leg: Leg, settlement: Producer) -> None:
        pool = self.pools[leg.pool]
        i_in, i_out = pool.asset_ids.index(leg.asset_in), pool.asset_ids.index(leg.asset_out)
        r, w = self.states[leg.pool][0], pool.weights
        honest = forge_math.swap_output(r[i_in], r[i_out], leg.gross, pool.fee_bps, w[i_in], w[i_out])
        # V11.1: the DAO slice comes off the same output at the pool's CURRENT rate (state)
        pfee = honest * pool.protocol_fee_bps // 10_000 + honest * self.states[leg.pool][5] // 10_000
        leg.out, leg.protocol_fee = honest - pfee, pfee
        if leg.out <= 0:
            raise OfferRejected(f"swap on pool {pool.launcher_id.hex()[:8]} releases nothing")
        self.spend_as_input(leg.pool, settlement)
        self.run(leg.pool, "forge_action_swap", [self.h, i_in, i_out, leg.gross, honest, settlement.coin.name()])
        self.produce(self.payout_coin(leg.pool, leg.asset_out, leg.out, f"swap payout pool {leg.pool}"), leg)

    def redeem(self, leg: Leg, lp_coin: Producer) -> None:
        """Burn the LP coin in the vault: the coin pays the pinned melt inner, the melt
        coin is its child, the vault's remove pays the underlying out."""
        pool = self.pools[leg.pool]
        if lp_coin.asset != pool.lp_asset_id:
            raise OfferRejected("a redeem leg consumes the vault's own LP")
        burn = leg.gross
        state = self.states[leg.pool]
        vf = forge_math.vault_fee_bps(len(state[0]), MATH_VERSION, pool.fee_bps)
        payouts = forge_math.withdrawal_amounts(state[0], burn, state[1], vf)
        melt_inner_hash = drv.LP_MELT_INNER.get_tree_hash()
        try:
            self.standalone.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
                lp_coin.coin, pool.lp_asset_id, OFFER_MOD,
                Program.to([(lp_coin.coin.name(), [[melt_inner_hash, burn]])]), lineage_proof=lp_coin.lineage)]).coin_spends)
        except ValueError as exc:
            raise OfferRejected(f"LP settlement ring: {exc}") from exc
        probe = self.run(leg.pool, "forge_action_remove", [self.h, burn, lp_coin.coin.name(), payouts])
        melt = self.new_coin(Coin(lp_coin.coin.name(),
                                  construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MELT_INNER).get_tree_hash(), uint64(burn)))
        action = [-burn, state[1] - burn, probe.get_tree_hash(), pool.inner_hash, ZERO_32]
        try:
            self.standalone.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
                melt, pool.lp_asset_id, drv.LP_MELT_INNER, Program.to([pool.lp_tail, action]),
                lineage_proof=LineageProof(lp_coin.coin.parent_coin_info, OFFER_PH, lp_coin.coin.amount),
                extra_delta=-burn, limitations_program_reveal=pool.lp_tail, limitations_solution=Program.to(action))]).coin_spends)
        except ValueError as exc:
            raise OfferRejected(f"LP melt ring: {exc}") from exc
        leg.out = sum(payouts)
        for asset, pay in zip(pool.asset_ids, payouts):
            if pay > 0:
                self.produce(self.payout_coin(leg.pool, asset, pay, f"redeem payout pool {leg.pool}"), leg)

    def add(self, leg: Leg, requested_lp: int) -> dict[str, Any]:
        """The last leg of a routed deposit: every asset the target holds that is still in
        the bundle (offer leftovers, sale outputs) is deposited in full; the XCH also
        carries the mint's backing and parents the LP eve."""
        pool = self.pools[leg.pool]
        xch = self.take(None, "all")
        if not xch:
            raise OfferRejected("a deposit needs XCH in the offer: it backs the LP mint and parents the LP eve")
        inputs = {asset: self.take(asset, "all") for asset in pool.asset_ids if asset is not None}
        state = self.states[leg.pool]
        deposits = [sum(int(p.coin.amount) for p in inputs.get(a, [])) if a is not None else 0 for a in pool.asset_ids]
        xch_total = sum(int(p.coin.amount) for p in xch)

        def mint(d):
            return forge_math.invariant_lp_mint(state[0], d, state[1], pool.fee_bps, pool.weights, version=MATH_VERSION)

        xi = pool.asset_ids.index(None) if None in pool.asset_ids else -1
        honest = mint(deposits)
        if xi >= 0:
            for _ in range(12):
                deposits[xi] = xch_total - honest
                if deposits[xi] < 0:
                    raise OfferRejected(f"XCH in the bundle ({xch_total}) does not cover the LP backing ({honest})")
                nh = mint(deposits)
                if nh == honest:
                    break
                honest = nh
            leftover = 0
        else:
            leftover = xch_total - honest
            if leftover < 0:
                raise OfferRejected(f"XCH in the bundle ({xch_total}) does not cover the LP backing ({honest})")
        if honest <= 0:
            raise OfferRejected("the deposit mints no LP")
        if honest < requested_lp:
            raise OfferRejected(f"pool mints {honest}, trader asks {requested_lp}: the deposit cannot settle at this state")
        eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
        # The XCH hub: its first coin creates the eve and, when XCH is a deposit, is the
        # add's XCH settlement (directly, or through a child when several coins hold XCH).
        # The backing (honest - 1) stays unpaid and reaches the mint through bundle balance.
        eve_parent = xch[0]
        ids: list = []
        if xi >= 0:
            if len(xch) == 1:
                eve_nonce = bytes32(hashlib.sha256(bytes(eve_parent.coin.name()) + b"forge-lp-eve").digest())
                self.spend_as_input(leg.pool, eve_parent, [(eve_nonce, [[eve_ph, 1]])])
                xch_settlement = eve_parent
            else:
                xch_settlement = self.children(xch, [deposits[xi]], None, extra_payments=[[eve_ph, 1]], unpaid=honest - 1)[0]
                self.spend_as_input(leg.pool, xch_settlement)
        else:
            payments = [[eve_ph, 1]]
            if leftover > 0:
                payments.append([self.surplus_ph, leftover, [self.surplus_ph]])
                self.surplus["leftover_xch"] = leftover
            self.spend_hub(xch, payments, [], None, unpaid=honest - 1)
        for asset in pool.asset_ids:
            if asset is None:
                ids.append(xch_settlement.coin.name())
                continue
            ps = inputs.get(asset) or []
            if not ps:
                ids.append(ZERO_32)
                continue
            settlement = ps[0] if len(ps) == 1 else self.children(ps, [sum(int(p.coin.amount) for p in ps)], asset)[0]
            self.spend_as_input(leg.pool, settlement)
            ids.append(settlement.coin.name())
        eve = self.new_coin(Coin(eve_parent.coin.name(), eve_ph, uint64(1)))
        probe = self.run(leg.pool, "forge_action_add", [self.h, deposits, honest, eve_parent.coin.name(), ids])
        action = [honest, state[1] + honest, probe.get_tree_hash(), pool.inner_hash, ZERO_32]
        self.standalone.extend(drv.lp_eve_ring(pool, eve, OFFER_PH, honest, action))
        lp_coin = self.new_coin(Coin(eve.name(), settle_ph(pool.lp_asset_id), uint64(honest)))
        self.produce(Producer(lp_coin, pool.lp_asset_id,
                              LineageProof(eve.parent_coin_info, drv.LP_MINT_INNER.get_tree_hash(), eve.amount), "LP mint"))
        leg.gross, leg.out = sum(deposits), honest
        return {"deposits": {_hex(a): int(d) for a, d in zip(pool.asset_ids, deposits)}, "minted": honest,
                "backing": honest, "leftover_xch": leftover}

    def wrap(self, leg: Leg, settlement: Producer) -> dict[str, Any]:
        """A vault crossed forward: the incoming asset is deposited, the vault mints its LP,
        and the LP settlement coin is the next leg's input. Like `add`, every LP mojo is an
        XCH mojo of bundle value: the eve's one mojo is paid by the offer's XCH coin (the
        remainder of the entry hub, or the whole offered XCH when the entry is another
        asset) and the rest of the backing rides through bundle balance. XCH beyond the
        mint stays a producer, so it reaches the trader with the route's output."""
        pool = self.pools[leg.pool]
        if len(pool.asset_ids) != 1:
            raise OfferRejected("a wrap deposits into a vault")
        if leg.asset_in != pool.asset_ids[0] or leg.asset_in is None:
            raise OfferRejected("a wrap deposits the vault's own asset; an XCH vault cannot be wrapped mid-route")
        xch = self.take(None, "offer")
        if not xch:
            raise OfferRejected("a wrap needs XCH in the offer: it backs the LP mint and parents the LP eve")
        state = self.states[leg.pool]
        deposit = int(settlement.coin.amount)
        honest = forge_math.invariant_lp_mint(state[0], [deposit], state[1], pool.fee_bps, pool.weights, version=MATH_VERSION)
        if honest <= 0:
            raise OfferRejected("the wrap mints no LP")
        xch_total = sum(int(p.coin.amount) for p in xch)
        leftover = xch_total - honest
        if leftover < 0:
            raise OfferRejected(f"XCH in the offer ({xch_total}) does not cover the wrap's LP backing ({honest})")
        eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
        eve_parent = xch[0]
        for kid in self.children(xch, [leftover] if leftover > 0 else [], None, extra_payments=[[eve_ph, 1]], unpaid=honest - 1):
            self.produce(kid)
        self.spend_as_input(leg.pool, settlement)
        eve = self.new_coin(Coin(eve_parent.coin.name(), eve_ph, uint64(1)))
        probe = self.run(leg.pool, "forge_action_add", [self.h, [deposit], honest, eve_parent.coin.name(), [settlement.coin.name()]])
        action = [honest, state[1] + honest, probe.get_tree_hash(), pool.inner_hash, ZERO_32]
        self.standalone.extend(drv.lp_eve_ring(pool, eve, OFFER_PH, honest, action))
        lp_coin = self.new_coin(Coin(eve.name(), settle_ph(pool.lp_asset_id), uint64(honest)))
        self.produce(Producer(lp_coin, pool.lp_asset_id,
                              LineageProof(eve.parent_coin_info, drv.LP_MINT_INNER.get_tree_hash(), eve.amount), "wrap mint"), leg)
        leg.out = honest
        return {"wrap": {"deposit": deposit, "minted": honest, "backing": honest, "leftover_xch": leftover}}

    # -- the exits -----------------------------------------------------------------------------------
    def pay_trader(self, wanted: dict) -> dict[str, int]:
        """Every asset still held pays the trader what they asked and the router the rest."""
        if self.offer_producers:
            names = ", ".join(_hex(_asset(bytes32(k)))[:8] for k in self.offer_producers)
            raise OfferRejected(f"the offer provides {names} that no leg consumes")
        for asset, amount in wanted.items():
            if int(amount) > 0 and _key(asset) not in self.leg_producers:
                raise OfferRejected(f"the offer requests {_hex(asset)[:8]}, which the route does not release")
        paid: dict[str, int] = {}
        for key in list(self.leg_producers):
            asset = _asset(bytes32(key))
            producers = self.take(asset, "legs")
            total = sum(int(p.coin.amount) for p in producers)
            requested = int(wanted.get(asset, 0))
            if requested > total:
                raise OfferRejected(f"route releases {total} of {_hex(asset)[:8]}, trader asks {requested}: "
                                    "the offer cannot settle at this state")
            groups = list(requested_solution(self.offer, asset)) if requested else []
            self.spend_hub(producers, [], groups, asset)
            paid[_hex(asset)] = total
        return paid

    # -- assembly ------------------------------------------------------------------------------------
    def assemble(self):
        spends, successors = [], []
        for idx, pool in enumerate(self.pools):
            if not self.steps[idx]:
                continue
            try:
                bundle, new_state = drv.spend_actions(pool, self.steps[idx], extra_spends=self.extra_spends[idx],
                                                      extra_cats=self.extra_cats[idx])
            except drv.Rejected as exc:
                raise OfferRejected(f"pool {pool.launcher_id.hex()[:8]}: {exc}") from exc
            spends.extend(bundle.coin_spends)
            successors.append(pool.advance(drv.state_to_list(new_state)))
        spends.extend(self.standalone)
        offer_bundle = self.offer.to_spend_bundle()
        trader = [s for s in offer_bundle.coin_spends if s.coin.parent_coin_info != ZERO_32]
        full = SpendBundle([*trader, *spends], offer_bundle.aggregated_signature)
        try:
            conds, _ = drv.validate(full)
        except drv.Rejected as exc:
            raise OfferRejected(f"bundle fails consensus validation: {exc}") from exc
        return full, successors, int(conds.cost)


# ---- ordering and composition ------------------------------------------------------------------------

def _toposort(legs: list[Leg]) -> list[int]:
    """Every producer of a leg's input runs before it; entry legs are ready at once;
    an add runs last. A stuck sort means the intermediates form a cycle."""
    produced_by: dict[bytes, set[int]] = {}
    for i, leg in enumerate(legs):
        if leg.kind in ("swap", "redeem", "wrap"):
            produced_by.setdefault(_key(leg.asset_out), set()).add(i)
    done: set[int] = set()
    order: list[int] = []
    while len(order) < len(legs):
        progressed = False
        for i, leg in enumerate(legs):
            if i in done:
                continue
            if leg.kind == "add":
                deps = {j for j in range(len(legs)) if j != i}
            elif leg.entry:
                deps = set()
            elif leg.feed_from is not None:
                deps = {j for j, other in enumerate(legs) if other is leg.feed_from}
            else:
                deps = produced_by.get(_key(leg.asset_in), set()) - {i}
            if deps <= done:
                order.append(i)
                done.add(i)
                progressed = True
        if not progressed:
            raise OfferRejected("the route's intermediate legs form a cycle with no execution order")
    return order


def compose(pools: list, legs: list[Leg], offer: Offer, height: int, surplus_ph: bytes32) -> RouteResult:
    all_cats = {a for p in pools for a in p.asset_ids if a is not None} | {p.lp_asset_id for p in pools}
    offered = find_offer_settlements(offer, sorted(all_cats))
    wanted = requested_amounts(offer)
    for leg in legs:
        pool = pools[leg.pool]
        if leg.kind == "swap":
            if leg.asset_in not in pool.asset_ids or leg.asset_out not in pool.asset_ids:
                raise OfferRejected("a swap leg names an asset its pool does not hold")
            if leg.asset_in == leg.asset_out:
                raise OfferRejected("a swap leg cannot output what it consumes")
            if len(pool.asset_ids) == 1:
                raise OfferRejected("a vault cannot be swapped through; name its LP as the next asset to cross it "
                                    "forward (wrap), or as the input to redeem it")
        elif leg.kind == "redeem":
            if len(pool.asset_ids) != 1 or leg.asset_in != pool.lp_asset_id:
                raise OfferRejected("a redeem leg burns a vault's own LP in that vault")
            leg.asset_out = pool.asset_ids[0]
        elif leg.kind == "wrap":
            if len(pool.asset_ids) != 1 or leg.asset_in != pool.asset_ids[0] or leg.asset_in is None:
                raise OfferRejected("a wrap deposits a vault's own asset into that vault")
            leg.asset_out = pool.lp_asset_id
        elif leg.kind != "add":
            raise OfferRejected(f"unknown leg kind {leg.kind!r}")
    if sum(1 for leg in legs if leg.kind in ("add", "wrap")) > 1:
        raise OfferRejected("a route mints LP in one pool at most")
    for leg in legs:
        if leg.feed_from is not None and (leg.entry or _key(leg.feed_from.asset_out) != _key(leg.asset_in)):
            raise OfferRejected("a chained leg consumes exactly what the leg before it produced")
    for leg in legs:
        if leg.entry and _key(leg.asset_in) not in {_key(a) for a in offered}:
            raise OfferRejected(f"the offer does not provide {_hex(leg.asset_in)[:8]}, which an entry leg consumes")

    c = _Composer(pools, offer, height, surplus_ph)
    for asset, s in offered.items():
        c.offer_producers[_key(asset)] = Producer(s.coin, asset, s.lineage_proof, "offer settlement")

    order = _toposort(legs)
    add_leg = next((leg for leg in legs if leg.kind == "add"), None)
    # consumers of each (asset, source), so a hub sizes every consumer at once; a
    # chained leg's source is the one leg it follows, a flow leg's the asset's pot
    def source_of(leg: Leg):
        if leg.entry:
            return "offer"
        return ("leg", id(leg.feed_from)) if leg.feed_from is not None else "legs"
    by_input: dict[tuple, list[Leg]] = {}
    for i in order:
        leg = legs[i]
        if leg.kind != "add":
            by_input.setdefault((_key(leg.asset_in), source_of(leg)), []).append(leg)
    # When a deposit follows, the offer's share of an asset that a sale also drinks is
    # split: the sale takes its declared amount, the deposit the rest.
    # A wrap does the same with the offer's XCH: the entry swap takes its declared
    # amount and the rest backs the mint (the lane sizes that split, see multihop_swap).
    wrap_leg = next((leg for leg in legs if leg.kind == "wrap"), None)
    remainders: dict[bytes, Leg] = {}
    if wrap_leg is not None and (_key(None), "offer") in by_input and None in offered:
        s = offered[None]
        declared = sum(int(x.amount_in) for x in by_input[(_key(None), "offer")])
        rest = int(s.coin.amount) - declared
        if rest <= 0:
            raise OfferRejected("the entry consumes all the offered XCH; nothing is left to back the wrap's mint")
        marker = Leg(wrap_leg.pool, "add", asset_in=None, amount_in=rest)
        by_input[(_key(None), "offer")].append(marker)
        remainders[_key(None)] = marker
    if add_leg is not None:
        target = pools[add_leg.pool]
        for asset, s in offered.items():
            key = (_key(asset), "offer")
            if asset in target.asset_ids and key in by_input:
                declared = sum(int(x.amount_in) for x in by_input[key])
                rest = int(s.coin.amount) - declared
                if rest <= 0:
                    raise OfferRejected(f"the sales sell all the offered {_hex(asset)[:8]}; nothing is left to deposit")
                marker = Leg(add_leg.pool, "add", asset_in=asset, amount_in=rest)
                by_input[key].append(marker)
                remainders[_key(asset)] = marker

    settlements: dict[int, Producer] = {}
    extra: dict[str, Any] = {}
    for i in order:
        leg = legs[i]
        if leg.kind == "add":
            extra.update(c.add(leg, int(wanted.get(pools[leg.pool].lp_asset_id, 0))))
            continue
        if id(leg) not in settlements:
            key = (_key(leg.asset_in), source_of(leg))
            consumers = by_input[key]
            coins = c.feed(leg.asset_in, consumers, key[1])
            for cons, coin in zip(consumers, coins):
                if cons.kind == "add":
                    c.offer_producers[_key(cons.asset_in)] = coin      # the deposit's share, waiting for the add
                else:
                    settlements[id(cons)] = coin
        if leg.kind == "swap":
            c.swap(leg, settlements[id(leg)])
        elif leg.kind == "wrap":
            extra.update(c.wrap(leg, settlements[id(leg)]))
        else:
            c.redeem(leg, settlements[id(leg)])
    paid = c.pay_trader(wanted)
    bundle, successors, cost = c.assemble()
    details = {"h": int(height), "cost": cost, "paid_out": paid, "surplus": c.surplus, **extra,
               "requested": {_hex(a): int(v) for a, v in wanted.items()}}
    return RouteResult(bundle, successors, legs, details)


# ---- the lanes -------------------------------------------------------------------------------------

def _pool_index(pools: list, pool) -> int:
    for i, p in enumerate(pools):
        if p.launcher_id == pool.launcher_id:
            return i
    pools.append(pool)
    return len(pools) - 1


def _kind(pool, a_in: Asset, a_out: Asset) -> str:
    """A vault on the path is crossed by its LP: LP in redeems, LP out wraps."""
    if len(pool.asset_ids) == 1:
        if a_in == pool.lp_asset_id:
            return "redeem"
        if a_out == pool.lp_asset_id:
            return "wrap"
    return "swap"


def _chain(distinct: list, pools: list, path: list, amount_in: int) -> list[Leg]:
    if len(path) != len(pools) + 1 or not pools:
        raise OfferRejected("a route names one more asset than pools")
    assets = [_asset(a) for a in path]
    legs = [Leg(_pool_index(distinct, pool), _kind(pool, assets[i], assets[i + 1]), assets[i], assets[i + 1],
                amount_in=int(amount_in), entry=(i == 0))
            for i, pool in enumerate(pools)]
    # A chain: each leg drinks the one before it, so the route may pass an asset twice
    # (TXCH -> A -> vault LP -> TXCH -> A) and each pass is its own coin.
    for prev, leg in zip(legs, legs[1:]):
        leg.feed_from = prev
    return legs


def simulate_chain(pools: list, path: list, amount: int) -> list[int]:
    """Quote a chain hop by hop on the pools' recorded states: swap outputs net of the
    protocol fee, a wrap's mint, a redeem's payout. Returns one figure per hop."""
    assets = [_asset(a) for a in path]
    outs: list[int] = []
    for i, pool in enumerate(pools):
        a_in, a_out = assets[i], assets[i + 1]
        kind = _kind(pool, a_in, a_out)
        r, w = pool.state[0], pool.weights
        if kind == "swap":
            i_in, i_out = pool.asset_ids.index(a_in), pool.asset_ids.index(a_out)
            honest = forge_math.swap_output(r[i_in], r[i_out], amount, pool.fee_bps, w[i_in], w[i_out])
            amount = honest - honest * pool.protocol_fee_bps // 10_000 - honest * pool.dao_fee_bps // 10_000
        elif kind == "wrap":
            amount = forge_math.invariant_lp_mint(r, [amount], pool.state[1], pool.fee_bps, w, version=MATH_VERSION)
        else:
            vf = forge_math.vault_fee_bps(1, MATH_VERSION, pool.fee_bps)
            amount = forge_math.withdrawal_amounts(r, amount, pool.state[1], vf)[0]
        outs.append(amount)
    return outs


def wrap_backing(pools: list, path: list, offered_xch: int) -> tuple[int, int]:
    """Split the offered XCH between an XCH entry and a wrap further along: the entry's
    gross and the wrap's mint satisfy gross + mint == offered, found as a fixed point
    (the mint falls as the gross falls, so it converges in a few rounds)."""
    assets = [_asset(a) for a in path]
    wrap_at = next((i for i, pool in enumerate(pools) if _kind(pool, assets[i], assets[i + 1]) == "wrap"), None)
    if wrap_at is None or assets[0] is not None:
        return offered_xch, 0
    gross = offered_xch
    for _ in range(40):
        mint = simulate_chain(pools[:wrap_at + 1], path[:wrap_at + 2], gross)[-1]
        nxt = offered_xch - mint
        if nxt <= 0:
            raise OfferRejected("the offered XCH does not cover the wrap's LP backing")
        if nxt == gross:
            break
        gross = nxt
    return gross, offered_xch - gross


def multihop_swap(pools: list, path: list, offer: Offer, height: int, surplus_ph: bytes32) -> RouteResult:
    """pools[i] carries path[i] -> path[i+1]; the offer funds path[0] and requests path[-1]."""
    if len(pools) < 2:
        raise OfferRejected("a multi-hop route needs at least two pools")
    if len({p.launcher_id for p in pools}) != len(pools):
        raise OfferRejected("a multi-hop route may not reuse a pool")
    distinct: list = []
    legs = _chain(distinct, pools, path, 1)
    if any(leg.kind == "wrap" for leg in legs) and _asset(path[0]) is None:
        # The offered XCH funds the entry AND the wrap's mint; declare the entry's share
        # so the hub leaves exactly the backing for the wrap.
        all_cats = {a for p in pools for a in p.asset_ids if a is not None} | {p.lp_asset_id for p in pools}
        offered = find_offer_settlements(offer, sorted(all_cats))
        if None not in offered:
            raise OfferRejected("the offer does not provide XCH, which the entry consumes")
        legs[0].amount_in, _backing = wrap_backing(pools, path, int(offered[None].coin.amount))
    result = compose(distinct, legs, offer, height, surplus_ph)
    return RouteResult(result.bundle, result.pools, legs,
                       {**result.details, "amounts": [legs[0].gross, *[leg.out for leg in legs]]})


def split_swap(branches: list, offer: Offer, height: int, surplus_ph: bytes32) -> RouteResult:
    """branches: [(pools, path, amount_in)], pool-disjoint, sharing the entry and exit asset."""
    if len(branches) < 2:
        raise OfferRejected("a split needs at least two branches")
    if len({_asset(b[1][0]) for b in branches}) != 1 or len({_asset(b[1][-1]) for b in branches}) != 1:
        raise OfferRejected("every branch of a split shares the entry and the exit asset")
    seen: set = set()
    distinct: list = []
    legs, branch_legs = [], []
    for pools, path, amount_in in branches:
        for p in pools:
            if p.launcher_id in seen:
                raise OfferRejected("split branches may not share a pool")
            seen.add(p.launcher_id)
        mine = _chain(distinct, pools, path, amount_in)
        legs.extend(mine)
        branch_legs.append(mine)
    result = compose(distinct, legs, offer, height, surplus_ph)
    return RouteResult(result.bundle, result.pools, legs, {
        **result.details,
        "branch_amounts": [[mine[0].gross, *[leg.out for leg in mine]] for mine in branch_legs],
        "total_out": sum(mine[-1].out for mine in branch_legs)})


def flow_balance(specs: list, offer: Offer, height: int, surplus_ph: bytes32, start_asset) -> RouteResult:
    """specs: [(pool, asset_in, asset_out, amount_in)]; one bundle, a pool crossed as often
    as the plan says (each crossing one action in that pool's single spend)."""
    if len(specs) < 2:
        raise OfferRejected("a flow needs at least two legs")
    start = _asset(start_asset)
    distinct: list = []
    legs, pairs = [], set()
    for pool, a_in, a_out, amount_in in specs:
        a_in, a_out = _asset(a_in), _asset(a_out)
        pair = (pool.launcher_id, a_in, a_out)
        if pair in pairs:
            raise OfferRejected("a flow crosses each pool pair at most once; net same-pair crossings first")
        pairs.add(pair)
        legs.append(Leg(_pool_index(distinct, pool), _kind(pool, a_in, a_out), a_in, a_out, amount_in=int(amount_in), entry=(a_in == start)))
    if not any(leg.entry for leg in legs):
        raise OfferRejected("no leg of the flow consumes the start asset")
    result = compose(distinct, legs, offer, height, surplus_ph)
    return RouteResult(result.bundle, result.pools, legs, {
        **result.details, "leg_amounts": [[leg.gross, leg.out] for leg in legs],
        "total_out": result.details["paid_out"].get(_hex(start), 0)})


def vault_route(swap_pool, asset_in, vault, offer: Offer, height: int, surplus_ph: bytes32) -> RouteResult:
    """Swap `asset_in` into the vault's LP on `swap_pool`, then redeem it in the vault."""
    if len(vault.asset_ids) != 1:
        raise OfferRejected("the vault has one reserve asset")
    lp = vault.lp_asset_id
    if lp not in swap_pool.asset_ids:
        raise OfferRejected("the swap pool does not hold the vault's LP")
    a_in = _asset(asset_in)
    legs = [Leg(0, "swap", a_in, lp, amount_in=1, entry=True), Leg(1, "redeem", lp, vault.asset_ids[0], amount_in=1)]
    result = compose([swap_pool, vault], legs, offer, height, surplus_ph)
    return RouteResult(result.bundle, result.pools, legs,
                       {**result.details, "swap_out": legs[0].out, "redeemed": legs[1].out})


def routed_deposit(target, sales: list, offer: Offer, height: int, surplus_ph: bytes32) -> RouteResult:
    """sales: [(pools, path, amount_in)] selling excess into deficit assets; then the add.

    A sale may cross the target itself: that is the zap (roadmap item 9), one asset in,
    LP out, the swap and the add two actions of the same pool spend. The V10 lane refused
    it because its add was priced before the swap; here every action is priced on the
    state the previous one left, so the deposit lands on the post-swap ratio. Whether to
    balance at market or inside the pool is the planner's race, not the composer's rule.
    """
    distinct: list = []
    legs: list[Leg] = []
    for pools, path, amount_in in sales:
        if _asset(path[-1]) not in target.asset_ids:
            raise OfferRejected("a sale must end in an asset the target holds")
        legs.extend(_chain(distinct, pools, path, amount_in))
    sale_legs = list(legs)
    legs.append(Leg(_pool_index(distinct, target), "add"))
    result = compose(distinct, legs, offer, height, surplus_ph)
    return RouteResult(result.bundle, result.pools, legs,
                       {**result.details, "sale_outputs": [leg.out for leg in sale_legs]})
