#!/usr/bin/env python3
"""A real Chia node in-process: farm coins, issue test CATs, push Forge bundles.

`chia._tests.util.spend_sim` runs the actual mempool manager and coin store, so
everything the offline validator cannot see is enforced here for real: coin
existence, lineage, `ASSERT_MY_BIRTH_HEIGHT` against a coin's true
`confirmed_block_index`, the ephemeral-coin rules, and block heights that
advance. That is the whole reason to use it -- it is the closest thing to
testnet11 that does not need testnet11 to be producing blocks.

Two conventions make the harness small:

  * **Anyone-can-spend funding.** Rewards are farmed to `IDENTITY` (`(1)`), whose
    solution IS its condition list. No key material, no signatures, so every
    bundle carries `G2Element()` and the mempool's signature check passes
    trivially. Forge's own puzzles never ask for a signature either.
  * **Permissive test TAILs.** A test token's TAIL is "return no conditions",
    salted so each token gets its own asset id. Minting is therefore free, which
    is what a faucet is. The pool's own LP TAIL is the real Forge one.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

sys.path.insert(0, ".")

from chia._tests.util.spend_sim import SimClient, SpendSim, sim_and_client  # noqa: F401
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.lineage_proof import LineageProof
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

IDENTITY = Program.to(1)
IDENTITY_HASH = IDENTITY.get_tree_hash()
CREATE_COIN = 51


class SimRejected(Exception):
    """The simulator's mempool refused the bundle."""


def quoted_nil_tail(salt: int) -> Program:
    """A TAIL that succeeds and emits nothing, with a distinct hash per salt.

    `(q . ())` returns nil whatever it is handed; currying a salt in front of it
    leaves the behaviour alone and changes the tree hash, which is what gives each
    test token its own asset id.
    """
    return Program.to((1, None)).curry(salt)


@dataclass
class Wallet:
    """Every anyone-can-spend coin the harness controls, by kind."""
    xch: list[Coin]
    cats: dict[bytes32, list[tuple[Coin, LineageProof]]]

    def take_xch(self, amount: int) -> Coin:
        """The smallest XCH coin that covers `amount`, removed from the pile."""
        for index, coin in enumerate(sorted(self.xch, key=lambda c: int(c.amount))):
            if int(coin.amount) >= amount:
                self.xch = [c for i, c in enumerate(sorted(self.xch, key=lambda c: int(c.amount))) if i != index]
                return coin
        raise SimRejected(f"no anyone-can-spend XCH coin covers {amount:,} (have "
                          f"{sorted(int(c.amount) for c in self.xch)})")

    def take_cat(self, asset_id: bytes32, amount: int) -> tuple[Coin, LineageProof]:
        pile = self.cats.get(asset_id) or []
        for index, (coin, lineage) in enumerate(sorted(pile, key=lambda item: int(item[0].amount))):
            if int(coin.amount) >= amount:
                ordered = sorted(pile, key=lambda item: int(item[0].amount))
                self.cats[asset_id] = [item for i, item in enumerate(ordered) if i != index]
                return coin, lineage
        raise SimRejected(f"no {asset_id.hex()[:8]} coin covers {amount:,}")


async def push(client: SimClient, sim: SpendSim, bundle: SpendBundle, label: str) -> None:
    """Push and farm. Raises SimRejected with the node's own error name."""
    status, error = await client.push_tx(bundle)
    if status != MempoolInclusionStatus.SUCCESS:
        raise SimRejected(f"{label}: {error.name if error else 'REJECTED'}")
    await sim.farm_block()


async def expect_refusal(client: SimClient, bundle: SpendBundle, label: str) -> str:
    """Push expecting a refusal; returns the node's error name."""
    status, error = await client.push_tx(bundle)
    if status == MempoolInclusionStatus.SUCCESS:
        raise SimRejected(f"{label}: ACCEPTED, but it should have been refused")
    return error.name if error else "REJECTED"


async def farm_to_identity(sim: SpendSim, client: SimClient, blocks: int = 2) -> list[Coin]:
    """Farm `blocks` blocks of rewards into anyone-can-spend coins.

    `farm_block` returns a block's TRANSACTION additions, not its rewards, so the
    freshly farmed coins are read back out of the coin store by puzzle hash.
    """
    for _ in range(blocks):
        await sim.farm_block(IDENTITY_HASH)
    records = await client.get_coin_records_by_puzzle_hash(IDENTITY_HASH, include_spent_coins=False)
    return [record.coin for record in records]


def split_xch(coin: Coin, amounts: list[int]) -> tuple[list[Coin], SpendBundle]:
    """Cut one anyone-can-spend coin into the pieces a step needs.

    A coin's id is (parent, puzzle hash, amount), so two outputs of the same amount
    from the same parent would BE the same coin -- the node calls that
    `DUPLICATE_OUTPUT`. Repeats are nudged up by a mojo until they are distinct, and
    the caller reads the real amounts off the returned coins.
    """
    assert sum(amounts) <= int(coin.amount), f"cannot split {coin.amount} into {amounts}"
    unique: list[int] = []
    for amount in amounts:
        value = amount
        while value in unique:
            value += 1
        unique.append(value)
    assert sum(unique) <= int(coin.amount), "nudging for uniqueness overran the coin"
    conditions = [[CREATE_COIN, IDENTITY_HASH, amount] for amount in unique]
    spend = make_spend(coin, IDENTITY, Program.to(conditions))
    children = [Coin(coin.name(), IDENTITY_HASH, uint64(amount)) for amount in unique]
    return children, SpendBundle([spend], G2Element())


def issue_cat(funding: Coin, amount: int, salt: int) -> tuple[bytes32, Coin, LineageProof, SpendBundle]:
    """Mint `amount` of a fresh test CAT from an anyone-can-spend XCH coin.

    Returns (asset_id, the minted CAT coin, its lineage proof, the bundle). The eve
    CAT is created by the funding coin, then spent with `extra_delta = amount` so the
    ring mints; the permissive TAIL lets that through, which is the point of a faucet.
    """
    tail = quoted_nil_tail(salt)
    asset_id = tail.get_tree_hash()
    eve_ph = construct_cat_puzzle(CAT_MOD, asset_id, IDENTITY).get_tree_hash()

    # Every CAT mojo is an XCH mojo, so the funding coin has to BACK the mint, not just
    # pay for the eve: it creates the one-mojo eve, and the change it keeps is reduced by
    # the whole minted amount. Keep the full change and the bundle creates value out of
    # nothing, which the node calls MINTING_COIN.
    change = int(funding.amount) - 1 - amount
    if change < 0:
        raise SimRejected(f"funding coin of {int(funding.amount):,} cannot back a mint of {amount:,}")
    funding_conditions = [[CREATE_COIN, eve_ph, 1]]
    if change > 0:
        funding_conditions.append([CREATE_COIN, IDENTITY_HASH, change])
    funding_spend = make_spend(funding, IDENTITY, Program.to(funding_conditions))

    eve = Coin(funding.name(), eve_ph, uint64(1))
    # CAT2 only runs the TAIL when the INNER puzzle asks it to, and it asks with a
    # CREATE_COIN of amount -113 that carries the TAIL REVEAL and its solution as
    # two extra arguments: (51 () -113 TAIL TAIL_SOLUTION). `limitations_program_reveal`
    # on SpendableCAT only tells the ring builder what to expect; the reveal that
    # actually runs is this one. Omitting the extra arguments fails at run time with
    # nothing that names the cause.
    inner_solution = Program.to([
        [CREATE_COIN, IDENTITY_HASH, amount, [IDENTITY_HASH]],
        [CREATE_COIN, 0, -113, tail, Program.to(0)],
    ])
    spendable = SpendableCAT(
        eve, asset_id, IDENTITY, inner_solution,
        lineage_proof=LineageProof(), extra_delta=amount - 1,
        limitations_program_reveal=tail, limitations_solution=Program.to(0),
    )
    ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends

    minted_ph = construct_cat_puzzle(CAT_MOD, asset_id, IDENTITY).get_tree_hash()
    minted = Coin(eve.name(), minted_ph, uint64(amount))
    lineage = LineageProof(eve.parent_coin_info, IDENTITY_HASH, uint64(1))
    return asset_id, minted, lineage, SpendBundle([funding_spend, *ring], G2Element())
