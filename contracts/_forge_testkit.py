#!/usr/bin/env python3
"""Shared harness for the Forge suites.

Synthetic makers, offer assembly, and the bundle audit -- the parts that are the
same whatever revision is under test.

These used to live inside the per-revision suites, so the current suites imported
them from `_test_v6_transition` and friends. That coupling meant a checkout
without the archive (which is not published, because superseded puzzles carry
critical authorization bugs) skipped the legacy suites at import AND took the
current ones down with them -- six suites running out of thirty-three. The
harness belongs somewhere neither depends on.
"""
import hashlib
import sys

sys.path.insert(0, ".")

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode as OP
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD, LineageProof, SpendableCAT, construct_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.trading.offer import OFFER_MOD_HASH, Offer
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import Coin, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

IDENTITY = Program.to(1)
USER_PH = bytes32.fromhex("ab" * 32)
NONCE = bytes32.fromhex("ee" * 32)
MAX_COST = 11_000_000_000


def cat_maker_spend(asset_id: bytes32, amount: int, seed: int):
    """A real CAT spend creating an OFFER_MOD settlement of `amount`."""
    outer_ph = construct_cat_puzzle(CAT_MOD, asset_id, IDENTITY).get_tree_hash()
    grandparent = bytes32.fromhex(f"{seed:02x}" * 32)
    parent = Coin(grandparent, outer_ph, uint64(amount))
    maker = Coin(parent.name(), outer_ph, uint64(amount))
    spendable = SpendableCAT(
        maker, asset_id, IDENTITY,
        Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, amount]]),
        lineage_proof=LineageProof(grandparent, IDENTITY.get_tree_hash(), uint64(amount)),
    )
    return list(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends)

def xch_maker_spend(amount: int, seed: int = 0xCD):
    maker = Coin(bytes32.fromhex(f"{seed:02x}" * 32), IDENTITY.get_tree_hash(), uint64(amount))
    return [make_spend(maker, IDENTITY, Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, amount]]))]

def make_offer(spends, requested):
    drivers = {a: PuzzleInfo({"type": "CAT", "tail": "0x" + a.hex()}) for a in requested if a is not None}
    return Offer(requested, WalletSpendBundle(spends, G2Element()), drivers)

def audit(bundle, external: set) -> list[str]:
    """`external` is the set of coin ids expected to pre-exist (pool, reserves,
    and the synthetic maker coins). Anything else a spend consumes must be
    created inside the bundle, or the node answers UNKNOWN_UNSPENT."""
    sha = lambda *p: hashlib.sha256(b"".join(p)).digest()
    made_coin, made_puzzle, asserts, problems = set(), set(), [], []
    spent_ids, created_ids, seen = set(), set(), []
    for spend in bundle.coin_spends:
        spent_ids.add(bytes(spend.coin.name()))
        seen.append(bytes(spend.coin.name()))
        try:
            cond = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, 11_000_000_000)
        except Exception as exc:
            problems.append(f"spend {spend.coin.name().hex()[:16]} failed: {exc}")
            continue
        cid, ph = bytes(spend.coin.name()), bytes(spend.coin.puzzle_hash)
        for c in cond.get(OP.CREATE_COIN_ANNOUNCEMENT, []):
            made_coin.add(sha(cid, c.vars[0]))
        for c in cond.get(OP.CREATE_PUZZLE_ANNOUNCEMENT, []):
            made_puzzle.add(sha(ph, c.vars[0]))
        for c in cond.get(OP.ASSERT_COIN_ANNOUNCEMENT, []):
            asserts.append(("coin", spend.coin.name().hex()[:16], bytes(c.vars[0])))
        for c in cond.get(OP.ASSERT_PUZZLE_ANNOUNCEMENT, []):
            asserts.append(("puzzle", spend.coin.name().hex()[:16], bytes(c.vars[0])))
        for c in cond.get(OP.ASSERT_MY_COIN_ID, []):
            if bytes(c.vars[0]) != cid:
                problems.append(f"ASSERT_MY_COIN_ID mismatch on {spend.coin.name().hex()[:16]}")
        for c in cond.get(OP.ASSERT_MY_PARENT_ID, []):
            if bytes(c.vars[0]) != bytes(spend.coin.parent_coin_info):
                problems.append(f"ASSERT_MY_PARENT_ID mismatch on {spend.coin.name().hex()[:16]}")
        for c in cond.get(OP.CREATE_COIN, []):
            amount = int.from_bytes(c.vars[1], "big") if c.vars[1] else 0
            if amount >= 0:
                created_ids.add(bytes(Coin(spend.coin.name(), bytes32(c.vars[0]), uint64(amount)).name()))
    for kind, who, ann in asserts:
        pool = made_coin if kind == "coin" else made_puzzle
        if ann not in pool:
            problems.append(f"unsatisfied {kind} announcement from {who}: {ann.hex()[:16]}")
    for coin_id in spent_ids - created_ids - external:
        problems.append(f"spends {coin_id.hex()[:16]} which is never created in-bundle (UNKNOWN_UNSPENT)")
    for coin_id in {c for c in seen if seen.count(c) > 1}:
        problems.append(f"spends {coin_id.hex()[:16]} {seen.count(coin_id)} times (DOUBLE_SPEND)")
    return problems, len(asserts)


# ── pools, built at the shipping revision ───────────────────────────────────
import forge_puzzles
import forge_stdin as fs
from forge_create_pool import deploy
from forge_offer import ZERO_32
from chia.wallet.util.curry_and_treehash import (
    calculate_hash_of_quoted_mod_hash, curry_and_treehash, shatree_atom,
)

FORGE_VERSION = forge_puzzles.FORGE_VERSION
FEE_BPS = 30
PROTOCOL_BPS = 25
TREASURY = bytes32.fromhex("7e" * 32)
X = bytes32.fromhex("a1" * 32)
Y = bytes32.fromhex("b2" * 32)
Z = bytes32.fromhex("c3" * 32)


def creation_offer_for(assets, bootstrap, salt: int, lp_ratio: int = 1) -> Offer:
    """A creation offer for any asset mix: native XCH, CATs, or both.

    The single XCH settlement does double duty, which is why it is not simply the
    LP mint: it backs the genesis LP mint (plus the two mojos for the eve and
    guard coins) AND, when the pool holds a native XCH reserve, seeds that
    reserve too. `forge_create_pool` checks the exact total, so getting this
    wrong reads as "XCH settlement must equal N".

    This used to assume every reserve was a CAT, so no suite could build a pool
    with a native XCH reserve -- and nearly every real pool has one.
    """
    initial_lp = lp_ratio * min(bootstrap[a] for a in assets)
    xch_reserve = sum(bootstrap[a] for a in assets if a == ZERO_32)
    xch_amount = 2 + initial_lp + xch_reserve
    spends = [make_spend(
        Coin(bytes32.fromhex(f"{salt:02x}" * 32), IDENTITY.get_tree_hash(), uint64(xch_amount)),
        IDENTITY,
        Program.to([[OP.CREATE_COIN, OFFER_MOD_HASH, xch_amount]]),
    )]
    cats = [a for a in assets if a != ZERO_32]
    for index, asset_id in enumerate(cats):
        spends.extend(cat_maker_spend(asset_id, bootstrap[asset_id], salt + index + 1))
    return Offer(
        {}, WalletSpendBundle(spends, G2Element()),
        {a: PuzzleInfo({"type": "CAT", "tail": "0x" + a.hex()}) for a in cats},
    )


def make_pool(assets, bootstrap, salt, protocol_bps=PROTOCOL_BPS, fee_bps=FEE_BPS, weights=None, lp_ratio=1):
    """A pool at the shipping revision, through the real creation path."""
    execution = {
        "protocolVersion": FORGE_VERSION,
        "assetIds": [a.hex() for a in assets],
        "bootstrapAmounts": [str(bootstrap[a]) for a in assets],
        "swapFeeBps": fee_bps,
        "protocolFeeBps": protocol_bps,
        "protocolPuzzleHash": TREASURY.hex(),
        "lpRecipientPuzzleHash": USER_PH.hex(),
    }
    if weights is not None:
        execution["weights"] = list(weights)
    if lp_ratio != 1:
        execution["lpRatio"] = lp_ratio
    result = deploy({"offer": creation_offer_for(assets, bootstrap, salt, lp_ratio).to_bech32(),
                     "dry_run": True, "execution": execution})
    if not result.get("success"):
        raise RuntimeError(result.get("error"))
    return fs._pool(result["poolSnapshot"])


def external_of(pools, spends) -> set:
    """Coins that legitimately pre-exist: pool coins, reserves, maker inputs."""
    pools = pools if isinstance(pools, (list, tuple)) else [pools]
    ids = {bytes(p.pool.coin.name()) for p in pools}
    for p in pools:
        ids |= {bytes(r.coin.name()) for r in p.reserves.values()}
    ids |= {bytes(s.coin.name()) for s in spends}
    return ids


def paid_to(bundle, asset_id, inner_ph) -> int:
    """How much of `asset_id` the bundle creates at `inner_ph`."""
    quoted = calculate_hash_of_quoted_mod_hash(CAT_MOD.get_tree_hash())
    wrapped = curry_and_treehash(quoted, shatree_atom(CAT_MOD.get_tree_hash()),
                                 shatree_atom(asset_id), inner_ph)
    total = 0
    for spend in bundle.coin_spends:
        try:
            cond = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_COST)
        except Exception:
            continue
        for c in cond.get(OP.CREATE_COIN, []):
            amount = int.from_bytes(c.vars[1], "big") if c.vars[1] else 0
            if amount > 0 and bytes32(c.vars[0]) in (wrapped, inner_ph):
                total += amount
    return total


# ── the standard two-CAT fixture ────────────────────────────────────────────
# One pair, equally bootstrapped: enough for anything that just needs a live
# pool to act on rather than a particular shape.
ASSETS = sorted([X, Y])
BOOTSTRAP = {a: 1_000_000 for a in ASSETS}


def creation_offer(salt: int = 0xCD) -> Offer:
    return creation_offer_for(ASSETS, BOOTSTRAP, salt)


def standard_pool(salt: int = 0xCD, protocol_bps=PROTOCOL_BPS):
    return make_pool(ASSETS, BOOTSTRAP, salt, protocol_bps)
