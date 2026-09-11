"""NFTs a lock owns: sending one, and putting one under the lock's DID.

An NFT is a singleton like the lock's DID, so moving one is the same kind of
spend the lock already makes -- the singleton announces the coin, the coin runs
the delegated puzzle the owners signed. What is different is the two layers in
between. The lock's deposit puzzle sits at the bottom of

    singleton( nft_state_layer( metadata, updater,
                 nft_ownership_layer( owner_did, transfer_program,
                   p2 = the lock's deposit puzzle )))

and each layer wraps the solution one deeper and re-wraps whatever the inner
puzzle creates. So the p2 says only `CREATE_COIN(where it goes)` and the layers
above carry the metadata, the royalties and the owner across for free.

**Assigning a DID is not a payment.** The condition is `-10`
(`new_owner, trade_prices, new_did_inner_hash`), and the transfer program then
demands a `ASSERT_PUZZLE_ANNOUNCEMENT` from the DID's *full singleton puzzle*
naming this NFT's launcher id -- read the `if` in
`nft_ownership_transfer_program_one_way_claim_with_royalties.clsp` and it is
right there. In other words the DID has to be spent in the same transaction and
say yes. For a Forge lock that is the easy case: it owns both, so one proposal
carries both singleton spends and one signature covers them.

Clearing an owner needs no such approval -- the assertion only appears when the
new owner is set and differs from the current one.

Nothing here is rebuilt on trust. The NFT's current puzzle is reconstructed from
its parent's spend and then **checked against the coin's own puzzle hash**; if it
does not match, the spend is refused rather than built. That is the lesson the
inert DID taught: hashing something is not the same as its being right.
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.lineage_proof import LineageProof as SingletonLineageProof
from chia.wallet.nft_wallet.nft_puzzle_utils import (
    create_nft_layer_puzzle_with_curry_params,
    get_metadata_and_phs,
    recurry_nft_puzzle,
)
from chia.wallet.nft_wallet.uncurry_nft import UncurriedNFT
from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia.util.bech32m import decode_puzzle_hash

from multisig_tool import MultisigError, Node, hex32, record_coin, strip0x
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER_HASH

from vault_tool import (
    FundsSpend,
    SingletonHost,
    VaultState,
    coin_spend_of,
    deposit_puzzle,
    deposit_puzzle_hash,
    did_eve_coin,
    did_inner_puzzle,
)

CREATE_COIN = ConditionOpcode.CREATE_COIN
CREATE_PUZZLE_ANNOUNCEMENT = ConditionOpcode.CREATE_PUZZLE_ANNOUNCEMENT
# The NFT's own opcode for "this has a new owner". Not a condition the chain
# knows: the ownership layer reads it out of the inner puzzle's output.
NEW_OWNER = -10


@dataclass
class NftMove:
    """One NFT, and what is to become of it."""

    launcher_id: bytes32
    # Where it goes. None leaves it at the lock, which is what an assignment
    # that only changes the owner wants.
    target_puzzle_hash: bytes32 | None = None
    # The DID to put it under. None leaves the owner alone; `clear_owner` takes
    # it out of whatever DID holds it now.
    new_owner: bytes32 | None = None
    clear_owner: bool = False

    @property
    def changes_owner(self) -> bool:
        return self.clear_owner or self.new_owner is not None


def parse_moves(raw: Any, hrp: str = "txch") -> list[NftMove]:
    """`[{launcher_id, address? | puzzle_hash?, did?, clear_did?}]`."""
    if not isinstance(raw, list):
        raw = [raw]
    moves: list[NftMove] = []
    seen: set[bytes32] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or not entry.get("launcher_id"):
            continue
        launcher_id = hex32(entry["launcher_id"], f"nft {index + 1} launcher_id")
        if launcher_id in seen:
            raise MultisigError("the same NFT is named twice in one proposal")
        seen.add(launcher_id)
        target: bytes32 | None = None
        if entry.get("address"):
            address = str(entry["address"]).strip().lower()
            if not address.startswith(f"{hrp}1"):
                raise MultisigError(f"nft {index + 1}: address must start with {hrp}1")
            try:
                target = bytes32(decode_puzzle_hash(address))
            except Exception as exc:                           # noqa: BLE001
                raise MultisigError(f"nft {index + 1}: invalid address ({exc})") from exc
        target_raw = strip0x(entry.get("puzzle_hash"))
        if target is None and target_raw:
            target = hex32(target_raw, f"nft {index + 1} puzzle_hash")
        did_raw = strip0x(entry.get("did"))
        clear = bool(entry.get("clear_did"))
        if did_raw and clear:
            raise MultisigError("an NFT cannot both be given a DID and taken out of one")
        move = NftMove(
            launcher_id,
            target,
            hex32(did_raw, f"nft {index + 1} did") if did_raw else None,
            clear,
        )
        if move.target_puzzle_hash is None and not move.changes_owner:
            raise MultisigError("an NFT move must send it somewhere or change its owner")
        moves.append(move)
    if not moves:
        raise MultisigError("no NFT was named")
    return moves


def resolve_owned_nft(node: Node, lock_launcher_id: bytes32, nft_launcher_id: bytes32) -> tuple[Coin, SingletonHost, UncurriedNFT]:
    """The lock's unspent coin for this NFT, with its puzzle rebuilt and checked.

    An NFT is found by the hint its last transfer wrote, not at the lock's
    address, so the search is over hinted coins; the shape comes from the
    parent's spend, because that reveal is the puzzle this coin was created
    from.
    """
    deposit = deposit_puzzle(lock_launcher_id)
    deposit_ph = deposit.get_tree_hash()
    for record in node.coin_records_by_hint(deposit_ph):
        if record.get("spent"):
            continue
        coin = record_coin(record)
        if int(coin.amount) % 2 == 0:
            continue
        parent_id = bytes32(coin.parent_coin_info)
        parent = node.coin_record(parent_id)
        if not parent or not parent.get("spent_block_index"):
            continue
        try:
            parent_puzzle, parent_solution = coin_spend_of(node, parent_id, int(parent["spent_block_index"]))
        except MultisigError:
            continue
        unft = UncurriedNFT.uncurry(*parent_puzzle.uncurry())
        if unft is None or unft.singleton_launcher_id != nft_launcher_id:
            continue

        # What the parent's spend made of it: a new owner and possibly new
        # metadata are both decided there, so the current puzzle is derived from
        # that spend rather than from the parent's curried values.
        ownership = recurry_nft_puzzle(unft, parent_solution, deposit)
        metadata, _target = get_metadata_and_phs(unft, parent_solution)
        state_layer = create_nft_layer_puzzle_with_curry_params(
            metadata, bytes32(unft.metadata_updater_hash.as_atom()), ownership,
        )
        rebuilt = puzzle_for_singleton(nft_launcher_id, state_layer)
        if rebuilt.get_tree_hash() != bytes32(coin.puzzle_hash):
            raise MultisigError(
                f"NFT {nft_launcher_id.hex()[:12]}… could not be reconstructed: the puzzle rebuilt from its "
                "parent's spend does not hash to the coin on chain, so this lock cannot spend it safely"
            )
        parent_coin = record_coin(parent)
        host = SingletonHost(
            nft_launcher_id,
            state_layer,
            SingletonLineageProof(
                bytes32(parent_coin.parent_coin_info),
                unft.nft_state_layer.get_tree_hash(),
                uint64(parent_coin.amount),
            ),
            layers="nft",
        )
        return coin, host, unft
    raise MultisigError(f"this lock does not hold an unspent NFT {nft_launcher_id.hex()[:12]}…")


def resolve_did_now(node: Node, lock_launcher_id: bytes32, did_launcher_id: bytes32) -> tuple[Coin, SingletonHost]:
    """The lock's DID as it stands now -- eve or long since published.

    `resolve_owned_did` finds only the eve coin, which is all publishing needs.
    Approving an NFT needs whichever coin is unspent today, because that is the
    one that has to be spent alongside to say yes.

    The inner puzzle is not read off the chain but rebuilt: a DID the lock owns
    is `did_innerpuz` curried with the lock's own deposit puzzle, and it
    recreates itself unchanged every spend. Rebuilt, then checked against the
    coin's puzzle hash -- if the two disagree this is not the DID we think it
    is, and nothing is signed.
    """
    inner = did_inner_puzzle(deposit_puzzle(lock_launcher_id), did_launcher_id)
    expected_ph = puzzle_for_singleton(did_launcher_id, inner).get_tree_hash()
    deposit_ph = deposit_puzzle_hash(lock_launcher_id)

    candidates = [record_coin(r) for r in node.coin_records_by_hint(deposit_ph) if not r.get("spent")]
    # The eve coin is not hinted -- the launcher wrote no memo -- so it is
    # derived rather than searched for.
    eve = did_eve_coin(lock_launcher_id, did_launcher_id)
    eve_record = node.coin_record(eve.name())
    if eve_record is not None and not eve_record.get("spent"):
        candidates.append(eve)

    for coin in candidates:
        if bytes32(coin.puzzle_hash) != expected_ph:
            continue
        parent = node.coin_record(bytes32(coin.parent_coin_info))
        if not parent:
            continue
        parent_coin = record_coin(parent)
        # A DID created by its launcher has no inner puzzle above it; one that
        # has spent before was recreated from this same inner puzzle.
        from_launcher = bytes32(parent_coin.puzzle_hash) == SINGLETON_LAUNCHER_HASH
        lineage = SingletonLineageProof(
            bytes32(parent_coin.parent_coin_info),
            None if from_launcher else inner.get_tree_hash(),
            uint64(parent_coin.amount),
        )
        return coin, SingletonHost(did_launcher_id, inner, lineage)
    raise MultisigError(
        f"this lock has no unspent coin for DID {did_launcher_id.hex()[:12]}…, so that DID cannot approve anything"
    )


def nft_conditions(move: NftMove, deposit_ph: bytes32, did_inner_hash: bytes32 | None) -> list[Any]:
    """What the lock's own puzzle says, under the NFT's two layers.

    A `CREATE_COIN` of amount 1 is where the NFT lands, hinted so the recipient's
    wallet can find it; the layers above re-wrap it into a new NFT coin. `-10`
    is the owner change, which the ownership layer reads and the transfer
    program turns into a demand for the DID's approval.
    """
    target = move.target_puzzle_hash or deposit_ph
    conditions: list[Any] = [[CREATE_COIN, target, 1, [target]]]
    if move.clear_owner:
        conditions.append([NEW_OWNER, None, [], None])
    elif move.new_owner is not None:
        if did_inner_hash is None:
            raise MultisigError("assigning a DID needs that DID's current inner puzzle hash")
        conditions.append([NEW_OWNER, move.new_owner, [], did_inner_hash])
    return conditions


def approval_condition(nft_launcher_id: bytes32) -> list[Any]:
    """What the DID has to say for an NFT to move under it.

    The transfer program asserts a puzzle announcement from the DID's full
    singleton puzzle carrying the NFT's launcher id. Nothing else will do: the
    announcement is checked against the DID's own puzzle hash, so it cannot be
    made by anything but that DID being spent alongside.
    """
    return [CREATE_PUZZLE_ANNOUNCEMENT, nft_launcher_id]


def build_nft_spends(
    node: Node,
    state: VaultState,
    moves: list[NftMove],
    did_inner_hash_of: Any,
) -> tuple[list[FundsSpend], set[bytes32]]:
    """The NFT spends, and the DIDs that must be spent alongside to approve them.

    `did_inner_hash_of(launcher_id)` returns the DID's current inner puzzle
    hash, which the transfer program needs in order to compute the puzzle hash
    it will demand an announcement from.
    """
    deposit_ph = deposit_puzzle_hash(state.launcher_id)
    spends: list[FundsSpend] = []
    approvals: set[bytes32] = set()
    for move in moves:
        coin, host, unft = resolve_owned_nft(node, state.launcher_id, move.launcher_id)
        if move.changes_owner and not unft.supports_did:
            raise MultisigError(
                f"NFT {move.launcher_id.hex()[:12]}… has no ownership layer, so it cannot belong to a DID"
            )
        inner_hash: bytes32 | None = None
        if move.new_owner is not None:
            if move.new_owner == unft.owner_did:
                raise MultisigError("that NFT is already under this DID")
            inner_hash = did_inner_hash_of(move.new_owner)
            approvals.add(move.new_owner)
        if move.clear_owner and unft.owner_did is None:
            raise MultisigError("that NFT is not under a DID")
        conditions = nft_conditions(move, deposit_ph, inner_hash)
        spends.append(FundsSpend("did", None, coin, None, Program.to((1, conditions)), host))
    return spends, approvals
