"""Our MIPS module against the one Chia ships.

Every expected hash in ``_mips_vectors.json`` was produced by
``chia-wallet-sdk``'s own bindings, not by this file, so a passing run means
a Forge lock and a Chia vault agree byte for byte. Regenerate the vectors with
``node scripts/gen-mips-vectors.mjs`` if the SDK is upgraded; a diff there is a
real change in the standard, not a test to relax.

The puzzles are also *run*, because matching hashes only proves we compose the
same tree — running proves the solutions we build satisfy it.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode
from chia_rs.sized_bytes import bytes32

import mips

VECTORS = json.loads((pathlib.Path(__file__).parent / "_mips_vectors.json").read_text())

PASSED = 0
FAILED: list[str] = []


def check(name: str, got: object, want: object) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}\n     got  {got}\n     want {want}")


def hexed(value: bytes) -> str:
    return bytes(value).hex()


KEYS = [bytes.fromhex(k) for k in VECTORS["keys"]]
PLAIN = mips.MemberConfig()
TOP = mips.MemberConfig(top_level=True)


# ─── members ─────────────────────────────────────────────────────────────────

for index, key in enumerate(KEYS):
    check(
        f"bls member {index}",
        hexed(mips.member_hash(PLAIN, mips.bls_member(key))),
        VECTORS["blsPlain"][index],
    )
    check(
        f"bls member fast-forward {index}",
        hexed(mips.member_hash(PLAIN, mips.bls_member(key, fast_forward=True))),
        VECTORS["blsFastForward"][index],
    )

for nonce, want in enumerate(VECTORS["blsNonce"]):
    check(
        f"bls member at nonce {nonce}",
        hexed(mips.member_hash(PLAIN.with_nonce(nonce), mips.bls_member(KEYS[0]))),
        want,
    )

check(
    "bls member at the top level",
    hexed(mips.member_hash(TOP, mips.bls_member(KEYS[0]))),
    VECTORS["blsTopLevel"],
)

LAUNCHER = bytes32.fromhex(VECTORS["launcherId"])
check(
    "singleton member",
    hexed(mips.member_hash(PLAIN, mips.singleton_member(LAUNCHER))),
    VECTORS["singletonMember"],
)
check(
    "singleton member at nonce 1",
    hexed(mips.member_hash(PLAIN.with_nonce(1), mips.singleton_member(LAUNCHER))),
    VECTORS["singletonMemberNonce1"],
)
check(
    "fixed puzzle member",
    hexed(mips.member_hash(PLAIN, mips.fixed_puzzle_member(bytes32.fromhex(VECTORS["fixedPuzzleHash"])))),
    VECTORS["fixedMember"],
)

# The wrapping applied to a hash and applied to a puzzle must agree, or a lock
# would be built one way and spent another.
for config, label in ((PLAIN, "plain"), (TOP, "top level"), (PLAIN.with_nonce(7), "nonce 7")):
    member = mips.bls_member(KEYS[0])
    check(
        f"puzzle and hash agree, {label}",
        hexed(mips.mips_puzzle(config, member).get_tree_hash()),
        hexed(mips.mips_puzzle_hash(config, member.get_tree_hash())),
    )


# ─── thresholds ──────────────────────────────────────────────────────────────

LEAVES = [bytes32.fromhex(h) for h in VECTORS["blsPlain"]]

check(
    "1-of-2",
    hexed(mips.mips_puzzle_hash(PLAIN, mips.MofN(1, tuple(LEAVES[:2])).inner_puzzle_hash())),
    VECTORS["oneOfTwo"],
)
for required, count, key in ((1, 2, "oneOfTwoTop"), (2, 2, "twoOfTwoTop"), (1, 3, "oneOfThreeTop"), (2, 3, "twoOfThreeTop"), (3, 3, "threeOfThreeTop")):
    custody = mips.Custody(required, tuple(LEAVES[:count]))
    check(f"{required}-of-{count} custody hash", hexed(custody.custody_hash()), VECTORS[key])

# The dispatch is the thing most easily got wrong, so pin it directly.
check("1-of-2 uses one_of_n", mips.MofN(1, tuple(LEAVES[:2])).puzzle({}).uncurry()[0], mips.ONE_OF_N)
check("2-of-3 uses m_of_n", mips.MofN(2, tuple(LEAVES)).puzzle({}).uncurry()[0], mips.M_OF_N)

# A 1-of-2 written with m_of_n, the way Forge used to, is a *different* lock.
legacy = mips.FEEDER.curry(mips.M_OF_N.curry(1, mips.merkle_root(LEAVES[:2]))).get_tree_hash()
check(
    "the legacy shape is not the CNI shape",
    legacy != mips.Custody(1, tuple(LEAVES[:2])).custody_hash(),
    True,
)


# ─── restrictions ────────────────────────────────────────────────────────────

timelock_puzzle, timelock = mips.timelock_restriction(60)
check("timelock puzzle hash", hexed(timelock.puzzle_hash), VECTORS["timelock"]["puzzleHash"])
check("timelock kind", int(timelock.kind), VECTORS["timelock"]["kind"])
check(
    "bls member with a timelock",
    hexed(mips.member_hash(PLAIN.with_restrictions([timelock]), mips.bls_member(KEYS[0]))),
    VECTORS["blsTimelock"],
)

_, prevent_multiple = mips.prevent_multiple_create_coins_restriction()
check(
    "prevent-multiple-create-coins kind",
    int(prevent_multiple.kind),
    VECTORS["preventMultipleCreateCoins"]["kind"],
)
check(
    "prevent-multiple-create-coins puzzle hash",
    hexed(prevent_multiple.puzzle_hash),
    VECTORS["preventMultipleCreateCoins"]["puzzleHash"],
)
check(
    "bls member that may create one coin",
    hexed(mips.member_hash(PLAIN.with_restrictions([prevent_multiple]), mips.bls_member(KEYS[0]))),
    VECTORS["blsPreventMultiple"],
)

_, prevent_create_coin = mips.prevent_condition_opcode_restriction(51)
check(
    "prevent-condition-opcode puzzle hash",
    hexed(prevent_create_coin.puzzle_hash),
    VECTORS["preventCreateCoin"]["puzzleHash"],
)
check(
    "three restrictions at once",
    hexed(
        mips.member_hash(
            PLAIN.with_restrictions([prevent_multiple, timelock, prevent_create_coin]),
            mips.bls_member(KEYS[0]),
        )
    ),
    VECTORS["blsBothRestrictions"],
)
check(
    "a restricted 1-of-2 custody puzzle",
    hexed(
        mips.mips_puzzle_hash(
            TOP.with_restrictions([timelock]),
            mips.MofN(1, tuple(LEAVES[:2])).inner_puzzle_hash(),
        )
    ),
    VECTORS["oneOfTwoTopTimelock"],
)


# ─── the merkle proofs, by running the puzzles ───────────────────────────────

AGG_SIG_ME = int.from_bytes(ConditionOpcode.AGG_SIG_ME, "big")
DELEGATED = Program.to((1, [[51, bytes32([9] * 32), 1]]))


def run_custody(required: int, count: int, signer: int) -> list[list[object]]:
    """Build and run a whole custody puzzle, returning its conditions."""
    # Each owner sits at its own nonce, which is how MIPS lets the same key hold
    # two seats; it also means these runs exercise the nonce in the leaf path.
    configs = [PLAIN.with_nonce(i) for i in range(count)]
    members = [mips.bls_member(KEYS[i % len(KEYS)]) for i in range(count)]
    leaves = tuple(mips.member_hash(configs[i], members[i]) for i in range(count))
    custody = mips.Custody(required, leaves)
    if required == count:
        chosen = list(range(count))
    else:
        chosen = [signer] + [i for i in range(count) if i != signer][: required - 1]
    revealed_puzzles = {leaves[i]: mips.mips_puzzle(configs[i], members[i]) for i in chosen}
    revealed = {leaves[i]: (revealed_puzzles[leaves[i]], Program.to(0)) for i in chosen}
    puzzle = custody.puzzle(revealed_puzzles)
    check(f"{required}-of-{count} puzzle matches its hash", hexed(puzzle.get_tree_hash()), hexed(custody.custody_hash()))
    solution = custody.solution(DELEGATED, Program.to(0), revealed)
    return [c.as_python() for c in puzzle.run(solution).as_iter()]


for required, count in ((1, 1), (1, 2), (1, 3), (2, 3), (2, 2), (3, 3), (1, 5), (3, 5), (5, 5)):
    for signer in range(count):
        if required == count and signer:
            continue  # n-of-n reveals everybody; one run says it all
        conditions = run_custody(required, count, signer)
        signatures = [c for c in conditions if int.from_bytes(c[0], "big") == AGG_SIG_ME]
        check(f"{required}-of-{count} signer {signer} asks for {required} signatures", len(signatures), required)
        signed = {bytes(c[1]).hex() for c in signatures}
        expected_message = DELEGATED.get_tree_hash()
        check(
            f"{required}-of-{count} signer {signer} signs the delegated puzzle hash",
            {bytes(c[2]) for c in signatures},
            {bytes(expected_message)},
        )
        if required < count:
            check(f"{required}-of-{count} signer {signer} is one of the signers", hexed(KEYS[signer % len(KEYS)]) in signed, True)
        payments = [c for c in conditions if int.from_bytes(c[0], "big") == 51]
        check(f"{required}-of-{count} signer {signer} runs the delegated puzzle", len(payments), 1)


# A member who is not in the policy cannot prove membership.
outsider = mips.member_hash(PLAIN, mips.bls_member(KEYS[2]))
try:
    mips.MofN(1, (mips.member_hash(PLAIN, mips.bls_member(KEYS[0])),)).solution(
        {outsider: (mips.mips_puzzle(PLAIN, mips.bls_member(KEYS[2])), Program.to(0))}
    )
    check("an outsider is refused", "accepted", "refused")
except mips.MipsError:
    check("an outsider is refused", "refused", "refused")

# Two identical members would collide in the tree; nonces are how MIPS keeps
# them apart, so the same key twice must be refused unless the nonces differ.
same = mips.member_hash(PLAIN, mips.bls_member(KEYS[0]))
try:
    mips.MofN(1, (same, same))
    check("a duplicate member is refused", "accepted", "refused")
except mips.MipsError:
    check("a duplicate member is refused", "refused", "refused")
check(
    "the same key at two nonces is two members",
    mips.member_hash(PLAIN, mips.bls_member(KEYS[0]))
    != mips.member_hash(PLAIN.with_nonce(1), mips.bls_member(KEYS[0])),
    True,
)

# A threshold outside 1..N is not a policy.
for required, count in ((0, 2), (3, 2), (-1, 2)):
    try:
        mips.MofN(required, tuple(LEAVES[:count]))
        check(f"{required}-of-{count} is refused", "accepted", "refused")
    except mips.MipsError:
        check(f"{required}-of-{count} is refused", "refused", "refused")


print(f"mips: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  ✗ " + failure)
sys.exit(1 if FAILED else 0)
