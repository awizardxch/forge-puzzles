#!/usr/bin/env python3
"""The merkle tree over a V11 pool's action leaves, in chia-wallet-sdk's shape.

Both drivers must produce the same root from the same five leaves, so this
mirrors `MerkleTree` in chia-sdk-types exactly: the leaf list is split at
`(n + 1) // 2`, a leaf hashes as sha256(0x01 || leaf), a node as
sha256(0x02 || left || right), and a right child sets the path bit at its
depth (LSB is the level nearest the leaf). The proof is `(path . hashes)`, which
is what upstream's merkle_utils.rue walks.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


def _sha(*parts: bytes) -> bytes:
    return hashlib.sha256(b"".join(parts)).digest()


@dataclass(frozen=True)
class MerkleProof:
    path: int
    hashes: tuple[bytes, ...]

    def to_program_list(self) -> list:
        return [self.path, *self.hashes]


class MerkleTree:
    def __init__(self, leaves: list[bytes]):
        if not leaves:
            self.root = bytes(32)
            self.proofs: dict[bytes, MerkleProof] = {}
            return
        self.root, self.proofs = self._build(list(leaves))

    @classmethod
    def _build(cls, leaves: list[bytes]) -> tuple[bytes, dict[bytes, MerkleProof]]:
        if len(leaves) == 1:
            leaf = leaves[0]
            return _sha(b"\x01", leaf), {leaf: MerkleProof(0, ())}
        mid = (len(leaves) + 1) // 2
        left_root, left = cls._build(leaves[:mid])
        right_root, right = cls._build(leaves[mid:])
        proofs: dict[bytes, MerkleProof] = {}
        for leaf, p in left.items():
            proofs[leaf] = MerkleProof(p.path, (*p.hashes, right_root))
        for leaf, p in right.items():
            proofs[leaf] = MerkleProof(p.path | (1 << len(p.hashes)), (*p.hashes, left_root))
        return _sha(b"\x02", left_root, right_root), proofs

    def proof(self, leaf: bytes) -> MerkleProof:
        return self.proofs[leaf]


def verify(root: bytes, leaf: bytes, proof: MerkleProof) -> bool:
    """merkle_utils.rue's simplify_merkle_proof, for the harness."""
    current, path = _sha(b"\x01", leaf), proof.path
    for sibling in proof.hashes:
        current = _sha(b"\x02", sibling, current) if path & 1 else _sha(b"\x02", current, sibling)
        path >>= 1
    return current == root


# The five Forge leaves, in the order every driver must use.
# V11.1 adds the sixth leaf; the registry's six_leaf_root hard-codes this order.
LEAF_ORDER = ("forge_action_swap", "forge_action_add", "forge_action_remove",
              "forge_action_observe", "forge_action_collect", "forge_action_dao_fee")
