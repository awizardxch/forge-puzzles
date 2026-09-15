#!/usr/bin/env python3
"""The Forge V11 Python driver: every puzzle construction and spend assembly.

One implementation. The simulator harness (_v11_testkit.py) imports everything
here and adds only fake-coin factories and the consensus validator; the testnet
deploy script (scripts/deploy-v11-testnet.py) imports the same functions and
feeds them real coins from Sage. If the two ever needed different puzzle math,
one of them would be wrong.

What lives here:
  * loading the compiled V13 puzzles from contracts/v13/compiled;
  * PoolSpec-free construction of a V11 pool (V13Pool): reserve inners on
    p2_delegated_by_singleton with nonce = asset index, the multi-reserve
    finalizer, the five leaves curried with PoolConfig, the wallet-sdk merkle
    tree, the action-layer inner and the singleton coin;
  * running a leaf locally to learn the new state and tagged conditions, and
    assembling the singleton spend plus every reserve spend around it;
  * the LP eve (message and genesis mints), the melt coin, offer settlements;
  * the registry singleton (init and register), slots, launcher and fee spends.

Coin plumbing is pluggable: `make_pool` fabricates reserve coins from a salt
when none are given, and takes real (coin, lineage) pairs when they are.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sys
from dataclasses import dataclass, field, replace

sys.path.insert(0, ".")

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.singleton_top_layer_v1_1 import (
    SINGLETON_LAUNCHER_HASH, SINGLETON_TOP_LAYER_V1_1_HASH, puzzle_for_singleton, solution_for_singleton,
)
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH
from chia_rs import Coin, G2Element, SpendBundle, get_conditions_from_spendbundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from forge_merkle import LEAF_ORDER, MerkleTree

CONTRACTS = pathlib.Path(__file__).resolve().parent
# Normally the project's own build. `FORGE_V13_COMPILED` points it somewhere
# else, which is how the mutation harness runs a suite against a deliberately
# broken build without ever writing into contracts/v13/compiled -- leaving a
# mutant behind in the real build is the one way this tooling could do harm.
V13_COMPILED = (pathlib.Path(os.environ["FORGE_V13_COMPILED"]).resolve()
                if os.environ.get("FORGE_V13_COMPILED")
                else CONTRACTS / "v13" / "compiled")
MAX_COST = 11_000_000_000
VALIDATION_HEIGHT = 7_000_000  # any height past every soft fork; messages are consensus

CREATE_COIN, CREATE_PUZZLE_ANNOUNCEMENT, SEND_MESSAGE, RECEIVE_MESSAGE = 51, 62, 66, 67
RESERVE_TAG = -42
PROTOCOL_VERSION = 14      # V13 (CHIP-0062 review revision); was 12 for V11.1
MIN_LOCKED_LP = 1000       # V13: mirrors forge_action_common.rue; enforced at registration and in remove: the DAO fee revision (config, state and leaf set changed)
PRICE_SCALE = 2 ** 64
ORACLE_WINDOW = 32
IDENTITY = Program.to(1)
ZERO_32 = bytes32(b"\x00" * 32)
SINGLE_LEAF_SELECTOR = 2   # `puzzles` is a list; the first puzzle is path 2, then 5, 11, ...


class Rejected(Exception):
    """The bundle would not be accepted: by consensus, or by a builder that mirrors it."""


def v13_program(name: str) -> Program:
    path = V13_COMPILED / f"{name}.rue.hex"
    if not path.is_file():
        path = V13_COMPILED / f"{name}.clvm.hex"
    return Program.from_bytes(bytes.fromhex(path.read_text().strip()))


def v13_available() -> bool:
    return (V13_COMPILED / "forge_action_swap.rue.hex").is_file()


def amt(n: int) -> bytes:
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 8) // 8, "big")


def coin_id(parent: bytes, ph: bytes, a: int) -> bytes32:
    return bytes32(hashlib.sha256(bytes(parent) + bytes(ph) + amt(a)).digest())


def tree_hash_atom(value: bytes) -> bytes32:
    return bytes32(hashlib.sha256(b"\x01" + value).digest())


def program_hash(value) -> bytes32:
    return Program.to(value).get_tree_hash()


if v13_available():
    ACTION_LAYER = v13_program("action")
    P2_DELEGATED = v13_program("p2_delegated_by_singleton")
    SLOT = v13_program("slot")
    FINALIZER_MOD = v13_program("forge_multi_reserve_finalizer")
    RESERVE_AMOUNT = v13_program("forge_reserve_amount")
    PASSTHROUGH = v13_program("passthrough_action")
    PASSTHROUGH_OBSERVE = v13_program("passthrough_observe")
    LP_TAIL_MOD = v13_program("forge_lp_cat_tail")
    LP_MINT_INNER = v13_program("forge_lp_mint_inner")
    LP_MELT_INNER = v13_program("forge_lp_melt_inner")
    LEAF_MODS = {name: v13_program(name) for name in LEAF_ORDER}
else:  # the suites skip; keep import-time failures out of the way
    ACTION_LAYER = P2_DELEGATED = SLOT = FINALIZER_MOD = RESERVE_AMOUNT = PASSTHROUGH = PASSTHROUGH_OBSERVE = None
    LP_TAIL_MOD = LP_MINT_INNER = LP_MELT_INNER = None
    LEAF_MODS = {}


# ---- state ------------------------------------------------------------------------

def forge_state(reserves: list[int], total_lp: int, fees: list[int] | None = None,
                last_height: int = 0, cums: list[int] | None = None, prev_root: bytes = ZERO_32,
                dao_fee_bps: int = 0, dao_owed: list[int] | None = None,
                reserve_parents: list | None = None, last_spot: list[int] | None = None) -> list:
    """[reserves, total_lp, fees_owed, [last_height, cums, last_spot], prev_root, dao_fee_bps, dao_owed,
    reserve_parents] -- V13: the oracle carries the spot it last credited (second audit, exact oracle)."""
    fees = fees if fees is not None else [0] * len(reserves)
    cums = cums if cums is not None else [0] * max(0, len(reserves) - 1)
    last_spot = last_spot if last_spot is not None else [0] * max(0, len(reserves) - 1)
    dao_owed = dao_owed if dao_owed is not None else [0] * len(reserves)
    reserve_parents = reserve_parents if reserve_parents is not None else [ZERO_32] * len(reserves)
    return [reserves, total_lp, fees, [last_height, cums, last_spot], prev_root, dao_fee_bps, dao_owed, reserve_parents]


def spots(reserves: list[int], weights: list[int], price_scale: int = PRICE_SCALE) -> list[int]:
    """forge_action_common::spots -- price of asset i in asset 0, scaled, on the given reserves."""
    r0, w0 = reserves[0], weights[0]
    return [(r0 * wi * price_scale) // (ri * w0) for ri, wi in zip(reserves[1:], weights[1:])]


def reserve_amounts(state) -> list[int]:
    """Reserve coin i holds the curve reserve plus both owed fees (forge_reserve_amount.rue)."""
    return [r + f + d for r, f, d in zip(state[0], state[2], state[6])]


def spot_price(r0: int, w0: int, ri: int, wi: int, scale: int = PRICE_SCALE) -> int:
    """Balancer spot price of asset i in asset 0, scaled: (r0/w0) / (ri/wi)."""
    return (r0 * wi * scale) // (ri * w0)


def expected_oracle(state, weights, h: int, birth: int | None = None, scale: int = PRICE_SCALE) -> list:
    """The oracle the V13 prologue must produce for a spend at height h by a coin born at `birth`:
    [h, cums + last_spot * (birth - last_height) + spot * (h - birth), spot], with spot on the
    pre-spend reserves. `birth` defaults to the block after the last claimed height, which is
    what make_pool gives a fabricated pool (see V13Pool.birth)."""
    r, (last_h, cums, last_spot) = state[0], state[3]
    birth = last_h + 1 if birth is None else birth
    assert birth > last_h and h >= birth, f"birth {birth} must lie in ({last_h}, {h}]"
    spot = spots(r, weights, scale)
    new_cums = [c + ls * (birth - last_h) + s * (h - birth) for c, ls, s in zip(cums, last_spot, spot)]
    return [h, new_cums, spot]


def expected_cums(state, weights, h: int, birth: int | None = None, scale: int = PRICE_SCALE) -> list[int]:
    """The accumulator alone; see expected_oracle."""
    return expected_oracle(state, weights, h, birth, scale)[1]


def spend_actions(pool: V13Pool, steps: list, parent_ids: list | None = None, extra_spends: list = (),
                  extra_cats: dict | None = None, omit_proofs: bool = True, force_no_proofs: bool = False, **knobs):
    """Several leaves in ONE spend, in order. `steps` is [(leaf name, solution), ...].

    Follows the wallet-sdk driver: `puzzles` lists each distinct leaf once in order
    of first use with selectors 2, 5, 11, ...; `selectors_and_proofs` is given in
    REVERSE execution order and only the first occurrence of a selector there (the
    last action to use it) carries a proof -- the action layer prepends as it
    verifies, so a repeated selector is found in the already-verified list.
    Ephemeral state flows from one leaf to the next. Returns (bundle, new_state)."""
    ephemeral, state = None, pool.state
    # The action layer prepends each action's condition list, so the finalizer
    # walks the LAST action's conditions first and prepends each tagged one: the
    # per-reserve order it hashes is each action's tagged conditions reversed,
    # concatenated in execution order.
    ordered_tagged = []
    for name, solution in steps:
        new_state, tagged_conds, _base, ephemeral = run_leaf(pool, name, solution, ephemeral=ephemeral, state=state)
        ordered_tagged.extend(reversed(tagged_conds))
        state = new_state
    puzzles, selector_of, entries = [], {}, []
    next_selector = SINGLE_LEAF_SELECTOR
    for name, _ in steps:
        leaf = pool.leaves[name]
        h = leaf.get_tree_hash()
        if h not in selector_of:
            selector_of[h] = next_selector
            puzzles.append(leaf)
            next_selector = next_selector * 2 + 1
        entries.append((selector_of[h], pool.leaf_proof(name)))
    reversed_entries, proven = [], set()
    for selector, proof in reversed(entries):
        if force_no_proofs or (omit_proofs and selector in proven):
            reversed_entries.append([selector])          # (selector . nil): already verified
        else:
            reversed_entries.append([selector, *proof])
            proven.add(selector)
    # V13: no reserve parent ids in the solution -- the finalizer reads them from state.
    inner_solution = Program.to([puzzles, reversed_entries, [with_birth(pool, s) for _, s in steps]])
    singleton_spend = make_spend(pool.coin, puzzle_for_singleton(pool.launcher_id, pool.inner),
                                 solution_for_singleton(pool.lineage, uint64(1), inner_solution))
    spends = [singleton_spend, *extra_spends]
    cats = {k: list(v) for k, v in (extra_cats or {}).items()}
    amounts = _state_amounts(state)
    for r in pool.reserves:
        recreate = Program.to([CREATE_COIN, r.inner_hash, amounts[r.index], [pool.hint]])
        mine = [c for (i, c) in ordered_tagged if i == r.index]
        dp = Program.to((1, [recreate, *mine]))
        p2_solution = Program.to([pool.inner_hash, dp])
        if r.asset_id is None:
            spends.append(make_spend(r.coin, r.inner, p2_solution))
        else:
            cats.setdefault(r.asset_id, []).append(SpendableCAT(r.coin, r.asset_id, r.inner, p2_solution, lineage_proof=r.lineage))
    for asset, spendables in cats.items():
        try:
            spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends)
        except ValueError as exc:
            raise Rejected(f"CAT ring: {exc}") from exc
    return SpendBundle(spends, G2Element()), state


def state_to_list(new_state: Program) -> list:
    items = list(new_state.as_iter())
    oracle = list(items[3].as_iter())
    return [[x.as_int() for x in items[0].as_iter()], items[1].as_int(), [x.as_int() for x in items[2].as_iter()],
            [oracle[0].as_int(), [x.as_int() for x in oracle[1].as_iter()], [x.as_int() for x in oracle[2].as_iter()]],
            bytes32(items[4].as_atom()),
            items[5].as_int(), [x.as_int() for x in items[6].as_iter()],
            [bytes32(x.as_atom()) for x in items[7].as_iter()]]


def _state_amounts(new_state) -> list[int]:
    return reserve_amounts(new_state if isinstance(new_state, list) else state_to_list(new_state))


# ---- pools -----------------------------------------------------------------------------

@dataclass
class Reserve:
    index: int
    asset_id: bytes32 | None          # None is native XCH
    inner: Program                    # p2_delegated_by_singleton, curried
    full_hash: bytes32
    coin: Coin
    lineage: LineageProof | None      # CAT reserves only

    @property
    def inner_hash(self) -> bytes32:
        return self.inner.get_tree_hash()


@dataclass
class V13Pool:
    launcher_parent: bytes32
    launcher_id: bytes32
    struct_hash: bytes32
    hint: bytes32
    asset_ids: list
    weights: list[int]
    fee_bps: int
    protocol_fee_bps: int
    protocol_ph: bytes32
    reserves: list[Reserve]
    state: list
    finalizer: Program
    leaves: dict                      # curried leaf programs by name
    tree: MerkleTree
    inner: Program
    coin: Coin
    lineage: LineageProof
    lp_tail: Program                  # curried TAIL; its tree hash is the LP asset id
    slot_first_curry_hash: bytes32
    extra: dict = field(default_factory=dict)
    dao_ph: bytes32 = ZERO_32
    # V13: the height this pool coin was created at; every leaf solution carries it and the
    # prologue asserts it (ASSERT_MY_BIRTH_HEIGHT). Offline, the validator cannot check it.
    birth: int = 0         # V11.1: the DAO recipient; the current rate is state[5]

    @property
    def dao_fee_bps(self) -> int:
        return int(self.state[5])

    @property
    def inner_hash(self) -> bytes32:
        return self.inner.get_tree_hash()

    @property
    def lp_asset_id(self) -> bytes32:
        return self.lp_tail.get_tree_hash()

    @property
    def merkle_root(self) -> bytes32:
        return bytes32(self.tree.root)

    def config(self) -> list:
        """PoolConfig, in struct field order, as curried into every leaf."""
        return [[a if a is not None else ZERO_32 for a in self.asset_ids], self.weights, self.fee_bps,
                self.protocol_fee_bps, self.protocol_ph, self.lp_asset_id, PRICE_SCALE, ORACLE_WINDOW, self.dao_ph]

    def inner_for_state(self, state) -> Program:
        return ACTION_LAYER.curry(self.finalizer, self.merkle_root, Program.to(state))

    def committed(self, new_state) -> list:
        """V13: what the finalizer actually commits -- the leaf's new state with reserve_parents
        rewritten to the ids of the reserve coins this spend consumes (their children's parents)."""
        st = list(new_state if isinstance(new_state, list) else state_to_list(new_state))
        st[7] = [r.coin.name() for r in self.reserves]
        return st

    def successor_puzzle_hash(self, new_state) -> bytes32:
        return puzzle_for_singleton(self.launcher_id, self.inner_for_state(self.committed(new_state))).get_tree_hash()

    def leaf_proof(self, name: str) -> list:
        p = self.tree.proof(self.leaves[name].get_tree_hash())
        return [p.path, *p.hashes]

    def advance(self, new_state) -> "V13Pool":
        """The pool after a spend that produced `new_state`: successor singleton and reserves."""
        new_state = self.committed(new_state)       # V13: the finalizer's parent rewrite
        inner = self.inner_for_state(new_state)
        coin = Coin(self.coin.name(), puzzle_for_singleton(self.launcher_id, inner).get_tree_hash(), uint64(1))
        lineage = LineageProof(self.coin.parent_coin_info, self.inner_hash, uint64(1))
        amounts = reserve_amounts(new_state)
        reserves = []
        for r in self.reserves:
            succ = Coin(r.coin.name(), r.full_hash, uint64(amounts[r.index]))
            lin = None if r.asset_id is None else LineageProof(r.coin.parent_coin_info, r.inner_hash, r.coin.amount)
            reserves.append(replace(r, coin=succ, lineage=lin))
        st = new_state if isinstance(new_state, list) else state_to_list(new_state)
        # V13: a successor is born AFTER the height its predecessor claimed (consensus checks a claimed
        # height against the previous transaction block), and the prologue asserts birth > last_height.
        # Offline, the earliest block the successor can exist in is the next one.
        return replace(self, state=new_state, inner=inner, coin=coin, lineage=lineage, reserves=reserves, birth=st[3][0] + 1)


def make_pool(asset_ids: list, reserves: list[int], total_lp: int = 1_000_000,
              fees: list[int] | None = None, salt: int = 0x51, leaves=None,
              weights: list[int] | None = None, fee_bps: int = 30, protocol_fee_bps: int = 5,
              protocol_ph: bytes32 | None = None, last_height: int = 0,
              reserve_full_hashes: list[bytes32] | None = None,
              reserve_inner_hashes: list[bytes32] | None = None,
              launcher_parent: bytes32 | None = None,
              reserve_coins: list | None = None, state: list | None = None,
              dao_ph: bytes32 | None = None, dao_fee_bps: int = 0) -> V13Pool:
    """A V11 pool ready to spend.

    `leaves`: None for the passthrough test leaf alone, "forge" for the five real
    leaves behind the production root, or an explicit list of curried programs.
    `reserve_full_hashes` / `reserve_inner_hashes` let a suite curry a finalizer
    with ANOTHER pool's reserves, which is how the isolation probe is built.
    `launcher_parent` names the real launcher's parent (else derived from `salt`);
    `reserve_coins` is a list of (Coin, LineageProof | None) for real reserves
    (else coins are fabricated from `salt`); `state` overrides the genesis state.
    """
    assert len(asset_ids) == len(reserves)
    n = len(asset_ids)
    weights = weights if weights is not None else [1] * n
    # V13: defaults to the fabricated registry's treasury (make_registry), which pins it
    protocol_ph = protocol_ph if protocol_ph is not None else bytes32(b"\x7a" * 32)
    launcher_parent = launcher_parent if launcher_parent is not None else bytes32(bytes([salt]) * 32)
    launcher_id = coin_id(launcher_parent, SINGLETON_LAUNCHER_HASH, 1)
    struct_hash = Program.to((SINGLETON_TOP_LAYER_V1_1_HASH, (launcher_id, SINGLETON_LAUNCHER_HASH))).get_tree_hash()
    state = state if state is not None else forge_state(reserves, total_lp, fees, last_height=last_height, dao_fee_bps=dao_fee_bps)
    dao_ph = dao_ph if dao_ph is not None else ZERO_32
    amounts = reserve_amounts(state)
    lp_tail = LP_TAIL_MOD.curry(launcher_id, PROTOCOL_VERSION)
    slot_first = SLOT.curry(Program.to((SINGLETON_TOP_LAYER_V1_1_HASH, struct_hash)), 1).get_tree_hash()

    built: list[Reserve] = []
    for i, (asset, amount) in enumerate(zip(asset_ids, amounts)):
        inner = P2_DELEGATED.curry(SINGLETON_TOP_LAYER_V1_1_HASH, struct_hash, i)
        inner_hash = inner.get_tree_hash()
        if reserve_coins is not None:
            coin, lineage = reserve_coins[i]
            full_hash = inner_hash if asset is None else construct_cat_puzzle(CAT_MOD, asset, inner).get_tree_hash()
            assert bytes(coin.puzzle_hash) == bytes(full_hash), f"reserve {i} coin is not at the reserve puzzle hash"
            assert int(coin.amount) == amount, f"reserve {i} coin holds {coin.amount}, state says {amount}"
        elif asset is None:
            full_hash = inner_hash
            parent = bytes32(bytes([0x70 + i, salt]) * 16)
            coin = Coin(parent, full_hash, uint64(amount))
            lineage = None
        else:
            full_hash = construct_cat_puzzle(CAT_MOD, asset, inner).get_tree_hash()
            grandparent = bytes32(bytes([0x90 + i, salt]) * 16)
            parent = coin_id(grandparent, full_hash, amount)
            coin = Coin(parent, full_hash, uint64(amount))
            lineage = LineageProof(grandparent, inner_hash, uint64(amount))
        built.append(Reserve(i, asset, inner, bytes32(full_hash), coin, lineage))

    # V13: reserve parents live in state and must be the parents of the coins this pool will
    # actually spend. Fabricated reserves (no `reserve_coins`) get their fabricated parents even
    # when a caller re-seeds a pool from an earlier state; real coins carry their own.
    if isinstance(state, list) and (reserve_coins is None or all(p == ZERO_32 for p in state[7])):
        state = [*state[:7], [r.coin.parent_coin_info for r in built]]
    full_hashes = reserve_full_hashes if reserve_full_hashes is not None else [r.full_hash for r in built]
    inner_hashes = reserve_inner_hashes if reserve_inner_hashes is not None else [r.inner_hash for r in built]
    # V13: the finalizer is curried with the configuration binding -- the config hash, the six
    # leaf mod hashes and the observe slot -- and asserts on every spend that the action
    # layer's merkle root is the six-leaf root of exactly that configuration (second audit M-4).
    # A test pool built on the passthrough leaf puts six passthroughs behind the same binding.
    config_list = [[a if a is not None else ZERO_32 for a in asset_ids], list(weights), fee_bps, protocol_fee_bps,
                   protocol_ph, lp_tail.get_tree_hash(), PRICE_SCALE, ORACLE_WINDOW, dao_ph]
    config_hash = Program.to(config_list).get_tree_hash()
    if leaves == "forge":
        leaf_mod_hashes = [LEAF_MODS[name].get_tree_hash() for name in LEAF_ORDER]
    elif leaves is None:
        leaf_mod_hashes = [PASSTHROUGH.get_tree_hash()] * 3 + [PASSTHROUGH_OBSERVE.get_tree_hash()] + [PASSTHROUGH.get_tree_hash()] * 2
    else:
        # an explicit leaf list is curried against the HONEST binding: a list that is not the six
        # real leaves of this config in driver order is refused by the finalizer, by design
        leaf_mod_hashes = [LEAF_MODS[name].get_tree_hash() for name in LEAF_ORDER]
    first = FINALIZER_MOD.curry(ACTION_LAYER.get_tree_hash(), full_hashes, inner_hashes, RESERVE_AMOUNT, launcher_id,
                                config_hash, leaf_mod_hashes, bytes32(slot_first))
    finalizer = first.curry(first.get_tree_hash())

    pool = V13Pool(launcher_parent, launcher_id, struct_hash, launcher_id, list(asset_ids), list(weights),
                   fee_bps, protocol_fee_bps, protocol_ph, built, state, finalizer, {}, MerkleTree([]),
                   Program.to(0), Coin(launcher_id, ZERO_32, uint64(1)),
                   LineageProof(launcher_parent, None, uint64(1)), lp_tail, bytes32(slot_first), dao_ph=dao_ph)
    # V13: the prologue asserts birth > last_height, so a fabricated pool is born the block after the
    # height its state last claimed -- the earliest block it could exist in. Real pools override this
    # with the chain's confirmed height (deploy commit_pool, resync, snapshot_to_pool).
    pool.birth = int(state[3][0]) + 1
    config = Program.to(pool.config())
    assert config.get_tree_hash() == config_hash, "the finalizer's config binding must be the pool's config"
    if leaves is None:
        # six passthroughs behind one binding: [pt, pt, pt, pt_observe, pt, pt] in driver order
        pt = PASSTHROUGH.curry(config)
        pt_obs = PASSTHROUGH_OBSERVE.curry(config, slot_first)
        curried = {"passthrough_action": pt}
        order = ["passthrough_action", "passthrough_action", "passthrough_action", "passthrough_observe",
                 "passthrough_action", "passthrough_action"]
        curried["passthrough_observe"] = pt_obs
    elif leaves == "forge":
        curried = {name: (LEAF_MODS[name].curry(config, slot_first) if name == "forge_action_observe"
                          else LEAF_MODS[name].curry(config)) for name in LEAF_ORDER}
        order = list(LEAF_ORDER)
    else:
        curried = {f"leaf{i}": p for i, p in enumerate(leaves)}
        order = list(curried)
    pool.leaves = curried
    pool.tree = MerkleTree([curried[name].get_tree_hash() for name in order])
    pool.inner = ACTION_LAYER.curry(finalizer, pool.merkle_root, Program.to(state))
    pool.coin = Coin(launcher_id, puzzle_for_singleton(launcher_id, pool.inner).get_tree_hash(), uint64(1))
    return pool


# ---- running a leaf locally --------------------------------------------------------------

def with_birth(pool, solution: list) -> list:
    """V13: every leaf solution is [h, birth, ...]; callers pass [h, ...] and the pool's birth is inserted."""
    return [solution[0], pool.birth, *solution[1:]]


def run_leaf(pool: V13Pool, name: str, solution: list, program: Program | None = None,
             ephemeral=None, state=None):
    """What the action layer will see: ((ephemeral' . state') . conditions).
    Returns (new_state, tagged [(index, condition)], base conditions, ephemeral')."""
    leaf = program if program is not None else pool.leaves[name]
    state = state if state is not None else pool.state
    out = leaf.run(Program.to(((ephemeral, state), with_birth(pool, solution))))
    new_state = out.first().rest()
    tagged_conds, base = [], []
    for c in out.rest().as_iter():
        if c.first().atom is not None and c.first().as_int() == RESERVE_TAG:
            tagged_conds.append((c.rest().first().as_int(), c.rest().rest()))
        else:
            base.append(c)
    return new_state, tagged_conds, base, out.first().first()


def tagged(index: int, condition: list) -> Program:
    """(-42 index . condition): the reserve marker with its index."""
    return Program.to((RESERVE_TAG, (index, condition)))


def delegated_puzzle_for(pool: V13Pool, reserve: Reserve, new_state, tagged_in_emission_order: list) -> Program:
    """The (q . conditions) the finalizer will hash for this reserve: recreate first,
    then this reserve's tagged conditions in REVERSE emission order (split_conditions prepends)."""
    amounts = _state_amounts(new_state)
    recreate = Program.to([CREATE_COIN, reserve.inner_hash, amounts[reserve.index], [pool.hint]])
    mine = [c for (i, c) in reversed(tagged_in_emission_order) if i == reserve.index]
    return Program.to((1, [recreate, *mine]))


# ---- assembling spends -------------------------------------------------------------------------

def assemble(pool: V13Pool, leaf: Program, proof: list, solution: list, new_state, tagged_conditions: list,
             parent_ids: list | None = None, reserve_delegated: dict | None = None,
             reserve_sender_inner_hash: bytes | None = None, extra_spends: list = (),
             extra_cats: dict | None = None, spend_reserves: bool = True,
             selector: int = SINGLE_LEAF_SELECTOR) -> SpendBundle:
    # V13: no reserve parent ids in the solution; the finalizer reads them from state
    # (`parent_ids` is accepted and ignored so old call sites fail loudly in their asserts, not here).
    inner_solution = Program.to([[leaf], [[selector, *proof]], [solution]])
    singleton_spend = make_spend(pool.coin, puzzle_for_singleton(pool.launcher_id, pool.inner),
                                 solution_for_singleton(pool.lineage, uint64(1), inner_solution))
    spends = [singleton_spend, *extra_spends]
    if spend_reserves:
        sender_inner = reserve_sender_inner_hash if reserve_sender_inner_hash is not None else pool.inner_hash
        cats = {k: list(v) for k, v in (extra_cats or {}).items()}
        for r in pool.reserves:
            dp = (reserve_delegated or {}).get(r.index) or delegated_puzzle_for(pool, r, new_state, tagged_conditions)
            p2_solution = Program.to([sender_inner, dp])
            if r.asset_id is None:
                spends.append(make_spend(r.coin, r.inner, p2_solution))
            else:
                cats.setdefault(r.asset_id, []).append(
                    SpendableCAT(r.coin, r.asset_id, r.inner, p2_solution, lineage_proof=r.lineage))
        for asset, spendables in cats.items():
            try:
                spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables).coin_spends)
            except ValueError as exc:
                # chia's ring builder refuses an unbalanced CAT ring before consensus
                # would; the CAT puzzle itself fails the same way on chain.
                raise Rejected(f"CAT ring: {exc}") from exc
    return SpendBundle(spends, G2Element())


def spend_pool(pool: V13Pool, new_state, tagged_conditions=(), base_conditions=(), **knobs) -> SpendBundle:
    """Passthrough-leaf spend: the state and conditions are whatever the caller names."""
    solution = [Program.to(new_state), *[tagged(i, c) for i, c in tagged_conditions], *base_conditions]
    tagged_progs = [(i, Program.to(c)) for i, c in tagged_conditions]
    # V13: reveal the CURRIED passthrough -- it is what sits behind the root the finalizer asserts
    return assemble(pool, pool.leaves["passthrough_action"], pool.leaf_proof("passthrough_action"), solution, new_state, tagged_progs, **knobs)


def spend_action(pool: V13Pool, name: str, solution: list, leaf: Program | None = None,
                 proof: list | None = None, **knobs):
    """Run leaf `name` locally, then assemble the full bundle around what it returned.
    `leaf` / `proof` substitute what is revealed to the action layer (adversarial lane).
    Returns (bundle, new_state_program)."""
    new_state, tagged_conds, _base, _eph = run_leaf(pool, name, solution)
    leaf = leaf if leaf is not None else pool.leaves[name]
    proof = proof if proof is not None else pool.leaf_proof(name)
    # V13: the on-chain solution carries birth, exactly as run_leaf's local run did.
    return assemble(pool, leaf, proof, with_birth(pool, solution), new_state, tagged_conds, **knobs), new_state


# ---- settlement coins ----------------------------------------------------------------------------

def offer_settlement_xch(amount: int, salt: int = 0xC0):
    """An OFFER_MOD coin spent under its own id with no payments: the trader's input
    as V10 shaped it. Its mojos fund the growing reserve; the leaf asserts its
    announcement. Returns (coin, coin_spend)."""
    coin = Coin(bytes32(bytes([salt]) * 32), bytes32(OFFER_MOD_HASH), uint64(amount))
    return coin, make_spend(coin, OFFER_MOD, Program.to([[coin.name()]]))


def offer_settlement_cat(asset_id: bytes32, amount: int, salt: int = 0xC1):
    outer_ph = construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash()
    grandparent = bytes32(bytes([salt]) * 32)
    parent = coin_id(grandparent, outer_ph, amount)
    coin = Coin(parent, outer_ph, uint64(amount))
    return coin, SpendableCAT(coin, asset_id, OFFER_MOD, Program.to([[coin.name()]]),
                              lineage_proof=LineageProof(grandparent, bytes32(OFFER_MOD_HASH), uint64(amount)))


def xch_settlement(amount: int, salt: int = 0xC0):
    """A plain coin spent for `amount` with no outputs (finalizer suite)."""
    coin = Coin(bytes32(bytes([salt]) * 32), IDENTITY.get_tree_hash(), uint64(amount))
    return make_spend(coin, IDENTITY, Program.to([]))


def cat_settlement(asset_id: bytes32, amount: int, salt: int = 0xC1) -> SpendableCAT:
    outer_ph = construct_cat_puzzle(CAT_MOD, asset_id, IDENTITY).get_tree_hash()
    grandparent = bytes32(bytes([salt]) * 32)
    parent = coin_id(grandparent, outer_ph, amount)
    coin = Coin(parent, outer_ph, uint64(amount))
    return SpendableCAT(coin, asset_id, IDENTITY, Program.to([]),
                        lineage_proof=LineageProof(grandparent, IDENTITY.get_tree_hash(), uint64(amount)))


# ---- the LP CAT ------------------------------------------------------------------------------------

def lp_mint_spends(pool: V13Pool, lp_delta: int, new_total_lp: int, next_state_root: bytes32,
                   recipient_ph: bytes32, salt: int = 0xE0, lp_action: list | None = None,
                   eve_amount: int = 1, inner: Program | None = None):
    """The add-side LP coins: a funding coin that creates the mint eve, and the eve
    itself (LP CAT wrapping the pinned mint inner) minting `lp_delta` to
    `recipient_ph` with the hint the inner attaches. Returns (lp_parent_id, [coin_spends])."""
    inner = inner if inner is not None else LP_MINT_INNER
    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, inner).get_tree_hash()
    # Every CAT mojo is an XCH mojo: the funding coin carries the whole mint's
    # backing, creates the one-mojo eve, and the rest flows to the ring through
    # bundle-level conservation -- exactly how V10's creation offer sized it.
    funding = Coin(bytes32(bytes([salt]) * 32), IDENTITY.get_tree_hash(), uint64(max(lp_delta, eve_amount)))
    funding_spend = make_spend(funding, IDENTITY, Program.to([[CREATE_COIN, eve_ph, eve_amount]]))
    eve = Coin(funding.name(), eve_ph, uint64(eve_amount))
    action = lp_action if lp_action is not None else [lp_delta, new_total_lp, next_state_root, pool.inner_hash, ZERO_32]
    eve_solution = Program.to([recipient_ph, lp_delta, pool.lp_tail, action])
    spendable = SpendableCAT(eve, pool.lp_asset_id, inner, eve_solution, lineage_proof=LineageProof(),
                             extra_delta=lp_delta - eve_amount, limitations_program_reveal=pool.lp_tail,
                             limitations_solution=Program.to(action))
    try:
        ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends
    except ValueError as exc:
        raise Rejected(f"LP mint ring: {exc}") from exc
    return funding.name(), [funding_spend, *ring]


def lp_eve_ring(pool: V13Pool, eve: Coin, recipient_ph: bytes32, mint: int, action: list):
    """Spend an existing one-mojo eve (LP CAT wrapping the pinned mint inner, no CAT
    parent) minting `mint` LP to `recipient_ph` under `action` -- the genesis form
    when action names the pool's full puzzle hash, the message form otherwise.
    Returns [coin_spends] for the single-coin ring; the caller supplies the mojos."""
    eve_solution = Program.to([recipient_ph, mint, pool.lp_tail, action])
    spendable = SpendableCAT(eve, pool.lp_asset_id, LP_MINT_INNER, eve_solution, lineage_proof=LineageProof(),
                             extra_delta=mint - int(eve.amount), limitations_program_reveal=pool.lp_tail,
                             limitations_solution=Program.to(action))
    try:
        return unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends
    except ValueError as exc:
        raise Rejected(f"LP eve ring: {exc}") from exc


def genesis_action(pool: V13Pool) -> list:
    """The TAIL solution for the genesis mint: expected = new total = total_lp, launcher-authorized."""
    return [pool.state[1], pool.state[1], ZERO_32, ZERO_32, pool.coin.puzzle_hash]


def lp_melt_spend(pool: V13Pool, burn: int, new_total_lp: int, next_state_root: bytes32,
                  salt: int = 0xE8, lp_action: list | None = None, fabricated: bool = False):
    """The remove-side LP coin: `burn` LP wrapping the pinned melt inner, with a real
    CAT parent -- or, for the finding-4 probe, a parent that is NOT a CAT: the
    coin is created out of ordinary mojos by a plain coin in the same bundle. The
    ring still balances (amount - outputs + extra_delta = 0), so the only thing
    standing between that coin and a reserve release is the TAIL's
    `parent_is_cat || delta > 0`; a TAIL without it (CHIP-0040's) would score
    the message's delta and accept. Returns (lp_parent_id, [coin_spends])."""
    melt_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, LP_MELT_INNER).get_tree_hash()
    action = lp_action if lp_action is not None else [-burn, new_total_lp, next_state_root, pool.inner_hash, ZERO_32]
    if fabricated:
        funding = Coin(bytes32(bytes([salt]) * 32), IDENTITY.get_tree_hash(), uint64(burn))
        funding_spend = make_spend(funding, IDENTITY, Program.to([[CREATE_COIN, melt_ph, burn]]))
        melt = Coin(funding.name(), melt_ph, uint64(burn))
        lineage, before, extra = LineageProof(), [funding_spend], -burn
    else:
        grandparent = bytes32(bytes([salt]) * 32)
        holder_inner = IDENTITY.get_tree_hash()
        parent = coin_id(grandparent, construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, IDENTITY).get_tree_hash(), burn)
        melt = Coin(parent, melt_ph, uint64(burn))
        lineage, before, extra = LineageProof(grandparent, holder_inner, uint64(burn)), [], -burn
    melt_solution = Program.to([pool.lp_tail, action])
    spendable = SpendableCAT(melt, pool.lp_asset_id, LP_MELT_INNER, melt_solution, lineage_proof=lineage,
                             extra_delta=extra, limitations_program_reveal=pool.lp_tail,
                             limitations_solution=Program.to(action))
    try:
        ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends
    except ValueError as exc:
        raise Rejected(f"LP melt ring: {exc}") from exc
    return melt.parent_coin_info, [*before, *ring]


# ---- validation ------------------------------------------------------------------------------------

def validate(bundle: SpendBundle):
    """Consensus-level validation: raises Rejected on any failure, else returns
    (conditions, additions) where additions is a list of (puzzle_hash, amount)."""
    try:
        conds = get_conditions_from_spendbundle(bundle, MAX_COST, DEFAULT_CONSTANTS, VALIDATION_HEIGHT)
    except Exception as exc:  # chia_rs raises ValueError / TypeError with the validation code
        raise Rejected(f"{type(exc).__name__}: {exc}") from exc
    # chia_rs reports create_coin as (puzzle_hash, amount, hint) tuples
    additions = [(bytes32(cc[0]), int(cc[1])) for s in conds.spends for cc in s.create_coin]
    removed = sum(int(cs.coin.amount) for cs in bundle.coin_spends)
    added = sum(a for _, a in additions)
    if added > removed:
        raise Rejected(f"MINTING_COIN: creates {added} from {removed}")
    return conds, additions


def additions_with_hints(conds):
    return [(bytes32(cc[0]), int(cc[1]), (bytes(cc[2]) if cc[2] is not None else None))
            for s in conds.spends for cc in s.create_coin]


# ---- the registry ------------------------------------------------------------------------------------

from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER  # noqa: E402

REGISTRY_LEAF_ORDER = ("forge_registry_init", "forge_registry_register")
MIN_KEY, MAX_KEY = bytes32(b"\x00" * 32), bytes32(b"\xff" * 32)


def registry_available() -> bool:
    return (V13_COMPILED / "forge_registry_register.rue.hex").is_file()


if registry_available():
    DEFAULT_FINALIZER = v13_program("finalizer")
    REGISTRY_LEAF_MODS = {name: v13_program(name) for name in REGISTRY_LEAF_ORDER}
else:
    DEFAULT_FINALIZER, REGISTRY_LEAF_MODS = None, {}


@dataclass
class Registry:
    launcher_parent: bytes32
    launcher_id: bytes32
    struct_hash: bytes32
    creation_fee: int
    treasury_ph: bytes32
    constants: list
    leaves: dict
    tree: MerkleTree
    finalizer: Program
    slot_first: Program           # slot.rue curried with (singleton info, nonce 0)
    state: list
    inner: Program
    coin: Coin
    lineage: LineageProof

    @property
    def inner_hash(self) -> bytes32:
        return self.inner.get_tree_hash()

    @property
    def merkle_root(self) -> bytes32:
        return bytes32(self.tree.root)

    # V13: the pinned protocol parameters, from the curried constants
    @property
    def protocol_ph(self) -> bytes32:
        return bytes32(self.constants[11])

    @property
    def price_scale(self) -> int:
        return int(self.constants[12])

    @property
    def oracle_window(self) -> int:
        return int(self.constants[13])

    def leaf_proof(self, name: str) -> list:
        p = self.tree.proof(self.leaves[name].get_tree_hash())
        return [p.path, *p.hashes]

    def inner_for_state(self, state) -> Program:
        return ACTION_LAYER.curry(self.finalizer, self.merkle_root, Program.to(state))

    def successor_puzzle_hash(self, new_state) -> bytes32:
        return puzzle_for_singleton(self.launcher_id, self.inner_for_state(new_state)).get_tree_hash()

    def slot_puzzle(self, value) -> Program:
        return self.slot_first.curry(Program.to(value).get_tree_hash())

    def advance(self, new_state) -> "Registry":
        inner = self.inner_for_state(new_state)
        coin = Coin(self.coin.name(), puzzle_for_singleton(self.launcher_id, inner).get_tree_hash(), uint64(1))
        return replace(self, state=new_state, inner=inner, coin=coin,
                       lineage=LineageProof(self.coin.parent_coin_info, self.inner_hash, uint64(1)))


def pool_key(config: list) -> bytes32:
    """The registry's uniqueness key: tree_hash([asset_ids, weights, fee_bps, protocol_fee_bps, dao_puzzle_hash])."""
    return Program.to([config[0], config[1], config[2], config[3], config[8]]).get_tree_hash()


def slot_value(key: bytes, launcher_id: bytes, left_key: bytes, right_key: bytes):
    """(key . (launcher_id . (left_key . right_key)))"""
    return (key, (launcher_id, (left_key, right_key)))


def p2_delegated_solution(conditions: list) -> Program:
    """Solution for the standard p2_delegated_puzzle_or_hidden_puzzle: (() (q . conditions) ())."""
    return Program.to([[], (1, conditions), []])


def make_registry(salt: int = 0x21, creation_fee: int = 1_000_000, treasury_ph: bytes32 | None = None,
                  launcher_parent: bytes32 | None = None, state: list | None = None,
                  protocol_ph: bytes32 | None = None, price_scale: int = PRICE_SCALE,
                  oracle_window: int = ORACLE_WINDOW) -> Registry:
    """V13: `protocol_ph`, `price_scale` and `oracle_window` are registry constants every registered
    pool must carry verbatim (second audit M-3/L-3). The protocol fee recipient defaults to the treasury."""
    treasury_ph = treasury_ph if treasury_ph is not None else bytes32(b"\x7a" * 32)
    protocol_ph = protocol_ph if protocol_ph is not None else treasury_ph
    launcher_parent = launcher_parent if launcher_parent is not None else bytes32(bytes([salt]) * 32)
    launcher_id = coin_id(launcher_parent, SINGLETON_LAUNCHER_HASH, 1)
    struct_hash = Program.to((SINGLETON_TOP_LAYER_V1_1_HASH, (launcher_id, SINGLETON_LAUNCHER_HASH))).get_tree_hash()
    slot_first = SLOT.curry(Program.to((SINGLETON_TOP_LAYER_V1_1_HASH, struct_hash)), 0)
    constants = [
        ACTION_LAYER.get_tree_hash(), P2_DELEGATED.get_tree_hash(), SLOT.get_tree_hash(),
        FINALIZER_MOD.get_tree_hash(), RESERVE_AMOUNT.get_tree_hash(), LP_TAIL_MOD.get_tree_hash(),
        [LEAF_MODS[name].get_tree_hash() for name in LEAF_ORDER],
        creation_fee, treasury_ph, slot_first.get_tree_hash(), launcher_id,
        protocol_ph, price_scale, oracle_window,          # V13
    ]
    curried = {name: REGISTRY_LEAF_MODS[name].curry(Program.to(constants)) for name in REGISTRY_LEAF_ORDER}
    tree = MerkleTree([curried[name].get_tree_hash() for name in REGISTRY_LEAF_ORDER])
    first = DEFAULT_FINALIZER.curry(ACTION_LAYER.get_tree_hash(), launcher_id)
    finalizer = first.curry(first.get_tree_hash())
    state = state if state is not None else [0, 0]
    inner = ACTION_LAYER.curry(finalizer, bytes32(tree.root), Program.to(state))
    coin = Coin(launcher_id, puzzle_for_singleton(launcher_id, inner).get_tree_hash(), uint64(1))
    return Registry(launcher_parent, launcher_id, struct_hash, creation_fee, treasury_ph, constants, curried,
                    tree, finalizer, slot_first, state, inner, coin, LineageProof(launcher_parent, None, uint64(1)))


def registry_spend(reg: Registry, name: str, solution: list, extra_spends=(), leaf: Program | None = None,
                   proof: list | None = None):
    """Run the registry leaf locally, then the singleton spend plus whatever else the
    bundle needs (slots, launcher, fee). Returns (bundle, new_state_program)."""
    program = reg.leaves[name]
    out = program.run(Program.to(((None, reg.state), solution)))
    new_state = out.first().rest()
    leaf = leaf if leaf is not None else program
    proof = proof if proof is not None else reg.leaf_proof(name)
    inner_solution = Program.to([[leaf], [[SINGLE_LEAF_SELECTOR, *proof]], [solution]])
    singleton_spend = make_spend(reg.coin, puzzle_for_singleton(reg.launcher_id, reg.inner),
                                 solution_for_singleton(reg.lineage, uint64(1), inner_solution))
    return SpendBundle([singleton_spend, *extra_spends], G2Element()), new_state


def slot_spend(reg: Registry, value, parent: Coin, parent_inner_hash: bytes32, spender_inner_hash: bytes32 | None = None):
    """Spend a slot that `parent` (a registry singleton coin) created, on the current
    registry coin's message. Solution is (parent_proof . spender_inner_puzzle_hash)."""
    puzzle = reg.slot_puzzle(value)
    coin = Coin(parent.name(), puzzle.get_tree_hash(), uint64(0))
    proof = (parent.parent_coin_info, (parent_inner_hash, 1))
    spender = spender_inner_hash if spender_inner_hash is not None else reg.inner_hash
    return coin, make_spend(coin, puzzle, Program.to((proof, spender)))


def launcher_spend(pool: V13Pool, kv_list=None):
    """The standard singleton launcher creating the pool's eve coin. Its key-value
    list is (total_lp): what the TAIL's genesis mint and the registry both require."""
    # V13: (total_lp, eve_coin_id) -- the eve fabricated by lp_genesis_mint_spends with the default salt
    kv_list = [pool.state[1], pool.extra.get("eve_coin_id", genesis_eve_id(pool))] if kv_list is None else list(kv_list)
    coin = Coin(pool.launcher_parent, SINGLETON_LAUNCHER_HASH, uint64(1))
    assert coin.name() == pool.launcher_id
    return coin, make_spend(coin, SINGLETON_LAUNCHER, Program.to([pool.coin.puzzle_hash, 1, kv_list]))


def genesis_eve_id(pool, salt: int = 0xE4, mint: int | None = None) -> bytes32:
    """The id of the eve lp_genesis_mint_spends fabricates for this salt (no CAT parent)."""
    total_lp = pool.state[1]; mint = total_lp if mint is None else mint
    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, LP_MINT_INNER).get_tree_hash()
    funding = Coin(bytes32(bytes([salt]) * 32), IDENTITY.get_tree_hash(), uint64(max(mint, 1)))
    return Coin(funding.name(), eve_ph, uint64(1)).name()


def genesis_lp_settlement_spends(pool: V13Pool, eve: Coin, recipient_ph: bytes32, mint: int,
                                 burn: int | None = None) -> list:
    """V13: the genesis mint lands at CAT(lp, OFFER_MOD); this settlement pays the locked floor
    to the zero puzzle hash and the rest to the creator, both under the launcher id as nonce.
    `register` asserts the burn group's announcement, so a registered pool has burned its
    floor by construction (second audit: the V12 website lane paid it all to the creator).
    `burn` defaults to MIN_LOCKED_LP; a test may name another amount to prove the assert."""
    burn = MIN_LOCKED_LP if burn is None else burn
    settlement = Coin(eve.name(), construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, OFFER_MOD).get_tree_hash(), uint64(mint))
    groups = []
    if burn > 0:
        groups.append((pool.launcher_id, [[ZERO_32, burn]]))
    if mint - burn > 0:
        groups.append((pool.launcher_id, [[recipient_ph, mint - burn, [recipient_ph]]]))
    return unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(
        settlement, pool.lp_asset_id, OFFER_MOD, Program.to(groups),
        lineage_proof=LineageProof(eve.parent_coin_info, LP_MINT_INNER.get_tree_hash(), uint64(int(eve.amount))))]).coin_spends


def lp_genesis_mint_spends(pool: V13Pool, recipient_ph: bytes32, salt: int = 0xE4, mint: int | None = None,
                           with_cat_parent: bool = False, burn: int | None = None):
    """The genesis LP mint: an eve with no CAT parent minting the pool's whole genesis supply
    to the LP settlement, which pays the locked floor to the burn address and the rest to the
    creator -- authorized by the launcher's announcement rather than by a pool message.
    Returns [coin_spends]. V13: the split is what `register` asserts."""
    total_lp = pool.state[1]
    mint = total_lp if mint is None else mint
    eve_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, LP_MINT_INNER).get_tree_hash()
    action = [mint, mint, ZERO_32, ZERO_32, pool.coin.puzzle_hash]
    eve_solution = Program.to([bytes32(OFFER_MOD_HASH), mint, pool.lp_tail, action])
    if with_cat_parent:
        grandparent = bytes32(bytes([salt]) * 32)
        parent = coin_id(grandparent, construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, IDENTITY).get_tree_hash(), 1)
        eve = Coin(parent, eve_ph, uint64(1))
        lineage, before = LineageProof(grandparent, IDENTITY.get_tree_hash(), uint64(1)), []
        funding_spend = xch_settlement(mint, salt=salt + 1)
        before = [funding_spend]
    else:
        funding = Coin(bytes32(bytes([salt]) * 32), IDENTITY.get_tree_hash(), uint64(max(mint, 1)))
        funding_spend = make_spend(funding, IDENTITY, Program.to([[CREATE_COIN, eve_ph, 1]]))
        eve = Coin(funding.name(), eve_ph, uint64(1))
        lineage, before = LineageProof(), [funding_spend]
    spendable = SpendableCAT(eve, pool.lp_asset_id, LP_MINT_INNER, eve_solution, lineage_proof=lineage,
                             extra_delta=mint - 1, limitations_program_reveal=pool.lp_tail,
                             limitations_solution=Program.to(action))
    try:
        ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends
    except ValueError as exc:
        raise Rejected(f"LP genesis ring: {exc}") from exc
    return [*before, *ring, *genesis_lp_settlement_spends(pool, eve, recipient_ph, mint, burn=burn)]


def fee_settlement(reg: Registry, launcher_id: bytes32, amount: int | None = None, salt: int = 0xF0):
    """An OFFER_MOD coin paying the creation fee to the treasury under the pool's launcher id as nonce."""
    amount = reg.creation_fee if amount is None else amount
    coin = Coin(bytes32(bytes([salt]) * 32), bytes32(OFFER_MOD_HASH), uint64(amount))
    return coin, make_spend(coin, OFFER_MOD, Program.to([[launcher_id, [reg.treasury_ph, amount, [reg.treasury_ph]]]]))


def register_solution(pool: V13Pool, left: tuple, right: tuple) -> list:
    """`left` / `right` are (key, launcher_id, far_key) of the two adjacent slots."""
    return [pool.launcher_parent, pool.config(), pool.state[0], pool.state[1], pool.state[5],
            pool.state[7], pool.extra.get("eve_coin_id", genesis_eve_id(pool)),
            (left[1], left[2]), (right[1], right[2]), left[0], right[0]]
