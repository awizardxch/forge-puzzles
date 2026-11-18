#!/usr/bin/env python3
"""The V18 -> V19 batch move (owner, 2026-11-17): drain the owner's V18 pools into V19 twins.

For each V18 pool in a batch, ONE bundle:

  1. melts ALL of the owner's LP of that pool through the V18 pool's own `remove` (exact pro-rata
     payout; the pool is left at its burned creation dust);
  2. pays every released reserve to the owner: the XCH to the owner's address, each CAT to a coin
     of the owner's inner puzzle -- ephemeral coins, spent again in the same bundle;
  3. UNPAIRS every released Forge LP (owner rule, 2026-11-17: a V19 pool holds only V19 assets,
     plain CATs/XCH and V19 LP, never V16/V18 LP): the owner's released V16 or V18 LP is removed
     from its own pool (recursively, down to plain assets; ordinary removes of the owner's LP, each
     inner pool keeps its burned floor and stays tradable), and the plain assets are added,
     proportionally, to the inner market's V19 twin (created in an EARLIER batch), minting V19 LP
     to the owner -- so the V19 graph mirrors the V18/V16 one with V19 LP in place of older LP;
     (when the pool's own V19 market is ALREADY registered -- a twin made earlier, or any pool of
     the twin's exact key -- step 4 is an add into that pool instead of a creation: the registry
     admits one pool per key);
  4. creates and registers the pool's V19 twin (mirror rule: weights, LP fee, DAO recipient and
     rate; the assets are the V19 assets, so a nested twin's market key is new), seeded with the
     released reserves -- for a nested twin, scaled so it prices exactly like the V18 pool did
     (value ratio kept; every rounding goes to the pool, never more than the removes released;
     what is left is returned to the owner as dust and reported). The XCH reserve, every add's
     XCH settlement and every genesis LP backing come from the owner's funding coins; the released
     XCH comes back to the owner in the same bundle;
  5. pays the twin's genesis LP (all but the burned floor) to the owner.

Registrations chain on the registry's ephemeral coin; the DAO (the V18 pools' lock) consents to
every twin from its ONE proposal; every owner-key spend is signed in ONE call. Nothing here
signs or pushes: scripts/v18-to-v19-batch.py (Sage, the lock board, the node) and the simulator
test scripts/sim-v19-batch-move.py do.

The genesis supply of a twin is the owner's burned V18 LP one for one when V19's genesis bounds
allow it, else the nearest bound (floor 10^6, at most 10^6 x the scarcest reserve, at most the
XCH reserve). A pool whose released reserves admit no supply is refused by name for the owner to
decide; nothing is bought or guessed.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forge_math  # noqa: E402
import forge_v16_driver as drv16  # noqa: E402
import forge_v16_offer as off16  # noqa: E402
import forge_v18_driver as drv18  # noqa: E402
import forge_v18_offer as off18  # noqa: E402
import forge_v19_create as cre  # noqa: E402
import forge_v19_driver as drv  # noqa: E402
import forge_v19_offer as off19  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats  # noqa: E402
from chia.wallet.lineage_proof import LineageProof  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH  # noqa: E402
from chia_rs import Coin, G2Element, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

ZERO_32 = bytes32(b"\x00" * 32)
OFFER_PH = bytes32(OFFER_MOD_HASH)
SEND_MESSAGE, MODE_PUZZLE_TO_COIN = 66, 0b010111
# POST /api/multisig-execute keeps at most 64 fee spends and 128 attached spends (api/multisig-execute.js).
MAX_SIGNED, MAX_ATTACHED = 64, 128
# The mempool refuses a transaction costing over half a block; a batch stays under this.
MAX_BATCH_COST = 5_000_000_000
# The fee floor of a FULL mempool (forge-fee-floor): 5 mojos per unit of cost.
FULL_MEMPOOL_FEE_PER_COST = 5
RATIO = forge_math.RATIO_SCALE


class MoveRefused(ValueError):
    """A pool this flow will not move, with the reason; listed for the owner, never guessed."""


# ---- the owner -------------------------------------------------------------------------------------

@dataclass
class Owner:
    """The owner's standard p2 puzzle (its LP and every coin the move pays it sit under it) and a
    funding source: `fund(amount)` returns (an XCH coin of the owner's wallet holding at least
    `amount`, that coin's own standard puzzle) -- any address of the wallet ("the wallet names
    the payer")."""
    puzzle: Program
    fund: Callable[[int], tuple]

    @property
    def ph(self) -> bytes32:
        return bytes32(self.puzzle.get_tree_hash())

    def solution(self, conditions: list) -> Program:
        return drv.p2_delegated_solution(conditions)


@dataclass
class Held:
    """A coin the owner holds (or will, inside the bundle): XCH at the owner's puzzle hash, or a
    CAT under the owner's inner puzzle (layered with `hidden` for an rCAT)."""
    asset: bytes32 | None
    coin: Coin
    lineage: LineageProof | None = None
    hidden: bytes32 = ZERO_32

    @property
    def amount(self) -> int:
        return int(self.coin.amount)


@dataclass
class Step:
    """One piece of a move: the spends it adds and what it reports."""
    kind: str
    pool: Any
    owner_spends: list = field(default_factory=list)
    keyless: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)


# ---- the revisions a move touches ------------------------------------------------------------------

class Lane:
    def __init__(self, drv_mod, off_mod, layered: bool):
        self.drv, self.off, self.layered = drv_mod, off_mod, layered

    def hidden(self, pool, asset):
        return self.off.hidden_of(pool, asset) if self.layered else None

    def settle_ph(self, asset, h):
        return self.off.settle_ph(asset, h) if self.layered else self.off.settle_ph(asset)

    def settle_inner(self, h):
        return self.off.settle_inner(h) if self.layered else OFFER_MOD

    def settle_solution(self, h, sol):
        return self.off.settle_solution(h, sol) if self.layered else sol


V16 = Lane(drv16, off16, False)
V18 = Lane(drv18, off18, True)
V19 = Lane(drv, off19, True)


def lane_of(pool) -> Lane:
    mod = type(pool).__module__
    return {drv16.__name__: V16, drv18.__name__: V18, drv.__name__: V19}[mod]


def owner_cat(owner: Owner, asset: bytes32, h) -> Program:
    return drv.layered(h if h and h != ZERO_32 else None, owner.puzzle)


def owner_cat_spend(owner: Owner, held: Held, conditions: list) -> list:
    """The owner's spend of a held CAT: its conditions through the layer's owner path."""
    h = held.hidden if held.hidden != ZERO_32 else None
    inner = drv.layered(h, owner.puzzle)
    sol = drv.layered_solution(h, owner.puzzle, owner.solution(conditions)) if h else owner.solution(conditions)
    return list(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        held.coin, held.asset, inner, sol, lineage_proof=held.lineage)]).coin_spends)


# ---- remove ----------------------------------------------------------------------------------------

def remove_owner_lp(pool, lp_coins: list, owner: Owner, height: int) -> tuple[Step, dict, list]:
    """Melt every coin of `lp_coins` (Held LP of `pool`, owner-held) through the pool's own
    `remove`, paying each released reserve to the owner. Returns (step, released {asset: Held},
    the pool's state after)."""
    lane = lane_of(pool)
    d = lane.drv
    burn = sum(c.amount for c in lp_coins)
    total = int(pool.state[1])
    if burn <= 0:
        raise MoveRefused("the owner holds none of this pool's LP")
    if total - burn < d.LOCKED_BURN:
        raise MoveRefused(f"burning {burn:,} of {total:,} LP would take the pool below its locked floor")
    vf = forge_math.vault_fee_bps(len(pool.state[0]), lane.off.MATH_VERSION, pool.fee_bps)
    payouts = [int(p) for p in forge_math.withdrawal_amounts(pool.state[0], burn, total, vf)]
    first = lp_coins[0]
    if not any(payouts):
        raise MoveRefused(f"removing {burn:,} of {total:,} LP of pool {pool.launcher_id.hex()[:12]} releases nothing "
                          f"(reserves {list(pool.state[0])}): the owner's share rounds to zero")
    try:
        probe_state, _, _, _ = d.run_leaf(pool, "forge_action_remove", [int(height), burn, first.coin.name(), payouts])
    except Exception as exc:
        raise MoveRefused(f"pool {pool.launcher_id.hex()[:12]} refuses the remove of {burn:,} of {total:,} LP "
                          f"(payouts {payouts}): {exc}") from exc
    melt_inner_hash = d.LP_MELT_INNER.get_tree_hash()
    ring = []
    for k, held in enumerate(lp_coins):
        conds = [[51, melt_inner_hash, burn]] if k == 0 else []
        ring.append(SpendableCAT(held.coin, pool.lp_asset_id, owner.puzzle, owner.solution(conds), lineage_proof=held.lineage))
    owner_spends = list(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, ring).coin_spends)
    melt = Coin(first.coin.name(), construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, d.LP_MELT_INNER).get_tree_hash(), uint64(burn))
    action = [-burn, total - burn, probe_state.get_tree_hash(), pool.inner_hash, ZERO_32]
    keyless = list(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        melt, pool.lp_asset_id, d.LP_MELT_INNER, Program.to([pool.lp_tail, action]),
        lineage_proof=LineageProof(first.coin.parent_coin_info, owner.ph, first.coin.amount),
        extra_delta=-burn, limitations_program_reveal=pool.lp_tail, limitations_solution=Program.to(action))]).coin_spends)
    extra_cats, released = {}, {}
    owner_inner_hash = owner.ph
    for reserve, asset, pay in zip(pool.reserves, pool.asset_ids, payouts):
        if pay <= 0:
            continue
        h = lane.hidden(pool, asset)
        payout = Coin(reserve.coin.name(), lane.settle_ph(asset, h), uint64(pay))
        nonce = bytes32(hashlib.sha256(b"forge-v18-to-v19" + bytes(payout.name())).digest())
        if asset is None:
            keyless.append(make_spend(payout, OFFER_MOD, Program.to([(nonce, [[owner.ph, pay, [owner.ph]]])])))
            released[None] = Held(None, Coin(payout.name(), owner.ph, uint64(pay)))
        else:
            sol = Program.to([(nonce, [[owner_inner_hash, pay, [owner_inner_hash]]])])
            extra_cats.setdefault(asset, []).append(SpendableCAT(
                payout, asset, lane.settle_inner(h), lane.settle_solution(h, sol),
                lineage_proof=LineageProof(reserve.coin.parent_coin_info, getattr(reserve, "cat_inner_hash", reserve.inner_hash),
                                           reserve.coin.amount)))
            hb = bytes32(h) if h else ZERO_32
            coin = Coin(payout.name(), construct_cat_puzzle(CAT_MOD, asset, drv.layered(h, owner.puzzle)).get_tree_hash(), uint64(pay))
            released[asset] = Held(asset, coin, LineageProof(payout.parent_coin_info, lane.settle_inner(h).get_tree_hash(), uint64(pay)), hb)
    bundle, new_state = d.spend_action(pool, "forge_action_remove", [int(height), burn, melt.parent_coin_info, payouts],
                                       extra_spends=keyless, extra_cats=extra_cats)
    owned = {cs.coin.name() for cs in owner_spends}
    step = Step("remove", pool, owner_spends, [cs for cs in bundle.coin_spends if cs.coin.name() not in owned],
                {"burn": burn, "payouts": payouts})
    return step, released, d.state_to_list(new_state)


# ---- add (into a V19 twin) -------------------------------------------------------------------------

def value(pool, amounts: list) -> Fraction:
    """A vector of `pool`'s assets valued at its own marginal prices: in a weighted pool asset i
    holds w_i / K of the value, so one unit is worth w_i / r_i (up to the common factor)."""
    return sum((Fraction(int(a)) * int(w) / int(r) for a, w, r in zip(amounts, pool.weights, pool.state[0])), Fraction(0))


def add_to_twin(pool19, held: dict, owner: Owner, height: int, network_fee: int = 0) -> tuple[Step, Held, dict, int, list]:
    """Add the owner's `held` (asset -> Held; XCH by amount only, funded from the owner's funding
    coin) to the V19 pool `pool19`, proportionally at its own ratio. Returns (step, the minted V19
    LP as Held, the dust returned per asset, the minted amount, the deposits)."""
    r = [int(x) for x in pool19.state[0]]
    have = []
    for asset in pool19.asset_ids:
        if asset not in held:
            raise MoveRefused(f"the add into {pool19.launcher_id.hex()[:12]} needs every asset of the pool")
        have.append(held[asset].amount)
    # Proportional at the twin's own ratio, bound by the scarcest held asset: that one is deposited
    # whole, every other asset floor(r_i * h_j / r_j) <= what is held (the rest is dust).
    j = min(range(len(r)), key=lambda i: Fraction(have[i], r[i]))
    deposits = [have[j] if i == j else r[i] * have[j] // r[j] for i in range(len(r))]
    if any(dep <= 0 for dep in deposits):
        raise MoveRefused(f"the released amounts {have} are too small to add to the V19 twin {pool19.launcher_id.hex()[:12]}")
    rates = drv.add_fee_rates(pool19)
    minted = forge_math.invariant_lp_mint(r, deposits, int(pool19.state[1]), pool19.fee_bps, pool19.weights, version=19, **rates)
    if minted <= 0:
        raise MoveRefused(f"the add into {pool19.launcher_id.hex()[:12]} mints no LP")
    xi = pool19.asset_ids.index(None) if None in pool19.asset_ids else None
    xch_dep = deposits[xi] if xi is not None else 0
    settle_amount = xch_dep + minted
    funding, fund_puzzle = owner.fund(settle_amount + network_fee)
    xch_settle = Coin(funding.name(), OFFER_PH, uint64(settle_amount))
    owner_spends = [make_spend(funding, fund_puzzle, owner.solution(
        [[51, OFFER_PH, settle_amount], [51, owner.ph, int(funding.amount) - settle_amount - network_fee, [owner.ph]]]))]
    eve_ph = construct_cat_puzzle(CAT_MOD, pool19.lp_asset_id, drv.LP_MINT_INNER).get_tree_hash()
    eve_nonce = bytes32(hashlib.sha256(bytes(xch_settle.name()) + b"forge-lp-eve").digest())
    keyless = [make_spend(xch_settle, OFFER_MOD, off19._own_group(xch_settle, [(eve_nonce, [[eve_ph, 1]])]))]
    parents, amounts, extra_cats, dust = [], [], {}, {}
    for asset, dep in zip(pool19.asset_ids, deposits):
        if asset is None:
            parents.append(xch_settle.parent_coin_info); amounts.append(settle_amount)
            dust[None] = held[None].amount - dep
            continue
        hd = held[asset]
        h = off19.hidden_of(pool19, asset)
        settle_inner = off19.settle_inner(h)
        conds = [[51, OFFER_PH, dep]]
        if hd.amount > dep:
            conds.append([51, owner.ph, hd.amount - dep, [owner.ph]])
        owner_spends.extend(owner_cat_spend(owner, hd, conds))
        settle = Coin(hd.coin.name(), off19.settle_ph(asset, h), uint64(dep))
        extra_cats.setdefault(asset, []).append(SpendableCAT(
            settle, asset, settle_inner, off19.settle_solution(h, off19._own_group(settle)),
            lineage_proof=LineageProof(hd.coin.parent_coin_info, owner_cat(owner, asset, h).get_tree_hash(), hd.coin.amount)))
        parents.append(hd.coin.name()); amounts.append(dep)
        dust[asset] = hd.amount - dep
    solution = [int(height), deposits, minted, xch_settle.name(), parents, amounts]
    probe_state, _, _, _ = drv.run_leaf(pool19, "forge_action_add", solution)
    eve = Coin(xch_settle.name(), eve_ph, uint64(1))
    new_total = int(pool19.state[1]) + minted
    eve_spends = drv.lp_eve_ring(pool19, eve, OFFER_PH, minted, [minted, new_total, probe_state.get_tree_hash(), pool19.inner_hash, ZERO_32])
    lp_settle = Coin(eve.name(), off19.settle_ph(pool19.lp_asset_id), uint64(minted))
    nonce = bytes32(hashlib.sha256(b"forge-v18-to-v19-lp" + bytes(lp_settle.name())).digest())
    lp_settle_spends = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        lp_settle, pool19.lp_asset_id, OFFER_MOD, Program.to([(nonce, [[owner.ph, minted, [owner.ph]]])]),
        lineage_proof=LineageProof(eve.parent_coin_info, drv.LP_MINT_INNER.get_tree_hash(), eve.amount))]).coin_spends
    bundle, new_state = drv.spend_action(pool19, "forge_action_add", solution,
                                         extra_spends=[*keyless, *eve_spends, *lp_settle_spends], extra_cats=extra_cats)
    lp_coin = Coin(lp_settle.name(), construct_cat_puzzle(CAT_MOD, pool19.lp_asset_id, owner.puzzle).get_tree_hash(), uint64(minted))
    minted_held = Held(pool19.lp_asset_id, lp_coin, LineageProof(lp_settle.parent_coin_info, OFFER_PH, uint64(minted)))
    owned = {cs.coin.name() for cs in owner_spends}
    step = Step("add", pool19, owner_spends, [cs for cs in bundle.coin_spends if cs.coin.name() not in owned],
                {"deposits": deposits, "minted": minted, "dust": dust, "state_after": drv.state_to_list(new_state)})
    return step, minted_held, dust, minted, deposits


# ---- the market graph --------------------------------------------------------------------------------

@dataclass
class Graph:
    """Every Forge pool the move can see, by LP asset id, and the V19 pools by market signature."""
    by_lp: dict                     # lp asset id -> pool (V16, V18 or V19)
    v19: list                       # V19 pools: every one already registered, and the twins created so far
    # launcher id -> the DAO puzzle hash the pool really names, where a caller (the plan) swapped it
    # for a stand-in; the market signature is always the real one.
    true_dao: dict = field(default_factory=dict)

    def lp_pool(self, asset):
        return self.by_lp.get(asset) if asset is not None else None

    def v19_asset(self, asset):
        """The V19 equivalent of an asset: itself when plain, the V19 twin's LP for a V16/V18 LP."""
        pool = self.lp_pool(asset)
        if pool is None or lane_of(pool) is V19:
            return asset
        twin = self.twin_of(pool)
        if twin is None:
            raise MoveRefused(f"no V19 twin yet for the pool behind LP {asset.hex()[:12]}; move the inner market first")
        return twin.lp_asset_id

    def signature(self, pool) -> tuple:
        """What makes a V16/V18 pool and its V19 twin the same market once LP is mapped to V19: the
        V19 registry's key (forge_v19_driver.pool_key) -- assets, weights, LP fee, DAO, and each
        asset's hidden puzzle hash (an rCAT under H, under H' and plain are three markets). A V19
        pool's own assets are taken as they are (one holding pre-V19 LP is never a twin)."""
        lane = lane_of(pool)
        rows = []
        for a, w in zip(pool.asset_ids, pool.weights):
            v = a if lane is V19 else self.v19_asset(a)
            h = lane.hidden(pool, a) if (a is not None and v == a) else None
            rows.append((bytes(ZERO_32) if v is None else bytes(v), int(w), bytes(h) if h else bytes(ZERO_32)))
        dao = self.true_dao.get(bytes(pool.launcher_id), pool.dao_ph)
        return (tuple(sorted(rows)), int(pool.fee_bps), bytes(dao) if dao else bytes(ZERO_32))

    def twin_of(self, pool):
        if lane_of(pool) is V19:
            return pool
        want = self.signature(pool)
        for p in self.v19:
            if self.signature(p) == want:
                return p
        return None

    def depth(self, pool) -> int:
        """0 for a pool of plain assets; else one more than its deepest LP asset's pool."""
        inner = [self.lp_pool(a) for a in pool.asset_ids]
        return max((1 + self.depth(p) for p in inner if p is not None), default=0)


def unpair(graph: Graph, held: Held, owner: Owner, height: int, steps: list, touched: set) -> tuple[Held, Fraction]:
    """The owner's `held` as a V19 asset: itself when plain; for Forge LP, removed from its pool
    (recursively) and the plain assets added to that market's V19 twin. Returns (V19 Held, the
    value kept: the minted LP's value over the value released, at the twin's prices; 1 if plain)."""
    pool = graph.lp_pool(held.asset)
    if pool is None:
        return held, Fraction(1)
    if lane_of(pool) is V19:
        return held, Fraction(1)
    # V16 LP (mainnet: a V18 pool holding V16 LP) and V18 LP (testnet N1: a V18 pool holding V18 LP)
    # unpair alike (owner rule: nested LP goes back to the original assets, which are added to that
    # market's V19 twin). The inner market's twin must exist: an inner V18 pool moves in an EARLIER
    # batch (plan_batches orders by depth), an inner V16 market through its V18 twin.
    twin = graph.twin_of(pool)
    if twin is None:
        rev = "V18" if lane_of(pool) is V18 else "V16"
        raise MoveRefused(f"the {rev} market {pool.launcher_id.hex()[:12]} has no V19 twin yet: "
                          f"move {'it' if rev == 'V18' else 'its V18 twin'} in an earlier batch")
    for p in (pool, twin):
        if p.launcher_id in touched:
            raise MoveRefused(f"pool {p.launcher_id.hex()[:12]} is spent twice in one batch; split the batch")
        touched.add(p.launcher_id)
    step, released, after = remove_owner_lp(pool, [held], owner, height)
    step.detail["state_after"] = after
    steps.append(step)
    converted, kept = {}, {}
    for asset, h in released.items():
        if asset is None:
            converted[None], kept[None] = h, Fraction(1)
            continue
        v, f = unpair(graph, h, owner, height, steps, touched)
        converted[v.asset], kept[v.asset] = v, f
    add, minted, dust, m, deposits = add_to_twin(twin, converted, owner, height)
    steps.append(add)
    released_value = value(twin, [Fraction(converted[a].amount) / kept[a] for a in twin.asset_ids])
    f = value(twin, deposits) / released_value
    add.detail.update({"from_lp": held.asset.hex(), "kept": float(f)})
    return minted, f


# ---- one V18 pool, moved ---------------------------------------------------------------------------

@dataclass
class PoolMove:
    label: str
    source: Any                     # the V18 pool at its current state
    lp_coins: list                  # Held LP of the source (owner)
    steps: list = field(default_factory=list)
    burn: int = 0
    payouts: list = field(default_factory=list)
    seeds: list = field(default_factory=list)       # the twin's genesis reserves, in the twin's asset order
    twin_assets: list = field(default_factory=list)
    dust: dict = field(default_factory=dict)        # asset hex -> amount returned to the owner
    total_lp: int = 0
    twin: Any = None
    plan: Any = None
    consent: list | None = None
    source_after: list | None = None
    nested: list = field(default_factory=list)      # [(V16/V18 LP asset, V19 LP asset, kept)]
    conv: dict = field(default_factory=dict)        # V19 asset -> (released amount, V19 amount, value kept)
    # True when a V19 pool of exactly the twin's market (its registry key) is already registered:
    # the released assets are ADDED to it (proportionally at its own ratio, dust to the owner) and
    # nothing is created -- the registry admits one pool per key. `twin` is that pool (before the
    # add), `seeds` the deposits and `total_lp` the LP minted to the owner.
    merged: bool = False

    @property
    def result_pool(self):
        """The V19 pool this move leaves on chain: the new twin's eve, or the merged pool after the add."""
        if not self.merged:
            return self.twin
        add = next(st for st in self.steps if st.kind == "add" and st.pool is self.twin)
        return self.twin.advance(add.detail["state_after"])

    @property
    def owner_spends(self) -> list:
        return [cs for st in self.steps for cs in st.owner_spends] + (list(self.plan.creator_spends) if self.plan else [])

    @property
    def keyless(self) -> list:
        return [cs for st in self.steps for cs in st.keyless] + (list(self.plan.router_spends) if self.plan else [])


def twin_supply(asset_ids: list, reserves: list, v18_burn: int) -> int:
    """The twin's genesis supply: the burned V18 LP one for one, clamped into V19's bounds."""
    if not reserves or min(reserves) <= 0:
        raise MoveRefused(f"a seed reserve is zero ({reserves}): V19 needs every genesis reserve positive")
    lo = drv.MIN_GENESIS_LP
    hi = drv.MAX_GENESIS_LP_PER_MOJO * min(reserves)
    if None in asset_ids:
        hi = min(hi, reserves[asset_ids.index(None)])
    if lo > hi:
        raise MoveRefused(f"the released reserves {reserves} admit no V19 genesis supply: the floor is {lo:,} LP and the "
                          f"bounds allow at most {hi:,} (10^6 x the scarcest reserve{', and the XCH reserve' if None in asset_ids else ''})")
    total = min(max(int(v18_burn), lo), hi)
    assert drv.genesis_bound_reason(asset_ids, reserves, total) is None
    return total


# A merge adds at the REGISTERED pool's ratio. If that pool prices the market away from the V18 pool
# (anyone may register a market's no-DAO key first, at any price, while creation is open), the
# owner's deposit follows the wrong price and arbitrage takes the difference. So a merge is refused
# unless the two prices agree within this tolerance.
MERGE_PRICE_TOLERANCE = Fraction(1, 100)


def merge_price_refusal(pool19, held: dict, kept: dict, tolerance: Fraction = MERGE_PRICE_TOLERANCE) -> str | None:
    """None when the released assets (each in V19 units, divided by the value its unpairing kept, so
    proportional to the V18 pool's reserves) price the market like the registered V19 pool `pool19`
    within `tolerance`; otherwise the refusal. Both pools share one registry key, so the weights
    are equal and cancel."""
    r = [int(x) for x in pool19.state[0]]
    q = []
    for asset, ri in zip(pool19.asset_ids, r):
        if asset not in held or ri <= 0 or held[asset].amount <= 0:
            return f"the registered V19 pool {pool19.launcher_id.hex()[:12]} cannot be priced against the released assets"
        q.append(Fraction(held[asset].amount) / kept.get(asset, Fraction(1)) / ri)
    spread = max(q) / min(q) - 1
    if spread > tolerance:
        return (f"the registered V19 pool {pool19.launcher_id.hex()[:12]} prices the market {float(spread):.2%} away from "
                f"the V18 pool (limit {float(tolerance):.0%}); bring it to the V18 price with a swap first, or move this pool by hand")
    return None


def move_pool(move: PoolMove, graph: Graph, *, registry, slots: dict, owner: Owner, height: int, network_fee: int,
              touched: set) -> None:
    """Fill `move` in: the remove, the unpairing of any released LP, the twin's creation."""
    src = move.source
    for p in (src,):
        if p.launcher_id in touched:
            raise MoveRefused(f"pool {p.launcher_id.hex()[:12]} is spent twice in one batch")
        touched.add(p.launcher_id)
    step, released, after = remove_owner_lp(src, move.lp_coins, owner, height)
    move.steps.append(step)
    move.burn, move.payouts, move.source_after = step.detail["burn"], step.detail["payouts"], after
    # Every released asset as a V19 asset: plain, or unpaired into the inner market's V19 LP.
    v19_held, kept = {}, {}
    for asset, h in released.items():
        if asset is None:
            v19_held[None], kept[None] = h, Fraction(1)
            move.conv[None] = (h.amount, h.amount, Fraction(1))
            continue
        v, f = unpair(graph, h, owner, height, move.steps, touched)
        v19_held[v.asset], kept[v.asset] = v, f
        move.conv[v.asset] = (h.amount, v.amount, f)
        if v.asset != asset:
            move.nested.append((asset.hex(), v.asset.hex(), float(f)))
    assets_src = [graph.v19_asset(a) for a in src.asset_ids]
    existing = graph.twin_of(src)
    if existing is not None:
        # The market's V19 pool is already registered (a twin created earlier, or a pool made with
        # the twin's exact config): the registry refuses a second one with this key, so the
        # released assets are added to it -- proportionally at ITS ratio (it prices the market now),
        # every rounding to the pool, the rest back to the owner as dust. An add needs no DAO consent.
        if existing.launcher_id in touched:
            raise MoveRefused(f"pool {existing.launcher_id.hex()[:12]} is spent twice in one batch; split the batch")
        touched.add(existing.launcher_id)
        why = merge_price_refusal(existing, v19_held, kept)
        if why:
            raise MoveRefused(f"{move.label}: {why}")
        add, _minted_held, dust, minted, deposits = add_to_twin(existing, v19_held, owner, height, network_fee)
        add.detail.update({"merge_from": src.launcher_id.hex()})
        move.steps.append(add)
        move.merged, move.twin, move.consent = True, existing, None
        move.twin_assets, move.seeds, move.total_lp = list(existing.asset_ids), [int(x) for x in deposits], int(minted)
        move.dust = {("xch" if a is None else a.hex()): int(v) for a, v in dust.items()}
        return
    # Price like the V18 pool: scale every seed by the least value kept (plain assets keep all).
    t = min(kept.values())
    seeds = []
    for a in assets_src:
        amt = v19_held[a].amount
        seeds.append(int(Fraction(amt) * t / kept[a]))
    move.dust = {("xch" if a is None else a.hex()): v19_held[a].amount - sd for a, sd in zip(assets_src, seeds)}
    # The twin's config in canonical order (XCH first, then ascending id); weights follow.
    order = sorted(range(len(assets_src)), key=lambda i: bytes(32) if assets_src[i] is None else bytes(assets_src[i]))
    t_assets = [assets_src[i] for i in order]
    t_seeds = [seeds[i] for i in order]
    t_weights = [int(src.weights[i]) for i in order]
    t_hidden = [off18.hidden_of(src, src.asset_ids[i]) if assets_src[i] == src.asset_ids[i] else ZERO_32 for i in order]
    t_hidden = [bytes32(h) if h else ZERO_32 for h in t_hidden]
    move.total_lp = twin_supply(t_assets, t_seeds, move.burn)
    move.seeds, move.twin_assets = t_seeds, t_assets
    for a in t_assets:
        pool = graph.lp_pool(a)
        if pool is not None and lane_of(pool) is not V19:
            raise MoveRefused(f"{move.label}: the twin would hold pre-V19 LP {a.hex()[:12]}")
    protocol_fee_bps = int(registry.constants[15]) if len(registry.constants) > 15 else int(src.protocol_fee_bps)
    config = cre.CreationConfig(t_assets, t_seeds, t_weights, int(src.fee_bps), protocol_fee_bps, registry.protocol_ph,
                                move.total_lp, dao_ph=src.dao_ph, dao_fee_bps=int(src.state[5]), hidden_puzzle_hashes=t_hidden)
    if not move.nested:
        cre.check_mirror(config, src)
    # The creator's coins: the XCH reserve and the genesis backing from a funding coin; each CAT
    # reserve from the owner's V19 coin of it, with the dust back to the owner.
    xi = t_assets.index(None) if None in t_assets else None
    need = 2 + (t_seeds[xi] if xi is not None else 0) + registry.current_fee + move.total_lp + network_fee
    funding, fund_puzzle = owner.fund(need)
    cats, pre = {}, []
    for a, sd in zip(t_assets, t_seeds):
        if a is None:
            continue
        hd = v19_held[a]
        cats[a] = cre.CreatorCat(a, hd.coin, owner.puzzle, hd.lineage, hidden=hd.hidden)
    try:
        plan = cre.plan(registry, slots, config, cre.CreatorXch(funding, fund_puzzle), cats, owner.ph, network_fee,
                        dao_consent=cre.FROM_LOCK if config.dao_ph != ZERO_32 else None)
    except Exception as exc:  # noqa: BLE001
        if "already registered" in str(exc):
            raise MoveRefused(f"{move.label}: a V19 pool of this market is registered but is not among the V19 pools read "
                              "(the router index and the V19 record); resync them so the move adds to it") from exc
        raise
    move.plan, move.twin = plan, plan.pool
    move.consent = plan.details.get("dao_consent")
    if bytes(src.launcher_id) in graph.true_dao:     # the twin names the source's DAO (a stand-in in the plan)
        graph.true_dao[bytes(plan.pool.launcher_id)] = graph.true_dao[bytes(src.launcher_id)]


def build_batch(moves: list, graph: Graph, *, registry, slots: dict, owner: Owner, height: int, network_fee: int = 0) -> dict:
    """Every move of a batch, chained on the registry's ephemeral coin. Returns the registry and
    slots after, and the moves filled in. Refuses a batch that spends any pool twice."""
    reg = registry
    slots = {k: dict(v) for k, v in slots.items()}
    touched: set = set()
    for move in moves:
        move_pool(move, graph, registry=reg, slots=slots, owner=owner, height=height, network_fee=network_fee, touched=touched)
        if move.plan is not None:          # a merge into a registered V19 pool registers nothing
            slots = slots_after(slots, move.plan, reg)
            reg = move.plan.registry_after
    return {"registry": reg, "slots": slots, "moves": moves}


def slots_after(slots: dict, plan, reg) -> dict:
    """The registry's slot list after `plan`'s registration (deploy-v19-testnet._commit_registry_move)."""
    key, left, right = plan.key, plan.left, plan.right
    parent = {"parent_coin_info": reg.coin.parent_coin_info.hex(), "puzzle_hash": reg.coin.puzzle_hash.hex(),
              "amount": int(reg.coin.amount)}
    out = {k: dict(v) for k, v in slots.items()}
    out[key.hex()] = {"key": key.hex(), "launcher_id": plan.pool.launcher_id.hex(), "left": left[0].hex(),
                      "right": right[0].hex(), "parent": parent, "parent_inner_hash": reg.inner_hash.hex()}
    out[left[0].hex()].update({"right": key.hex(), "parent": parent, "parent_inner_hash": reg.inner_hash.hex()})
    out[right[0].hex()].update({"left": key.hex(), "parent": parent, "parent_inner_hash": reg.inner_hash.hex()})
    return out


def batch_spends(result: dict) -> tuple[list, list]:
    """(owner-signed, keyless) across a batch, refused if the lock board cannot carry them."""
    signed = [cs for m in result["moves"] for cs in m.owner_spends]
    keyless = [cs for m in result["moves"] for cs in m.keyless]
    if len(signed) > MAX_SIGNED or len(keyless) > MAX_ATTACHED:
        raise MoveRefused(f"the batch has {len(signed)} signed and {len(keyless)} keyless spends; the board carries at most "
                          f"{MAX_SIGNED} and {MAX_ATTACHED}: split it")
    return signed, keyless


def consents(result: dict) -> list:
    """The lock's ONE proposal: a consent message per twin, to the registry coin it registers on."""
    return [[SEND_MESSAGE, MODE_PUZZLE_TO_COIN, bytes32(m.consent[2]), bytes32(m.consent[3])]
            for m in result["moves"] if m.consent is not None]


def check_batch(result: dict, spends: list, owner_ph: bytes32, validate=drv.validate) -> dict:
    """The dry run `execute` requires before any push. Refuses on any mismatch:
      * the assembled bundle (lock spends included) passes the mempool's rules within half a block;
      * every source pool (V18, and every V16 pool unpaired) is recreated at the remove's exact
        successor, the V18 pools at their creation dust;
      * every V19 twin's eve is created at the planned state, seeded exactly as planned, and holds
        only V19 assets (a merge: the one add into the registered pool, as planned);
      * every V19 add's successor is created at its planned state."""
    conds, additions = validate(SpendBundle(list(spends), G2Element()))
    if conds.cost > MAX_BATCH_COST:
        raise MoveRefused(f"cost {conds.cost:,} is over {MAX_BATCH_COST:,}: split the batch")
    made = {bytes(ph) for ph, _a in additions}
    for m in result["moves"]:
        if int(m.source_after[1]) != int(m.source.state[1]) - m.burn or int(m.source_after[1]) < drv18.LOCKED_BURN:
            raise MoveRefused(f"{m.label}: the V18 pool would not end at its creation dust")
        for st in m.steps:
            after = st.detail.get("state_after")
            if st.kind == "remove" and st.pool is m.source:
                after = m.source_after
            if after is None:
                continue
            ph = st.pool.successor_puzzle_hash(Program.to(after))
            if bytes(ph) not in made:
                raise MoveRefused(f"{m.label}: pool {st.pool.launcher_id.hex()[:12]} ({st.kind}) has no successor at its planned state")
        if m.merged:
            # The add into the registered V19 pool (its successor is checked above) is the whole move.
            add = [st for st in m.steps if st.kind == "add" and st.pool is m.twin]
            if len(add) != 1 or [int(x) for x in add[0].detail["deposits"]] != list(m.seeds) or int(add[0].detail["minted"]) != m.total_lp:
                raise MoveRefused(f"{m.label}: the add into the registered V19 pool is not as planned")
            continue
        if bytes(m.twin.coin.puzzle_hash) not in made:
            raise MoveRefused(f"{m.label}: the V19 twin's eve is not created")
        if [int(x) for x in m.twin.state[0]] != list(m.seeds) or int(m.twin.state[1]) != m.total_lp:
            raise MoveRefused(f"{m.label}: the V19 twin is not seeded as planned")
    return {"cost": int(conds.cost), "spends": len(spends)}


def rated_fee(cost: int, mempool_full: bool, floor_per_cost: int = 1) -> int:
    """The network fee for a bundle of `cost`: 5 mojos per cost when the mempool is full (the fee
    floor is a cliff), else `floor_per_cost`."""
    return int(cost) * (FULL_MEMPOOL_FEE_PER_COST if mempool_full else int(floor_per_cost))


def order_moves(moves: list, graph: Graph) -> list:
    """Inner markets before the pools that hold their LP (topological by depth)."""
    return sorted(moves, key=lambda m: graph.depth(m.source))
