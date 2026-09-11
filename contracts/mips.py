"""MIPS: the member/restriction puzzle standard Chia Network ships.

Forge's locks were hand-rolled: ``delegated_puzzle_feeder`` over ``m_of_n``
over bare ``bls_member`` leaves. That is valid CLVM and it works, but it is
*our* shape, so every new member kind and every guard would have been ours to
write and ours to audit. CNI already ships both, and the wallet SDK, the
custody tool and the Chia Cloud Wallet all speak one composition. This module
reproduces that composition in Python, byte for byte, so a Forge lock is a
vault those tools can read.

The composition, from ``chia-sdk-driver``'s ``mips_puzzle_hash``:

    INDEX_WRAPPER(nonce,
      [DELEGATED_PUZZLE_FEEDER]        # only at the top level
        ( [RESTRICTIONS(member_validators, delegated_puzzle_validators)]
            ( inner ) ) )

``INDEX_WRAPPER`` is ``(a 5 7)`` — apply the curried inner puzzle to the
solution, unchanged. It exists only to give the same member a different hash at
a different position, so two owners may share a key without colliding in the
merkle tree.

The M-of-N layer dispatches on the threshold, and this is the part most easily
got wrong: **1-of-N is ``one_of_n``, N-of-N is ``n_of_n``, and only a strict
middle uses ``m_of_n``**. A 1-of-2 lock written with ``m_of_n`` hashes to a
different puzzle than the same 1-of-2 lock written the way CNI writes it.

Every hash in here is checked against ``chia-wallet-sdk`` in
``_test_mips.py``; the puzzles are checked by running them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, Sequence

from chia.types.blockchain_format.program import Program
from chia_rs.sized_bytes import bytes32
from chia.util.hash import std_hash
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER_HASH, SINGLETON_MOD_HASH
from chia_puzzles_py import programs as PUZZLES

# ─── The puzzles ─────────────────────────────────────────────────────────────

# Not in chia_puzzles_py: the SDK carries it inline. `(a 5 7)`.
INDEX_WRAPPER = Program.from_bytes(bytes.fromhex("ff02ff05ff0780"))

FEEDER = Program.from_bytes(PUZZLES.DELEGATED_PUZZLE_FEEDER)
RESTRICTIONS = Program.from_bytes(PUZZLES.RESTRICTIONS)
ONE_OF_N = Program.from_bytes(PUZZLES.ONE_OF_N)
M_OF_N = Program.from_bytes(PUZZLES.M_OF_N)
N_OF_N = Program.from_bytes(PUZZLES.N_OF_N)

BLS_MEMBER = Program.from_bytes(PUZZLES.BLS_MEMBER)
BLS_MEMBER_PUZZLE_ASSERT = Program.from_bytes(PUZZLES.BLS_MEMBER_PUZZLE_ASSERT)
SINGLETON_MEMBER = Program.from_bytes(PUZZLES.SINGLETON_MEMBER)
FIXED_PUZZLE_MEMBER = Program.from_bytes(PUZZLES.FIXED_PUZZLE_MEMBER)
SECP256K1_MEMBER = Program.from_bytes(PUZZLES.SECP256K1_MEMBER)
SECP256R1_MEMBER = Program.from_bytes(PUZZLES.SECP256R1_MEMBER)
PASSKEY_MEMBER = Program.from_bytes(PUZZLES.PASSKEY_MEMBER)

TIMELOCK = Program.from_bytes(PUZZLES.TIMELOCK)
PREVENT_CONDITION_OPCODE = Program.from_bytes(PUZZLES.PREVENT_CONDITION_OPCODE)
PREVENT_MULTIPLE_CREATE_COINS = Program.from_bytes(PUZZLES.PREVENT_MULTIPLE_CREATE_COINS)
ENFORCE_DPUZ_WRAPPERS = Program.from_bytes(PUZZLES.ENFORCE_DPUZ_WRAPPERS)
ADD_DPUZ_WRAPPER = Program.from_bytes(PUZZLES.ADD_DPUZ_WRAPPER)


class MipsError(Exception):
    """A member set or spend that MIPS cannot express."""


# ─── Restrictions ────────────────────────────────────────────────────────────


class RestrictionKind(IntEnum):
    """Where a restriction sits, matching the SDK's ``RestrictionKind``.

    ``MEMBER_CONDITION`` validates the conditions a member emits.
    ``DELEGATED_PUZZLE_HASH`` validates the delegated puzzle being run.
    ``DELEGATED_PUZZLE_WRAPPER`` is collected and enforced together by
    ``enforce_delegated_puzzle_wrappers``, so a wrapper cannot be dropped.
    """

    MEMBER_CONDITION = 0
    DELEGATED_PUZZLE_HASH = 1
    DELEGATED_PUZZLE_WRAPPER = 2


@dataclass(frozen=True)
class Restriction:
    """A validator and where it sits.

    ``puzzle`` is the validator itself. It is needed to *build* the restricted
    puzzle, because ``restrictions.clsp`` curries the validators as programs and
    applies them; ``puzzle_hash`` alone is enough to compute the hash.
    """

    kind: RestrictionKind
    puzzle_hash: bytes32
    puzzle: Program | None = None

    def to_json(self) -> dict[str, object]:
        return {"kind": int(self.kind), "puzzleHash": self.puzzle_hash.hex()}

    @staticmethod
    def from_json(value: object) -> "Restriction":
        if not isinstance(value, dict):
            raise MipsError("a restriction must be an object")
        raw_kind = value.get("kind")
        try:
            kind = RestrictionKind(int(raw_kind))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise MipsError(f"unknown restriction kind {raw_kind!r}") from None
        raw_hash = str(value.get("puzzleHash") or value.get("puzzle_hash") or "")
        body = raw_hash[2:] if raw_hash.lower().startswith("0x") else raw_hash
        if len(body) != 64:
            raise MipsError("a restriction needs a 32-byte puzzle hash")
        return Restriction(kind, bytes32.fromhex(body))


def timelock_restriction(seconds: int) -> tuple[Program, Restriction]:
    """Nothing spends before ``seconds`` have passed. A member-condition validator:
    it inspects what the member emits, so it binds that member, not the puzzle."""
    if int(seconds) <= 0:
        raise MipsError("a timelock needs a positive number of seconds")
    puzzle = TIMELOCK.curry(int(seconds))
    return puzzle, Restriction(RestrictionKind.MEMBER_CONDITION, puzzle.get_tree_hash(), puzzle)


def prevent_condition_opcode_restriction(opcode: int) -> tuple[Program, Restriction]:
    """This delegated puzzle may not emit ``opcode``. The Safe guard analogue:
    a signing view that cannot, say, re-key or reserve a fee."""
    puzzle = PREVENT_CONDITION_OPCODE.curry(int(opcode))
    return puzzle, Restriction(RestrictionKind.DELEGATED_PUZZLE_WRAPPER, puzzle.get_tree_hash(), puzzle)


def prevent_multiple_create_coins_restriction() -> tuple[Program, Restriction]:
    """At most one ``CREATE_COIN``: one payment per spend, no fan-out."""
    return (
        PREVENT_MULTIPLE_CREATE_COINS,
        Restriction(
            RestrictionKind.DELEGATED_PUZZLE_WRAPPER,
            PREVENT_MULTIPLE_CREATE_COINS.get_tree_hash(),
            PREVENT_MULTIPLE_CREATE_COINS,
        ),
    )


def _quoted(mod_hash: bytes32) -> bytes32:
    """``(q . <the puzzle with this hash>)`` hashed.

    ``enforce_dpuz_wrappers`` is curried with quoted *programs*, so the hash goes
    in directly rather than as an atom — this is ``(q . mod)``, not ``(q . hash)``.
    """
    from chia.wallet.util.curry_and_treehash import calculate_hash_of_quoted_mod_hash

    return calculate_hash_of_quoted_mod_hash(mod_hash)


def enforce_delegated_puzzle_wrappers(wrappers: Sequence[bytes32]) -> Program:
    """The validator the SDK appends whenever any wrapper restriction is present,
    so that dropping one wrapper fails instead of silently widening the spend."""
    return ENFORCE_DPUZ_WRAPPERS.curry(
        _quoted(ADD_DPUZ_WRAPPER.get_tree_hash()), [_quoted(w) for w in wrappers]
    )


# ─── The wrapping, hash and puzzle in step ───────────────────────────────────


@dataclass(frozen=True)
class MemberConfig:
    """The SDK's ``MemberConfig``: where a member sits and what binds it."""

    nonce: int = 0
    top_level: bool = False
    restrictions: tuple[Restriction, ...] = ()

    def with_nonce(self, nonce: int) -> "MemberConfig":
        return MemberConfig(int(nonce), self.top_level, self.restrictions)

    def with_top_level(self, top_level: bool) -> "MemberConfig":
        return MemberConfig(self.nonce, bool(top_level), self.restrictions)

    def with_restrictions(self, restrictions: Iterable[Restriction]) -> "MemberConfig":
        return MemberConfig(self.nonce, self.top_level, tuple(restrictions))


def _split_restrictions(
    restrictions: Sequence[Restriction],
) -> tuple[list[Restriction], list[Restriction]]:
    """Member validators and delegated-puzzle validators, with every wrapper
    folded into one ``enforce_delegated_puzzle_wrappers`` exactly as the SDK does."""
    member_validators: list[Restriction] = []
    delegated_validators: list[Restriction] = []
    wrappers: list[bytes32] = []
    for restriction in restrictions:
        if restriction.kind == RestrictionKind.MEMBER_CONDITION:
            member_validators.append(restriction)
        elif restriction.kind == RestrictionKind.DELEGATED_PUZZLE_HASH:
            delegated_validators.append(restriction)
        else:
            wrappers.append(restriction.puzzle_hash)
    if wrappers:
        enforcer = enforce_delegated_puzzle_wrappers(wrappers)
        delegated_validators.append(
            Restriction(
                RestrictionKind.DELEGATED_PUZZLE_HASH, enforcer.get_tree_hash(), enforcer
            )
        )
    return member_validators, delegated_validators


def mips_puzzle(config: MemberConfig, inner: Program) -> Program:
    """``inner`` wrapped the way MIPS wraps it. The hash of this equals
    :func:`mips_puzzle_hash` of ``inner.get_tree_hash()``."""
    puzzle = inner
    if config.restrictions:
        member_validators, delegated_validators = _split_restrictions(config.restrictions)
        missing = [r for r in member_validators + delegated_validators if r.puzzle is None]
        if missing:
            raise MipsError("building a restricted puzzle needs each validator's program, not just its hash")
        puzzle = RESTRICTIONS.curry(
            [r.puzzle for r in member_validators],
            [r.puzzle for r in delegated_validators],
            puzzle,
        )
    if config.top_level:
        puzzle = FEEDER.curry(puzzle)
    return INDEX_WRAPPER.curry(config.nonce, puzzle)


def mips_puzzle_hash(config: MemberConfig, inner_hash: bytes32) -> bytes32:
    """The same wrapping, from a hash alone. This is the SDK's ``mips_puzzle_hash``."""
    from chia.wallet.util.curry_and_treehash import (
        calculate_hash_of_quoted_mod_hash,
        curry_and_treehash,
        shatree_atom,
    )

    puzzle_hash = inner_hash
    if config.restrictions:
        member_validators, delegated_validators = _split_restrictions(config.restrictions)
        quoted = calculate_hash_of_quoted_mod_hash(RESTRICTIONS.get_tree_hash())
        # The validators are curried as *programs*, so each list's tree hash is
        # built from the validators' own puzzle hashes, not from atoms holding them.
        puzzle_hash = curry_and_treehash(
            quoted,
            _list_tree_hash([r.puzzle_hash for r in member_validators]),
            _list_tree_hash([r.puzzle_hash for r in delegated_validators]),
            puzzle_hash,
        )
    if config.top_level:
        quoted = calculate_hash_of_quoted_mod_hash(FEEDER.get_tree_hash())
        puzzle_hash = curry_and_treehash(quoted, puzzle_hash)
    quoted = calculate_hash_of_quoted_mod_hash(INDEX_WRAPPER.get_tree_hash())
    return curry_and_treehash(quoted, shatree_atom(_int_bytes(config.nonce)), puzzle_hash)


def _int_bytes(value: int) -> bytes:
    """CLVM's atom encoding for a small non-negative int, so ``0`` is ``()``."""
    return bytes(Program.to(int(value)).as_atom() or b"")


# ─── Members ─────────────────────────────────────────────────────────────────


def bls_member(public_key: bytes, fast_forward: bool = False) -> Program:
    """A BLS key. ``fast_forward`` picks the puzzle-assert variant, which signs
    against the puzzle hash instead of the coin id, so a signature survives the
    coin being re-created (what CNI calls fast-forward)."""
    mod = BLS_MEMBER_PUZZLE_ASSERT if fast_forward else BLS_MEMBER
    return mod.curry(bytes(public_key))


def singleton_member(launcher_id: bytes32) -> Program:
    """**The member that makes a treasure chest possible.** Authority is held by
    whoever can spend a singleton — an NFT, a DID, another lock — so ownership
    transfers by sending that singleton, with no re-key and no new address."""
    return SINGLETON_MEMBER.curry(
        Program.to((SINGLETON_MOD_HASH, (launcher_id, SINGLETON_LAUNCHER_HASH)))
    )


def fixed_puzzle_member(puzzle_hash: bytes32) -> Program:
    """Authority held by a fixed puzzle: a plain wallet address, a burn, a
    keeper's scoped puzzle. No signature of its own."""
    return FIXED_PUZZLE_MEMBER.curry(puzzle_hash)


def member_hash(config: MemberConfig, inner: Program) -> bytes32:
    return mips_puzzle_hash(config, inner.get_tree_hash())


# ─── The merkle tree MIPS proves against ─────────────────────────────────────

_LEAF = bytes([1])
_PAIR = bytes([2])


def _hash_leaf(leaf: bytes32) -> bytes32:
    return bytes32(std_hash(_LEAF + leaf))


def _hash_pair(left: bytes32, right: bytes32) -> bytes32:
    return bytes32(std_hash(_PAIR + left + right))


def merkle_root(leaves: Sequence[bytes32]) -> bytes32:
    """Chia's tree shape: ceil split, leaves prefixed 1, nodes prefixed 2."""
    if not leaves:
        raise MipsError("a merkle tree needs at least one leaf")
    if len(leaves) == 1:
        return _hash_leaf(leaves[0])
    mid = math.ceil(len(leaves) / 2)
    return _hash_pair(merkle_root(leaves[:mid]), merkle_root(leaves[mid:]))


def merkle_proof(leaves: Sequence[bytes32], leaf: bytes32) -> Program:
    """``(path sibling …)`` — the proof shape ``one_of_n`` and ``p2_1_of_n`` want.

    The path's bit *i* says whether the leaf was on the right at level *i*,
    counting from the leaf upward, which is the order the siblings are listed in.
    """
    path, siblings = _proof_parts(leaves, leaf)
    return Program.to([path] + siblings)


def _proof_parts(leaves: Sequence[bytes32], leaf: bytes32) -> tuple[int, list[bytes32]]:
    if len(leaves) == 1:
        if leaves[0] != leaf:
            raise MipsError("that member is not in this policy")
        return 0, []
    mid = math.ceil(len(leaves) / 2)
    left, right = leaves[:mid], leaves[mid:]
    if leaf in left:
        path, siblings = _proof_parts(left, leaf)
        return path, siblings + [merkle_root(right)]
    path, siblings = _proof_parts(right, leaf)
    return path | (1 << len(siblings)), siblings + [merkle_root(left)]


# ─── M-of-N: the dispatch that has to match ──────────────────────────────────


@dataclass(frozen=True)
class MofN:
    """A threshold over member hashes, written the way CNI writes it."""

    required: int
    items: tuple[bytes32, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise MipsError("a policy needs at least one member")
        if not 1 <= self.required <= len(self.items):
            raise MipsError(f"threshold {self.required} is not between 1 and {len(self.items)}")
        if len(set(self.items)) != len(self.items):
            raise MipsError("two members hash the same; give them different nonces")

    @property
    def root(self) -> bytes32:
        return merkle_root(list(self.items))

    def inner_puzzle_hash(self) -> bytes32:
        from chia.wallet.util.curry_and_treehash import (
            calculate_hash_of_quoted_mod_hash,
            curry_and_treehash,
            shatree_atom,
        )

        if self.required == 1:
            quoted = calculate_hash_of_quoted_mod_hash(ONE_OF_N.get_tree_hash())
            return curry_and_treehash(quoted, shatree_atom(self.root))
        if self.required == len(self.items):
            quoted = calculate_hash_of_quoted_mod_hash(N_OF_N.get_tree_hash())
            return curry_and_treehash(quoted, _list_tree_hash(self.items))
        quoted = calculate_hash_of_quoted_mod_hash(M_OF_N.get_tree_hash())
        return curry_and_treehash(
            quoted, shatree_atom(_int_bytes(self.required)), shatree_atom(self.root)
        )

    def puzzle(self, revealed: dict[bytes32, Program]) -> Program:
        """The threshold puzzle. ``n_of_n`` curries the member puzzles themselves,
        so it needs every one of them revealed; the merkle forms need none."""
        if self.required == 1:
            return ONE_OF_N.curry(self.root)
        if self.required == len(self.items):
            missing = [h for h in self.items if h not in revealed]
            if missing:
                raise MipsError("an n-of-n spend must reveal every member")
            return N_OF_N.curry([revealed[h] for h in self.items])
        return M_OF_N.curry(self.required, self.root)

    def solution(self, revealed: dict[bytes32, tuple[Program, Program]]) -> Program:
        """``revealed`` maps a member hash to its (puzzle, solution)."""
        if len(revealed) != self.required:
            raise MipsError(f"this policy needs exactly {self.required} members, got {len(revealed)}")
        unknown = [h for h in revealed if h not in self.items]
        if unknown:
            raise MipsError("a revealed member is not in this policy")
        if self.required == 1:
            leaf = next(iter(revealed))
            puzzle, inner_solution = revealed[leaf]
            return Program.to([merkle_proof(list(self.items), leaf), puzzle, inner_solution])
        if self.required == len(self.items):
            return Program.to([[revealed[h][1] for h in self.items]])
        return Program.to([_partial_tree(list(self.items), revealed)])


def _list_tree_hash(items: Sequence[bytes32]) -> bytes32:
    from chia.wallet.util.curry_and_treehash import shatree_atom, shatree_pair

    result = shatree_atom(b"")
    for item in reversed(items):
        result = shatree_pair(item, result)
    return result


def _partial_tree(
    leaves: Sequence[bytes32], revealed: dict[bytes32, tuple[Program, Program]]
) -> Program | bytes32:
    """``m_of_n``'s proof: revealed leaves become ``(() puzzle . solution)``,
    everything else collapses to its hash."""
    if len(leaves) == 1:
        leaf = leaves[0]
        if leaf in revealed:
            puzzle, solution = revealed[leaf]
            return Program.to(([], (puzzle, solution)))
        return _hash_leaf(leaf)
    mid = math.ceil(len(leaves) / 2)
    left = _partial_tree(leaves[:mid], revealed)
    right = _partial_tree(leaves[mid:], revealed)
    if isinstance(left, bytes) and isinstance(right, bytes):
        return _hash_pair(bytes32(left), bytes32(right))
    return Program.to((left, right))


# ─── The whole custody puzzle ────────────────────────────────────────────────


@dataclass(frozen=True)
class Custody:
    """A vault's inner puzzle: an M-of-N of wrapped members, at the top level.

    ``members`` are the *wrapped* member hashes, in the order the merkle tree
    sees them. ``config`` is the outer config, which carries ``top_level``.
    """

    required: int
    members: tuple[bytes32, ...]
    config: MemberConfig = field(default=MemberConfig(top_level=True))

    def m_of_n(self) -> MofN:
        return MofN(self.required, self.members)

    def custody_hash(self) -> bytes32:
        """What ``VaultInfo.custodyHash`` holds, and what the singleton commits to."""
        return mips_puzzle_hash(self.config, self.m_of_n().inner_puzzle_hash())

    def puzzle(self, revealed: dict[bytes32, Program]) -> Program:
        return mips_puzzle(self.config, self.m_of_n().puzzle(revealed))

    def solution(
        self,
        delegated_puzzle: Program,
        delegated_solution: Program,
        revealed: dict[bytes32, tuple[Program, Program]],
    ) -> Program:
        """``(delegated_puzzle delegated_solution . inner_solution)``.

        The index wrapper passes its solution through untouched, so it does not
        appear here; the feeder is what reads the delegated puzzle.
        """
        if not self.config.top_level:
            raise MipsError("only a top-level custody puzzle takes a delegated puzzle")
        if self.config.restrictions:
            raise MipsError("restrictions at the custody level need their own solutions")
        inner = self.m_of_n().solution(revealed)
        return Program.to((delegated_puzzle, (delegated_solution, inner)))
