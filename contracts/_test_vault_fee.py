"""The fee is the pusher's, and it is attached after the owners have signed.

Forge's own model note says the signer who pushes pays the fee. The code did
not: the fee came from a sponsor coin chosen when the proposal was BUILT, read
out of the proposer's wallet, and bound into the delegated puzzle the owners
signed. So the proposer paid, every proposal needed a wallet coin read before it
could exist, and a proposal with no sponsor carried no fee at all.

A fee spend attached at execute time cannot be inside what the owners signed --
it did not exist yet. What makes it safe anyway is a one-way binding: the lock
always announces its nonce, and the fee spend asserts that announcement. So the
fee spend is worthless in any other bundle, while the lock's spend does not
depend on it. This suite pins both halves of that.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk
from chia_rs import AugSchemeMPL, G1Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import vault_tool as vault

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
    except vault.MultisigError:
        return "refused"
    return "accepted"


OPCODES = {int.from_bytes(v, "big"): k for k, v in vars(ConditionOpcode).items() if isinstance(v, bytes)}
ASSERT_COIN_ANNOUNCEMENT = int.from_bytes(ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT, "big")
CREATE_COIN_ANNOUNCEMENT = int.from_bytes(ConditionOpcode.CREATE_COIN_ANNOUNCEMENT, "big")
RESERVE_FEE = int.from_bytes(ConditionOpcode.RESERVE_FEE, "big")
CREATE_COIN = int.from_bytes(ConditionOpcode.CREATE_COIN, "big")

SECRET = AugSchemeMPL.key_gen(bytes([7] * 32))
PUBKEY: G1Element = SECRET.get_g1()
WALLET_PUZZLE = puzzle_for_pk(PUBKEY)
NONCE = bytes32([0xAB] * 32)
TIP = Coin(bytes32([0x01] * 32), bytes32([0x02] * 32), uint64(1))


class FakePlan:
    """Only the two things a fee spend reads: the lock's tip coin and the nonce."""

    def __init__(self, tip: Coin, nonce: bytes32) -> None:
        self.tip = tip
        self.summary = {"nonce": nonce.hex()}


PLAN = FakePlan(TIP, NONCE)


def wallet_coin(amount: int) -> dict[str, object]:
    coin = Coin(bytes32([0x03] * 32), WALLET_PUZZLE.get_tree_hash(), uint64(amount))
    return {
        "coin": {
            "parent_coin_info": coin.parent_coin_info.hex(),
            "puzzle_hash": coin.puzzle_hash.hex(),
            "amount": int(coin.amount),
        },
        "puzzle": bytes(WALLET_PUZZLE).hex(),
    }


FEE = 1_127_500_000
built = vault.build_execution_fee(PLAN, [wallet_coin(10_000_000_000)], FEE)
check("one coin spend is built", len(built["coin_spends"]), 1)
check("the fee is what was asked for", built["fee"], FEE)
check("the change is the remainder", built["change"], 10_000_000_000 - FEE)
# The reported key is the SYNTHETIC one, which is what a standard wallet puzzle
# actually signs with -- reporting the raw key would send the client looking for
# a signature no wallet produces.
FEE_COIN = Coin(bytes32([0x03] * 32), WALLET_PUZZLE.get_tree_hash(), uint64(10_000_000_000))
check(
    "it names the key that must sign",
    built["pubkey"],
    vault.pubkey_hex(vault.sponsor_key_of(WALLET_PUZZLE, FEE_COIN)),
)
check("which is not the wallet's raw key", built["pubkey"] != vault.pubkey_hex(PUBKEY), True)

spend = vault.spend_from_json(built["coin_spends"][0])
conditions = [list(c.as_iter()) for c in Program.from_bytes(bytes(spend.puzzle_reveal)).run(Program.from_bytes(bytes(spend.solution))).as_iter()]
by_op: dict[int, list[list[Program]]] = {}
for condition in conditions:
    by_op.setdefault(int.from_bytes(condition[0].as_atom(), "big"), []).append(condition)

check("it reserves exactly the fee", [c[1].as_int() for c in by_op.get(RESERVE_FEE, [])], [FEE])
check("it returns the change to the same wallet", len(by_op.get(CREATE_COIN, [])), 1)
if by_op.get(CREATE_COIN):
    change = by_op[CREATE_COIN][0]
    check("to the coin's own puzzle hash", bytes32(change[1].as_atom()), WALLET_PUZZLE.get_tree_hash())
    check("for the remainder", change[2].as_int(), 10_000_000_000 - FEE)

# ─── the binding ─────────────────────────────────────────────────────────────

expected = vault.announcement_id(TIP.name(), NONCE)
asserted = [bytes32(c[1].as_atom()) for c in by_op.get(ASSERT_COIN_ANNOUNCEMENT, [])]
check("it asserts the lock's own announcement", asserted, [expected])
check(
    "which no other lock spend can satisfy",
    vault.announcement_id(Coin(bytes32([0x09] * 32), bytes32([0x02] * 32), uint64(1)).name(), NONCE) in asserted,
    False,
)
check(
    "and no other nonce can satisfy either",
    vault.announcement_id(TIP.name(), bytes32([0xCD] * 32)) in asserted,
    False,
)

# ─── what it refuses ─────────────────────────────────────────────────────────

check("a zero fee is refused", refused(lambda: vault.build_execution_fee(PLAN, [wallet_coin(10_000)], 0)), "refused")
check("no coins is refused", refused(lambda: vault.build_execution_fee(PLAN, [], FEE)), "refused")
check(
    "a coin too small for the fee is refused",
    refused(lambda: vault.build_execution_fee(PLAN, [wallet_coin(FEE - 1)], FEE)),
    "refused",
)
class PlanWithoutNonce:
    tip = TIP
    summary: dict[str, object] = {}


check(
    "a plan with no nonce is refused",
    refused(lambda: vault.build_execution_fee(PlanWithoutNonce(), [wallet_coin(10_000_000_000)], FEE)),
    "refused",
)

# The smallest sufficient coin is used, so a fee does not consume a large coin
# and leave the wallet fragmented.
many = [wallet_coin(50_000_000_000), wallet_coin(2_000_000_000), wallet_coin(9_000_000_000)]
picked = vault.build_execution_fee(PLAN, many, FEE)
check("the smallest sufficient coin pays", picked["change"], 2_000_000_000 - FEE)


print(f"vault fee: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
