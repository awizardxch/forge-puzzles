"""A DID the lock owns: launched right, and spendable afterwards.

Forge shipped a DID whose owner was curried as a puzzle HASH instead of a
puzzle. It hashed fine, launched fine, confirmed fine, and can never be spent:
``did_innerpuz`` runs its owner with ``(a INNER_PUZZLE inner_solution)``, so an
atom there is run as a program and fails, while mode 0 is closed off by a
recovery list of length zero. Nothing on the way to the chain caught it, because
nothing ran the puzzle.

So this suite runs it. Every DID puzzle built here is executed against a real
solution and its conditions are read back, which is the only check that would
have failed before that DID was launched.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.did_wallet.did_wallet_puzzles import create_innerpuz
from chia.wallet.lineage_proof import LineageProof as SingletonLineageProof
from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton
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


def runs(puzzle: Program, solution: Program) -> Program | None:
    try:
        return puzzle.run(solution)
    except Exception:                                          # noqa: BLE001
        return None


CREATE_COIN = int.from_bytes(ConditionOpcode.CREATE_COIN, "big")
LOCK = bytes32([0x11] * 32)
DEPOSIT = vault.deposit_puzzle(LOCK)
DEPOSIT_HASH = DEPOSIT.get_tree_hash()

# The singleton layer checks that an eve coin's parent really is its launcher, so
# the launcher id and the lineage proof have to come from one real payer coin
# rather than from arbitrary bytes -- an inconsistent pair fails inside the
# singleton and would hide whether the DID layer itself is sound.
PAYER = Coin(bytes32([0x55] * 32), DEPOSIT_HASH, uint64(1_000_000))
DID_LAUNCHER = vault.generate_launcher_coin(PAYER, uint64(1)).name()
EVE_LINEAGE = SingletonLineageProof(PAYER.name(), None, uint64(1))


# ─── the owner must be a puzzle ──────────────────────────────────────────────

try:
    vault.did_inner_puzzle(Program.to(DEPOSIT_HASH), DID_LAUNCHER)
    check("a hash as the owner is refused", "accepted", "refused")
except vault.MultisigError:
    check("a hash as the owner is refused", "refused", "refused")

good = vault.did_inner_puzzle(DEPOSIT, DID_LAUNCHER)
inert = create_innerpuz(DEPOSIT_HASH, [], uint64(0), DID_LAUNCHER, Program.to([]))
check("the two shapes are different puzzles", good.get_tree_hash() != inert.get_tree_hash(), True)


# ─── and the difference is that only one of them runs ────────────────────────

DID_COIN = vault.did_eve_coin(LOCK, DID_LAUNCHER)
check(
    "the eve coin's puzzle hash matches the puzzle built for it",
    DID_COIN.puzzle_hash,
    puzzle_for_singleton(DID_LAUNCHER, good).get_tree_hash(),
)
check(
    "the inert shape lands at a different coin",
    vault.did_eve_coin(LOCK, DID_LAUNCHER, owner_as_hash=True).name() != DID_COIN.name(),
    True,
)

HOST = vault.SingletonHost(DID_LAUNCHER, good, EVE_LINEAGE)
SPEND = vault.publish_did_spend(DID_COIN, HOST, DEPOSIT_HASH)
LOCK_INNER = bytes32([0x44] * 32)
P2_SOLUTION = vault.funds_solution(LOCK_INNER, SPEND.delegated_puzzle, DID_COIN)

check("the good DID runs its owner", runs(good, Program.to([1, P2_SOLUTION])) is not None, True)
check("the inert DID cannot run at all", runs(inert, Program.to([1, P2_SOLUTION])), None)
# Mode 0 is the only other way in, and an empty recovery list closes it.
check("the inert DID cannot recover either", runs(inert, Program.to([0, 1, DEPOSIT_HASH])), None)


# ─── what the publish spend actually says ────────────────────────────────────

conditions = runs(good, Program.to([1, P2_SOLUTION]))
assert conditions is not None
parsed = [list(c.as_iter()) for c in conditions.as_iter()]
creates = [c for c in parsed if int.from_bytes(c[0].as_atom(), "big") == CREATE_COIN]
check("the publish spend creates exactly one coin", len(creates), 1)
if creates:
    created = creates[0]
    # The DID recreates itself: same inner puzzle hash, same amount. Nothing about
    # the identity changes; the only new thing on chain is the memo.
    check("it recreates the same DID", bytes32(created[1].as_atom()), good.get_tree_hash())
    check("at the same amount", created[2].as_int(), int(DID_COIN.amount))
    memos = [bytes(m.as_atom()) for m in created[3].as_iter()] if len(created) > 3 else []
    check("hinted to the lock's own address", memos, [bytes(DEPOSIT_HASH)])

# The whole singleton spend has to run too, not just the DID layer.
full = HOST.puzzle()
check("the singleton puzzle matches the coin", full.get_tree_hash(), DID_COIN.puzzle_hash)
check("the whole singleton spend runs", runs(full, HOST.solution(P2_SOLUTION, int(DID_COIN.amount))) is not None, True)


# ─── the plan carries it across its JSON round trip ──────────────────────────

restored = vault.FundsSpend.from_json(SPEND.to_json())
check("a DID spend survives serialisation", restored.kind, "did")
check("with its launcher", restored.host.launcher_id if restored.host else None, DID_LAUNCHER)
check(
    "and rebuilds the same puzzle",
    restored.host.puzzle().get_tree_hash() if restored.host else None,
    DID_COIN.puzzle_hash,
)
check(
    "and the same delegated puzzle",
    restored.delegated_puzzle.get_tree_hash(),
    SPEND.delegated_puzzle.get_tree_hash(),
)


# ─── a launch built by the action is the runnable shape ──────────────────────

build = vault.did_action(DEPOSIT, "Forge Protocol")
built_conditions, riders = build(PAYER)
check("the launch carries one rider, the launcher spend", len(riders), 1)
launcher_id = riders[0].coin.name()
launched_inner = vault.did_inner_puzzle(DEPOSIT, launcher_id)
launched_eve = Coin(launcher_id, puzzle_for_singleton(launcher_id, launched_inner).get_tree_hash(), uint64(1))
check(
    "the eve coin the launch produces is the one the resolver looks for",
    launched_eve.name(),
    vault.did_eve_coin(LOCK, launcher_id).name(),
)
check("the launch derives the same launcher the eve coin names", launcher_id, DID_LAUNCHER)
# The check that would have caught the shipped bug: run what the launch created.
launched_spend = vault.publish_did_spend(
    launched_eve,
    vault.SingletonHost(launcher_id, launched_inner, SingletonLineageProof(PAYER.name(), None, uint64(1))),
    DEPOSIT_HASH,
)
check(
    "and it is spendable",
    runs(launched_inner, Program.to([1, vault.funds_solution(LOCK_INNER, launched_spend.delegated_puzzle, launched_eve)])) is not None,
    True,
)
check("the launcher names the DID", vault.DID_TAG in bytes(riders[0].solution), True)


print(f"vault did: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
