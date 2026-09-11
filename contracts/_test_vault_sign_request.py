"""What is put in front of a wallet to sign, and what is kept away from it.

A lock's bundle is mostly things a wallet has no business being handed. The
funds coins are authorised by the singleton's announcement and carry no
signature condition; a launcher spend has neither a signature nor a puzzle any
wallet recognises. Only the singleton spend, where the revealed members emit
``AGG_SIG_ME``, needs the owner at all.

Forge sent all of it. Sage was seen to crash on the launcher spend — no prompt,
no signature, no error — which from the outside is indistinguishable from a
relay that never delivered the request. So the rule this suite pins is: a wallet
is handed exactly the spends that ask it for a signature, and nothing else.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER, SINGLETON_LAUNCHER_HASH
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


OWNER: G1Element = AugSchemeMPL.key_gen(bytes([3] * 32)).get_g1()
STRANGER: G1Element = AugSchemeMPL.key_gen(bytes([4] * 32)).get_g1()
AGG_SIG_ME = ConditionOpcode.AGG_SIG_ME
MESSAGE = bytes32([0x7A] * 32)


def spend_asking_for(key: G1Element) -> tuple[object, str]:
    """A standard wallet spend whose delegated puzzle asks `key` to sign."""
    puzzle = puzzle_for_pk(key)
    coin = Coin(bytes32([0x01] * 32), puzzle.get_tree_hash(), uint64(5))
    conditions = [[AGG_SIG_ME, bytes(key), MESSAGE]]
    return make_spend(coin, puzzle, vault.standard_solution(conditions)), 'asks for a signature'


def spend_asking_nothing() -> object:
    """A funds coin: authorised by an announcement, signed by nobody."""
    puzzle = Program.to((1, [[ConditionOpcode.CREATE_COIN, bytes32([0x09] * 32), 1]]))
    coin = Coin(bytes32([0x02] * 32), puzzle.get_tree_hash(), uint64(7))
    return make_spend(coin, puzzle, Program.to(0))


def launcher_spend() -> object:
    """The rider. No signature, and a puzzle no wallet is meant to interpret."""
    coin = Coin(bytes32([0x03] * 32), SINGLETON_LAUNCHER_HASH, uint64(1))
    solution = Program.to([bytes32([0x0A] * 32), 1, []])
    return make_spend(coin, Program.from_bytes(bytes(SINGLETON_LAUNCHER)), solution)


owner_spend, _ = spend_asking_for(OWNER)
stranger_spend, _ = spend_asking_for(STRANGER)
funds = spend_asking_nothing()
launcher = launcher_spend()
BUNDLE = [owner_spend, funds, launcher, stranger_spend]


def ids(spends) -> list[str]:
    return [s.coin.name().hex() for s in spends]


chosen = vault.spends_needing_signature(BUNDLE, [OWNER])
check("only the spend that asks this owner is chosen", ids(chosen), ids([owner_spend]))
check("the funds coin is kept from the wallet", funds.coin.name().hex() in ids(chosen), False)
check("the launcher is kept from the wallet", launcher.coin.name().hex() in ids(chosen), False)
check("another owner's spend is kept from the wallet", stranger_spend.coin.name().hex() in ids(chosen), False)

both = vault.spends_needing_signature(BUNDLE, [OWNER, STRANGER])
check("two keys bring two spends", len(both), 2)
check("and still not the launcher", launcher.coin.name().hex() in ids(both), False)

check("a key nobody asks for chooses nothing", vault.spends_needing_signature(BUNDLE, []), [])
check("an empty bundle chooses nothing", vault.spends_needing_signature([], [OWNER]), [])

# A spend that cannot be run is a spend a wallet cannot sign either, so it is
# skipped rather than raising and taking the whole request with it.
broken = make_spend(
    Coin(bytes32([0x04] * 32), bytes32([0x05] * 32), uint64(1)),
    Program.to(b"\x08"),        # (x) — raises on any solution
    Program.to(0),
)
check("an unrunnable spend does not break the selection", ids(vault.spends_needing_signature([broken, owner_spend], [OWNER])), ids([owner_spend]))


# ─── narrowing does not change what is signed ────────────────────────────────

# The point of the whole thing: AGG_SIG_ME binds its message to its own coin, so
# the condition the wallet sees is identical whether or not the rest of the
# bundle travelled with it. Anything else would mean a signature that no longer
# verifies against the plan.
from chia.consensus.condition_tools import conditions_dict_for_solution  # noqa: E402


def sig_conditions(spend) -> list[tuple[str, str]]:
    conditions = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, vault.MAX_CLVM_COST)
    return [
        (bytes(cvp.vars[0]).hex(), bytes(cvp.vars[1]).hex())
        for cvp in conditions.get(AGG_SIG_ME, [])
    ]


check(
    "the signature asked for is the same spend, alone or in company",
    sig_conditions(chosen[0]),
    sig_conditions(owner_spend),
)
# A standard puzzle also asks for its own synthetic key over the delegated
# puzzle hash, so the owner's condition is one of several — what matters is that
# it is there and unchanged.
check(
    "and it names this owner over this message",
    (bytes(OWNER).hex(), MESSAGE.hex()) in sig_conditions(chosen[0]),
    True,
)


# ─── partial or not is a fact about the spends, not a guess ──────────────────

check("one key asked for is one key", vault.signature_keys([owner_spend]) == {bytes(OWNER)} | {
    # the standard puzzle also asks for its own synthetic key
    k for k in vault.signature_keys([owner_spend]) if k != bytes(OWNER)
}, True)
check(
    "a spend asking two owners needs a partial signature",
    len(vault.signature_keys([owner_spend, stranger_spend])) > len(vault.signature_keys([owner_spend])),
    True,
)
check("a spend asking nobody asks for no keys", vault.signature_keys([funds, launcher]), set())


# ─── re-signing is safe, which is what makes a lost answer recoverable ───────

# The interface tells a user that pressing Sign again after a lost response is
# harmless. That rests on BLS being deterministic: the same key over the same
# message makes the same signature, so a repeat cannot conflict with a share
# that did arrive. Worth pinning, because the advice is only true if it is.
SECRET = AugSchemeMPL.key_gen(bytes([11] * 32))
MSG = b"the same proposal, asked twice"
check(
    "signing the same proposal twice makes the same signature",
    bytes(AugSchemeMPL.sign(SECRET, MSG)),
    bytes(AugSchemeMPL.sign(SECRET, MSG)),
)
check(
    "and a different proposal makes a different one",
    bytes(AugSchemeMPL.sign(SECRET, MSG)) != bytes(AugSchemeMPL.sign(SECRET, MSG + b"!")),
    True,
)


print(f"sign request: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
