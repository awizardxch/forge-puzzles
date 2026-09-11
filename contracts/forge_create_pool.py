#!/usr/bin/env python3
"""Keyless Forge V6 pool creation for 2 to 10 equal-weight reserves.

Generalizes V5 from a fixed asset pair to N assets, any mix of native XCH and
CATs. Assets are canonically sorted ascending, which puts native XCH first (it
is the zero asset id) and rules out duplicates. Weights are equal to within one
bps, matching valid_assets in pool_singleton_v6.rue.

The bundle shape is unchanged from V5: the user's offer carries every mojo, the
launcher and LP genesis are assembled here, and nothing is signed server-side.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, LineageProof, SpendableCAT, construct_cat_puzzle, get_innerpuzzle_from_puzzle, unsigned_spend_bundle_for_spendable_cats
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER, SINGLETON_LAUNCHER_HASH, SINGLETON_TOP_LAYER_V1_1_HASH, puzzle_for_singleton
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH, Offer
from chia_rs import Coin as RsCoin, G2Element, SpendBundle

from forge_offer import find_offer_settlements

import forge_puzzles

ROOT = Path(__file__).resolve().parent
ZERO32 = b"\x00" * 32
V6 = 6
V7 = 7
V8 = 8
V9 = 9
V10 = 10
# V6 requires two assets; V7 lifts the floor to one, so a single-asset pool must
# be created as V7. Everything else about creation is identical between them.
MIN_ASSETS = {V6: 2, V7: 1, V8: 1, V9: 1, V10: 1}
# The version new pools mint at unless one is named explicitly.
#
# V9 is the default and no earlier revision should be minted again: V4 through V8
# all share the LP-authorization bug (the pool never bound its LP action coin to
# its own LP CAT, so reserves could be withdrawn without burning any LP -- see
# pool_singleton_v9). A pool's puzzle is fixed at creation, so anything minted
# below V9 is permanently exploitable.
DEFAULT_VERSION = V10
# Mirrors MAX_WEIGHT_UNITS / MAX_TOTAL_WEIGHT in pool_singleton_v7.rue. Reserves
# are raised to their weight, so the caps bound the intermediate's size.
MAX_WEIGHT_UNITS = 8
MAX_TOTAL_WEIGHT = 20
# Matches MAX_ASSETS in the pool puzzles. Every N-asset check recurses over the
# reserve list, so the ceiling is a cost bound rather than a design limit;
# measured cost at ten assets is well under one percent of a block.
MAX_ASSETS = 10
# Liquidity fee ceilings, per revision, mirroring MAX_FEE_BPS in each puzzle.
# V8 tightened this from 1000 to 200; the older puzzles are deployed and keep
# their own bound, so this cannot be a single number.
MAX_FEE_BPS = {V6: 1000, V7: 1000, V8: 200, V9: 200, V10: 200}
# MAX_PROTOCOL_FEE_BPS in pool_singleton_v8.
MAX_PROTOCOL_FEE_BPS = 100
# Mirrors forge_offer.MINIMUM_LOCKED_LP: a sliver of the genesis mint is sent
# to an unspendable puzzle hash so pool LP supply always exceeds what holders
# own. Without it the first LP holder owns 100% of supply and can never exit —
# the router caps every burn at total_lp - 1 to keep the singleton alive.
MINIMUM_LOCKED_LP = 1


WEIGHT_SCALE = 10000


def default_weights(count: int) -> list[int]:
    """Equal weights, with the indivisible remainder on the first asset."""
    base = WEIGHT_SCALE // count
    weights = [base] * count
    weights[0] += WEIGHT_SCALE - base * count
    return weights


def program(name: str) -> Program:
    return Program.from_bytes(bytes.fromhex(
        forge_puzzles.hex_path(name).read_text(encoding="ascii").strip()))


def coin_id(parent: bytes, puzzle_hash: bytes, amount: int) -> bytes:
    return hashlib.sha256(parent + puzzle_hash + (amount.to_bytes((amount.bit_length() + 7) // 8, "big") if amount else b"\x00")).digest()


def native_id(value: object) -> bool:
    return str(value).lower().removeprefix("0x") in ("", "xch", "txch", ZERO32.hex())


def settlement_coins(offer: Offer, asset_ids: list[bytes]) -> dict[bytes | None, tuple[Coin, object]]:
    targets: dict[bytes, bytes | None] = {bytes(OFFER_MOD_HASH): None}
    for asset_id in asset_ids:
        if asset_id != ZERO32:
            targets[bytes(construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash())] = asset_id
    found: dict[bytes | None, tuple[Coin, object]] = {}
    for spend in offer.to_spend_bundle().coin_spends:
        if spend.coin.parent_coin_info == ZERO32:
            continue
        try:
            conditions = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, 11_000_000_000)
        except Exception:
            continue
        for condition in conditions.get(ConditionOpcode.CREATE_COIN, []):
            puzzle_hash = bytes(condition.vars[0])
            if puzzle_hash not in targets:
                continue
            asset_id = targets[puzzle_hash]
            if asset_id in found:
                raise ValueError(f"offer creates multiple settlements for {asset_id}")
            amount = int.from_bytes(condition.vars[1], "big") if isinstance(condition.vars[1], bytes) else int(condition.vars[1])
            if amount <= 0:
                raise ValueError("settlement amount must be positive")
            found[asset_id] = (Coin(spend.coin.name(), puzzle_hash, amount), spend)
    return found


def requested_groups(offer: Offer, asset_id: bytes | None) -> list[Program]:
    groups: dict[bytes, list[list[object]]] = {}
    for payment in offer.get_requested_payments().get(asset_id, []):
        groups.setdefault(bytes(payment.nonce), []).append([bytes(payment.puzzle_hash), int(payment.amount), [bytes(m) for m in payment.memos]])
    return [Program.to((nonce, payments)) for nonce, payments in groups.items()]


# The join rule names the maths family, not just the version: V6 is the
# unweighted N-asset invariant, V7 raises each reserve to its weight. Every
# consumer that branches on quoting behavior reads this string.
# V7 and V8 share a join rule because the mint maths is identical; the protocol
# fee only changes what a swap pays out, and protocol_version distinguishes them.
JOIN_RULES = {V6: "geometric-invariant-v3", V7: "geometric-invariant-v4", V8: "geometric-invariant-v4", V9: "geometric-invariant-v4", V10: "geometric-invariant-v4"}


def assert_config_spendable(config: list, version: int) -> None:
    """Mirror the puzzle's validate_config before a pool is minted.

    A config the puzzle rejects does not fail loudly at creation -- it mints a
    singleton that can never be spent. From V10 that is unrecoverable rather than
    merely awkward: a reserve will only move on an announcement from its pool,
    and a pool whose validate_config fails can never produce one, so the deposit
    is gone.

    The individual checks above already cover most of this. What they missed is
    that an absent protocolPuzzleHash becomes ZERO32, which is thirty-two bytes
    long and so passed a length test while the puzzle asserts it is non-zero.
    Restating the puzzle's rules in one place is the durable fix: any future
    divergence shows up here rather than on chain.
    """
    version_field, pool_mod_hash, asset_ids, weights, fee_bps = config[0:5]
    if int(version_field) != version:
        raise ValueError("config protocol version does not match the puzzle being minted")
    protocol_fee_bps = int(config[5]) if version >= V8 else 0
    protocol_puzzle_hash = bytes(config[6]) if version >= V8 else ZERO32
    lp_tail_hash = bytes(config[7] if version >= V8 else config[5])
    reserve_hash = bytes(config[8] if version >= V8 else config[6])

    for label, value in (("pool module", bytes(pool_mod_hash)),
                         ("LP tail", lp_tail_hash),
                         ("reserve puzzle", reserve_hash)):
        if len(value) != 32 or value == ZERO32:
            raise ValueError(f"{label} hash must be a non-zero 32-byte hash")
    if not 0 <= int(fee_bps) <= MAX_FEE_BPS.get(version, 1000):
        raise ValueError("liquidity fee outside the puzzle's bounds")
    if not 0 <= protocol_fee_bps <= MAX_PROTOCOL_FEE_BPS:
        raise ValueError("protocol fee outside the puzzle's bounds")
    # The one the length check let through: a fee with nowhere to go would be
    # burned rather than collected, so the puzzle refuses to run at all.
    if protocol_fee_bps > 0 and (len(protocol_puzzle_hash) != 32
                                 or protocol_puzzle_hash == ZERO32):
        raise ValueError(
            "a protocol fee needs a non-zero protocolPuzzleHash; minting this pool "
            "would strand its deposit in a singleton that can never be spent")

    count = len(asset_ids)
    units = [int(w) for w in weights]
    if count < MIN_ASSETS.get(version, 2) or count > MAX_ASSETS:
        raise ValueError("asset count outside the puzzle's bounds")
    if len(units) != count:
        raise ValueError("one weight per asset is required")
    if any(bytes(asset_ids[i]) >= bytes(asset_ids[i + 1]) for i in range(count - 1)):
        raise ValueError("assets must be unique and canonically sorted ascending")
    if version >= V7:
        if any(w < 1 or w > MAX_WEIGHT_UNITS for w in units):
            raise ValueError("weights are integer units within the puzzle's cap")
        if sum(units) < count or sum(units) > MAX_TOTAL_WEIGHT:
            raise ValueError("weights must sum within the puzzle's cap")


def pool_snapshot(version: int, pool_coin: Coin, pool_inner: Program, launcher_id: bytes, lineage_parent_name: bytes, config: list[object], state: list[object], reserves: list[Coin], reserve_lineages: list[LineageProof], reserve_inner: Program, lp_asset_id: bytes, tail: Program) -> dict:
    return {
        # Must be the version actually built, not a constant. Hardcoding V6 here
        # made a V7 pool report itself as V6, so the responder dispatched V6
        # puzzles against a V7 singleton and every transition failed.
        "protocol_version": version,
        "join_rule": JOIN_RULES[version],
        # Present from V8 on; older pools report zero so consumers can read the
        # field unconditionally.
        "protocol_fee_bps": int(config[5]) if int(config[0]) >= 8 else 0,
        "protocol_puzzle_hash": bytes(config[6]).hex() if int(config[0]) >= 8 else None,
        "pool_module_hash": bytes(config[1]).hex(),
        "reserve_inner_puzzle_hash": reserve_inner.get_tree_hash().hex(),
        "launcher_id": launcher_id.hex(),
        "lp_asset_id": lp_asset_id.hex(),
        "pool_coin_id": pool_coin.name().hex(),
        "pool_coin": {"parent_coin_info": pool_coin.parent_coin_info.hex(), "puzzle_hash": pool_coin.puzzle_hash.hex(), "amount": int(pool_coin.amount)},
        "pool_lineage_parent_name": lineage_parent_name.hex(),
        "parent_inner_puzzle_hash": None,
        "asset_ids": [bytes(asset).hex() for asset in config[2]],
        "weights": [int(weight) for weight in config[3]],
        "fee_bps": int(config[4]),
        "total_lp": int(state[1]),
        "reserves": [{"asset_id": bytes(item[0]).hex(), "coin": {"parent_coin_info": coin.parent_coin_info.hex(), "puzzle_hash": coin.puzzle_hash.hex(), "amount": int(coin.amount)}, "lineage_proof": {"parent_name": lineage.parent_name.hex() if lineage.parent_name is not None else ZERO32.hex(), "inner_puzzle_hash": lineage.inner_puzzle_hash.hex() if lineage.inner_puzzle_hash is not None else None, "amount": int(lineage.amount or 0)}} for item, coin, lineage in zip(state[0], reserves, reserve_lineages)],
        "reserve_puzzle_kinds": ["native" if bytes(item[0]) == ZERO32 else "cat" for item in state[0]],
        "lp_tail_hash": tail.get_tree_hash().hex(),
    }


def deploy(payload: dict) -> dict:
    execution = payload.get("execution") or {}
    raw_assets = list(execution.get("assetIds") or [])
    asset_ids = [ZERO32 if native_id(asset) else bytes.fromhex(str(asset).removeprefix("0x")) for asset in raw_assets]
    asset_count = len(asset_ids)
    # New pools are created at V10 and nothing else. A pool's puzzle is fixed at
    # creation, so minting an older revision permanently inherits whatever that
    # revision got wrong -- and V4 through V9 each carry a critical
    # authorization bug (docs/FORGE_SECURITY_AUDIT.md findings 1 and 2). The
    # regression suites still build older pools to prove the older maths, which
    # is what `allowUnsafeLegacyVersion` below is for; it is unreachable through
    # the HTTP relay.
    version = int(execution.get("protocolVersion") or DEFAULT_VERSION)
    if version not in (V7, V8, V9, V10):
        raise ValueError(
            f"new pools are created at V{V7}..V{V10}; V{version} pools remain fully "
            "tradeable but can no longer be minted")
    # V4..V8 share the LP-authorization bug: the pool never bound its LP action
    # coin to its own LP CAT, so reserves could be withdrawn without burning any
    # LP. A pool's puzzle is fixed at creation, so minting one of those revisions
    # creates a permanently exploitable pool. The regression suites still need to
    # build legacy pools to prove the older maths, so they opt in explicitly --
    # the HTTP relay maps fields by name and drops anything it does not know, so
    # this flag cannot be set through the public API.
    if version < V10 and not execution.get("allowUnsafeLegacyVersion"):
        raise ValueError(
            f"V{version} pools carry a critical LP-authorization bug and can no longer be "
            f"minted; create at V{V10} instead")
    floor = MIN_ASSETS[version]
    if asset_count < floor or asset_count > MAX_ASSETS:
        raise ValueError(
            f"V{version} pools take between {floor} and {MAX_ASSETS} assets, got {asset_count}")
    if any(asset_ids[i] >= asset_ids[i + 1] for i in range(asset_count - 1)):
        raise ValueError(f"V{version} assets must be unique and canonically sorted ascending")

    # The two versions express weights differently. V6 stores basis points that
    # must be equal to within 1 bps; V7 stores small integer units, so an uneven
    # split like 80/20 is [4, 1] and equal weights are all ones. Percentages
    # shown to a user are weight / sum(weights).
    raw_weights = execution.get("weights")
    if version >= V7:
        weights = [int(value) for value in raw_weights or [1] * asset_count]
        if len(weights) != asset_count:
            raise ValueError(f"V{version} creation requires one weight per asset")
        if any(w < 1 or w > MAX_WEIGHT_UNITS for w in weights):
            raise ValueError(
                f"V{version} weights are integer units between 1 and {MAX_WEIGHT_UNITS}")
        if sum(weights) > MAX_TOTAL_WEIGHT:
            raise ValueError(
                f"V{version} weights must sum to at most {MAX_TOTAL_WEIGHT}, got {sum(weights)}")
    else:
        base = WEIGHT_SCALE // asset_count
        weights = [int(value) for value in raw_weights or default_weights(asset_count)]
        if len(weights) != asset_count:
            raise ValueError(f"V{version} creation requires one weight per asset")
        if sum(weights) != WEIGHT_SCALE or any(w not in (base, base + 1) for w in weights):
            raise ValueError(f"V6 weights must sum to {WEIGHT_SCALE} and be equal to within 1 bps")

    bootstrap = [int(value) for value in execution.get("bootstrapAmounts") or []]
    if len(bootstrap) != asset_count or any(value <= 0 for value in bootstrap):
        raise ValueError(f"V{version} creation requires one positive bootstrap amount per asset")
    # `or 30` would turn a deliberate zero into the default, and a pool's fee is
    # fixed at creation -- so the creator would get a 30 bps pool while believing
    # they had made a free one, with no way to change it. Absent means default;
    # present and zero means zero.
    raw_fee_bps = execution.get("swapFeeBps")
    fee_bps = 30 if raw_fee_bps is None or raw_fee_bps == "" else int(raw_fee_bps)
    # Check here as well as in the puzzle. Without this the builder happily mints
    # a pool whose config validate_config rejects, so the singleton exists and
    # every spend against it fails -- the funds are recoverable but the pool is
    # inert, which is a far worse outcome than refusing up front.
    fee_ceiling = MAX_FEE_BPS.get(version, 1000)
    if not 0 <= fee_bps <= fee_ceiling:
        raise ValueError(
            f"V{version} liquidity fee must be between 0 and {fee_ceiling} bps, got {fee_bps}")
    offer = Offer.from_bech32(payload["offer"])
    parsed = find_offer_settlements(offer, asset_ids)
    found = {asset_id: (settlement.coin, settlement.creator) for asset_id, settlement in parsed.items()}
    if ZERO32 in asset_ids and None in found:
        found[ZERO32] = found[None]
    required_keys = (None, *(None if asset == ZERO32 else asset for asset in asset_ids))
    missing = [asset for asset in required_keys if asset not in found]
    if missing:
        raise ValueError(f"V6 creation offer is missing settlements: {[None if asset is None else bytes(asset).hex() for asset in missing]}")
    requested = offer.get_requested_payments()
    xch, _ = found[None]
    # The opening LP supply per unit of the scarcest reserve. One is the historic
    # behavior and stays the default; a higher ratio simply denominates the same
    # ownership in more LP mojos, which is what a vault wants when its LP is meant
    # to be split into shares.
    #
    # Nothing on chain constrains this. The puzzle asserts `total_lp > 0` and its
    # mint bracket is homogeneous in total_lp -- scale the supply by c and c**K
    # cancels off both sides -- so every ratio is equally valid and, once set, is
    # preserved by every later mint and burn. It is therefore fixed here forever:
    # the pool has no ratio field to correct later, only the state it opens with.
    #
    # The cost is XCH. Every CAT mojo is an XCH mojo, so `xch_required` below
    # carries `initial_lp` mojos of backing and the ratio multiplies it.
    # Not `or 1`: that folds a declared 0 into the default and launches a pool at
    # 1x that the caller asked to open at 0, silently and permanently. Absent is
    # the only thing that may mean "default".
    raw_lp_ratio = execution.get("lpRatio")
    lp_ratio = 1 if raw_lp_ratio is None or raw_lp_ratio == "" else int(raw_lp_ratio)
    if lp_ratio < 1:
        raise ValueError(f"lpRatio must be a positive integer (got {lp_ratio})")
    # A vault only. One reserve and no curve means the LP IS the exchange rate --
    # burn a claim, receive `1/ratio` of the one thing the pool holds, and the peg
    # is a statement anyone can check. On a basket there is no such statement to
    # make: LP is a share of several reserves priced against each other, so a
    # ratio there is a denominator with no referent, and one set by accident would
    # be silently permanent. Refused rather than ignored, because ignoring it
    # would mint at 1x a pool whose offer was already backed at the asked-for
    # ratio -- and that mismatch is the one thing this must never do quietly.
    if lp_ratio != 1 and asset_count != 1:
        raise ValueError(
            f"lpRatio is a single-asset vault setting; this pool has {asset_count} "
            "reserves, where LP is a share of the basket rather than a claim on one asset")
    initial_lp = lp_ratio * min(bootstrap)
    if initial_lp <= MINIMUM_LOCKED_LP:
        raise ValueError(f"V{version} creation requires initial_lp to exceed the locked minimum liquidity")
    lp_recipient = bytes.fromhex(str(execution.get("lpRecipientPuzzleHash") or "").removeprefix("0x"))
    if len(lp_recipient) != 32:
        raise ValueError(f"V{version} creation requires lpRecipientPuzzleHash")
    # A burn is checked here as well as in the browser and the relay, because
    # this is the only layer that actually emits the CREATE_COIN. ZERO32 is the
    # burn destination on every network -- what changes between mainnet and a
    # testnet is the bech32m address rendering, never these bytes.
    if execution.get("burnInitialLp") and lp_recipient != ZERO32:
        raise ValueError(
            f"V{version} creation requested burnInitialLp but lpRecipientPuzzleHash is "
            f"{lp_recipient.hex()}, not the burn address {ZERO32.hex()}")
    platform_fee_mojos = int(execution.get("platformFeeMojos") or 0)
    platform_fee_puzzle_hash = bytes.fromhex(str(execution.get("platformFeePuzzleHash") or "").removeprefix("0x"))
    if platform_fee_mojos > 0 and len(platform_fee_puzzle_hash) != 32:
        raise ValueError(f"V{version} creation requires platformFeePuzzleHash when platformFeeMojos is set")

    launcher_coin = Coin(xch.name(), SINGLETON_LAUNCHER_HASH, 1)
    launcher_id = launcher_coin.name()
    singleton = [SINGLETON_TOP_LAYER_V1_1_HASH, launcher_id, SINGLETON_LAUNCHER_HASH]
    # From V10 the reserve is curried with the launcher, so a reserve's puzzle
    # hash commits to the pool that owns it. config.reserve_inner_puzzle_hash
    # below therefore becomes pool-specific, which is exactly what the pool needs
    # to derive its own successors -- no other creation logic changes.
    reserve_inner = program(f"forge_reserve_v{version}")
    if version >= V10:
        reserve_inner = reserve_inner.curry(launcher_id)
    pool_mod = program(f"pool_singleton_v{version}")
    tail = program(f"forge_lp_cat_tail_v{version}").curry(launcher_id, version)
    lp_asset_id = tail.get_tree_hash()
    # V8 carries the protocol fee ahead of the tail hash. The pool puzzle reads
    # its config positionally, so this order has to match the struct exactly.
    protocol_fee_bps = int(execution.get("protocolFeeBps") or 0)
    if not 0 <= protocol_fee_bps <= MAX_PROTOCOL_FEE_BPS:
        raise ValueError(
            f"protocol fee must be between 0 and {MAX_PROTOCOL_FEE_BPS} bps, got {protocol_fee_bps}")
    raw_recipient = str(execution.get("protocolPuzzleHash") or "").removeprefix("0x")
    protocol_puzzle_hash = bytes.fromhex(raw_recipient) if raw_recipient else ZERO32
    if version >= V8:
        if protocol_fee_bps > 0 and len(protocol_puzzle_hash) != 32:
            raise ValueError(f"V{version} creation requires protocolPuzzleHash when protocolFeeBps is set")
        config = [version, pool_mod.get_tree_hash(), asset_ids, weights, fee_bps,
                  protocol_fee_bps, protocol_puzzle_hash,
                  lp_asset_id, reserve_inner.get_tree_hash()]
    elif protocol_fee_bps > 0:
        raise ValueError(f"V{version} pools have no protocol fee; mint a V{V8} pool for one")
    else:
        config = [version, pool_mod.get_tree_hash(), asset_ids, weights, fee_bps,
                  lp_asset_id, reserve_inner.get_tree_hash()]
    assert_config_spendable(config, version)
    state = [[[asset_ids[i], b"", bootstrap[i]] for i in range(asset_count)], initial_lp]
    reserves: list[Coin] = []
    reserve_lineages: list[LineageProof] = []
    for i, asset_id in enumerate(asset_ids):
        settlement, creator = found[asset_id]
        reserve_hash = reserve_inner.get_tree_hash() if asset_id == ZERO32 else construct_cat_puzzle(CAT_MOD, asset_id, reserve_inner).get_tree_hash()
        reserves.append(Coin(settlement.name(), reserve_hash, bootstrap[i]))
        reserve_lineages.append(
            LineageProof()
            if asset_id == ZERO32
            else LineageProof(
                settlement.parent_coin_info,
                OFFER_MOD_HASH,
                settlement.amount,
            )
        )
        state[0][i][1] = reserves[-1].name()
    pool_inner = pool_mod.curry(singleton, config, state)
    pool_puzzle = puzzle_for_singleton(launcher_id, pool_inner)
    pool_coin = Coin(launcher_id, pool_puzzle.get_tree_hash(), 1)

    identity = Program.to(1)
    lp_eve_outer = construct_cat_puzzle(CAT_MOD, lp_asset_id, identity)
    lp_eve = Coin(xch.name(), lp_eve_outer.get_tree_hash(), 1)
    guard_coin = Coin(xch.name(), identity.get_tree_hash(), 1)
    lp_action = [guard_coin.name(), lp_eve.name(), initial_lp, initial_lp, Program.to(state).get_tree_hash()]
    lp_message = Program.to(["forge-lp-action-v3", launcher_id, *lp_action]).get_tree_hash()
    xch_required = 2 + initial_lp + platform_fee_mojos + sum(int(payment.amount) for payment in requested.get(None, [])) + sum(bootstrap[i] for i, asset_id in enumerate(asset_ids) if asset_id == ZERO32)
    if int(xch.amount) != xch_required:
        raise ValueError(f"V6 XCH settlement must equal {xch_required}, got {xch.amount}")
    xch_groups = requested_groups(offer, None)
    xch_groups.append(Program.to((xch.name(), [[SINGLETON_LAUNCHER_HASH, 1], [identity.get_tree_hash(), 1], [lp_eve_outer.get_tree_hash(), 1], *([[reserve_inner.get_tree_hash(), bootstrap[asset_ids.index(ZERO32)]]] if ZERO32 in asset_ids else []), *([[platform_fee_puzzle_hash, platform_fee_mojos]] if platform_fee_mojos > 0 else [])])))
    xch_spend = make_spend(xch, OFFER_MOD, Program.to(xch_groups))
    launcher_spend = make_spend(launcher_coin, SINGLETON_LAUNCHER, Program.to([pool_puzzle.get_tree_hash(), 1, []]))
    guard_spend = make_spend(guard_coin, identity, Program.to([[ConditionOpcode.CREATE_COIN_ANNOUNCEMENT, lp_message]]))

    cat_spends: list = []
    for i, asset_id in enumerate(asset_ids):
        if asset_id == ZERO32:
            continue
        settlement, creator = found[asset_id]
        creator_puzzle = Program.from_bytes(bytes(creator.puzzle_reveal))
        lineage = LineageProof(creator.coin.parent_coin_info, get_innerpuzzle_from_puzzle(creator_puzzle).get_tree_hash(), creator.coin.amount)
        groups = requested_groups(offer, asset_id)
        groups.append(Program.to((settlement.name(), [[reserve_inner.get_tree_hash(), bootstrap[i]]])))
        spendable = SpendableCAT(settlement, asset_id, OFFER_MOD, Program.to(groups), lineage_proof=lineage)
        cat_spends.extend(unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends)

    recipient_payments = requested.get(lp_asset_id, [])
    lp_destination = recipient_payments[0].puzzle_hash if recipient_payments else lp_recipient
    user_initial_lp = initial_lp - MINIMUM_LOCKED_LP
    if execution.get("burnInitialLp"):
        # lp_destination, not lp_recipient: a requested LP payment in the offer
        # overrides the recipient above, and that override must not be a way to
        # redirect a burn.
        if lp_destination != ZERO32:
            raise ValueError(
                f"V{version} creation requested burnInitialLp but the LP destination resolved to "
                f"{bytes(lp_destination).hex()}, not the burn address {ZERO32.hex()}")
        # Both the seed and the locked minimum land on ZERO32, so equal amounts
        # would mint two coins with the same parent, puzzle hash and value --
        # one duplicate coin id, rejected by the chain. Only reachable at
        # initial_lp == 2, but it fails as an opaque DUPLICATE_OUTPUT if not
        # named here.
        if user_initial_lp == MINIMUM_LOCKED_LP:
            raise ValueError(
                f"V{version} creation cannot burn an initial_lp of {initial_lp}: the burned seed and the "
                f"locked minimum would be the same coin. Bootstrap with more liquidity.")
    if recipient_payments and sum(int(payment.amount) for payment in recipient_payments) != user_initial_lp:
        raise ValueError("V6 creation Offer must request initial_lp minus the locked minimum liquidity")
    lp_conditions = Program.to([[ConditionOpcode.CREATE_COIN, lp_destination, user_initial_lp, [lp_destination]], [ConditionOpcode.CREATE_COIN, ZERO32, MINIMUM_LOCKED_LP], [ConditionOpcode.CREATE_COIN, 0, -113, tail, Program.to(lp_action)]])
    lp_spend = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [SpendableCAT(lp_eve, lp_asset_id, identity, lp_conditions, lineage_proof=LineageProof(), extra_delta=initial_lp - 1, limitations_program_reveal=tail, limitations_solution=Program.to(lp_action))])
    user_spends = [spend for spend in offer.to_spend_bundle().coin_spends if spend.coin.parent_coin_info != ZERO32]
    bundle = SpendBundle(user_spends + [xch_spend, launcher_spend, guard_spend, *cat_spends, *lp_spend.coin_spends], offer.to_spend_bundle().aggregated_signature)
    result = {"success": True, "protocol_version": version, "launcher_coin_id": launcher_id.hex(), "launcher_parent_coin_info": xch.name().hex(), "lp_cat_asset_id": lp_asset_id.hex(), "lp_out": initial_lp, "target_coin_id": pool_coin.name().hex(), "target_puzzle_hash": pool_coin.puzzle_hash.hex(), "current_coin_id": pool_coin.name().hex(), "current_puzzle_hash": pool_coin.puzzle_hash.hex(), "current_puzzle_reveal": bytes(pool_puzzle.as_bin()).hex(), "reserve_coin_ids": [coin.name().hex() for coin in reserves], "poolSnapshot": pool_snapshot(version, pool_coin, pool_inner, launcher_id, xch.name(), config, state, reserves, reserve_lineages, reserve_inner, lp_asset_id, tail)}
    if payload.get("dry_run"):
        result["dry_run"] = True
        result["bundle"] = bundle.to_json_dict()
        return result
    node_url = payload.get("node_url", "https://testnet11.api.coinset.org").rstrip("/")
    request = urllib.request.Request(node_url + "/push_tx", data=json.dumps({"spend_bundle": bundle.to_json_dict()}).encode(), headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (aWizard-Forge/1.0)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        pushed = json.loads(response.read())
    if pushed.get("status") != "SUCCESS" and "ALREADY_INCLUDING_TRANSACTION" not in str(pushed.get("error", "")):
        raise RuntimeError(f"V6 push_tx rejected: {pushed}")
    result["transaction_id"] = bundle.name().hex()
    result["push_status"] = pushed.get("status")
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(deploy(json.loads(sys.stdin.read()))))
    except Exception as error:
        print(json.dumps({"success": False, "error": f"[aWizard] {error}"}))
        raise