"""Keyless, offer-driven Forge V3 spend builders.

The caller supplies a complete Chia Offer whose aggregate signature already
authorizes every signed condition, including the launch guard. This module
never holds a key and never modifies or augments that signature.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend, make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD,
    LineageProof,
    SpendableCAT,
    construct_cat_puzzle,
    get_innerpuzzle_from_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.puzzles.singleton_top_layer_v1_1 import (
    SINGLETON_LAUNCHER,
    SINGLETON_LAUNCHER_HASH,
    SINGLETON_TOP_LAYER_V1_1_HASH,
    puzzle_for_singleton,
    solution_for_singleton,
)
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH, Offer
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import AugSchemeMPL, G1Element, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_puzzles
from forge_math import swap_output

PROTOCOL_VERSION = 4
LEGACY_PROTOCOL_VERSION = 3
MINIMUM_LOCKED_LP = 1
MODE_SWAP = 0
MODE_ADD = 1
MODE_REMOVE = 2
ZERO_32 = bytes32.zeros
CONTRACTS_DIR = Path(__file__).resolve().parent


def compiled_program(name: str) -> Program:
    # forge_puzzles owns both the FORGE alias and the development/ archive, so a
    # request for the current revision loads the shipping puzzle either way.
    path = forge_puzzles.hex_path(name)
    return Program.from_bytes(bytes.fromhex(path.read_text(encoding="ascii").strip()))


def tree_hash(value: object) -> bytes32:
    return Program.to(value).get_tree_hash()


# From V10 the reserve puzzle is curried with its pool's launcher id, so a
# reserve's puzzle hash commits to the pool that owns it, and the launcher moves
# out of the solution. V10 also needs the pool's inner puzzle hash, to rebuild
# the singleton puzzle hash it asserts the authorizing announcement against.
# Both shapes live here so no builder has to remember which era it is in.
RESERVE_BINDING_VERSION = 10


def reserve_inner_puzzle(version: int, launcher_id: bytes32) -> Program:
    """The reserve inner puzzle for a pool, curried from V10 on."""
    program = compiled_program(f"forge_reserve_v{version}")
    return program.curry(launcher_id) if version >= RESERVE_BINDING_VERSION else program


def reserve_solution(
    version: int,
    launcher_id: bytes32,
    asset_id: bytes32,
    reserve_inner_hash: bytes32,
    pool_inner_hash: bytes32,
    plan: Sequence[object],
) -> Program:
    """Solution for a reserve spend, in the shape that version expects."""
    if version >= RESERVE_BINDING_VERSION:
        return Program.to([asset_id, reserve_inner_hash, pool_inner_hash, list(plan)])
    return Program.to([launcher_id, asset_id, reserve_inner_hash, list(plan)])


# Builders import this under a distinct name to avoid shadowing their own
# local `reserve_solution` variables.
build_reserve_solution = reserve_solution


@dataclass(frozen=True)
class LaunchIntent:
    xch_settlement_coin_id: bytes32
    cat_settlement_coin_ids: tuple[bytes32, bytes32]
    asset_ids: tuple[bytes32, bytes32]
    weights: tuple[int, int]
    bootstrap_amounts: tuple[int, int]
    fee_bps: int
    lp_recipient: bytes32
    min_initial_lp: int
    max_initial_lp: int
    expiry_height: int
    salt: bytes32

    def as_program(self) -> list[object]:
        return [
            PROTOCOL_VERSION,
            self.xch_settlement_coin_id,
            list(self.cat_settlement_coin_ids),
            list(self.asset_ids),
            list(self.weights),
            list(self.bootstrap_amounts),
            self.fee_bps,
            self.lp_recipient,
            self.min_initial_lp,
            self.max_initial_lp,
            self.expiry_height,
            self.salt,
        ]

    @property
    def commitment(self) -> bytes32:
        return tree_hash(self.as_program())

    def validate(self, current_height: int) -> None:
        if self.xch_settlement_coin_id == ZERO_32:
            raise ValueError("XCH settlement coin ID cannot be zero")
        if ZERO_32 in self.cat_settlement_coin_ids or len(set(self.cat_settlement_coin_ids)) != 2:
            raise ValueError("CAT settlement coin IDs must be nonzero and unique")
        if self.asset_ids[0] >= self.asset_ids[1]:
            raise ValueError("launch assets must be unique and canonically sorted")
        if self.weights != (5_000, 5_000):
            raise ValueError("V3 currently supports exactly 50/50 weights")
        if any(amount <= 0 for amount in self.bootstrap_amounts):
            raise ValueError("bootstrap amounts must be positive")
        if not 0 <= self.fee_bps <= 1_000:
            raise ValueError("fee_bps outside V3 bounds")
        if self.lp_recipient == ZERO_32:
            raise ValueError("LP recipient cannot be zero")
        if self.min_initial_lp <= 0 or self.max_initial_lp < self.min_initial_lp:
            raise ValueError("invalid initial LP bounds")
        if current_height > self.expiry_height:
            raise ValueError("launch intent has expired")


@dataclass(frozen=True)
class LaunchConfig:
    asset_ids: tuple[bytes32, bytes32]
    weights: tuple[int, int]
    bootstrap_amounts: tuple[int, int]
    fee_bps: int
    lp_recipient: bytes32
    initial_lp: int
    expiry_height: int
    salt: bytes32


@dataclass(frozen=True)
class CreatePreparation:
    offer: Offer
    intent: LaunchIntent
    signer_public_key: G1Element
    guard_coin: Coin
    guard_spend: CoinSpend
    launcher_id: bytes32
    lp_asset_id: bytes32
    initial_lp: int


@dataclass(frozen=True)
class EphemeralSettlement:
    asset_id: bytes32 | None
    coin: Coin
    creator: CoinSpend
    lineage_proof: LineageProof | None


@dataclass(frozen=True)
class PoolCoin:
    coin: Coin
    inner_puzzle: Program
    lineage_parent_name: bytes32
    parent_inner_puzzle_hash: bytes32 | None
    launcher_id: bytes32

    @property
    def puzzle(self) -> Program:
        return puzzle_for_singleton(self.launcher_id, self.inner_puzzle)


@dataclass(frozen=True)
class ReserveCoin:
    asset_id: bytes32
    coin: Coin
    inner_puzzle: Program
    lineage_proof: LineageProof


@dataclass(frozen=True)
class V3Pool:
    launcher_id: bytes32
    singleton: list[bytes32]
    config: list[object]
    state: list[object]
    pool: PoolCoin
    reserves: Mapping[bytes32, ReserveCoin]
    lp_asset_id: bytes32
    lp_tail: Program


@dataclass(frozen=True)
class CreateResult:
    bundle: WalletSpendBundle
    pool: V3Pool
    guard_coin: Coin


@dataclass(frozen=True)
class TransitionResult:
    bundle: WalletSpendBundle
    pool: V3Pool
    lp_output_coin: Coin | None = None
    lp_output_lineage: LineageProof | None = None


def _condition_amount(value: bytes | int) -> int:
    return int.from_bytes(value, "big", signed=True) if isinstance(value, bytes) else int(value)


def _cat_lineage(creator: CoinSpend) -> LineageProof:
    parent_puzzle = Program.from_bytes(bytes(creator.puzzle_reveal))
    return LineageProof(
        creator.coin.parent_coin_info,
        get_innerpuzzle_from_puzzle(parent_puzzle).get_tree_hash(),
        uint64(creator.coin.amount),
    )


def find_offer_settlements(
    offer: Offer,
    asset_ids: Iterable[bytes32],
) -> dict[bytes32 | None, EphemeralSettlement]:
    """Extract actual XCH/CAT OFFER_MOD children created by an Offer bundle."""
    targets: dict[bytes32, bytes32 | None] = {OFFER_MOD_HASH: None}
    for asset_id in asset_ids:
        if asset_id == ZERO_32:
            continue
        targets[construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash()] = asset_id

    found: dict[bytes32 | None, EphemeralSettlement] = {}
    for coin_spend in offer.to_spend_bundle().coin_spends:
        if coin_spend.coin.parent_coin_info == ZERO_32:
            continue
        try:
            conditions = conditions_dict_for_solution(
                coin_spend.puzzle_reveal,
                coin_spend.solution,
                11_000_000_000,
            )
        except Exception:
            continue
        for condition in conditions.get(ConditionOpcode.CREATE_COIN, []):
            puzzle_hash = bytes32(condition.vars[0])
            if puzzle_hash not in targets:
                continue
            asset_id = targets[puzzle_hash]
            if asset_id in found:
                raise ValueError(f"offer creates multiple settlement coins for {asset_id}")
            amount = _condition_amount(condition.vars[1])
            if amount <= 0:
                raise ValueError("offer settlement amount must be positive")
            found[asset_id] = EphemeralSettlement(
                asset_id,
                Coin(coin_spend.coin.name(), puzzle_hash, uint64(amount)),
                coin_spend,
                None if asset_id is None else _cat_lineage(coin_spend),
            )
    return found


def requested_amounts(offer: Offer) -> dict[bytes32 | None, int]:
    return {
        asset_id: sum(int(payment.amount) for payment in payments)
        for asset_id, payments in offer.get_requested_payments().items()
    }


def requested_solution(offer: Offer, asset_id: bytes32 | None) -> list[Program]:
    groups: dict[bytes32, list[list[object]]] = {}
    for payment in offer.get_requested_payments().get(asset_id, []):
        item: list[object] = [payment.puzzle_hash, int(payment.amount)]
        if payment.memos:
            item.append([bytes(memo) for memo in payment.memos])
        groups.setdefault(payment.nonce, []).append(item)
    return [Program.to((nonce, payments)) for nonce, payments in groups.items()]


def singleton_struct(launcher_id: bytes32) -> list[bytes32]:
    return [SINGLETON_TOP_LAYER_V1_1_HASH, launcher_id, SINGLETON_LAUNCHER_HASH]


def aggregate_with_offer(
    offer: Offer,
    extra_spends: Sequence[CoinSpend],
) -> WalletSpendBundle:
    offer_bundle = offer.to_spend_bundle()
    real_offer_spends = [
        spend for spend in offer_bundle.coin_spends if spend.coin.parent_coin_info != ZERO_32
    ]
    return WalletSpendBundle(
        [*real_offer_spends, *extra_spends],
        offer_bundle.aggregated_signature,
    )


def _settlement_group(
    nonce: bytes32,
    payments: Sequence[tuple[bytes32, int]],
) -> Program:
    return Program.to((nonce, [[puzzle_hash, amount] for puzzle_hash, amount in payments]))


def _cat_bundle(spendables: Sequence[SpendableCAT]) -> WalletSpendBundle:
    return unsigned_spend_bundle_for_spendable_cats(CAT_MOD, list(spendables))


def _create_request_asset(
    offer: Offer,
    intent: LaunchIntent,
    lp_asset_id: bytes32,
    initial_lp: int,
) -> bytes32 | None | str:
    requested = requested_amounts(offer)
    if requested == {lp_asset_id: initial_lp - MINIMUM_LOCKED_LP}:
        return "lp"
    if len(requested) == 1:
        receipt_asset, receipt_amount = next(iter(requested.items()))
        if receipt_amount == 1 and (receipt_asset is None or receipt_asset in intent.asset_ids):
            return receipt_asset
    raise ValueError(
        "create Offer must request exact initial LP or a 1-mojo XCH/reserve receipt"
    )


def prepare_create_v3(
    offer: Offer,
    config: LaunchConfig,
    signer_public_key: G1Element,
    current_height: int,
) -> CreatePreparation:
    settlements = find_offer_settlements(offer, config.asset_ids)
    missing = [asset_id for asset_id in (None, *config.asset_ids) if asset_id not in settlements]
    if missing:
        raise ValueError(f"offer is missing settlements for {missing}")
    xch = settlements[None]
    cats = tuple(settlements[asset_id] for asset_id in config.asset_ids)
    intent = LaunchIntent(
        xch.coin.name(),
        (cats[0].coin.name(), cats[1].coin.name()),
        config.asset_ids,
        config.weights,
        config.bootstrap_amounts,
        config.fee_bps,
        config.lp_recipient,
        config.initial_lp,
        config.initial_lp,
        config.expiry_height,
        config.salt,
    )
    intent.validate(current_height)
    if config.initial_lp != min(config.bootstrap_amounts):
        raise ValueError("initial_lp must equal the canonical minimum bootstrap amount")
    if config.initial_lp <= MINIMUM_LOCKED_LP:
        raise ValueError("initial_lp must exceed the locked minimum liquidity")

    launcher_coin = Coin(xch.coin.name(), SINGLETON_LAUNCHER_HASH, uint64(1))
    launcher_id = launcher_coin.name()
    reserve_inner = compiled_program("forge_reserve_v4")
    pool_mod = compiled_program("pool_singleton_v4")
    lp_tail = compiled_program("forge_lp_cat_tail_v4").curry(launcher_id, PROTOCOL_VERSION)
    lp_asset_id = lp_tail.get_tree_hash()
    requested = requested_amounts(offer)
    _create_request_asset(offer, intent, lp_asset_id, config.initial_lp)
    for index, cat in enumerate(cats):
        expected = config.bootstrap_amounts[index] + requested.get(config.asset_ids[index], 0)
        if cat.coin.amount != expected:
            raise ValueError("offered CAT amount does not cover bootstrap plus receipt")
    xch_required = config.initial_lp + 2 + requested.get(None, 0)
    if xch.coin.amount != xch_required:
        raise ValueError(f"create Offer must provide exactly {xch_required} XCH mojos")

    reserve_states: list[list[object]] = []
    for index, cat in enumerate(cats):
        asset_id = config.asset_ids[index]
        reserve_hash = construct_cat_puzzle(CAT_MOD, asset_id, reserve_inner).get_tree_hash()
        reserve_coin = Coin(cat.coin.name(), reserve_hash, uint64(config.bootstrap_amounts[index]))
        reserve_states.append([asset_id, reserve_coin.name(), config.bootstrap_amounts[index]])
    singleton = singleton_struct(launcher_id)
    pool_config: list[object] = [
        PROTOCOL_VERSION,
        pool_mod.get_tree_hash(),
        list(config.asset_ids),
        list(config.weights),
        config.fee_bps,
        lp_asset_id,
        reserve_inner.get_tree_hash(),
    ]
    state: list[object] = [reserve_states, config.initial_lp]
    pool_inner = pool_mod.curry(singleton, pool_config, state)
    pool_puzzle = puzzle_for_singleton(launcher_id, pool_inner)
    guard_puzzle = compiled_program("forge_launch_guard_v4").curry(
        signer_public_key,
        intent.commitment,
    )
    guard_coin = Coin(xch.coin.name(), guard_puzzle.get_tree_hash(), uint64(1))
    identity = Program.to(1)
    lp_eve_outer = construct_cat_puzzle(CAT_MOD, lp_asset_id, identity)
    lp_eve = Coin(xch.coin.name(), lp_eve_outer.get_tree_hash(), uint64(1))
    guard_solution = Program.to([[
        guard_coin.name(),
        pool_puzzle.get_tree_hash(),
        lp_eve.name(),
        config.initial_lp,
        tree_hash(state),
        intent.as_program(),
    ]])
    guard_spend = make_spend(guard_coin, guard_puzzle, guard_solution)
    return CreatePreparation(
        offer,
        intent,
        signer_public_key,
        guard_coin,
        guard_spend,
        launcher_id,
        lp_asset_id,
        config.initial_lp,
    )


def finalize_create_offer(
    preparation: CreatePreparation,
    guard_signature: G2Element,
) -> Offer:
    offer_bundle = preparation.offer.to_spend_bundle()
    real_spends = [
        spend for spend in offer_bundle.coin_spends if spend.coin.parent_coin_info != ZERO_32
    ]
    if any(spend.coin.name() == preparation.guard_coin.name() for spend in real_spends):
        raise ValueError("create Offer already contains the launch guard spend")
    signature = AugSchemeMPL.aggregate([offer_bundle.aggregated_signature, guard_signature])
    return Offer(
        preparation.offer.get_requested_payments(),
        WalletSpendBundle([*real_spends, preparation.guard_spend], signature),
        preparation.offer.driver_dict,
    )


def build_create_v3(
    offer: Offer,
    intent: LaunchIntent,
    signer_public_key: G1Element,
    current_height: int,
) -> CreateResult:
    """Build a standard-launcher V3 pool creation bundle without signing."""
    intent.validate(current_height)
    settlements = find_offer_settlements(offer, intent.asset_ids)
    missing = [asset_id for asset_id in (None, *intent.asset_ids) if asset_id not in settlements]
    if missing:
        raise ValueError(f"offer is missing settlements for {missing}")

    xch = settlements[None]
    cats = tuple(settlements[asset_id] for asset_id in intent.asset_ids)
    if xch.coin.name() != intent.xch_settlement_coin_id:
        raise ValueError("launch intent XCH settlement does not match Offer")
    if tuple(cat.coin.name() for cat in cats) != intent.cat_settlement_coin_ids:
        raise ValueError("launch intent CAT settlements do not match Offer")
    launcher_coin = Coin(xch.coin.name(), SINGLETON_LAUNCHER_HASH, uint64(1))
    launcher_id = launcher_coin.name()
    reserve_inner = compiled_program("forge_reserve_v4")
    pool_mod = compiled_program("pool_singleton_v4")
    lp_tail = compiled_program("forge_lp_cat_tail_v4").curry(launcher_id, PROTOCOL_VERSION)
    lp_asset_id = lp_tail.get_tree_hash()
    initial_lp = min(intent.bootstrap_amounts)
    if initial_lp <= MINIMUM_LOCKED_LP:
        raise ValueError("initial LP must exceed the locked minimum liquidity")
    if not intent.min_initial_lp <= initial_lp <= intent.max_initial_lp:
        raise ValueError("computed initial LP is outside signed intent bounds")

    requested = requested_amounts(offer)
    request_asset = _create_request_asset(offer, intent, lp_asset_id, initial_lp)
    for index, settlement in enumerate(cats):
        expected_amount = intent.bootstrap_amounts[index] + requested.get(intent.asset_ids[index], 0)
        if settlement.coin.amount != expected_amount:
            raise ValueError("offered CAT amount does not cover bootstrap plus receipt")

    reserves: dict[bytes32, ReserveCoin] = {}
    reserve_bundles: list[WalletSpendBundle] = []
    reserve_states: list[list[object]] = []
    for index, settlement in enumerate(cats):
        asset_id = intent.asset_ids[index]
        amount = intent.bootstrap_amounts[index]
        assert settlement.lineage_proof is not None
        settlement_groups = [
            _settlement_group(settlement.coin.name(), [(reserve_inner.get_tree_hash(), amount)])
        ]
        if request_asset == asset_id:
            settlement_groups.extend(requested_solution(offer, asset_id))
        reserve_spendable = SpendableCAT(
            settlement.coin,
            asset_id,
            OFFER_MOD,
            Program.to(settlement_groups),
            lineage_proof=settlement.lineage_proof,
        )
        reserve_bundles.append(_cat_bundle([reserve_spendable]))
        reserve_outer_hash = construct_cat_puzzle(CAT_MOD, asset_id, reserve_inner).get_tree_hash()
        reserve_coin = Coin(settlement.coin.name(), reserve_outer_hash, uint64(amount))
        reserve_states.append([asset_id, reserve_coin.name(), amount])
        reserves[asset_id] = ReserveCoin(
            asset_id,
            reserve_coin,
            reserve_inner,
            LineageProof(
                settlement.coin.parent_coin_info,
                OFFER_MOD_HASH,
                uint64(settlement.coin.amount),
            ),
        )

    singleton = singleton_struct(launcher_id)
    config: list[object] = [
        PROTOCOL_VERSION,
        pool_mod.get_tree_hash(),
        list(intent.asset_ids),
        list(intent.weights),
        intent.fee_bps,
        lp_asset_id,
        reserve_inner.get_tree_hash(),
    ]
    state: list[object] = [reserve_states, initial_lp]
    pool_inner = pool_mod.curry(singleton, config, state)
    pool_puzzle = puzzle_for_singleton(launcher_id, pool_inner)
    pool_coin = Coin(launcher_id, pool_puzzle.get_tree_hash(), uint64(1))
    launcher_spend = make_spend(
        launcher_coin,
        SINGLETON_LAUNCHER,
        Program.to([pool_puzzle.get_tree_hash(), 1, []]),
    )

    guard_puzzle = compiled_program("forge_launch_guard_v4").curry(
        signer_public_key,
        intent.commitment,
    )
    guard_coin = Coin(xch.coin.name(), guard_puzzle.get_tree_hash(), uint64(1))

    identity = Program.to(1)
    lp_eve_outer = construct_cat_puzzle(CAT_MOD, lp_asset_id, identity)
    lp_eve = Coin(xch.coin.name(), lp_eve_outer.get_tree_hash(), uint64(1))
    xch_required = initial_lp + 2 + requested.get(None, 0)
    if xch.coin.amount != xch_required:
        raise ValueError(f"create Offer must provide exactly {xch_required} XCH mojos")
    xch_groups = [
        _settlement_group(xch.coin.name(), [
            (SINGLETON_LAUNCHER_HASH, 1),
            (guard_puzzle.get_tree_hash(), 1),
            (lp_eve_outer.get_tree_hash(), 1),
        ]),
    ]
    if request_asset is None:
        xch_groups.extend(requested_solution(offer, None))
    xch_solution = Program.to(xch_groups)
    xch_spend = make_spend(xch.coin, OFFER_MOD, xch_solution)

    lp_action = [
        guard_coin.name(),
        lp_eve.name(),
        initial_lp,
        initial_lp,
        tree_hash(state),
    ]
    lp_offer_outer_hash = construct_cat_puzzle(CAT_MOD, lp_asset_id, OFFER_MOD).get_tree_hash()
    lp_destination = OFFER_MOD_HASH if request_asset == "lp" else intent.lp_recipient
    user_initial_lp = initial_lp - MINIMUM_LOCKED_LP
    lp_mint_conditions = Program.to([
        [ConditionOpcode.CREATE_COIN, lp_destination, user_initial_lp],
        [ConditionOpcode.CREATE_COIN, ZERO_32, MINIMUM_LOCKED_LP],
        [ConditionOpcode.CREATE_COIN, 0, -113, lp_tail, lp_action],
    ])
    lp_spendables = [
        SpendableCAT(
            lp_eve,
            lp_asset_id,
            identity,
            lp_mint_conditions,
            lineage_proof=LineageProof(),
            extra_delta=initial_lp - 1,
            limitations_program_reveal=lp_tail,
            limitations_solution=Program.to(lp_action),
        )
    ]
    if request_asset == "lp":
        lp_settlement = Coin(lp_eve.name(), lp_offer_outer_hash, uint64(user_initial_lp))
        lp_spendables.append(SpendableCAT(
            lp_settlement,
            lp_asset_id,
            OFFER_MOD,
            Program.to(requested_solution(offer, lp_asset_id)),
            lineage_proof=LineageProof(lp_eve.parent_coin_info, identity.get_tree_hash(), uint64(1)),
        ))
    lp_bundle = _cat_bundle(lp_spendables)

    guard_solution = Program.to([[
        guard_coin.name(),
        pool_puzzle.get_tree_hash(),
        lp_eve.name(),
        initial_lp,
        tree_hash(state),
        intent.as_program(),
    ]])
    expected_guard_spend = make_spend(guard_coin, guard_puzzle, guard_solution)
    guard_spends = [
        spend
        for spend in offer.to_spend_bundle().coin_spends
        if spend.coin.parent_coin_info != ZERO_32 and spend.coin.name() == guard_coin.name()
    ]
    if len(guard_spends) != 1:
        raise ValueError("create Offer is unsigned/unfinalized: launch guard spend missing")
    if (
        bytes(guard_spends[0].puzzle_reveal) != bytes(expected_guard_spend.puzzle_reveal)
        or bytes(guard_spends[0].solution) != bytes(expected_guard_spend.solution)
    ):
        raise ValueError("create Offer launch guard spend does not match signed intent")
    bundle = aggregate_with_offer(
        offer,
        [
            xch_spend,
            launcher_spend,
            *(spend for reserve_bundle in reserve_bundles for spend in reserve_bundle.coin_spends),
            *lp_bundle.coin_spends,
        ],
    )
    return CreateResult(
        bundle,
        V3Pool(
            launcher_id,
            singleton,
            config,
            state,
            PoolCoin(pool_coin, pool_inner, xch.coin.name(), None, launcher_id),
            reserves,
            lp_asset_id,
            lp_tail,
        ),
        guard_coin,
    )


# swap_output_v3 was the V3-era closed form: constant product, no weights.
# It was exact only when the traded pair's weights were equal, and it was
# still being used for every routed leg -- see finding 8 in
# docs/FORGE_SECURITY_AUDIT.md. Retired in favor of forge_math.swap_output,
# which reproduces the puzzle's bracket. Archived copies of old scripts still
# reference it; they are not on any live path.



def invariant_lp_mint_v3(
    old_amounts: tuple[int, int],
    successor_amounts: tuple[int, int],
    total_lp: int,
) -> int:
    if total_lp <= 0 or any(amount <= 0 for amount in (*old_amounts, *successor_amounts)):
        raise ValueError("V3 invariant inputs must be positive")
    if any(successor_amounts[index] < old_amounts[index] for index in range(2)):
        raise ValueError("add reserves cannot decrease")
    if successor_amounts == old_amounts:
        raise ValueError("add must increase at least one reserve")
    scaled_product = (
        total_lp * total_lp * successor_amounts[0] * successor_amounts[1]
        // (old_amounts[0] * old_amounts[1])
    )
    return math.isqrt(scaled_product) - total_lp


def solve_native_add_deposit(
    total_xch: int,
    native_reserve: int,
    other_reserve: int,
    other_deposit: int,
    total_lp: int,
) -> tuple[int, int]:
    """Split one raw XCH settlement into native reserve funding and LP backing."""
    if total_xch <= 0 or native_reserve <= 0 or other_reserve <= 0 or total_lp <= 0:
        raise ValueError("native add split requires positive reserves, LP supply, and XCH")

    lo = 0
    hi = total_xch
    best: tuple[int, int] | None = None
    while lo <= hi:
        native_deposit = (lo + hi) // 2
        lp_delta = invariant_lp_mint_v3(
            (native_reserve, other_reserve),
            (native_reserve + native_deposit, other_reserve + other_deposit),
            total_lp,
        )
        required = native_deposit + lp_delta
        if required == total_xch:
            return native_deposit, lp_delta
        if required < total_xch:
            best = (native_deposit, lp_delta)
            lo = native_deposit + 1
        else:
            hi = native_deposit - 1

    for native_deposit in range(max(0, (best[0] if best else 0) - 4), min(total_xch, (best[0] if best else total_xch) + 4) + 1):
        lp_delta = invariant_lp_mint_v3(
            (native_reserve, other_reserve),
            (native_reserve + native_deposit, other_reserve + other_deposit),
            total_lp,
        )
        if native_deposit + lp_delta == total_xch:
            return native_deposit, lp_delta
    raise ValueError("native add Offer XCH does not split into reserve funding plus LP backing")


def _pool_inner(singleton: object, config: object, state: object) -> Program:
    version = int(config[0])
    return compiled_program(f"pool_singleton_v{version}").curry(singleton, config, state)


def build_transition_v3(pool: V3Pool, offer: Offer, mode: int) -> TransitionResult:
    """Build an add, swap, or remove bundle from a real Chia Offer."""
    if mode not in (MODE_ADD, MODE_SWAP, MODE_REMOVE):
        raise ValueError(f"unsupported V3 mode: {mode}")
    asset_ids = tuple(bytes32(asset_id) for asset_id in pool.config[2])
    parsed = find_offer_settlements(offer, (*asset_ids, pool.lp_asset_id))
    requested = requested_amounts(offer)
    old_amounts = tuple(int(reserve[2]) for reserve in pool.state[0])
    total_lp = int(pool.state[1])
    protocol_version = int(pool.config[0])
    unbalanced_join = protocol_version >= PROTOCOL_VERSION

    def settlement_key(asset_id: bytes32) -> bytes32 | None:
        return None if asset_id == ZERO_32 else asset_id

    offered_assets = {
        asset_id: parsed[settlement_key(asset_id)].coin.amount
        for asset_id in asset_ids
        if settlement_key(asset_id) in parsed
    }
    lp_delta = 0
    native_add_deposit = 0
    successor_amounts: tuple[int, int]
    if mode == MODE_ADD:
        if not offered_assets:
            raise ValueError("add Offer must provide at least one reserve asset")
        if not unbalanced_join and set(offered_assets) != set(asset_ids):
            raise ValueError("legacy V3 add Offer must provide both reserve assets")
        if ZERO_32 in asset_ids:
            native_index = asset_ids.index(ZERO_32)
            other_index = 1 - native_index
            xch_total = parsed[None].coin.amount if None in parsed else 0
            native_add_deposit, lp_delta = solve_native_add_deposit(
                xch_total,
                old_amounts[native_index],
                old_amounts[other_index],
                int(offered_assets.get(asset_ids[other_index], 0)),
                total_lp,
            )
            offered_assets[ZERO_32] = native_add_deposit
        deposits = tuple(int(offered_assets.get(asset_id, 0)) for asset_id in asset_ids)
        successor_amounts = tuple(old_amounts[index] + deposits[index] for index in range(2))
        lp_delta = lp_delta or (invariant_lp_mint_v3(old_amounts, successor_amounts, total_lp) if unbalanced_join else min(
            deposits[index] * total_lp // old_amounts[index]
            for index in range(2)
        ))
        if lp_delta <= 0 or requested.get(pool.lp_asset_id, 0) != lp_delta:
            raise ValueError("add Offer LP minimum does not equal canonical mint")
        expected_xch = lp_delta + native_add_deposit
        if None not in parsed or parsed[None].coin.amount != expected_xch:
            raise ValueError("add Offer must provide XCH backing equal to LP mint plus native reserve funding")
    elif mode == MODE_SWAP:
        if len(offered_assets) != 1:
            raise ValueError("swap Offer must provide exactly one pool asset")
        input_index = 0 if asset_ids[0] in offered_assets else 1
        output_index = 1 - input_index
        amount_in = int(offered_assets[asset_ids[input_index]])
        # The weight-aware form, which reproduces the puzzle's bracket. V7 pools
        # come down this lane and V7 introduced real weights, so the old
        # constant-product closed form under-quoted them and the puzzle rejected
        # the successor it implied.
        _units = ([int(w) for w in pool.config[3]] if int(pool.config[0]) >= 7
                  else [1] * len(asset_ids))
        amount_out = swap_output(
            old_amounts[input_index],
            old_amounts[output_index],
            amount_in,
            int(pool.config[4]),
            _units[input_index],
            _units[output_index],
        )
        values = list(old_amounts)
        values[input_index] += amount_in
        values[output_index] -= amount_out
        if requested.get(asset_ids[output_index], 0) > amount_out:
            raise ValueError("swap output is below Offer minimum")
        unexpected = set(requested) - {asset_ids[output_index]}
        if unexpected:
            raise ValueError("swap Offer requests an asset outside the selected output")
        successor_amounts = (values[0], values[1])
    else:
        if pool.lp_asset_id not in parsed:
            raise ValueError("remove Offer does not provide LP CAT")
        offered_lp_amount = int(parsed[pool.lp_asset_id].coin.amount)
        burn = min(offered_lp_amount, total_lp - 1)
        if burn <= 0:
            raise ValueError("invalid LP burn amount")
        lp_delta = -burn
        successor_amounts = tuple(
            old_amounts[index] - old_amounts[index] * burn // total_lp
            for index in range(2)
        )
        for index, asset_id in enumerate(asset_ids):
            withdrawal = old_amounts[index] - successor_amounts[index]
            requested_key = settlement_key(asset_id)
            if requested.get(requested_key, 0) > withdrawal:
                raise ValueError("remove output is below Offer minimum")
        expected_requested_keys = {settlement_key(asset_id) for asset_id in asset_ids}
        unexpected = set(requested) - expected_requested_keys
        if unexpected:
            raise ValueError("remove Offer requests a non-reserve asset")

    reserve_inner = reserve_inner_puzzle(protocol_version, pool.launcher_id)
    pool_inner_hash = pool.pool.inner_puzzle.get_tree_hash()
    offer_inner_hash = OFFER_MOD_HASH
    plans: list[list[object]] = []
    next_reserves: dict[bytes32, ReserveCoin] = {}
    reserve_bundles: list[WalletSpendBundle] = []
    for index, asset_id in enumerate(asset_ids):
        current = pool.reserves[asset_id]
        successor_amount = successor_amounts[index]
        is_native = asset_id == ZERO_32
        reserve_outer_hash = (
            reserve_inner.get_tree_hash()
            if is_native
            else construct_cat_puzzle(CAT_MOD, asset_id, reserve_inner).get_tree_hash()
        )
        successor = Coin(current.coin.name(), reserve_outer_hash, uint64(successor_amount))
        settlement: EphemeralSettlement | None
        if successor_amount > current.coin.amount:
            if settlement_key(asset_id) not in parsed:
                raise ValueError("reserve increase has no offered settlement coin")
            settlement = parsed[settlement_key(asset_id)]
            if is_native:
                settlement_lineage = None
            else:
                assert settlement.lineage_proof is not None
                settlement_lineage = settlement.lineage_proof
            settlement_coin_id = settlement.coin.name()
        elif successor_amount == current.coin.amount and unbalanced_join:
            settlement = None
            settlement_lineage = None
            settlement_coin_id = ZERO_32
        else:
            output_amount = current.coin.amount - successor_amount
            settlement_coin = Coin(
                current.coin.name(),
                OFFER_MOD_HASH if is_native else construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash(),
                uint64(output_amount),
            )
            settlement = EphemeralSettlement(
                asset_id,
                settlement_coin,
                make_spend(current.coin, current.inner_puzzle, Program.to([])),
                LineageProof(current.coin.parent_coin_info, current.inner_puzzle.get_tree_hash(), current.coin.amount),
            )
            settlement_lineage = settlement.lineage_proof
            settlement_coin_id = settlement.coin.name()

        plan = [
            asset_id,
            current.coin.name(),
            current.coin.amount,
            settlement_coin_id,
            successor.name(),
            successor.amount,
        ]
        plans.append(plan)
        reserve_solution = build_reserve_solution(
            protocol_version, pool.launcher_id, asset_id, reserve_inner.get_tree_hash(),
            pool_inner_hash, [mode, pool.pool.coin.name(), *plan])
        if is_native:
            native_spends = [make_spend(current.coin, current.inner_puzzle, reserve_solution)]
            if settlement is not None and not (mode == MODE_ADD and successor_amount > current.coin.amount):
                settlement_groups = requested_solution(offer, None) if successor_amount < current.coin.amount else []
                settlement_groups.append(_settlement_group(settlement.coin.name(), []))
                native_spends.append(make_spend(settlement.coin, OFFER_MOD, Program.to(settlement_groups)))
            reserve_bundles.append(WalletSpendBundle(native_spends, G2Element()))
        else:
            reserve_spendable = SpendableCAT(
                current.coin,
                asset_id,
                current.inner_puzzle,
                reserve_solution,
                lineage_proof=current.lineage_proof,
            )
            if settlement is None:
                reserve_bundles.append(_cat_bundle([reserve_spendable]))
            else:
                settlement_groups = requested_solution(offer, asset_id) if successor_amount < current.coin.amount else []
                if successor_amount < current.coin.amount:
                    output_amount = current.coin.amount - successor_amount
                    minimum = requested.get(asset_id, 0)
                    payments = offer.get_requested_payments().get(asset_id, [])
                    if minimum <= 0 or not payments:
                        raise ValueError("reserve withdrawal has no requested payment")
                    surplus = output_amount - minimum
                    if surplus > 0:
                        settlement_groups.append(_settlement_group(
                            tree_hash([settlement.coin.name(), b"forge-surplus-v3"]),
                            [(payments[0].puzzle_hash, surplus)],
                        ))
                settlement_groups.append(_settlement_group(settlement.coin.name(), []))
                assert settlement_lineage is not None
                reserve_bundles.append(_cat_bundle([
                    reserve_spendable,
                    SpendableCAT(
                        settlement.coin,
                        asset_id,
                        OFFER_MOD,
                        Program.to(settlement_groups),
                        lineage_proof=settlement_lineage,
                    ),
                ]))
        next_reserves[asset_id] = ReserveCoin(
            asset_id,
            successor,
            reserve_inner,
            LineageProof(current.coin.parent_coin_info, current.inner_puzzle.get_tree_hash(), current.coin.amount),
        )

    new_total_lp = total_lp + lp_delta
    next_state: list[object] = [
        [[asset_ids[index], plans[index][4], plans[index][5]] for index in range(2)],
        new_total_lp,
    ]

    lp_spends: list[CoinSpend] = []
    lp_output: Coin | None = None
    lp_output_lineage: LineageProof | None = None
    lp_action_coin_id = ZERO_32
    # From V9 the pool derives the LP action coin id from its parent, a pinned
    # LP-CAT puzzle hash, and its amount, so an impostor coin can no longer stand
    # in for a real burn/mint. Pre-V9 pools do not read this field.
    lp_parent_id = ZERO_32
    lp_binding = protocol_version >= 9
    if mode == MODE_ADD:
        xch = parsed[None]
        expected_xch = lp_delta + native_add_deposit if ZERO_32 in asset_ids else lp_delta
        if xch.coin.amount != expected_xch:
            raise ValueError("add Offer must provide XCH value funding equal to LP mint plus native reserve funding")
        # V9+: the mint eve runs LP_MINT_INNER, a fixed puzzle whose only outputs
        # are the minted settlement coin and the -113 mint. The pool binds the
        # eve's id to the LP CAT wrapping that inner, so the acknowledgement it
        # asserts cannot be produced by any coin that did not actually mint.
        # Pre-V9 pools use a plain identity inner carrying the same two outputs.
        eve_inner = compiled_program("forge_lp_mint_inner_FORGE") if lp_binding else Program.to(1)
        lp_eve_outer = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, eve_inner)
        lp_eve = Coin(xch.coin.name(), lp_eve_outer.get_tree_hash(), uint64(1))
        lp_action_coin_id = lp_eve.name()
        if lp_binding:
            lp_parent_id = xch.coin.name()
        xch_spend = make_spend(
            xch.coin,
            OFFER_MOD,
            Program.to([
                _settlement_group(xch.coin.name(), [(lp_eve_outer.get_tree_hash(), 1)]),
                *(
                    [_settlement_group(xch.coin.name(), [])]
                    if ZERO_32 in asset_ids
                    else []
                ),
            ]),
        )
        lp_offer_hash = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, OFFER_MOD).get_tree_hash()
        lp_settlement = Coin(lp_eve.name(), lp_offer_hash, uint64(lp_delta))
        lp_action = [pool.pool.coin.name(), lp_eve.name(), lp_delta, new_total_lp, tree_hash(next_state)]
        eve_solution = Program.to(
            [offer_inner_hash, lp_delta, pool.lp_tail, lp_action]
            if lp_binding
            else [
                [ConditionOpcode.CREATE_COIN, offer_inner_hash, lp_delta],
                [ConditionOpcode.CREATE_COIN, 0, -113, pool.lp_tail, lp_action],
            ]
        )
        lp_bundle = _cat_bundle([
            SpendableCAT(
                lp_eve,
                pool.lp_asset_id,
                eve_inner,
                eve_solution,
                lineage_proof=LineageProof(),
                extra_delta=lp_delta - 1,
                limitations_program_reveal=pool.lp_tail,
                limitations_solution=Program.to(lp_action),
            ),
            SpendableCAT(
                lp_settlement,
                pool.lp_asset_id,
                OFFER_MOD,
                Program.to(requested_solution(offer, pool.lp_asset_id)),
                lineage_proof=LineageProof(lp_eve.parent_coin_info, eve_inner.get_tree_hash(), uint64(1)),
            ),
        ])
        lp_spends = [xch_spend, *lp_bundle.coin_spends]
        payments = offer.get_requested_payments()[pool.lp_asset_id]
        if len(payments) == 1:
            lp_output_hash = construct_cat_puzzle(
                CAT_MOD,
                pool.lp_asset_id,
                payments[0].puzzle_hash,
            ).get_tree_hash_precalc(payments[0].puzzle_hash)
            lp_output = Coin(lp_settlement.name(), lp_output_hash, payments[0].amount)
            lp_output_lineage = LineageProof(
                lp_settlement.parent_coin_info,
                OFFER_MOD_HASH,
                lp_settlement.amount,
            )
    elif mode == MODE_REMOVE:
        offered_lp = parsed[pool.lp_asset_id]
        assert offered_lp.lineage_proof is not None
        burn = -lp_delta
        lp_nonce = bytes32(hashlib.sha256(bytes(offered_lp.coin.name())).digest())
        lp_change = int(offered_lp.coin.amount) - burn

        if lp_binding:
            # V9+: the burned LP flows into a coin whose puzzle is the LP CAT
            # wrapping LP_MELT_INNER -- a puzzle whose only behavior is the
            # -113 melt of its whole amount. The offered LP is split at the
            # settlement into exactly that burn-sized melt coin (plus any change
            # back to the owner), and the pool binds the melt coin's id, so the
            # reserve release cannot happen without the burn.
            melt_inner = compiled_program("forge_lp_melt_inner_FORGE")
            melt_cat_hash = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, melt_inner).get_tree_hash()
            melt_coin = Coin(offered_lp.coin.name(), melt_cat_hash, uint64(burn))
            lp_action_coin_id = melt_coin.name()
            lp_parent_id = offered_lp.coin.name()
            lp_action = [
                pool.pool.coin.name(),
                melt_coin.name(),
                lp_delta,
                new_total_lp,
                tree_hash(next_state),
            ]
            payments: list[tuple[bytes32, int]] = [(melt_inner.get_tree_hash(), burn)]
            if lp_change > 0:
                reserve_payments = [
                    payment
                    for asset_id in asset_ids
                    for payment in offer.get_requested_payments().get(asset_id, [])
                ]
                if not reserve_payments:
                    raise ValueError("full-position remove Offer has no destination for residual LP change")
                payments.append((reserve_payments[0].puzzle_hash, lp_change))
            lp_route_bundle = _cat_bundle([
                SpendableCAT(
                    offered_lp.coin,
                    pool.lp_asset_id,
                    OFFER_MOD,
                    Program.to([_settlement_group(lp_nonce, payments)]),
                    lineage_proof=offered_lp.lineage_proof,
                ),
            ])
            lp_burn_bundle = _cat_bundle([
                SpendableCAT(
                    melt_coin,
                    pool.lp_asset_id,
                    melt_inner,
                    Program.to([pool.lp_tail, lp_action]),
                    lineage_proof=LineageProof(
                        offered_lp.coin.parent_coin_info,
                        OFFER_MOD_HASH,
                        offered_lp.coin.amount,
                    ),
                    extra_delta=lp_delta,
                    limitations_program_reveal=pool.lp_tail,
                    limitations_solution=Program.to(lp_action),
                ),
            ])
            lp_spends = [*lp_route_bundle.coin_spends, *lp_burn_bundle.coin_spends]
        else:
            identity = Program.to(1)
            intermediate_hash = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, identity).get_tree_hash()
            intermediate = Coin(offered_lp.coin.name(), intermediate_hash, offered_lp.coin.amount)
            lp_action_coin_id = intermediate.name()
            lp_action = [
                pool.pool.coin.name(),
                intermediate.name(),
                lp_delta,
                new_total_lp,
                tree_hash(next_state),
            ]
            lp_change_conditions: list[list[object]] = []
            if lp_change > 0:
                reserve_payments = [
                    payment
                    for asset_id in asset_ids
                    for payment in offer.get_requested_payments().get(asset_id, [])
                ]
                if not reserve_payments:
                    raise ValueError("full-position remove Offer has no destination for residual LP change")
                lp_change_conditions.append([
                    ConditionOpcode.CREATE_COIN,
                    reserve_payments[0].puzzle_hash,
                    lp_change,
                ])
            lp_route_bundle = _cat_bundle([
                SpendableCAT(
                    offered_lp.coin,
                    pool.lp_asset_id,
                    OFFER_MOD,
                    Program.to([_settlement_group(
                        lp_nonce,
                        [(identity.get_tree_hash(), offered_lp.coin.amount)],
                    )]),
                    lineage_proof=offered_lp.lineage_proof,
                ),
            ])
            lp_burn_bundle = _cat_bundle([
                SpendableCAT(
                    intermediate,
                    pool.lp_asset_id,
                    identity,
                    Program.to([
                        *lp_change_conditions,
                        [ConditionOpcode.CREATE_COIN, 0, -113, pool.lp_tail, lp_action],
                    ]),
                    lineage_proof=LineageProof(
                        offered_lp.coin.parent_coin_info,
                        OFFER_MOD_HASH,
                        offered_lp.coin.amount,
                    ),
                    extra_delta=lp_delta,
                    limitations_program_reveal=pool.lp_tail,
                    limitations_solution=Program.to(lp_action),
                ),
            ])
            lp_spends = [*lp_route_bundle.coin_spends, *lp_burn_bundle.coin_spends]

    # V9+ pool actions carry the LP action coin's parent so the puzzle can derive
    # and bind that coin's id. Pre-V9 pool puzzles ignore any trailing solution
    # field, so appending it is safe for every version.
    action = [mode, pool.pool.coin.name(), plans, lp_action_coin_id, lp_delta, lp_parent_id]
    pool_spend = make_spend(
        pool.pool.coin,
        pool.pool.puzzle,
        solution_for_singleton(
            LineageProof(
                pool.pool.lineage_parent_name,
                pool.pool.parent_inner_puzzle_hash,
                pool.pool.coin.amount,
            ),
            uint64(1),
            Program.to([action]),
        ),
    )
    next_inner = _pool_inner(pool.singleton, pool.config, next_state)
    next_puzzle = puzzle_for_singleton(pool.launcher_id, next_inner)
    next_pool_coin = Coin(pool.pool.coin.name(), next_puzzle.get_tree_hash(), uint64(1))
    bundle = aggregate_with_offer(
        offer,
        [
            pool_spend,
            *(spend for reserve_bundle in reserve_bundles for spend in reserve_bundle.coin_spends),
            *lp_spends,
        ],
    )
    return TransitionResult(
        bundle,
        V3Pool(
            pool.launcher_id,
            pool.singleton,
            pool.config,
            next_state,
            PoolCoin(
                next_pool_coin,
                next_inner,
                pool.pool.coin.parent_coin_info,
                pool.pool.inner_puzzle.get_tree_hash(),
                pool.launcher_id,
            ),
            next_reserves,
            pool.lp_asset_id,
            pool.lp_tail,
        ),
        lp_output,
        lp_output_lineage,
    )