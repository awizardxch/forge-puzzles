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
from chia_rs import AugSchemeMPL, Coin, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v16_driver as drv  # noqa: E402
import forge_v15_driver as drv15  # noqa: E402
import forge_v14_driver as drv14  # noqa: E402

# V14 and V15 are live beside V16 (owner, 2026-10-07 and 2026-10-09), so a route may cross
# revisions. Every pool on a route is spent by ITS OWN driver -- the module that defines its
# dataclass -- with its own leaves and message tags; the settlement coins, the hub
# arithmetic, the LP inners (byte-identical across the three builds, checked below) and the
# validator are shared, so one composer routes across all three. A pool of a revision this
# build does not carry never reaches here: forge_stdin._lane_pool refuses it.
def _drv(pool):
    return sys.modules[type(pool).__module__]


REJECTED = (drv.Rejected, drv15.Rejected, drv14.Rejected)
for _older in (drv15, drv14):
    if _older.LP_MINT_INNER is not None and drv.LP_MINT_INNER is not None:
        assert _older.LP_MINT_INNER.get_tree_hash() == drv.LP_MINT_INNER.get_tree_hash(), \
            f"the LP mint inner moved between {_older.__name__} and V16"
        assert _older.LP_MELT_INNER.get_tree_hash() == drv.LP_MELT_INNER.get_tree_hash(), \
            f"the LP melt inner moved between {_older.__name__} and V16"
from forge_offer import ZERO_32, find_offer_settlements, requested_amounts, requested_solution  # noqa: E402
from forge_v16_offer import (MATH_VERSION, OFFER_PH, OfferRejected, bound_groups, maker_payment_to,  # noqa: E402
                             minimum_router_fee, requested_to, router_fee_paid, router_surplus_band, settle_ph)

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


def nudge_equal_shares(grosses: list[int]) -> list[int]:
    """Make every share distinct while holding the total.

    Two children of one parent with the same amount would be the same coin (a
    50/50 split is exactly that), so equal shares are moved apart by a mojo,
    from the later share to the earlier one. The first version did this in one
    pass over index pairs and never re-checked a pair it had already visited:
    with four equal shares the second nudge recreated a pair the first had
    separated, and `new_coin` refused the route -- after the trader had signed
    (2026-09-26 external review, finding 13). This repeats until no two shares
    are equal. It terminates because every nudge moves a mojo from a share to
    one at least as large, which strictly raises the sum of squares, and that
    sum is bounded by the total squared. A share that would fall to zero is a
    refusal, not a collision: the old `> 1` guard let two 1-mojo shares through
    as the same coin.
    """
    shares = [int(g) for g in grosses]
    while True:
        collided = False
        for i in range(len(shares)):
            for j in range(i + 1, len(shares)):
                if shares[i] == shares[j]:
                    if shares[j] <= 1:
                        raise OfferRejected(
                            "two equal shares of one hub cannot be told apart without emptying one of them; "
                            "route less of the entry through this hub"
                        )
                    shares[i] += 1
                    shares[j] -= 1
                    collided = True
        if not collided:
            return shares

@dataclass(eq=False)
class Leg:
    """One pool action in the route.

    kind "swap":   asset_in -> asset_out on pool `pool`; `amount_in` is the intent
    kind "redeem": burn the vault's LP (asset_in) for its reserve asset (asset_out)
    kind "wrap":   deposit asset_in into a vault mid-route and mint its LP (asset_out) for the
                   next leg; the eve and the backing come from the offer's XCH
    kind "add":    deposit every hub asset the target holds and mint LP (last leg only)
    kind "remove": burn a multi-asset pool's LP (asset_in) for every reserve, proportionally;
                   swaps on the same pool chained to it (feed_from) turn the other reserves
                   into `asset_out`, so one asset leaves (the zap out, owner 2026-10-11)
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
    def __init__(self, pools: list, offer: Offer, height: int, surplus_ph: bytes32,
                 fee_bps: int = 0, fee_ph: bytes32 | None = None, preview: bool = False):
        self.pools = pools
        self.offer = offer
        self.h = int(height)
        self.surplus_ph = surplus_ph
        # A preview sizes the route from the offer's coins and reports what it releases and
        # what fee it needs -- the exact figures an offer must carry -- without settling:
        # the exactness checks are skipped and no bundle is assembled.
        self.preview = preview
        self.released: dict[str, int] = {}
        self.fee_required = 0
        # The router's fee comes off the ENTRY, once, before the route consumes it. Every
        # other fee on a route -- LP, protocol, DAO -- is inside the puzzle and already
        # paid out of each hop's output by the leaf itself, so carving here leaves the
        # route lanes with no fee logic of their own at all: they move coins, and the
        # amount they move is simply smaller.
        self.fee_bps = int(fee_bps)
        self.fee_ph = fee_ph
        self.router_fee = 0
        # A WALLET-built offer (owner, 2026-10-10: offers the trader's own wallet makes,
        # so they sit in its offer list, as Dexie and TibetSwap take them) pays the router
        # nothing itself; its fee comes off the route's payout instead (pay_trader). Set by
        # `compose` from the offer, or by a preview asked to size one (`fee_mode="surplus"`).
        self.wallet_built = False
        self.router_fee_out = 0
        self.states = [p.state for p in pools]
        self.ephemeral = [None] * len(pools)
        self.steps: list[list] = [[] for _ in pools]
        self.extra_spends: list[list] = [[] for _ in pools]
        self.extra_cats: list[dict] = [{} for _ in pools]
        self.standalone: list = []                  # coin spends outside any pool ring
        self.offer_producers: dict[bytes, Producer] = {}
        # A chain of mints (an LP minted and deposited into a second pool, one bundle): each
        # add's own XCH share, by id(add leg), so neither takes the other's backing.
        self.add_xch: dict[int, Producer] = {}
        self.leg_producers: dict[bytes, list[Producer]] = {}
        self.by_leg: dict[int, list[Producer]] = {}          # id(leg) -> what that leg produced
        self.used_ids: set = set()
        self.surplus: dict[str, int] = {}
        # Offers taken inside the route (a Dexie maker's): what they ask is paid out of the
        # trader's entry hub, under the maker's own nonce, and what they offer joins the
        # exit pot that pays the trader. Their signed spends ride in the bundle.
        self.ext_groups: dict[bytes, list] = {}
        self.ext_amount: dict[bytes, int] = {}
        self.ext_spends: list = []
        self.ext_sigs: list = []
        self.ext_details: list[dict[str, Any]] = []

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
            # Only this asset's coins: a remove produces every reserve, and each of its
            # chained swaps takes its own asset from it (a pop of the whole leg lost the rest).
            held = self.by_leg.get(source[1], [])
            mine = [p for p in held if _key(p.asset) == k]
            self.by_leg[source[1]] = [p for p in held if _key(p.asset) != k]
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
            new_state, _tagged, _base, eph = _drv(pool).run_leaf(pool, name, solution, ephemeral=self.ephemeral[pool_idx],
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
        if remainder > 0 and not self.preview:
            # A remainder used to go to the trader through a group nothing signed, which
            # whoever included the bundle could point at itself (F3, 2026-10-07 review).
            # A hub now pays out exactly what it holds, in the groups the trader signed.
            raise OfferRejected(f"the {_hex(asset)[:8]} hub holds {remainder} more than the offer asks for: an offer "
                                "asks for exactly what the route releases at this state; quote again")
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
                 unpaid: int = 0, requested_groups: list | None = None) -> list[Producer]:
        """Spend the hub paying each amount to a fresh settlement child; return the children."""
        first = producers[0]
        kids = []
        for a in amounts:
            coin = self.new_coin(Coin(first.coin.name(), settle_ph(asset), uint64(a)))
            lineage = None if asset is None else LineageProof(first.coin.parent_coin_info, OFFER_PH, first.coin.amount)
            kids.append(Producer(coin, asset, lineage, f"child of {first.label}"))
        payments = [[OFFER_MOD_HASH, a] for a in amounts] + list(extra_payments or [])
        self.spend_hub(producers, payments, list(requested_groups or []), asset, unpaid=unpaid)
        return kids

    # -- the legs ------------------------------------------------------------------------------------
    def feed(self, asset: Asset, consumers: list[Leg], source) -> list[Producer]:
        """Resolve one hub: size each consumer and hand each its settlement coin."""
        producers = self.take(asset, source)
        if not producers:
            where = "the offer" if source == "offer" else "the leg before it" if isinstance(source, tuple) else "an earlier leg"
            raise OfferRejected(f"no {_hex(asset)[:8]} from {where} for a leg to consume")
        total = sum(int(p.coin.amount) for p in producers)
        # Offers taken inside the route are paid first, out of the entry, at exactly what
        # their makers asked; the pools get the rest.
        external = self.ext_amount.get(_key(asset), 0) if source == "offer" else 0
        ext_groups = self.ext_groups.get(_key(asset), []) if source == "offer" else []
        if external >= total:
            raise OfferRejected(f"the taken offers ask {external} of {_hex(asset)[:8]}, the entry holds {total}; "
                                "nothing is left for the pools")
        # The entry is the only hub the trader funds; everything downstream is the
        # route's own coins. The router's rate on the entry is paid by the trader's own
        # signed spend, beside the settlement coin -- never carved out of it by the
        # router, which left it in a group nothing signed (F3, 2026-10-07 review). It is
        # checked here against the coin, so a crafted request cannot pay a smaller fee.
        # The rate is for the router's service, so it is on the whole entry, a taken
        # offer's share included (owner, 2026-10-05).
        if source == "offer" and self.fee_bps > 0 and self.fee_ph is not None and not self.wallet_built:
            if self.preview:
                self.fee_required += minimum_router_fee(total, self.fee_bps)
            else:
                self.router_fee += router_fee_paid(self.offer, asset, self.fee_ph, total, self.fee_bps)
        spendable = total - external
        if spendable <= 0:
            raise OfferRejected("the taken offers leave nothing of the entry for the pools")
        declared = [max(0, int(c.amount_in)) for c in consumers]
        if sum(declared) <= 0:
            raise OfferRejected("a hub's consumers declare no input")
        if len(producers) == 1 and len(consumers) == 1 and not external:
            consumers[0].gross = total
            return producers
        # the declared shares of what the producers actually hold. Each share floors, so the
        # shares can fall a few mojos short of the hub, and a hub must pay out exactly what
        # it holds: the shortfall goes to the largest share, in the preview and at settlement
        # alike. Without it a wallet-built split (the whole entry in, the declared branch
        # amounts sized on less) previewed fine and was refused when it settled -- "the
        # 00000000 hub holds 1 more than the offer asks for" (mainnet, 2026-10-12).
        shares = [d * spendable // sum(declared) for d in declared]
        short = spendable - sum(shares)
        if short > 0:
            biggest = max(range(len(shares)), key=lambda i: shares[i])
            shares[biggest] += short
        grosses = nudge_equal_shares(shares)
        for c, g in zip(consumers, grosses):
            c.gross = g
            if g <= 0:
                raise OfferRejected(f"a leg on pool {c.pool} would receive nothing")
        return self.children(producers, grosses, asset, requested_groups=ext_groups)

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
        self.run(leg.pool, "forge_action_swap", [self.h, i_in, i_out, leg.gross, honest, *drv.settlement_ref(settlement.coin)])
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

    def add(self, leg: Leg, requested_lp: int | None) -> dict[str, Any]:
        """The last leg of a routed deposit: every asset the target holds that is still in
        the bundle (offer leftovers, sale outputs) is deposited in full; the XCH also
        carries the mint's backing and parents the LP eve."""
        pool = self.pools[leg.pool]
        own = self.add_xch.pop(id(leg), None)
        xch = [own] if own is not None else self.take(None, "all")
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
        if requested_lp is not None and honest != requested_lp and not self.preview and not self.wallet_built:
            raise OfferRejected(f"the pool mints {honest} LP, the offer asks {requested_lp}: a deposit asks for exactly "
                                "what the pool mints at this state; quote again")
        if leftover > 0 and not self.preview:
            raise OfferRejected(f"the XCH in the bundle exceeds the mint's backing by {leftover}: a deposit brings "
                                "exactly the backing; quote again")
        eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
        # The XCH hub: its first coin creates the eve and, when XCH is a deposit, is the
        # add's XCH settlement (directly, or through a child when several coins hold XCH).
        # The backing (honest - 1) stays unpaid and reaches the mint through bundle balance.
        eve_parent = xch[0]
        parents: list = []
        amounts: list = []
        if xi >= 0:
            if len(xch) == 1:
                eve_nonce = bytes32(hashlib.sha256(bytes(eve_parent.coin.name()) + b"forge-lp-eve").digest())
                self.spend_as_input(leg.pool, eve_parent, [(eve_nonce, [[eve_ph, 1]])])
                xch_settlement = eve_parent
            else:
                xch_settlement = self.children(xch, [deposits[xi]], None, extra_payments=[[eve_ph, 1]], unpaid=honest - 1)[0]
                self.spend_as_input(leg.pool, xch_settlement)
        else:
            self.spend_hub(xch, [[eve_ph, 1]], [], None, unpaid=honest - 1)
        for asset in pool.asset_ids:
            if asset is None:
                parents.append(xch_settlement.coin.parent_coin_info); amounts.append(int(xch_settlement.coin.amount))
                continue
            ps = inputs.get(asset) or []
            if not ps:
                parents.append(ZERO_32); amounts.append(0)
                continue
            settlement = ps[0] if len(ps) == 1 else self.children(ps, [sum(int(p.coin.amount) for p in ps)], asset)[0]
            self.spend_as_input(leg.pool, settlement)
            parents.append(settlement.coin.parent_coin_info); amounts.append(int(settlement.coin.amount))
        eve = self.new_coin(Coin(eve_parent.coin.name(), eve_ph, uint64(1)))
        probe = self.run(leg.pool, "forge_action_add", [self.h, deposits, honest, eve_parent.coin.name(), parents, amounts])
        action = [honest, state[1] + honest, probe.get_tree_hash(), pool.inner_hash, ZERO_32]
        self.standalone.extend(_drv(pool).lp_eve_ring(pool, eve, OFFER_PH, honest, action))
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
        probe = self.run(leg.pool, "forge_action_add", [self.h, [deposit], honest, eve_parent.coin.name(), [settlement.coin.parent_coin_info], [int(settlement.coin.amount)]])
        action = [honest, state[1] + honest, probe.get_tree_hash(), pool.inner_hash, ZERO_32]
        self.standalone.extend(_drv(pool).lp_eve_ring(pool, eve, OFFER_PH, honest, action))
        lp_coin = self.new_coin(Coin(eve.name(), settle_ph(pool.lp_asset_id), uint64(honest)))
        self.produce(Producer(lp_coin, pool.lp_asset_id,
                              LineageProof(eve.parent_coin_info, drv.LP_MINT_INNER.get_tree_hash(), eve.amount), "wrap mint"), leg)
        leg.out = honest
        return {"wrap": {"deposit": deposit, "minted": honest, "backing": honest, "leftover_xch": leftover}}

    # -- the exits -----------------------------------------------------------------------------------
    def pay_trader(self, wanted: dict) -> dict[str, int]:
        """Every asset still held pays the trader exactly what they asked, in the groups
        they signed for; a route that releases more or less than the offer asks is refused."""
        if self.offer_producers:
            names = ", ".join(_hex(_asset(bytes32(k)))[:8] for k in self.offer_producers)
            raise OfferRejected(f"the offer provides {names} that no leg consumes")
        for asset, amount in wanted.items():
            if int(amount) > 0 and _key(asset) not in self.leg_producers:
                raise OfferRejected(f"the offer requests {_hex(asset)[:8]}, which the route does not release")
        paid: dict[str, int] = {}
        if self.wallet_built and len(self.leg_producers) != 1:
            # The fee is a rate on the payout; with two payout assets which one carries it
            # would be the trader's pick. Such a route keeps the site-built offer.
            raise OfferRejected("a route paying out more than one asset cannot take a wallet-built offer; "
                                "the site builds this one")
        for key in list(self.leg_producers):
            asset = _asset(bytes32(key))
            producers = self.take(asset, "legs")
            total = sum(int(p.coin.amount) for p in producers)
            requested = int(wanted.get(asset, 0))
            fee = total * self.fee_bps // 10_000 if self.wallet_built else 0
            if self.preview:
                self.released[_hex(asset)] = total
                paid[_hex(asset)] = total - fee
                self.router_fee_out = fee
                continue
            if self.wallet_built:
                # The trader's groups are paid exactly as signed; one group nobody signed pays
                # the router the rest, which must be its fee (plus a hair the pools moved in
                # the trader's favour). The most a block builder could redirect is that fee.
                surplus = total - requested
                if surplus < fee:
                    raise OfferRejected(f"the route releases {total} of {_hex(asset)[:8]}; less the router's {fee} that is "
                                        f"{total - fee}, and the offer asks {requested}: quote again")
                if surplus > fee + router_surplus_band(fee):
                    raise OfferRejected(f"the route releases {total} of {_hex(asset)[:8]}, the offer asks {requested}, leaving "
                                        f"{surplus} where the router's fee is {fee}: the price moved; quote again")
                # A fee that floors to nothing pays no router coin (a zero-amount CAT payment
                # breaks the CAT ring; see forge_v16_offer.surplus_payout_solution).
                self.spend_hub(producers, [[self.fee_ph, surplus, [self.fee_ph]]] if surplus > 0 else [],
                               bound_groups(self.offer, asset), asset)
                self.router_fee = surplus
                paid[_hex(asset)] = requested
                continue
            if requested != total:
                raise OfferRejected(f"the route releases {total} of {_hex(asset)[:8]}, the offer asks {requested}: an offer "
                                    "asks for exactly what the route releases at this state; quote again")
            self.spend_hub(producers, [], bound_groups(self.offer, asset), asset)
            paid[_hex(asset)] = total
        return paid

    # -- assembly ------------------------------------------------------------------------------------
    def assemble(self):
        spends, successors = [], []
        for idx, pool in enumerate(self.pools):
            if not self.steps[idx]:
                continue
            try:
                bundle, new_state = _drv(pool).spend_actions(pool, self.steps[idx], extra_spends=self.extra_spends[idx],
                                                      extra_cats=self.extra_cats[idx])
            except REJECTED as exc:
                raise OfferRejected(f"pool {pool.launcher_id.hex()[:8]}: {exc}") from exc
            spends.extend(bundle.coin_spends)
            successors.append(pool.advance(drv.state_to_list(new_state)))
        spends.extend(self.standalone)
        offer_bundle = self.offer.to_spend_bundle()
        trader = [s for s in offer_bundle.coin_spends if s.coin.parent_coin_info != ZERO_32]
        signature = offer_bundle.aggregated_signature
        if self.ext_sigs:
            signature = AugSchemeMPL.aggregate([signature, *self.ext_sigs])
        full = SpendBundle([*trader, *self.ext_spends, *spends], signature)
        try:
            conds, _ = drv.validate(full)
        except REJECTED as exc:
            raise OfferRejected(f"bundle fails consensus validation: {exc}") from exc
        return full, successors, int(conds.cost)


# ---- ordering and composition ------------------------------------------------------------------------

def _toposort(legs: list[Leg], pools: list | None = None) -> list[int]:
    """Every producer of a leg's input runs before it; entry legs are ready at once;
    an add runs last. A stuck sort means the intermediates form a cycle."""
    produced_by: dict[bytes, set[int]] = {}
    for i, leg in enumerate(legs):
        if leg.kind in ("swap", "redeem", "wrap", "remove"):
            produced_by.setdefault(_key(leg.asset_out), set()).add(i)
    done: set[int] = set()
    order: list[int] = []
    while len(order) < len(legs):
        progressed = False
        for i, leg in enumerate(legs):
            if i in done:
                continue
            if leg.kind == "add":
                # after every other leg, but in a chain of mints only after the adds whose LP
                # its pool holds (the others wait for it)
                deps = {j for j, other in enumerate(legs) if j != i and (other.kind != "add" or (
                    pools is not None and pools[other.pool].lp_asset_id in pools[leg.pool].asset_ids))}
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


def _take_external(c: "_Composer", maker: Offer, entry: set, exits: set, candidates: list) -> None:
    """Take a maker's offer inside the route: it must ask for one asset the trader's
    entry provides and offer one asset the trader asks for. What it asks is paid from
    the entry hub under the maker's own notarized nonce -- the payment its signed spends
    assert -- and the coin it offers joins the exit pot that pays the trader."""
    requested = maker.get_requested_payments()
    if len(requested) != 1:
        raise OfferRejected("a taken offer must ask for exactly one asset")
    (asset_in, payments), = requested.items()
    if _key(asset_in) not in entry:
        raise OfferRejected(f"a taken offer asks for {_hex(asset_in)[:8]}, which the trader does not offer")
    made = find_offer_settlements(maker, candidates)
    if len(made) != 1:
        raise OfferRejected("a taken offer must offer exactly one asset")
    (asset_out, settlement), = made.items()
    if _key(asset_out) not in exits:
        raise OfferRejected(f"a taken offer offers {_hex(asset_out)[:8]}, which the trader does not ask for")
    amount = sum(int(pay.amount) for pay in payments)
    if amount <= 0:
        raise OfferRejected("a taken offer asks for nothing")
    c.ext_groups.setdefault(_key(asset_in), []).extend(requested_solution(maker, asset_in))
    c.ext_amount[_key(asset_in)] = c.ext_amount.get(_key(asset_in), 0) + amount
    c.produce(Producer(settlement.coin, asset_out, settlement.lineage_proof, "taken offer"))
    bundle = maker.to_spend_bundle()
    c.ext_spends.extend(s for s in bundle.coin_spends if s.coin.parent_coin_info != ZERO_32)
    c.ext_sigs.append(bundle.aggregated_signature)
    c.ext_details.append({"asset_in": _hex(asset_in), "amount_in": amount,
                          "asset_out": _hex(asset_out), "amount_out": int(settlement.coin.amount)})


def compose(pools: list, legs: list[Leg], offer: Offer, height: int, surplus_ph: bytes32,
            fee_bps: int = 0, fee_ph: bytes32 | None = None, externals: list | None = None,
            preview: bool = False, fee_mode: str = "auto") -> RouteResult:
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
        elif leg.kind == "remove":
            if len(pool.asset_ids) < 2 or leg.asset_in != pool.lp_asset_id:
                raise OfferRejected("a remove leg burns a multi-asset pool's own LP in that pool")
            if leg.asset_out not in pool.asset_ids:
                raise OfferRejected("a remove leg exits in an asset its pool holds")
        elif leg.kind != "add":
            raise OfferRejected(f"unknown leg kind {leg.kind!r}")
    adds = [leg for leg in legs if leg.kind == "add"]
    if sum(1 for leg in legs if leg.kind in ("add", "wrap")) > 1:
        # A chain of mints (owner, 2026-10-12: XCH -> the 🌱💗 LP -> deposited into ⚒️🧙‍♂️, "if
        # the best route is to follow the 2 minting lps in one bundle then we should use it"):
        # several adds and no wrap, every add but one minting an LP the next add's pool holds,
        # each declaring the XCH it takes (`amount_in`: its backing, plus its XCH deposit).
        if any(leg.kind == "wrap" for leg in legs):
            raise OfferRejected("a route mints LP in one pool at most when it wraps")
        if len({leg.pool for leg in adds}) != len(adds):
            raise OfferRejected("a chain of mints adds to each pool once")
        finals = [a for a in adds if not any(pools[a.pool].lp_asset_id in pools[b.pool].asset_ids for b in adds if b is not a)]
        if len(finals) != 1:
            raise OfferRejected("a chain of mints ends in one pool, each other add minting an LP a later pool holds")
        if any(int(a.amount_in) <= 0 for a in adds):
            raise OfferRejected("every add in a chain of mints declares the XCH it takes")
    for leg in legs:
        if leg.feed_from is None:
            continue
        src = leg.feed_from
        if src.kind == "remove":
            # A remove pays out every reserve: a leg chained to it drinks one of the others.
            ok = (not leg.entry and leg.kind == "swap" and leg.pool == src.pool
                  and leg.asset_in in pools[src.pool].asset_ids and leg.asset_in != src.asset_out)
        else:
            ok = not leg.entry and _key(src.asset_out) == _key(leg.asset_in)
        if not ok:
            raise OfferRejected("a chained leg consumes exactly what the leg before it produced")
    for leg in legs:
        if leg.entry and _key(leg.asset_in) not in {_key(a) for a in offered}:
            raise OfferRejected(f"the offer does not provide {_hex(leg.asset_in)[:8]}, which an entry leg consumes")

    c = _Composer(pools, offer, height, surplus_ph, fee_bps, fee_ph, preview=preview)
    if int(fee_bps) > 0 and fee_ph is not None:
        # Wallet-built: the offer pays the router nothing, beside the entry or as a request.
        # A preview has no real offer, so it is told which kind it is sizing.
        c.wallet_built = fee_mode == "surplus" if preview else (
            all(maker_payment_to(offer, a, fee_ph) == 0 for a in offered)
            and all(requested_to(offer, a, fee_ph) == 0 for a in wanted))
    for asset, s in offered.items():
        c.offer_producers[_key(asset)] = Producer(s.coin, asset, s.lineage_proof, "offer settlement")
    candidates = sorted(all_cats | {a for a in wanted if a is not None})
    for maker in externals or []:
        _take_external(c, maker, {_key(a) for a in offered}, {_key(a) for a, v in wanted.items() if int(v) > 0}, candidates)

    order = _toposort(legs, pools)
    chain = len(adds) > 1
    add_leg = None if chain else next((leg for leg in legs if leg.kind == "add"), None)
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
        # A target without XCH still needs XCH for the mint's backing: when sales drink the
        # offered XCH, the rest is held back for the add (zap_in sizes it to the mint).
        key = (_key(None), "offer")
        if None not in target.asset_ids and None in offered and key in by_input:
            rest = int(offered[None].coin.amount) - sum(int(x.amount_in) for x in by_input[key])
            if rest <= 0:
                raise OfferRejected("the sales sell all the offered XCH; nothing is left to back the mint")
            marker = Leg(add_leg.pool, "add", asset_in=None, amount_in=rest)
            by_input[key].append(marker)
            remainders[_key(None)] = marker
        # ...or when the backing XCH comes out of a leg (a CAT zap sells some of its CAT for
        # it): the add leg declares the backing, and that share of the leg's XCH is held back.
        elif None not in target.asset_ids and int(add_leg.amount_in) > 0:
            leg_keys = [k for k in by_input if k[0] == _key(None) and k[1] != "offer"]
            if len(leg_keys) != 1:
                raise OfferRejected("the mint's backing must come from exactly one XCH source")
            marker = Leg(add_leg.pool, "add", asset_in=None, amount_in=int(add_leg.amount_in))
            by_input[leg_keys[0]].append(marker)
            remainders[_key(None)] = marker

    if chain:
        # each add's XCH, one marker per add, from the offer's XCH hub (or alone when no sale
        # drinks it); the lane sizes the shares so each covers its add exactly
        key = (_key(None), "offer")
        if None not in offered:
            raise OfferRejected("a chain of mints is backed by the offer's XCH")
        for a in adds:
            by_input.setdefault(key, []).append(Leg(a.pool, "add", asset_in=None, amount_in=int(a.amount_in), feed_from=a))
        # no sale drinks the offer's XCH (an inner pool minting by its own join): nothing
        # else would feed that hub, so it is fed here
        if all(x.kind == "add" for x in by_input[key]):
            for cons, coin in zip(by_input[key], c.feed(None, by_input[key], "offer")):
                c.add_xch[id(cons.feed_from)] = coin
    final_pool = next((a.pool for a in adds if not any(pools[a.pool].lp_asset_id in pools[b.pool].asset_ids for b in adds if b is not a)), None)

    settlements: dict[int, Producer] = {}
    extra: dict[str, Any] = {}
    for i in order:
        leg = legs[i]
        if leg.kind == "add":
            if chain and leg.pool != final_pool:
                inner = c.add(leg, None)
                extra.setdefault("inner_mints", []).append({"pool": pools[leg.pool].launcher_id.hex(), **inner})
                continue
            extra.update(c.add(leg, int(wanted.get(pools[leg.pool].lp_asset_id, 0))))
            continue
        if id(leg) not in settlements:
            key = (_key(leg.asset_in), source_of(leg))
            consumers = by_input[key]
            coins = c.feed(leg.asset_in, consumers, key[1])
            for cons, coin in zip(consumers, coins):
                if cons.kind == "add" and cons.feed_from is not None:
                    c.add_xch[id(cons.feed_from)] = coin                 # one add's own XCH in a chain of mints
                elif cons.kind == "add":
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
    if preview:
        # The figures an offer must carry to settle this route at this state: what it
        # releases per asset, and the router's fee on the entry. The successors are the
        # states the legs left behind; nothing was validated and nothing can be pushed.
        successors = [pool.advance(c.states[i]) for i, pool in enumerate(c.pools) if c.steps[i]]
        details = {"h": int(height), "paid_out": paid, "releases": dict(c.released),
                   "router_fee_required": c.fee_required,
                   "router_fee_side": "output-surplus" if c.wallet_built else "input" if c.fee_required else "none",
                   **({"router_fee_out": c.router_fee_out} if c.wallet_built or fee_mode == "surplus" else {}),
                   "router_fee_bps": int(fee_bps), **extra,
                   "requested": {_hex(a): int(v) for a, v in wanted.items()},
                   **({"taken_offers": c.ext_details} if c.ext_details else {})}
        return RouteResult(None, successors, legs, details)
    bundle, successors, cost = c.assemble()
    details = {"h": int(height), "cost": cost, "paid_out": paid, "surplus": c.surplus,
               "router_fee": c.router_fee,
               "router_fee_side": "output-surplus" if c.wallet_built else "input" if c.router_fee else "none",
               "router_fee_bps": int(fee_bps), **extra,
               "requested": {_hex(a): int(v) for a, v in wanted.items()},
               **({"taken_offers": c.ext_details} if c.ext_details else {})}
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


def wrap_backing(pools: list, path: list, offered_xch: int, fee_bps: int = 0) -> tuple[int, int]:
    """Split the offered XCH between an XCH entry and a wrap further along, and return the
    two DECLARED shares: `gross + backing == offered_xch` always.

    Take the largest `gross` whose backing still covers the mint. The mint rises with
    `gross` and the backing falls, so the predicate is monotone and a bisection is exact.

    `fee_bps` is the rate `compose` used to carve from the hub until 2026-10-07; the fee is
    now paid beside the settlement by the trader's own spend, so the lanes pass the
    settlement amount and no rate. The parameter stays for callers that size a trader's
    whole coin before the fee is split off (the fixed point is then `gross + mint == net`)."""
    assets = [_asset(a) for a in path]
    wrap_at = next((i for i, pool in enumerate(pools) if _kind(pool, assets[i], assets[i + 1]) == "wrap"), None)
    if wrap_at is None or assets[0] is not None:
        return offered_xch, 0
    head_pools, head_path, bps = pools[:wrap_at + 1], path[:wrap_at + 2], int(fee_bps)
    # `_Composer.feed` takes the fee off the hub's WHOLE amount and then prorates what is
    # left across the declared shares, so a share's net is `x * spendable // offered` --
    # not `x` less its own percentage, which is a mojo out at these sizes and refuses the
    # wrap for a one-mojo shortfall. Mirror the hub's arithmetic, do not approximate it.
    spendable = offered_xch - offered_xch * bps // 10_000

    def net(x: int) -> int:
        return x * spendable // offered_xch if offered_xch else 0

    def covered(gross: int) -> bool:
        entry = net(gross)
        if entry <= 0:
            return False
        try:
            mint = simulate_chain(head_pools, head_path, entry)[-1]
        except (ValueError, OfferRejected):
            return False
        return mint > 0 and net(offered_xch - gross) >= mint

    lo, hi, best = 1, offered_xch - 1, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if covered(mid):
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if not best:
        raise OfferRejected("the offered XCH does not cover the wrap's LP backing")
    return best, offered_xch - best


def multihop_swap(pools: list, path: list, offer: Offer, height: int, surplus_ph: bytes32, fee_bps: int = 0, fee_ph: bytes32 | None = None,
                  preview: bool = False, fee_mode: str = "auto") -> RouteResult:
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
        # The settlement already holds the net: the router's fee is paid beside it by the
        # trader's own spend, so nothing is carved from the hub and the split is exact.
        legs[0].amount_in, _backing = wrap_backing(pools, path, int(offered[None].coin.amount))
    result = compose(distinct, legs, offer, height, surplus_ph, fee_bps, fee_ph, preview=preview, fee_mode=fee_mode)
    return RouteResult(result.bundle, result.pools, legs,
                       {**result.details, "amounts": [legs[0].gross, *[leg.out for leg in legs]]})


def split_swap(branches: list, offer: Offer, height: int, surplus_ph: bytes32, fee_bps: int = 0, fee_ph: bytes32 | None = None,
               externals: list | None = None, preview: bool = False, fee_mode: str = "auto") -> RouteResult:
    """branches: [(pools, path, amount_in)], pool-disjoint, sharing the entry and exit asset.

    `externals`: maker offers (Dexie's open offers) taken inside the same bundle, each
    asking for the entry asset and offering the exit asset. With them a split may have
    one pool branch: the trade is split between the pools and the offers."""
    if len(branches) < (1 if externals else 2):
        raise OfferRejected("a split needs at least two branches, or one beside a taken offer")
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
    result = compose(distinct, legs, offer, height, surplus_ph, fee_bps, fee_ph, externals=externals, preview=preview,
                     fee_mode=fee_mode)
    taken_out = sum(int(t["amount_out"]) for t in result.details.get("taken_offers", []))
    return RouteResult(result.bundle, result.pools, legs, {
        **result.details,
        "branch_amounts": [[mine[0].gross, *[leg.out for leg in mine]] for mine in branch_legs],
        "total_out": sum(mine[-1].out for mine in branch_legs) + taken_out})


def flow_balance(specs: list, offer: Offer, height: int, surplus_ph: bytes32, start_asset, fee_bps: int = 0, fee_ph: bytes32 | None = None,
                 preview: bool = False) -> RouteResult:
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
    result = compose(distinct, legs, offer, height, surplus_ph, fee_bps, fee_ph, preview=preview)
    return RouteResult(result.bundle, result.pools, legs, {
        **result.details, "leg_amounts": [[leg.gross, leg.out] for leg in legs],
        "total_out": result.details["paid_out"].get(_hex(start), 0)})


def vault_route(swap_pool, asset_in, vault, offer: Offer, height: int, surplus_ph: bytes32, fee_bps: int = 0, fee_ph: bytes32 | None = None,
                preview: bool = False, fee_mode: str = "auto") -> RouteResult:
    """Swap `asset_in` into the vault's LP on `swap_pool`, then redeem it in the vault."""
    if len(vault.asset_ids) != 1:
        raise OfferRejected("the vault has one reserve asset")
    lp = vault.lp_asset_id
    if lp not in swap_pool.asset_ids:
        raise OfferRejected("the swap pool does not hold the vault's LP")
    a_in = _asset(asset_in)
    legs = [Leg(0, "swap", a_in, lp, amount_in=1, entry=True), Leg(1, "redeem", lp, vault.asset_ids[0], amount_in=1)]
    result = compose([swap_pool, vault], legs, offer, height, surplus_ph, fee_bps, fee_ph, preview=preview, fee_mode=fee_mode)
    return RouteResult(result.bundle, result.pools, legs,
                       {**result.details, "swap_out": legs[0].out, "redeemed": legs[1].out})


def routed_deposit(target, sales: list, offer: Offer, height: int, surplus_ph: bytes32, fee_bps: int = 0, fee_ph: bytes32 | None = None,
                   preview: bool = False, fee_mode: str = "auto") -> RouteResult:
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
    result = compose(distinct, legs, offer, height, surplus_ph, fee_bps, fee_ph, preview=preview, fee_mode=fee_mode)
    return RouteResult(result.bundle, result.pools, legs,
                       {**result.details, "sale_outputs": [leg.out for leg in sale_legs]})


def zap_in_sales(target, sale_pools: list, offered_xch: int) -> list[int]:
    """XCH into a pool that holds none (owner, 2026-10-12: a zap into the ❤️/🌱 LP found no
    route). The offered XCH is sold into each of the target's two assets on that asset's own
    XCH pool, both outputs are deposited, and what is left backs the mint: every LP mojo is
    an XCH mojo of bundle value, and the add refuses XCH beyond it. So the sales must leave
    EXACTLY the mint: `sum(sales) + mint == offered_xch`.

    The split between the two sales deposits in the target's own ratio (no imbalance fee).
    The total sold is the largest whose mint still fits; the mint moves one mojo per ~1/mint
    of the input, so the sum steps by one almost everywhere, and where it skips the
    neighbouring splits are tried. Returns the two declared sale amounts."""
    if len(target.asset_ids) != 2 or None in target.asset_ids:
        raise OfferRejected("an XCH zap through sales is for a two-asset pool without XCH")
    if len(sale_pools) != 2 or any(None not in p.asset_ids or a not in p.asset_ids
                                   for p, a in zip(sale_pools, target.asset_ids)):
        raise OfferRejected("each of the target's assets needs its own XCH pool to buy it on")
    if any(p.launcher_id == target.launcher_id for p in sale_pools) or sale_pools[0].launcher_id == sale_pools[1].launcher_id:
        raise OfferRejected("the sales and the target are three different pools")
    r = target.state[0]

    def out(i: int, amount: int) -> int:
        return simulate_chain([sale_pools[i]], [None, target.asset_ids[i]], amount)[-1] if amount > 0 else 0

    def split(total: int) -> int:
        # the first sale's share: the smallest a0 whose output leads the target's ratio
        lo, hi = 1, total - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if out(0, mid) * r[1] >= out(1, total - mid) * r[0]:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def mint(a0: int, a1: int) -> int:
        try:
            return forge_math.invariant_lp_mint(r, [out(0, a0), out(1, a1)], target.state[1], target.fee_bps,
                                                target.weights, version=MATH_VERSION)
        except (ValueError, ZeroDivisionError):
            return 0

    def fits(total: int) -> bool:
        a0 = split(total)
        m = mint(a0, total - a0)
        return m > 0 and total + m <= offered_xch

    lo, hi, best = 2, offered_xch - 2, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if fits(mid):
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if not best:
        raise OfferRejected("the offered XCH is too little to buy both assets and back the mint")
    base = split(best)
    for total in (best, best + 1, best - 1, best + 2):
        for a0 in (base, base + 1, base - 1, base + 2, base - 2):
            a1 = total - a0
            if a0 <= 0 or a1 <= 0:
                continue
            m = mint(a0, a1)
            # equal shares are nudged apart by the hub (nudge_equal_shares), which would move the backing
            if m > 0 and total + m == offered_xch and len({a0, a1, m}) == 3:
                return [a0, a1]
    raise OfferRejected("no split of this amount leaves exactly the mint's backing; change the amount by a mojo and quote again")


def zap_in_one_sale(target, sale_pool, offered_xch: int) -> tuple[object, int]:
    """XCH into a pool without XCH whose assets do not all trade against XCH (owner,
    2026-10-12: XCH -> the five-asset ⚒️🧙‍♂️ LP found no route; only 🍕 has an XCH pool):
    sell XCH into the one target asset `sale_pool` holds, deposit it one-sided through the
    target's own join, and keep back exactly the mint's backing -- `s + mint == offered`,
    `s` the largest that fits, the neighbours tried where the sum skips (as zap_in_sales).
    Returns (the asset bought, s)."""
    if None in target.asset_ids:
        raise OfferRejected("a one-sale XCH zap is for a pool without XCH")
    if sale_pool.launcher_id == target.launcher_id or None not in sale_pool.asset_ids:
        raise OfferRejected("the sale pool is another pool holding XCH")
    held = [a for a in target.asset_ids if a in sale_pool.asset_ids]
    if len(held) != 1:
        raise OfferRejected("the sale pool must hold exactly one of the target's assets")
    asset = held[0]
    i = target.asset_ids.index(asset)
    r = target.state[0]

    def mint(s: int) -> int:
        try:
            got = simulate_chain([sale_pool], [None, asset], s)[-1]
            d = [got if j == i else 0 for j in range(len(target.asset_ids))]
            return forge_math.invariant_lp_mint(r, d, target.state[1], target.fee_bps, target.weights, version=MATH_VERSION)
        except (ValueError, ZeroDivisionError, OfferRejected):
            return 0

    lo, hi, best = 1, offered_xch - 2, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        m = mint(mid)
        if m > 0 and mid + m <= offered_xch:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if not best:
        raise OfferRejected("the offered XCH is too little to buy into the pool and back the mint")
    for s in (best, best + 1, best - 1, best + 2):
        m = mint(s) if s > 0 else 0
        # equal shares of one hub are nudged apart (nudge_equal_shares), moving the backing
        if m > 0 and s + m == offered_xch and s != m:
            return asset, s
    raise OfferRejected("no sale of this amount leaves exactly the mint's backing; change the amount by a mojo and quote again")


def _xch_join_mint(pool, xch_total: int) -> int:
    """What `add` mints when its only deposit is XCH and `xch_total` also backs the mint:
    the same fixed point, step for step."""
    xi = pool.asset_ids.index(None)
    state = pool.state

    def mint(d):
        return forge_math.invariant_lp_mint(state[0], d, state[1], pool.fee_bps, pool.weights, version=MATH_VERSION)

    deposits = [0] * len(pool.asset_ids)
    try:
        honest = mint(deposits)
    except (ValueError, ZeroDivisionError):
        honest = 0
    for _ in range(12):
        deposits[xi] = xch_total - honest
        if deposits[xi] < 0:
            return 0
        try:
            nh = mint(deposits)
        except (ValueError, ZeroDivisionError):
            return 0
        if nh == honest:
            break
        honest = nh
    return honest


def zap_via_lp(target, inner, inner_sales: list, offered_xch: int) -> tuple[list[int], int, int]:
    """XCH into a pool without XCH through an LP it holds (owner, 2026-10-12: XCH -> the
    🌱💗 LP -> deposited into ⚒️🧙‍♂️; "if the best route is to follow the 2 minting lps in one
    bundle then we should use it"). The XCH mints `inner`'s LP -- by its own join when it
    holds XCH (no sales), or by buying both its assets on their XCH pools -- and that LP is
    deposited one-sided into `target`. Both mints need their backing, so
    `sum(sales) + inner_xch + mint == offered`, where inner_xch is the inner add's XCH
    (its backing, plus its XCH deposit for an XCH pool). Returns (sales, inner_xch, mint)."""
    if None in target.asset_ids or inner.lp_asset_id not in target.asset_ids:
        raise OfferRejected("a zap through an LP is for a pool without XCH that holds that LP")
    if inner.launcher_id == target.launcher_id:
        raise OfferRejected("the inner pool is another pool")
    j = target.asset_ids.index(inner.lp_asset_id)
    r = target.state[0]

    def outer(m1: int) -> int:
        if m1 <= 0:
            return 0
        d = [m1 if k == j else 0 for k in range(len(target.asset_ids))]
        try:
            return forge_math.invariant_lp_mint(r, d, target.state[1], target.fee_bps, target.weights, version=MATH_VERSION)
        except (ValueError, ZeroDivisionError):
            return 0

    if None in inner.asset_ids:
        if inner_sales:
            raise OfferRejected("an inner pool holding XCH mints by its own join; it sells on no other pool")

        def fits(x1: int) -> int:
            m2 = outer(_xch_join_mint(inner, x1))
            return m2 if m2 > 0 and x1 + m2 <= offered_xch else 0

        lo, hi, best = 1, offered_xch - 1, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if fits(mid):
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        for x1 in (best, best + 1, best - 1, best + 2):
            if x1 <= 0:
                continue
            m2 = outer(_xch_join_mint(inner, x1))
            if m2 > 0 and x1 + m2 == offered_xch and x1 != m2:
                return [], x1, m2
        raise OfferRejected("no split of this amount backs both mints exactly; change the amount by a mojo and quote again")

    if len(inner.asset_ids) != 2 or len(inner_sales) != 2:
        raise OfferRejected("an inner pool without XCH buys its two assets on their XCH pools")
    ri = inner.state[0]

    def out(i: int, amount: int) -> int:
        return simulate_chain([inner_sales[i]], [None, inner.asset_ids[i]], amount)[-1] if amount > 0 else 0

    def split(total: int) -> int:
        lo, hi = 1, total - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if out(0, mid) * ri[1] >= out(1, total - mid) * ri[0]:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def mints(a0: int, a1: int) -> tuple[int, int]:
        try:
            m1 = forge_math.invariant_lp_mint(ri, [out(0, a0), out(1, a1)], inner.state[1], inner.fee_bps, inner.weights,
                                              version=MATH_VERSION)
        except (ValueError, ZeroDivisionError):
            return 0, 0
        return m1, outer(m1)

    def fits_total(total: int) -> bool:
        a0 = split(total)
        m1, m2 = mints(a0, total - a0)
        return m1 > 0 and m2 > 0 and total + m1 + m2 <= offered_xch

    lo, hi, best = 2, offered_xch - 2, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if fits_total(mid):
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if not best:
        raise OfferRejected("the offered XCH is too little to mint both LPs")
    base = split(best)
    for total in (best, best + 1, best - 1, best + 2, best - 2):
        for a0 in (base, base + 1, base - 1, base + 2, base - 2):
            a1 = total - a0
            if a0 <= 0 or a1 <= 0:
                continue
            m1, m2 = mints(a0, a1)
            # equal shares of the XCH hub are nudged apart (nudge_equal_shares), moving a backing
            if m1 > 0 and m2 > 0 and total + m1 + m2 == offered_xch and len({a0, a1, m1, m2}) == 4:
                return [a0, a1], m1, m2
    raise OfferRejected("no split of this amount backs both mints exactly; change the amount by a mojo and quote again")


def zap_backing_sale(target, asset_in, amount: int) -> int:
    """A CAT zap into a pool holding XCH and that CAT (owner, 2026-10-12: "build the CAT-in
    zap"): every LP mojo needs an XCH mojo of bundle value, so some of the CAT is sold for
    XCH on the target itself first. The add deposits the rest of the CAT and every XCH mojo
    the mint does not take, so any sale that covers the backing settles exactly; the
    smallest one covering twice the one-sided mint (the sale and the join price the same
    curve) keeps the most in the pool's own join."""
    ci, xi = target.asset_ids.index(asset_in), target.asset_ids.index(None)
    one_sided = [amount if i == ci else 0 for i in range(len(target.asset_ids))]
    try:
        est = forge_math.invariant_lp_mint(target.state[0], one_sided, target.state[1], target.fee_bps,
                                           target.weights, version=MATH_VERSION)
    except (ValueError, ZeroDivisionError):
        est = 0
    if est <= 0:
        raise OfferRejected("the deposit mints no LP")
    need = 2 * est + 2

    def xch_for(s: int) -> int:
        try:
            return simulate_chain([target], [asset_in, None], s)[-1]
        except (ValueError, OfferRejected):
            return 0

    if xch_for(amount - 1) < need:
        raise OfferRejected("too little to zap: selling it all would not back the LP mint")
    lo, hi = 1, amount - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if xch_for(mid) >= need:
            hi = mid
        else:
            lo = mid + 1
    return lo


def zap_cat_sales(target, sale_pools: list, asset_in, amount: int) -> tuple[int, int, int]:
    """A CAT zap into a pool without XCH: sell `s` of the CAT for XCH on its XCH pool, sell
    `y` of that XCH for the target's other asset on that asset's XCH pool, deposit both, and
    the rest of the XCH backs the mint EXACTLY (the add refuses XCH beyond it):
    `xch(s) - y == mint(amount - s, other(y))`. `s` balances the deposit on the target's
    ratio; for each `s`, `y` is the largest whose mint still fits, and the neighbouring
    figures are tried where the sum skips (as zap_in_sales). Returns (s, y, backing)."""
    if len(target.asset_ids) != 2 or None in target.asset_ids or asset_in not in target.asset_ids:
        raise OfferRejected("a CAT zap through sales is for a two-asset pool without XCH, paying one of its assets")
    ci = target.asset_ids.index(asset_in)
    di = 1 - ci
    other = target.asset_ids[di]
    if len(sale_pools) != 2 or any(None not in p.asset_ids or a not in p.asset_ids for p, a in zip(sale_pools, target.asset_ids)):
        raise OfferRejected("each of the target's assets needs its own XCH pool")
    if any(p.launcher_id == target.launcher_id for p in sale_pools) or sale_pools[0].launcher_id == sale_pools[1].launcher_id:
        raise OfferRejected("the sales and the target are three different pools")
    r = target.state[0]

    def xch_for(s: int) -> int:
        return simulate_chain([sale_pools[ci]], [asset_in, None], s)[-1] if s > 0 else 0

    def other_for(y: int) -> int:
        return simulate_chain([sale_pools[di]], [None, other], y)[-1] if y > 0 else 0

    def mint(s: int, y: int) -> int:
        d = [0, 0]
        d[ci], d[di] = amount - s, other_for(y)
        try:
            return forge_math.invariant_lp_mint(r, d, target.state[1], target.fee_bps, target.weights, version=MATH_VERSION)
        except (ValueError, ZeroDivisionError):
            return 0

    def best_y(s: int) -> int:
        x = xch_for(s)
        lo, hi, best = 1, x - 1, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            m = mint(s, mid)
            if m > 0 and mid + m <= x:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best

    # the smallest s whose deposit has as much of the other asset as of the CAT, by the target's ratio
    lo, hi = 1, amount - 1
    while lo < hi:
        mid = (lo + hi) // 2
        y = best_y(mid)
        if y > 0 and other_for(y) * r[ci] >= (amount - mid) * r[di]:
            hi = mid
        else:
            lo = mid + 1
    base = lo
    for s in (base, base - 1, base + 1, base - 2, base + 2):
        if not 0 < s < amount:
            continue
        x = xch_for(s)
        y0 = best_y(s)
        for y in (y0, y0 + 1, y0 - 1, y0 + 2):
            if y <= 0:
                continue
            m = mint(s, y)
            # equal shares of one hub are nudged apart (nudge_equal_shares), moving the backing
            if m > 0 and y + m == x and y != m:
                return s, y, m
    raise OfferRejected("no split of this amount leaves exactly the mint's backing; change the amount slightly and quote again")


def zap_in(target, sale_pools: list, offer: Offer, height: int, surplus_ph: bytes32, fee_bps: int = 0,
           fee_ph: bytes32 | None = None, preview: bool = False, fee_mode: str = "auto", via=None) -> RouteResult:
    """One asset in, a pool's LP out, one bundle:
      XCH into a pool without XCH: two sales and the add (zap_in_sales), or where its assets
        do not all trade against XCH one sale deposited one-sided (zap_in_one_sale);
      a CAT into a pool holding XCH and it: a small sale on the target for the backing,
        then the add (zap_backing_sale);
      a CAT into a pool without XCH: the CAT sold for XCH, some XCH for the other asset,
        both deposited, the rest the backing (zap_cat_sales).
      XCH into a pool holding XCH: the pool's own join (a routed deposit with no sales)."""
    offered = find_offer_settlements(offer, sorted({a for a in target.asset_ids if a is not None}
                                                   | {a for p in sale_pools for a in p.asset_ids if a is not None}))
    if len(offered) != 1:
        raise OfferRejected("a zap offers one asset")
    (asset_in, settlement), = offered.items()
    amount = int(settlement.coin.amount)
    if via is not None:
        # through an LP the target holds: `via` is that LP's pool, `sale_pools` its assets' XCH pools
        if asset_in is not None:
            raise OfferRejected("a zap through an LP is paid in XCH")
        sales, inner_xch, backing = zap_via_lp(target, via, sale_pools, amount)
        distinct: list = []
        legs: list[Leg] = []
        for p, a, n in zip(sale_pools, via.asset_ids, sales):
            legs.extend(_chain(distinct, [p], [None, a], n))
        legs.append(Leg(_pool_index(distinct, via), "add", amount_in=inner_xch))
        legs.append(Leg(_pool_index(distinct, target), "add", amount_in=backing))
        result = compose(distinct, legs, offer, height, surplus_ph, fee_bps, fee_ph, preview=preview, fee_mode=fee_mode)
        return RouteResult(result.bundle, result.pools, legs,
                           {**result.details, "sale_outputs": [leg.out for leg in legs if leg.kind == "swap"]})
    if asset_in is None:
        if None in target.asset_ids:
            if sale_pools:
                raise OfferRejected("XCH into a pool holding XCH is the pool's own join; it sells on no other pool")
            return routed_deposit(target, [], offer, height, surplus_ph, fee_bps, fee_ph, preview=preview, fee_mode=fee_mode)
        if len(sale_pools) == 1:
            bought, s = zap_in_one_sale(target, sale_pools[0], amount)
            return routed_deposit(target, [([sale_pools[0]], [None, bought], s)], offer, height, surplus_ph, fee_bps, fee_ph,
                                  preview=preview, fee_mode=fee_mode)
        amounts = zap_in_sales(target, sale_pools, amount)
        sales = [([p], [None, a], n) for p, a, n in zip(sale_pools, target.asset_ids, amounts)]
        return routed_deposit(target, sales, offer, height, surplus_ph, fee_bps, fee_ph, preview=preview, fee_mode=fee_mode)
    if asset_in not in target.asset_ids:
        raise OfferRejected("a CAT zap pays in one of the pool's own assets")
    if None in target.asset_ids:
        if sale_pools:
            raise OfferRejected("a CAT zap into a pool holding XCH sells on the pool itself")
        s = zap_backing_sale(target, asset_in, amount)
        return routed_deposit(target, [([target], [asset_in, None], s)], offer, height, surplus_ph, fee_bps, fee_ph,
                              preview=preview, fee_mode=fee_mode)
    s, y, backing = zap_cat_sales(target, sale_pools, asset_in, amount)
    ci = target.asset_ids.index(asset_in)
    other = target.asset_ids[1 - ci]
    distinct: list = []
    sell = Leg(_pool_index(distinct, sale_pools[ci]), "swap", asset_in, None, amount_in=s, entry=True)
    buy = Leg(_pool_index(distinct, sale_pools[1 - ci]), "swap", None, other, amount_in=y, feed_from=sell)
    legs = [sell, buy, Leg(_pool_index(distinct, target), "add", amount_in=backing)]
    result = compose(distinct, legs, offer, height, surplus_ph, fee_bps, fee_ph, preview=preview, fee_mode=fee_mode)
    return RouteResult(result.bundle, result.pools, legs, {**result.details, "sale_outputs": [sell.out, buy.out]})


def zap_out(pool, asset_out, offer: Offer, height: int, surplus_ph: bytes32, fee_bps: int = 0, fee_ph: bytes32 | None = None,
            preview: bool = False, fee_mode: str = "auto") -> RouteResult:
    """LP in, one asset out (owner, 2026-10-11: "build the zap in and out for lp swaps").

    The pool's remove is proportional only, so the LP burns for every reserve and each
    other reserve is swapped into `asset_out` on the same pool, in the same spend: the
    remove, then the swaps, each priced on the state the one before it left. One asset
    leaves, so a wallet-built offer pays the router its rate off that payout like any swap.
    """
    a_out = _asset(asset_out)
    if len(pool.asset_ids) < 2:
        raise OfferRejected("a zap out burns a multi-asset pool's LP; a vault's LP redeems")
    if a_out not in pool.asset_ids:
        raise OfferRejected(f"the pool does not hold {_hex(a_out)[:8]}")
    remove = Leg(0, "remove", pool.lp_asset_id, a_out, amount_in=1, entry=True)
    swaps = [Leg(0, "swap", other, a_out, amount_in=1, feed_from=remove) for other in pool.asset_ids if other != a_out]
    legs = [remove, *swaps]
    result = compose([pool], legs, offer, height, surplus_ph, fee_bps, fee_ph, preview=preview, fee_mode=fee_mode)
    return RouteResult(result.bundle, result.pools, legs,
                       {**result.details, "burned": remove.gross, "removed": remove.out,
                        "swapped": [[leg.gross, leg.out] for leg in swaps]})
