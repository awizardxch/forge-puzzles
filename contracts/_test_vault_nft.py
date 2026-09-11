"""An NFT the lock owns: sent to an address, and put under the lock's DID.

Two layers sit between the lock's own puzzle and an NFT coin, and each of them
re-wraps whatever the layer below creates. Nothing about that can be checked by
hashing: the only way to know a transfer lands where it should, or that an owner
change will be accepted, is to RUN the puzzles and read what comes out. So that
is what this suite does — the same discipline the inert DID cost us.

The check that carries the most weight is the last one. Assigning an NFT to a
DID makes the transfer program demand a puzzle announcement from that DID's full
singleton puzzle, and the DID has to be spent alongside to make it. Here both
halves are built and run, and the announcement the DID emits is compared against
the one the NFT demands. If those two ever drift, an owner change would confirm
as a proposal and fail on chain.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.nft_wallet.nft_puzzles import NFT_METADATA_UPDATER_HASH, NFT_TRANSFER_PROGRAM_DEFAULT
from chia.wallet.nft_wallet.nft_puzzle_utils import (
    construct_ownership_layer,
    create_nft_layer_puzzle_with_curry_params,
)
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER, SINGLETON_LAUNCHER_HASH, puzzle_for_singleton
from chia_rs import AugSchemeMPL
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import multisig_tool as tool
import vault_tool as vault
import vault_nft as nfts

PASSED = 0
FAILED: list[str] = []


def check(name: str, got: object, want: object) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}\n     got  {got}\n     want {want}")


def refused(build) -> str:
    try:
        build()
    except tool.MultisigError as exc:
        return f"refused: {exc}"
    return "accepted"


# ─── a lock, a DID it owns, and an NFT it holds ──────────────────────────────

LOCK = bytes32([0x4C] * 32)
NFT_ID = bytes32([0x9F] * 32)
# A singleton's launcher id IS its launcher coin's id, and an eve spend proves
# its lineage against exactly that. So the DID's identity is derived from a real
# launcher coin rather than picked, or nothing it does would run.
DID_LAUNCHER = Coin(bytes32([0x0C] * 32), SINGLETON_LAUNCHER_HASH, uint64(1))
DID_ID = DID_LAUNCHER.name()
OWNER_A = AugSchemeMPL.key_gen(bytes([41] * 32)).get_g1()
POLICY = vault.Policy("nft", 1, (("a", OWNER_A),), vault.FORMAT_MIPS)

DEPOSIT = vault.deposit_puzzle(LOCK)
DEPOSIT_PH = DEPOSIT.get_tree_hash()

TIP = Coin(bytes32([0x01] * 32), vault.vault_puzzle(LOCK, POLICY).get_tree_hash(), uint64(1))
STATE = vault.VaultState(
    LOCK, POLICY, TIP,
    vault.SingletonLineageProof(bytes32([0x02] * 32), POLICY.inner_puzzle_hash(), uint64(1)),
    height=20, spends=1,
)

# The DID the lock owns, exactly as `vault_tool` builds it.
DID_INNER = vault.did_inner_puzzle(DEPOSIT, DID_ID)
DID_FULL = puzzle_for_singleton(DID_ID, DID_INNER)
DID_COIN = Coin(DID_ID, DID_FULL.get_tree_hash(), uint64(1))
DID_LINEAGE = vault.SingletonLineageProof(bytes32(DID_LAUNCHER.parent_coin_info), None, uint64(1))

# The NFT: metadata, the default royalty transfer program, no owner to start.
METADATA = Program.to([("u", ["https://example.invalid/one.png"]), ("h", bytes32([0x33] * 32))])
SINGLETON_STRUCT = Program.to((vault.SINGLETON_MOD_HASH, (NFT_ID, SINGLETON_LAUNCHER_HASH)))
TRANSFER_PROGRAM = NFT_TRANSFER_PROGRAM_DEFAULT.curry(SINGLETON_STRUCT, DEPOSIT_PH, 0)


def nft_puzzle(owner: bytes32 | None, p2: Program) -> Program:
    ownership = construct_ownership_layer(owner, TRANSFER_PROGRAM, p2)
    state_layer = create_nft_layer_puzzle_with_curry_params(METADATA, NFT_METADATA_UPDATER_HASH, ownership)
    return puzzle_for_singleton(NFT_ID, state_layer)


def state_layer_of(owner: bytes32 | None, p2: Program) -> Program:
    return create_nft_layer_puzzle_with_curry_params(
        METADATA, NFT_METADATA_UPDATER_HASH, construct_ownership_layer(owner, TRANSFER_PROGRAM, p2),
    )


NFT_FULL = nft_puzzle(None, DEPOSIT)
# The parent generation: the same NFT, spent to the lock, which is how the coin
# on hand came to exist. Its solution is what the current puzzle is rebuilt from.
NFT_PARENT = Coin(bytes32([0x0F] * 32), NFT_FULL.get_tree_hash(), uint64(1))
NFT_COIN = Coin(NFT_PARENT.name(), NFT_FULL.get_tree_hash(), uint64(1))

PARENT_DELEGATED = Program.to((1, [[ConditionOpcode.CREATE_COIN, DEPOSIT_PH, 1, [DEPOSIT_PH]]]))
PARENT_P2_SOLUTION = vault.funds_solution(POLICY.inner_puzzle_hash(), PARENT_DELEGATED, NFT_PARENT)
PARENT_SOLUTION = Program.to([
    vault.SingletonLineageProof(bytes32([0x0E] * 32), state_layer_of(None, DEPOSIT).get_tree_hash(), uint64(1)).to_program(),
    1,
    Program.to([Program.to([PARENT_P2_SOLUTION])]),
])


XCH_COIN = Coin(bytes32([0x1A] * 32), DEPOSIT_PH, uint64(1_000_000))


def record(coin: Coin, spent: bool = False, hinted: bool = False) -> dict:
    entry = {
        "coin": {
            "parent_coin_info": "0x" + coin.parent_coin_info.hex(),
            "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
            "amount": int(coin.amount),
        },
        "spent": spent,
        "spent_block_index": 15 if spent else 0,
        "confirmed_block_index": 12,
    }
    return entry


class FakeNode(tool.Node):
    """A chain holding one NFT, its parent, and the lock's DID."""

    def __init__(self, did_spent: bool = False) -> None:
        super().__init__("http://fake")
        self.did_spent = did_spent

    def rpc(self, route: str, payload: dict) -> dict:
        if route == "get_coin_records_by_puzzle_hashes":
            # One XCH coin at the lock, so a proposal that also pays out has
            # something to pay from.
            wanted = {tool.strip0x(ph) for ph in payload["puzzle_hashes"]}
            return {"success": True, "coin_records": [record(XCH_COIN)] if DEPOSIT_PH.hex() in wanted else []}
        if route == "get_coin_records_by_hint":
            return {"success": True, "coin_records": [record(NFT_COIN), record(DID_COIN, spent=self.did_spent)]}
        if route == "get_coin_record_by_name":
            name = bytes32(bytes.fromhex(tool.strip0x(payload["name"])))
            if name == NFT_PARENT.name():
                return {"success": True, "coin_record": record(NFT_PARENT, spent=True)}
            if name == DID_COIN.name():
                return {"success": True, "coin_record": record(DID_COIN, spent=self.did_spent)}
            if name == DID_LAUNCHER.name():
                return {"success": True, "coin_record": record(DID_LAUNCHER, spent=True)}
            return {"success": True, "coin_record": None}
        if route == "get_puzzle_and_solution":
            coin_id = bytes32(bytes.fromhex(tool.strip0x(payload["coin_id"])))
            if coin_id == NFT_PARENT.name():
                return {"success": True, "coin_solution": {
                    "puzzle_reveal": bytes(NFT_FULL).hex(),
                    "solution": bytes(PARENT_SOLUTION).hex(),
                }}
            if coin_id == DID_ID:
                # The DID's parent is its launcher. The NFT search walks past it,
                # which is the point: a hinted coin that is not an NFT is skipped,
                # not mistaken for one.
                return {"success": True, "coin_solution": {
                    "puzzle_reveal": bytes(Program.from_bytes(bytes(SINGLETON_LAUNCHER))).hex(),
                    "solution": "80",
                }}
            raise AssertionError(f"unexpected reveal request {coin_id.hex()}")
        raise AssertionError(f"unexpected route {route}")


NODE = FakeNode()
ASSERT_PUZZLE_ANNOUNCEMENT = ConditionOpcode.ASSERT_PUZZLE_ANNOUNCEMENT
CREATE_PUZZLE_ANNOUNCEMENT = ConditionOpcode.CREATE_PUZZLE_ANNOUNCEMENT
CREATE_COIN = ConditionOpcode.CREATE_COIN


def run_spend(spend: vault.FundsSpend) -> dict:
    """The NFT coin actually spent, with its conditions read back."""
    p2_solution = vault.funds_solution(POLICY.inner_puzzle_hash(), spend.delegated_puzzle, spend.coin)
    host = spend.host
    assert host is not None
    coin_spend = make_spend(spend.coin, host.puzzle(), host.solution(p2_solution, int(spend.coin.amount)))
    return conditions_dict_for_solution(coin_spend.puzzle_reveal, coin_spend.solution, vault.MAX_CLVM_COST)


def did_hash_of(_launcher: bytes32) -> bytes32:
    return DID_INNER.get_tree_hash()


# ─── the puzzle we hand the lock is the coin that is really there ────────────

coin, host, unft = nfts.resolve_owned_nft(NODE, LOCK, NFT_ID)
check("the lock's NFT coin is found", coin.name(), NFT_COIN.name())
check("and the puzzle rebuilt for it hashes to that coin", host.puzzle().get_tree_hash(), bytes32(NFT_COIN.puzzle_hash))
check("it is wrapped as an NFT, not a DID", host.layers, "nft")
check("with no owner yet", unft.owner_did, None)
check("and the ownership layer is there to take one", unft.supports_did, True)


# ─── sending it to an address ────────────────────────────────────────────────

STRANGER = Program.to((1, [[ConditionOpcode.CREATE_COIN, bytes32([0x77] * 32), 1]]))
STRANGER_PH = STRANGER.get_tree_hash()

spends, approvals = nfts.build_nft_spends(NODE, STATE, [nfts.NftMove(NFT_ID, STRANGER_PH)], did_hash_of)
check("a plain send needs no DID's approval", approvals, set())
check("and is one spend", len(spends), 1)

conds = run_spend(spends[0])
made = [(bytes32(c.vars[0]), int.from_bytes(c.vars[1], "big")) for c in conds.get(CREATE_COIN, [])]
odd = [entry for entry in made if entry[1] % 2 == 1]
check("exactly one NFT coin comes out", len(odd), 1)
# The layers rebuild the NFT around whatever puzzle hash the lock named, keeping
# metadata, royalties and owner. So the new coin must be the same NFT at the
# stranger's puzzle -- not a bare payment, and not a different NFT.
check("it lands as the same NFT at its new home", odd[0][0], nft_puzzle(None, STRANGER).get_tree_hash())
check("carrying one mojo, as a singleton must", odd[0][1], 1)
check("nothing else is created", len(made), 1)

# The lock's own gate is still there, under both NFT layers: the deposit puzzle
# refuses to run unless the lock's singleton announces this exact coin with this
# exact delegated puzzle. Everything else the NFT demands is on top of that.
def lock_gate(spend: vault.FundsSpend) -> bytes:
    message = vault.funds_announcement(spend.coin, spend.delegated_puzzle)
    return hashlib.sha256(bytes(vault.vault_puzzle(LOCK, POLICY).get_tree_hash()) + bytes(message)).digest()


demanded = [bytes(c.vars[0]) for c in conds.get(ASSERT_PUZZLE_ANNOUNCEMENT, [])]
check("the lock still has to authorise the spend", demanded, [lock_gate(spends[0])])


# ─── putting it under the lock's DID ─────────────────────────────────────────

spends, approvals = nfts.build_nft_spends(NODE, STATE, [nfts.NftMove(NFT_ID, None, DID_ID)], did_hash_of)
check("an owner change needs that DID to be spent too", approvals, {DID_ID})

conds = run_spend(spends[0])
made = [(bytes32(c.vars[0]), int.from_bytes(c.vars[1], "big")) for c in conds.get(CREATE_COIN, []) if int.from_bytes(c.vars[1], "big") % 2 == 1]
check("the NFT stays with the lock", len(made), 1)
check("now owned by the DID", made[0][0], nft_puzzle(DID_ID, DEPOSIT).get_tree_hash())

# The demand, and then the thing that satisfies it. This is the pair that has to
# agree; everything else about an owner change is bookkeeping.
demanded = [bytes(c.vars[0]) for c in conds.get(ASSERT_PUZZLE_ANNOUNCEMENT, []) if bytes(c.vars[0]) != lock_gate(spends[0])]
check("one more approval is demanded, beyond the lock's own", len(demanded), 1)

approval = vault.publish_did_spend(DID_COIN, vault.SingletonHost(DID_ID, DID_INNER, DID_LINEAGE), DEPOSIT_PH, [nfts.approval_condition(NFT_ID)])
did_p2_solution = vault.funds_solution(POLICY.inner_puzzle_hash(), approval.delegated_puzzle, DID_COIN)
did_spend = make_spend(DID_COIN, approval.host.puzzle(), approval.host.solution(did_p2_solution, 1))
did_conds = conditions_dict_for_solution(did_spend.puzzle_reveal, did_spend.solution, vault.MAX_CLVM_COST)
announced = [bytes(c.vars[0]) for c in did_conds.get(CREATE_PUZZLE_ANNOUNCEMENT, [])]
check("the DID announces the NFT's launcher id", NFT_ID in [bytes32(a) for a in announced], True)

# Consensus matches a puzzle announcement by sha256(puzzle_hash || message), so
# that is what is compared here rather than the message alone.
satisfied = {hashlib.sha256(bytes(DID_FULL.get_tree_hash()) + a).digest() for a in announced}
check("and that announcement is the one the NFT is waiting for", demanded[0] in satisfied, True)

check("the DID also recreates itself", any(int.from_bytes(c.vars[1], "big") % 2 == 1 for c in did_conds.get(CREATE_COIN, [])), True)


# ─── sending it and assigning it in the same move ────────────────────────────

spends, approvals = nfts.build_nft_spends(NODE, STATE, [nfts.NftMove(NFT_ID, STRANGER_PH, DID_ID)], did_hash_of)
conds = run_spend(spends[0])
made = [bytes32(c.vars[0]) for c in conds.get(CREATE_COIN, []) if int.from_bytes(c.vars[1], "big") % 2 == 1]
check("one move can do both", made, [nft_puzzle(DID_ID, STRANGER).get_tree_hash()])
check("and still needs the DID's yes", approvals, {DID_ID})


# ─── what it refuses ─────────────────────────────────────────────────────────

check(
    "an NFT the lock does not hold",
    refused(lambda: nfts.build_nft_spends(NODE, STATE, [nfts.NftMove(bytes32([0xAB] * 32), STRANGER_PH)], did_hash_of)).startswith("refused"),
    True,
)
check(
    "giving it a DID and taking it out of one at once",
    refused(lambda: nfts.parse_moves([{"launcher_id": NFT_ID.hex(), "did": DID_ID.hex(), "clear_did": True}])).startswith("refused"),
    True,
)
check(
    "the same NFT twice in one proposal",
    refused(lambda: nfts.parse_moves([{"launcher_id": NFT_ID.hex(), "puzzle_hash": STRANGER_PH.hex()}, {"launcher_id": NFT_ID.hex(), "clear_did": True}])).startswith("refused"),
    True,
)
check(
    "a move that neither sends it nor changes its owner",
    refused(lambda: nfts.parse_moves([{"launcher_id": NFT_ID.hex()}])).startswith("refused"),
    True,
)
check(
    "clearing an owner it does not have",
    refused(lambda: nfts.build_nft_spends(NODE, STATE, [nfts.NftMove(NFT_ID, None, None, True)], did_hash_of)).startswith("refused"),
    True,
)

# A DID with no unspent coin cannot say yes to anything, and finding that out at
# proposal time is much better than after the owners have signed.
SPENT_DID = FakeNode(did_spent=True)
check(
    "a DID with nothing left to spend cannot approve",
    refused(lambda: nfts.resolve_did_now(SPENT_DID, LOCK, DID_ID)).startswith("refused"),
    True,
)

# The reconstruction guard: a coin whose puzzle does not match what its parent's
# spend implies is not spent on a guess.
class WrongParent(FakeNode):
    def rpc(self, route: str, payload: dict) -> dict:
        if route == "get_coin_records_by_hint":
            # Same lineage, but the coin on chain carries a different puzzle.
            impostor = Coin(NFT_PARENT.name(), nft_puzzle(DID_ID, DEPOSIT).get_tree_hash(), uint64(1))
            return {"success": True, "coin_records": [record(impostor)]}
        return super().rpc(route, payload)


check(
    "a coin that does not hash to its rebuilt puzzle",
    refused(lambda: nfts.resolve_owned_nft(WrongParent(), LOCK, NFT_ID)).startswith("refused"),
    True,
)


# ─── the DID as it stands now, eve or long since published ───────────────────

did_coin, did_host = nfts.resolve_did_now(NODE, LOCK, DID_ID)
check("the DID's live coin is found", did_coin.name(), DID_COIN.name())
check("and its puzzle hashes to that coin", did_host.puzzle().get_tree_hash(), bytes32(DID_COIN.puzzle_hash))
check("its inner puzzle is what the NFT will be told to expect", did_host.inner_puzzle.get_tree_hash(), did_hash_of(DID_ID))


# ─── through the command surface, alongside everything else ──────────────────
#
# The point of doing this as an ordinary proposal: the NFT spend, the DID's
# approval and a payment all ride in one plan under one delegated puzzle, so the
# owners are asked for one signature covering the lot.

vault.state_from_payload = lambda _node, _payload: STATE


def run(payload: dict) -> dict:
    return vault.run("propose", {"network": "testnet11", "launcher_id": LOCK.hex(), **payload}, lambda _url: NODE)


built = run({"nft": [{"launcher_id": NFT_ID.hex(), "did": DID_ID.hex()}]})
plan = vault.VaultPlan.from_json(built["plan"])
layers = sorted((f.host.layers if f.host else f.kind) for f in plan.funds)
check("the NFT and the approving DID are both in the plan", layers, ["did", "nft"])
check("and the summary counts the NFT move", built["plan"]["summary"]["nft_moves"], 1)
check("one message covers both", len(built["messages"]), 1)

# The DID's spend must carry the announcement the NFT is waiting for, built by
# the tool rather than by the test.
did_spend = next(f for f in plan.funds if f.host is not None and f.host.layers == "did")
did_conditions = did_spend.delegated_puzzle.run(Program.to(0)).as_iter()
announced = [bytes32(c.at("rf").as_atom()) for c in did_conditions if c.first().as_int() == 62]
check("the tool made the DID announce this NFT", announced, [NFT_ID])

# And it composes with a payment, which is the whole reason NFT work is a
# proposal rather than a lane of its own.
STRANGER_ADDRESS = tool.encode_puzzle_hash(bytes32([0x77] * 32), "txch")
mixed = run({
    "nft": [{"launcher_id": NFT_ID.hex(), "puzzle_hash": STRANGER_PH.hex()}],
    "outputs": [{"puzzle_hash": DEPOSIT_PH.hex(), "amount": 1}],
})
check("an NFT move and a payment share one vote", len(vault.VaultPlan.from_json(mixed["plan"]).funds) >= 2, True)
check("still one message", len(mixed["messages"]), 1)

# An address is what the panel actually sends, so it has to arrive as the same
# destination a raw puzzle hash would.
by_address = run({"nft": [{"launcher_id": NFT_ID.hex(), "address": STRANGER_ADDRESS}]})
by_hash = run({"nft": [{"launcher_id": NFT_ID.hex(), "puzzle_hash": bytes32([0x77] * 32).hex()}]})
check(
    "an address and its puzzle hash mean the same move",
    vault.VaultPlan.from_json(by_address["plan"]).funds[0].delegated_puzzle,
    vault.VaultPlan.from_json(by_hash["plan"]).funds[0].delegated_puzzle,
)
check(
    "an address on the wrong network is refused",
    refused(lambda: nfts.parse_moves([{"launcher_id": NFT_ID.hex(), "address": "xch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"}], "txch")).startswith("refused"),
    True,
)

print(f"vault nft: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
