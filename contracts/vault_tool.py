"""Forge vault locks: CNI's vault puzzles, so a lock keeps its identity and
its deposit address while its owners change by vote.

The lock is a **singleton**. Its inner puzzle is CNI's
``delegated_puzzle_feeder`` wrapping ``m_of_n``, curried with M and the
merkle root of the owners' ``bls_member`` puzzles. Every spend of the
singleton reveals M members with merkle proofs and runs a delegated puzzle;
each revealed member emits ``AGG_SIG_ME(owner_key, delegated_puzzle_hash)``,
which the owner's Sage signs as it signs anything else.

Funds never sit in the singleton. They sit in ``p2_singleton_via_delegated_puzzle``
coins whose puzzle is curried with the singleton's identity (launcher id) —
the **deposit address**, fixed for the life of the lock. Such a coin spends
only when the singleton, in the same bundle, announces exactly that coin and
that delegated puzzle. So every payment is a singleton spend (the vote) plus
the funds spends it authorizes.

Changing owners is a singleton spend whose delegated puzzle recreates the
singleton with a new inner puzzle hash: a new M and a new merkle root. Nothing
else moves. The new policy rides in the recreated coin's memos, and the
launcher's key/value list carries the first one, so anyone can walk the
lineage from the launcher id and see every policy the lock ever had, on chain.

Two conventions make a vault findable from its deposit address: at launch a
1-mojo **pointer** coin is created at the deposit address with memos
``[hint, "forge-vault/1", launcher_id]``; and the launcher's key/value list is
``(("forge-vault/1" . "<name>|<m>|<label>=<key>;…"))``.

Commands mirror ``multisig_tool.py``: one JSON object in, one out.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend, make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.util.bech32m import encode_puzzle_hash
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, LineageProof, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats
import mips
from chia.wallet.did_wallet.did_wallet_puzzles import create_innerpuz
from chia.wallet.lineage_proof import LineageProof as SingletonLineageProof
from chia.wallet.puzzles.singleton_top_layer_v1_1 import (
    SINGLETON_LAUNCHER,
    SINGLETON_LAUNCHER_HASH,
    SINGLETON_MOD_HASH,
    generate_launcher_coin,
    launch_conditions_and_coinsol,
    puzzle_for_singleton,
    solution_for_singleton,
)
from chia.wallet.util.merkle_tree import MerkleTree, hash_a_pair, hash_an_atom
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_puzzles_py import programs as PUZZLES
from chia_rs import AugSchemeMPL, G1Element, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import multisig_profile as profile
from multisig_tool import (
    AGG_SIG_ME_DATA,
    MAX_CLVM_COST,
    MultisigError,
    Node,
    Output,
    PlannedSponsor,
    Safe,
    announcement_id,
    cat_lineage_proof,
    choose_shares,
    find_signed_roles,
    parse_shares,
    coin_from_json,
    coin_to_json,
    hex32,
    network_config,
    parse_amount,
    parse_outputs,
    parse_pubkey,
    pubkey_hex,
    record_coin,
    select_coins,
    spend_to_json,
    sponsor_key_of,
    strip0x,
)

def spend_from_json(value: Any) -> CoinSpend:
    """The inverse of ``spend_to_json``, for riders restored from a stored plan."""
    return make_spend(
        coin_from_json(value.get("coin")),
        Program.from_bytes(bytes.fromhex(strip0x(value.get("puzzle_reveal")))),
        Program.from_bytes(bytes.fromhex(strip0x(value.get("solution")))),
    )


PLAN_VERSION = 2
PUZZLE_KIND = "vault"
VAULT_TAG = b"forge-vault/1"
SINGLETON_AMOUNT = 1
MAX_LINEAGE_STEPS = 2000
MAX_COINS_PER_PROPOSAL = 40

# The two shapes a lock's policy can take. `forge/1` is what Forge wrote before
# it adopted MIPS: delegated_puzzle_feeder over m_of_n over bare bls_member
# leaves. `mips/1` is the composition CNI's SDK, custody tool and Cloud Wallet
# all speak. They hash differently, so a lock keeps whichever it was minted
# with; only new locks default to MIPS. See contracts/mips.py.
FORMAT_LEGACY = "forge/1"
FORMAT_MIPS = "mips/1"
POLICY_FORMATS = (FORMAT_LEGACY, FORMAT_MIPS)

M_OF_N = Program.from_bytes(PUZZLES.M_OF_N)
BLS_MEMBER = Program.from_bytes(PUZZLES.BLS_MEMBER)
FEEDER = Program.from_bytes(PUZZLES.DELEGATED_PUZZLE_FEEDER)
P2_VAULT = Program.from_bytes(PUZZLES.P2_SINGLETON_VIA_DELEGATED_PUZZLE)
# Used to tell a singleton's wrapper from its contents when classifying a coin.
SINGLETON_TOP_LAYER_HASH = Program.from_bytes(PUZZLES.SINGLETON_TOP_LAYER_V1_1).get_tree_hash()
# The funds puzzle's aggregation path is disabled: `(x)` always fails.
NO_AGGREGATOR = Program.to([8])

AGG_SIG_ME = ConditionOpcode.AGG_SIG_ME
CREATE_COIN = ConditionOpcode.CREATE_COIN
RESERVE_FEE = ConditionOpcode.RESERVE_FEE
CREATE_COIN_ANNOUNCEMENT = ConditionOpcode.CREATE_COIN_ANNOUNCEMENT
ASSERT_COIN_ANNOUNCEMENT = ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT
CREATE_PUZZLE_ANNOUNCEMENT = ConditionOpcode.CREATE_PUZZLE_ANNOUNCEMENT
ASSERT_MY_COIN_ID = ConditionOpcode.ASSERT_MY_COIN_ID


# ─── Policy: who owns the lock ───────────────────────────────────────────────


@dataclass(frozen=True)
class Policy:
    name: str
    m: int
    owners: tuple[tuple[str, G1Element], ...]  # (label, key)
    # Which composition this lock's inner puzzle uses. The default is the legacy
    # shape on purpose: a record written before MIPS carries no format, and
    # reading it as MIPS would compute a puzzle hash the chain does not hold.
    # New locks opt in explicitly, in `cmd_create`.
    fmt: str = FORMAT_LEGACY

    @property
    def is_mips(self) -> bool:
        return self.fmt == FORMAT_MIPS

    @staticmethod
    def from_json(value: Any) -> "Policy":
        if not isinstance(value, dict):
            raise MultisigError("policy must be an object")
        normalized = profile.normalize_safes([{"name": value.get("name", ""), "m": value.get("m", value.get("threshold")), "owners": value.get("owners") or []}])
        if not normalized or "owners" not in normalized[0]:
            raise MultisigError("policy needs owners")
        entry = normalized[0]
        fmt = str(value.get("format") or entry.get("format") or FORMAT_LEGACY)
        if fmt not in POLICY_FORMATS:
            raise MultisigError(f"unknown policy format {fmt!r}")
        return Policy(entry["name"], entry["m"], tuple((o["label"], parse_pubkey(o["pubkey"])) for o in entry["owners"]), fmt)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "m": self.m, "format": self.fmt, "owners": [{"label": label, "pubkey": pubkey_hex(key)} for label, key in self.owners]}

    @property
    def keys(self) -> list[G1Element]:
        return [key for _, key in self.owners]

    def safe(self) -> Safe:
        """The same policy as an M-of-N ``Safe``: reused for share bookkeeping."""
        return Safe(self.m, tuple(self.keys))

    def member_configs(self) -> list[mips.MemberConfig]:
        """MIPS seats one owner per nonce, so two owners may share a key."""
        return [mips.MemberConfig(nonce=index) for index in range(len(self.owners))]

    def member_puzzles(self) -> list[Program]:
        """The puzzles the merkle tree's leaves hash, wrappers included."""
        if self.is_mips:
            configs = self.member_configs()
            return [
                mips.mips_puzzle(configs[i], mips.bls_member(bytes(key)))
                for i, key in enumerate(self.keys)
            ]
        return [BLS_MEMBER.curry(bytes(key)) for key in self.keys]

    def leaves(self) -> list[bytes32]:
        return [member.get_tree_hash() for member in self.member_puzzles()]

    def merkle_root(self) -> bytes32:
        return MerkleTree(self.leaves()).calculate_root()

    def custody(self) -> mips.Custody:
        """The MIPS view of this policy. ``custody_hash()`` is what a Chia vault
        stores in ``VaultInfo``, so this is the bridge to CNI's tooling."""
        if not self.is_mips:
            raise MultisigError("this lock predates MIPS; its policy is not a custody puzzle")
        return mips.Custody(self.m, tuple(self.leaves()), mips.MemberConfig(top_level=True))

    def inner_puzzle(self) -> Program:
        if self.is_mips:
            members = self.member_puzzles()
            revealed = {member.get_tree_hash(): member for member in members}
            return self.custody().puzzle(revealed)
        return FEEDER.curry(M_OF_N.curry(self.m, self.merkle_root()))

    def inner_puzzle_hash(self) -> bytes32:
        if self.is_mips:
            return self.custody().custody_hash()
        return self.inner_puzzle().get_tree_hash()

    def encode(self) -> bytes:
        return profile.encode_safe({"name": self.name, "m": self.m, "format": self.fmt, "owners": [{"label": label, "pubkey": pubkey_hex(key)} for label, key in self.owners]})

    @staticmethod
    def decode(memo: bytes) -> "Policy | None":
        decoded = profile.decode_safe(memo)
        if decoded is None or "owners" not in decoded:
            return None
        return Policy(decoded["name"], decoded["m"], tuple((o["label"], parse_pubkey(o["pubkey"])) for o in decoded["owners"]), str(decoded.get("format") or FORMAT_LEGACY))

    def member_proof(self, signers: list[G1Element]) -> Program:
        """The partially revealed merkle tree ``m_of_n`` wants: revealed
        leaves are ``(() member_puzzle . member_solution)``, everything else
        is its hash. The tree shape is chia's ``MerkleTree`` (ceil split), so
        the proven root equals ``merkle_root()``.
        """
        chosen = {bytes(key) for key in signers}
        if len(chosen) != self.m:
            raise MultisigError(f"a spend must reveal exactly {self.m} owners, got {len(chosen)}")
        members = self.member_puzzles()
        leaves = self.leaves()
        reveal = {leaves[i]: members[i] for i, key in enumerate(self.keys) if bytes(key) in chosen}

        def build(hashes: list[bytes32]) -> Program | bytes32:
            if len(hashes) == 1:
                leaf = hashes[0]
                if leaf in reveal:
                    return Program.to(([], (reveal[leaf], [])))
                return hash_an_atom(leaf)
            mid = math.ceil(len(hashes) / 2)
            left = build(hashes[:mid])
            right = build(hashes[mid:])
            if isinstance(left, bytes) and isinstance(right, bytes):
                return hash_a_pair(left, right)
            return Program.to((left, right))

        built = build(leaves)
        if isinstance(built, bytes):
            raise MultisigError("member proof reveals nobody")
        return built

    def inner_solution(self, delegated_puzzle: Program, signers: list[G1Element]) -> Program:
        """What the singleton's inner puzzle is solved with.

        The two formats differ here as much as they differ in their hashes. The
        legacy shape feeds ``m_of_n`` a partially revealed tree in every case;
        MIPS dispatches, so a 1-of-N carries a compact merkle proof and an
        N-of-N carries no proof at all.
        """
        if not self.is_mips:
            return Program.to([delegated_puzzle, 0, self.member_proof(signers)])
        chosen = {bytes(key) for key in signers}
        if len(chosen) != self.m:
            raise MultisigError(f"a spend must reveal exactly {self.m} owners, got {len(chosen)}")
        members = self.member_puzzles()
        revealed = {
            members[i].get_tree_hash(): (members[i], Program.to(0))
            for i, key in enumerate(self.keys)
            if bytes(key) in chosen
        }
        return self.custody().solution(delegated_puzzle, Program.to(0), revealed)

    def signing_selection(self, signer: G1Element) -> list[G1Element]:
        """``signer`` plus the first M-1 other owners: the proof shape the
        signer's wallet sees. Which others are revealed does not change the
        message signed (the delegated puzzle hash), so it is arbitrary."""
        keys = self.keys
        if all(bytes(k) != bytes(signer) for k in keys):
            raise MultisigError(f"{pubkey_hex(signer)} is not an owner of this lock")
        chosen = [signer]
        for key in keys:
            if len(chosen) == self.m:
                break
            if bytes(key) != bytes(signer):
                chosen.append(key)
        return chosen


# ─── Puzzles ─────────────────────────────────────────────────────────────────


def singleton_struct(launcher_id: bytes32) -> Program:
    return Program.to((SINGLETON_MOD_HASH, (launcher_id, SINGLETON_LAUNCHER_HASH)))


def deposit_puzzle(launcher_id: bytes32) -> Program:
    return P2_VAULT.curry(singleton_struct(launcher_id), NO_AGGREGATOR)


def deposit_puzzle_hash(launcher_id: bytes32) -> bytes32:
    return deposit_puzzle(launcher_id).get_tree_hash()


def vault_puzzle(launcher_id: bytes32, policy: Policy) -> Program:
    return puzzle_for_singleton(launcher_id, policy.inner_puzzle())


def funds_solution(inner_puzzle_hash: bytes32, delegated_puzzle: Program, coin: Coin) -> Program:
    """``(aggregator_solution singleton_inner_puzhash delegated_puzzle delegated_solution my_id)``."""
    return Program.to([0, inner_puzzle_hash, delegated_puzzle, 0, coin.name()])


def funds_announcement(coin: Coin, delegated_puzzle: Program) -> bytes32:
    """What the singleton must announce (CREATE_PUZZLE_ANNOUNCEMENT) for this funds coin."""
    return Program.to([coin.name(), delegated_puzzle.get_tree_hash()]).get_tree_hash()


def standard_solution(conditions: list[Any]) -> Program:
    return Program.to([[], (1, conditions), []])


# ─── Chain reads ─────────────────────────────────────────────────────────────


def coin_spend_of(node: Node, coin_id: bytes32, height: int) -> tuple[Program, Program]:
    body = node.rpc("get_puzzle_and_solution", {"coin_id": coin_id.hex(), "height": int(height)})
    payload = body.get("coin_solution") or body.get("coin_spend")
    if not isinstance(payload, dict) or not payload.get("puzzle_reveal") or not payload.get("solution"):
        raise MultisigError(f"no spend for coin {coin_id.hex()} at height {height}")
    return (
        Program.from_bytes(bytes.fromhex(strip0x(payload["puzzle_reveal"]))),
        Program.from_bytes(bytes.fromhex(strip0x(payload["solution"]))),
    )


def children_of(node: Node, coin_id: bytes32) -> list[dict[str, Any]]:
    body = node.rpc("get_coin_records_by_parent_ids", {"parent_ids": [coin_id.hex()], "include_spent_coins": True})
    return [r for r in body.get("coin_records") or [] if isinstance(r, dict)]


def policy_from_launcher(solution: Program) -> Policy:
    """The launcher solution is ``(singleton_full_puzzle_hash amount key_value_list)``."""
    try:
        key_values = list(solution.as_iter())[2]
        for pair in key_values.as_iter():
            key, value = pair.first(), pair.rest()
            if key.atom == VAULT_TAG and value.atom is not None:
                decoded = Policy.decode(bytes(value.atom))
                if decoded is not None:
                    return decoded
    except (ValueError, TypeError, IndexError):
        pass
    raise MultisigError("the launcher carries no readable vault policy")


def policy_from_singleton_spend(puzzle: Program, solution: Program) -> Policy | None:
    """A re-key writes the new policy into the recreated singleton's memos."""
    _cost, output = puzzle.run_with_cost(MAX_CLVM_COST, solution)
    for condition in output.as_iter():
        try:
            parts = list(condition.as_iter())
        except (ValueError, TypeError):
            continue
        if len(parts) < 4 or parts[0].atom != CREATE_COIN:
            continue
        if parts[2].as_int() % 2 == 0:
            continue
        try:
            memos = [bytes(m.atom) for m in parts[3].as_iter() if m.atom is not None]
        except (ValueError, TypeError):
            continue
        for index, memo in enumerate(memos):
            if memo == VAULT_TAG and index + 1 < len(memos):
                decoded = Policy.decode(memos[index + 1])
                if decoded is not None:
                    return decoded
    return None


@dataclass
class VaultState:
    launcher_id: bytes32
    policy: Policy
    tip: Coin
    lineage: SingletonLineageProof
    height: int
    spends: int
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self, network: str) -> dict[str, Any]:
        hrp = network_config(network)["hrp"]
        return {
            "launcher_id": self.launcher_id.hex(),
            "policy": self.policy.to_json(),
            "format": self.policy.fmt,
            "custody_hash": self.policy.inner_puzzle_hash().hex() if self.policy.is_mips else None,
            "inner_puzzle_hash": self.policy.inner_puzzle_hash().hex(),
            "tip": {
                "coin": coin_to_json(self.tip),
                "coin_id": self.tip.name().hex(),
                "lineage_proof": {
                    "parent_name": self.lineage.parent_name.hex(),
                    "inner_puzzle_hash": self.lineage.inner_puzzle_hash.hex() if self.lineage.inner_puzzle_hash else None,
                    "amount": int(self.lineage.amount),
                },
            },
            "deposit_puzzle_hash": deposit_puzzle_hash(self.launcher_id).hex(),
            "deposit_address": encode_puzzle_hash(deposit_puzzle_hash(self.launcher_id), hrp),
            "height": self.height,
            "spends": self.spends,
            "history": self.history,
        }


def read_vault(node: Node, launcher_id: bytes32) -> VaultState:
    """Walk the singleton from its launcher to the unspent tip, collecting
    every policy along the way. Everything here is chain data."""
    launcher = node.coin_record(launcher_id)
    if launcher is None:
        raise MultisigError(f"launcher {launcher_id.hex()} not found")
    if not launcher.get("spent"):
        raise MultisigError("the launcher has not been spent yet; the vault is not launched")
    launcher_coin = record_coin(launcher)
    if launcher_coin.puzzle_hash != SINGLETON_LAUNCHER_HASH:
        raise MultisigError("that coin is not a singleton launcher")
    launch_height = int(launcher.get("spent_block_index") or 0)
    _puzzle, launcher_solution = coin_spend_of(node, launcher_id, launch_height)
    policy = policy_from_launcher(launcher_solution)
    history = [{"height": launch_height, "policy": policy.to_json(), "event": "launch"}]

    current_id = launcher_id
    lineage = SingletonLineageProof(launcher_coin.parent_coin_info, None, launcher_coin.amount)
    spends = 0
    for _ in range(MAX_LINEAGE_STEPS):
        expected_ph = vault_puzzle(launcher_id, policy).get_tree_hash()
        children = [r for r in children_of(node, current_id) if record_coin(r).puzzle_hash == expected_ph and int(record_coin(r).amount) % 2 == 1]
        if not children:
            raise MultisigError(f"lineage broken after coin {current_id.hex()}: no singleton child with the expected puzzle")
        record = children[0]
        coin = record_coin(record)
        if not record.get("spent"):
            return VaultState(launcher_id, policy, coin, lineage, int(record.get("confirmed_block_index") or 0), spends, history)
        height = int(record.get("spent_block_index") or 0)
        puzzle, solution = coin_spend_of(node, coin.name(), height)
        inner_hash = policy.inner_puzzle_hash()
        updated = policy_from_singleton_spend(puzzle, solution)
        if updated is not None and updated.inner_puzzle_hash() != inner_hash:
            policy = updated
            history.append({"height": height, "policy": policy.to_json(), "event": "rekey"})
        else:
            history.append({"height": height, "event": "spend"})
        lineage = SingletonLineageProof(coin.parent_coin_info, inner_hash, coin.amount)
        current_id = coin.name()
        spends += 1
    raise MultisigError("lineage too long to follow")


def find_launcher_from_deposit(node: Node, deposit_ph: bytes32) -> bytes32 | None:
    """The pointer coin at the deposit address names the launcher."""
    body = node.rpc("get_coin_records_by_puzzle_hash", {"puzzle_hash": deposit_ph.hex(), "include_spent_coins": True})
    records = [r for r in body.get("coin_records") or [] if isinstance(r, dict) and int((r.get("coin") or {}).get("amount", 0)) == 1]
    records.sort(key=lambda r: int(r.get("confirmed_block_index") or 0))
    for record in records[:40]:
        coin = record_coin(record)
        height = int(record.get("confirmed_block_index") or 0)
        if height <= 0:
            continue
        try:
            puzzle, solution = coin_spend_of(node, coin.parent_coin_info, height)
        except MultisigError:
            continue
        try:
            memos = profile.memos_creating(puzzle, solution, coin)
        except Exception:  # noqa: BLE001
            continue
        if memos and len(memos) >= 3 and memos[1] == VAULT_TAG and len(memos[2]) == 32:
            candidate = bytes32(memos[2])
            if deposit_puzzle_hash(candidate) == deposit_ph:
                return candidate
    return None


# ─── Launch ──────────────────────────────────────────────────────────────────


def build_launch(network: str, policy: Policy, coins_raw: Any, fee: int, profile_raw: Any = None) -> dict[str, Any]:
    """The creator's wallet coin launches the singleton and plants the pointer.

    One standard-puzzle spend (signed by the creator's Sage) creates the
    launcher; the launcher spend, which needs no signature, creates the eve
    singleton with the policy in its key/value list. The same wallet spend
    also drops the 1-mojo pointer at the deposit address and, optionally, the
    creator's updated profile record at their own address.
    """
    config = network_config(network)
    wallet_coins: list[tuple[Coin, Program]] = []
    for entry in coins_raw or []:
        if not isinstance(entry, dict):
            continue
        reveal = strip0x(entry.get("puzzle") or entry.get("puzzle_reveal") or "")
        if not reveal:
            continue
        coin = coin_from_json(entry.get("coin"))
        puzzle = Program.from_bytes(bytes.fromhex(reveal))
        sponsor_key_of(puzzle, coin)
        wallet_coins.append((coin, puzzle))
    if not wallet_coins:
        raise MultisigError("the wallet reported no XCH coin with a puzzle reveal")

    profile_memos: list[bytes] | None = None
    profile_ph: bytes32 | None = None
    inner = policy.inner_puzzle()
    comment = Program.to([(VAULT_TAG, policy.encode())])
    launch_conditions, launcher_spend = launch_conditions_and_coinsol(max(wallet_coins, key=lambda pair: int(pair[0].amount))[0], inner, comment, uint64(SINGLETON_AMOUNT))
    del launch_conditions, launcher_spend  # recomputed below for the chosen parent
    if isinstance(profile_raw, dict) and profile_raw.get("pubkey"):
        profile_ph = profile.signer_puzzle_hash(str(profile_raw["pubkey"]))
        entries = list(profile_raw.get("safes") or [])
        if profile_raw.get("include_self"):
            # The new lock, by its deposit address: resolvable through the
            # pointer coin this very spend creates. Its hash depends on the
            # launcher, i.e. on the parent coin chosen below, so it is added
            # after selection.
            entries.append({"name": "", "puzzle_hash": "__self__"})
        profile_memos = entries  # finalised after the parent coin is chosen

    needed = SINGLETON_AMOUNT + 1 + fee + (1 if profile_memos else 0)
    candidates = [pair for pair in wallet_coins if int(pair[0].amount) >= needed]
    if not candidates:
        raise MultisigError(f"no wallet coin covers {needed} mojos (singleton, pointer, fee{', profile' if profile_memos else ''})")
    parent, parent_puzzle = max(candidates, key=lambda pair: int(pair[0].amount))

    launch_conditions, launcher_spend = launch_conditions_and_coinsol(parent, inner, comment, uint64(SINGLETON_AMOUNT))
    launcher_coin = generate_launcher_coin(parent, uint64(SINGLETON_AMOUNT))
    launcher_id = launcher_coin.name()
    deposit_ph = deposit_puzzle_hash(launcher_id)

    conditions: list[Any] = list(launch_conditions)
    conditions.append([CREATE_COIN, deposit_ph, 1, [deposit_ph, VAULT_TAG, launcher_id]])
    if profile_memos is not None and profile_ph is not None:
        entries = [({**e, "puzzle_hash": deposit_ph.hex()} if isinstance(e, dict) and e.get("puzzle_hash") == "__self__" else e) for e in profile_memos]
        conditions.append([CREATE_COIN, profile_ph, 1, profile.profile_memos(profile_ph, profile.normalize_safes(entries))])
    change = int(parent.amount) - needed
    if change > 0:
        conditions.append([CREATE_COIN, parent.puzzle_hash, change, [parent.puzzle_hash]])
    if fee > 0:
        conditions.append([RESERVE_FEE, fee])
    conditions.append([ASSERT_MY_COIN_ID, parent.name()])
    parent_spend = make_spend(parent, parent_puzzle, standard_solution(conditions))

    # Prove the launch reads back: the launcher solution carries the policy.
    if policy_from_launcher(Program.from_bytes(bytes(launcher_spend.solution))).inner_puzzle_hash() != inner.get_tree_hash():
        raise MultisigError("launch does not round-trip its policy")

    return {
        "success": True,
        "network": network,
        "launcher_id": launcher_id.hex(),
        "policy": policy.to_json(),
        "format": policy.fmt,
        # A MIPS lock's inner puzzle hash IS a Chia vault's custody hash, so this
        # is the handle CNI's SDK, custody tool and Cloud Wallet address it by.
        "custody_hash": inner.get_tree_hash().hex() if policy.is_mips else None,
        "inner_puzzle_hash": inner.get_tree_hash().hex(),
        "singleton_puzzle_hash": vault_puzzle(launcher_id, policy).get_tree_hash().hex(),
        "deposit_puzzle_hash": deposit_ph.hex(),
        "deposit_address": encode_puzzle_hash(deposit_ph, config["hrp"]),
        "coin_spends": [spend_to_json(parent_spend), spend_to_json(launcher_spend)],
        "eve": {
            "coin": coin_to_json(Coin(launcher_id, vault_puzzle(launcher_id, policy).get_tree_hash(), uint64(SINGLETON_AMOUNT))),
            "lineage_proof": {"parent_name": launcher_coin.parent_coin_info.hex(), "inner_puzzle_hash": None, "amount": SINGLETON_AMOUNT},
        },
        "fee": fee,
    }


# ─── Plans ───────────────────────────────────────────────────────────────────


@dataclass
class SingletonHost:
    """A singleton whose inner puzzle is the lock's own deposit puzzle.

    A DID the lock owns is spent exactly like one of its coins -- the lock's
    singleton announces it and it runs a delegated puzzle -- except the coin sits
    behind two more layers: the singleton wrapper, and the DID inner puzzle whose
    mode-1 branch runs the p2 puzzle and returns its conditions unchanged. This
    carries what is needed to rebuild both.
    """

    launcher_id: bytes32
    inner_puzzle: Program
    lineage: SingletonLineageProof
    # Which singleton this is, because the layers between it and the lock's own
    # puzzle differ and so does the way a solution is wrapped. "did" is one
    # `did_innerpuz`; "nft" is a state layer over an ownership layer.
    layers: str = "did"

    def puzzle(self) -> Program:
        return puzzle_for_singleton(self.launcher_id, self.inner_puzzle)

    def solution(self, p2_solution: Program, amount: int) -> Program:
        if self.layers == "nft":
            # The state layer takes (inner_solution), and with an ownership
            # layer present that inner solution is itself (p2_solution) -- one
            # list per layer, exactly as chia's own NFT wallet builds it.
            inner = Program.to([Program.to([p2_solution])])
        else:
            # did_innerpuz solution is (mode . rest); mode 1 runs the p2 puzzle.
            inner = Program.to([1, p2_solution])
        return solution_for_singleton(self.lineage, uint64(amount), inner)

    def to_json(self) -> dict[str, Any]:
        return {
            "launcher_id": self.launcher_id.hex(),
            "inner_puzzle": bytes(self.inner_puzzle).hex(),
            "layers": self.layers,
            "lineage": {
                "parent_name": self.lineage.parent_name.hex(),
                "inner_puzzle_hash": self.lineage.inner_puzzle_hash.hex() if self.lineage.inner_puzzle_hash else None,
                "amount": int(self.lineage.amount),
            },
        }

    @staticmethod
    def from_json(value: Any) -> "SingletonHost":
        if not isinstance(value, dict):
            raise MultisigError("a singleton spend needs its launcher and inner puzzle")
        lineage = value.get("lineage") or {}
        inner_hash = lineage.get("inner_puzzle_hash")
        layers = str(value.get("layers") or "did")
        if layers not in ("did", "nft"):
            raise MultisigError(f"unknown singleton layering {layers!r}")
        return SingletonHost(
            hex32(value.get("launcher_id"), "launcher_id"),
            Program.from_bytes(bytes.fromhex(strip0x(value.get("inner_puzzle")))),
            SingletonLineageProof(
                hex32(lineage.get("parent_name"), "lineage parent"),
                hex32(inner_hash, "lineage inner") if inner_hash else None,
                uint64(parse_amount(lineage.get("amount", 1), "lineage amount")),
            ),
            layers,
        )


@dataclass
class FundsSpend:
    kind: str  # "xch" | "cat" | "did"
    asset_id: bytes32 | None
    coin: Coin
    cat_lineage: LineageProof | None
    delegated_puzzle: Program
    # Set only for "did": the layers between the lock's deposit puzzle and the coin.
    host: SingletonHost | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "asset_id": self.asset_id.hex() if self.asset_id else None,
            "coin": coin_to_json(self.coin),
            "coin_id": self.coin.name().hex(),
            "cat_lineage": {
                "parent_name": self.cat_lineage.parent_name.hex(),
                "inner_puzzle_hash": self.cat_lineage.inner_puzzle_hash.hex(),
                "amount": int(self.cat_lineage.amount),
            } if self.cat_lineage else None,
            "delegated_puzzle": bytes(self.delegated_puzzle).hex(),
            "host": self.host.to_json() if self.host else None,
        }

    @staticmethod
    def from_json(value: Any) -> "FundsSpend":
        kind = str(value.get("kind"))
        if kind not in ("xch", "cat", "did"):
            raise MultisigError(f"unknown funds kind {kind!r}")
        lineage = value.get("cat_lineage")
        proof = None
        if isinstance(lineage, dict):
            proof = LineageProof(hex32(lineage["parent_name"], "cat parent"), hex32(lineage["inner_puzzle_hash"], "cat inner"), uint64(parse_amount(lineage["amount"], "cat amount")))
        if kind == "cat" and proof is None:
            raise MultisigError("a CAT funds spend needs a lineage proof")
        host = SingletonHost.from_json(value.get("host")) if kind == "did" else None
        return FundsSpend(kind, hex32(value.get("asset_id"), "asset_id") if kind == "cat" else None, coin_from_json(value.get("coin")), proof, Program.from_bytes(bytes.fromhex(strip0x(value.get("delegated_puzzle")))), host)


@dataclass
class VaultPlan:
    network: str
    launcher_id: bytes32
    policy: Policy
    tip: Coin
    lineage: SingletonLineageProof
    delegated_puzzle: Program
    funds: list[FundsSpend]
    sponsor: PlannedSponsor | None
    summary: dict[str, Any]
    successor: Policy | None = None
    # Spends that ride in the bundle without a signature of their own, pinned by
    # an assertion inside the delegated puzzle the owners sign. A launcher is the
    # first of them. Never caller-supplied.
    riders: list[CoinSpend] = field(default_factory=list)

    @property
    def message(self) -> bytes:
        """What every revealed owner signs: AGG_SIG_ME over the delegated puzzle hash."""
        return bytes(self.delegated_puzzle.get_tree_hash()) + bytes(self.tip.name()) + AGG_SIG_ME_DATA[self.network]

    def to_json(self) -> dict[str, Any]:
        return {
            "version": PLAN_VERSION,
            "puzzle": PUZZLE_KIND,
            "network": self.network,
            "launcher_id": self.launcher_id.hex(),
            "policy": self.policy.to_json(),
            "tip": {"coin": coin_to_json(self.tip), "lineage_proof": {"parent_name": self.lineage.parent_name.hex(), "inner_puzzle_hash": self.lineage.inner_puzzle_hash.hex() if self.lineage.inner_puzzle_hash else None, "amount": int(self.lineage.amount)}},
            "delegated_puzzle": bytes(self.delegated_puzzle).hex(),
            "funds": [f.to_json() for f in self.funds],
            "sponsor": self.sponsor.to_json() if self.sponsor else None,
            "successor": self.successor.to_json() if self.successor else None,
            "summary": self.summary,
            "riders": [spend_to_json(r) for r in self.riders],
        }

    @staticmethod
    def from_json(value: Any) -> "VaultPlan":
        if not isinstance(value, dict) or int(value.get("version", 0)) != PLAN_VERSION or value.get("puzzle") != PUZZLE_KIND:
            raise MultisigError("not a vault plan")
        tip = value.get("tip") or {}
        lp = tip.get("lineage_proof") or {}
        inner = lp.get("inner_puzzle_hash")
        lineage = SingletonLineageProof(hex32(lp.get("parent_name"), "lineage parent"), hex32(inner, "lineage inner") if inner else None, uint64(parse_amount(lp.get("amount", 1), "lineage amount")))
        sponsor_raw = value.get("sponsor")
        successor_raw = value.get("successor")
        return VaultPlan(
            network=str(value.get("network") or "testnet11"),
            launcher_id=hex32(value.get("launcher_id"), "launcher_id"),
            policy=Policy.from_json(value.get("policy")),
            tip=coin_from_json(tip.get("coin")),
            lineage=lineage,
            delegated_puzzle=Program.from_bytes(bytes.fromhex(strip0x(value.get("delegated_puzzle")))),
            funds=[FundsSpend.from_json(f) for f in value.get("funds") or []],
            sponsor=PlannedSponsor.from_json(sponsor_raw) if sponsor_raw else None,
            summary=dict(value.get("summary") or {}),
            successor=Policy.from_json(successor_raw) if successor_raw else None,
            riders=[spend_from_json(r) for r in value.get("riders") or []],
        )

    def all_coin_ids(self) -> list[bytes32]:
        ids = [self.tip.name()] + [f.coin.name() for f in self.funds]
        if self.sponsor:
            ids.append(self.sponsor.coin.name())
        return ids

    def materialize(self, signers: list[G1Element]) -> list[CoinSpend]:
        """The singleton spend revealing ``signers``, the funds spends, the sponsor."""
        inner_solution = self.policy.inner_solution(self.delegated_puzzle, signers)
        singleton_spend = make_spend(self.tip, vault_puzzle(self.launcher_id, self.policy), solution_for_singleton(self.lineage, uint64(self.tip.amount), inner_solution))
        inner_hash = self.policy.inner_puzzle_hash()
        spends = [singleton_spend]
        deposit = deposit_puzzle(self.launcher_id)
        for f in self.funds:
            if f.kind == "xch":
                spends.append(make_spend(f.coin, deposit, funds_solution(inner_hash, f.delegated_puzzle, f.coin)))
            elif f.kind == "did" and f.host is not None:
                # The p2 solution is the same one a plain coin uses; the DID and
                # singleton layers wrap it and pass the conditions through.
                p2 = funds_solution(inner_hash, f.delegated_puzzle, f.coin)
                spends.append(make_spend(f.coin, f.host.puzzle(), f.host.solution(p2, int(f.coin.amount))))
        by_asset: dict[bytes32, list[FundsSpend]] = {}
        for f in self.funds:
            if f.kind == "cat":
                by_asset.setdefault(f.asset_id, []).append(f)  # type: ignore[arg-type]
        for asset_id, group in by_asset.items():
            spendables = [SpendableCAT(f.coin, asset_id, deposit, funds_solution(inner_hash, f.delegated_puzzle, f.coin), lineage_proof=f.cat_lineage) for f in group]  # type: ignore[arg-type]
            bundle = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables)
            by_name = {s.coin.name(): s for s in bundle.coin_spends}
            for f in group:
                spends.append(by_name[f.coin.name()])
        if self.sponsor:
            spends.append(make_spend(self.sponsor.coin, self.sponsor.puzzle_reveal, self.sponsor.solution()))
        spends.extend(self.riders)
        return spends


DID_TAG = b"forge-did/1"


def did_inner_puzzle(owner: Program, launcher_id: bytes32) -> Program:
    """The DID inner puzzle for a DID owned by ``owner``.

    ``owner`` must be the PUZZLE, never its hash. ``create_innerpuz`` accepts
    either and says so in its own docstring -- "receiving a standard P2 puzzle
    hash wouldn't calculate a valid puzzle" -- because ``did_innerpuz`` runs its
    owner with ``(a INNER_PUZZLE inner_solution)``. Curry a 32-byte hash there
    and the DID is well formed, hashes fine, launches fine, confirms fine, and
    can never be spent: mode 1 tries to run an atom as a program and mode 0 is
    closed off by a recovery list of length zero. Forge shipped exactly that
    mistake once, so this wrapper exists to make the type impossible to get
    wrong and ``_test_vault_did.py`` runs what it builds.
    """
    if owner.atom is not None:
        raise MultisigError("a DID's owner must be a puzzle, not a puzzle hash")
    return create_innerpuz(owner, [], uint64(0), launcher_id, Program.to([]))


def did_action(owner: Program, name: str):
    """A builder that launches a DID owned by ``owner``, from the lock's own coin.

    Returned as a callable because the launcher coin is derived from the coin that
    pays for it, and that coin is not chosen until selection has run.

    What makes the launcher spend safe to carry unsigned: `launch_conditions_and_coinsol`
    returns an ASSERT_COIN_ANNOUNCEMENT alongside the CREATE_COIN, and the launcher's
    announcement commits the eve singleton's puzzle hash, amount and key/value list.
    Those conditions go into the funds coin's delegated puzzle, whose hash the
    singleton names in the conditions the owners sign. So an executor cannot swap
    the launcher's solution for one that mints a DID owned by somebody else.
    """
    label = name.strip()
    if not label:
        raise MultisigError("a DID needs a name")

    def build(parent: Coin):
        launcher_coin = generate_launcher_coin(parent, uint64(SINGLETON_AMOUNT))
        launcher_id = launcher_coin.name()
        inner = did_inner_puzzle(owner, launcher_id)
        comment = Program.to([(DID_TAG, label.encode())])
        conditions, launcher_spend = launch_conditions_and_coinsol(
            parent, inner, comment, uint64(SINGLETON_AMOUNT))
        return list(conditions), [launcher_spend]

    return build


MAX_ACTION_CONDITIONS = 64


def parse_conditions(raw: Any) -> list[Any]:
    """Caller-supplied Chia conditions, as JSON, into CLVM-ready python.

    A condition is a list whose first element is an opcode. Hex strings become
    bytes, decimal strings and numbers become ints, and nesting is preserved so a
    memo list survives. Nothing is interpreted: the owners approve whatever this
    produces, because it is inside the delegated puzzle they sign.
    """
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list):
        raise MultisigError("conditions must be a list")
    if len(raw) > MAX_ACTION_CONDITIONS:
        raise MultisigError(f"a proposal may carry {MAX_ACTION_CONDITIONS} conditions; got {len(raw)}")

    def one(value: Any) -> Any:
        if isinstance(value, bool):
            raise MultisigError("a condition may not contain a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return [one(v) for v in value]
        if isinstance(value, str):
            text = value.strip()
            if text.lower().startswith("0x"):
                body = text[2:]
                if len(body) % 2:
                    raise MultisigError(f"hex value has an odd length: {text[:24]}")
                return bytes.fromhex(body)
            if text.lstrip("-").isdigit():
                return int(text)
            raise MultisigError(f"a condition value must be hex (0x…), a number, or a list; got {text[:24]!r}")
        raise MultisigError(f"unsupported condition value of type {type(value).__name__}")

    parsed = []
    for entry in raw:
        if not isinstance(entry, list) or not entry:
            raise MultisigError("each condition must be a non-empty list starting with an opcode")
        parsed.append([one(v) for v in entry])
    return parsed


def build_vault_plan(
    node: Node,
    network: str,
    state: VaultState,
    outputs: list[Output],
    fee: int,
    sponsor: tuple[Coin, Program] | None,
    successor: Policy | None,
    nonce: bytes | None = None,
    action_conditions: list[Any] | None = None,
    action_value: int = 0,
    action_builder: Any = None,
    singleton_spends: list[FundsSpend] | None = None,
    asset_conditions_builder: Any = None,
) -> VaultPlan:
    nonce = nonce or os.urandom(32)
    launcher_id = state.launcher_id
    policy = state.policy
    deposit = deposit_puzzle(launcher_id)
    deposit_ph = deposit.get_tree_hash()
    lock_fee = 0 if sponsor else fee
    if successor is not None and successor.inner_puzzle_hash() == policy.inner_puzzle_hash():
        raise MultisigError("the new policy is identical to the current one; nothing to change")

    # ── funds selection ──
    # `action_value` is what the action's own conditions spend beyond the outputs,
    # declared by the builder. Without it the change would over-create and the
    # spend would fail its amount rule.
    xch_needed = sum(o.amount for o in outputs if o.asset_id is None) + lock_fee + max(0, int(action_value))
    cat_needed: dict[bytes32, int] = {}
    for o in outputs:
        if o.asset_id is not None:
            cat_needed[o.asset_id] = cat_needed.get(o.asset_id, 0) + o.amount
    cat_outer = {construct_cat_puzzle(CAT_MOD, asset_id, deposit).get_tree_hash(): asset_id for asset_id in cat_needed}
    # Conditions need a coin to run on even when nothing is being paid out, so the
    # scan has to happen for them too.
    records = (node.coin_records_by_puzzle_hashes([deposit_ph] + list(cat_outer))
               if (xch_needed or cat_needed or action_conditions) else [])
    by_ph: dict[bytes32, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("spent"):
            continue
        by_ph.setdefault(record_coin(record).puzzle_hash, []).append(record)
    xch_records = {record_coin(r).name(): r for r in by_ph.get(deposit_ph, [])}
    xch_coins = select_coins([record_coin(r) for r in xch_records.values()], xch_needed, "XCH") if xch_needed else []
    cat_selection: dict[bytes32, tuple[list[Coin], dict[bytes32, dict[str, Any]]]] = {}
    for outer_ph, asset_id in cat_outer.items():
        asset_records = {record_coin(r).name(): r for r in by_ph.get(outer_ph, [])}
        chosen = select_coins([record_coin(r) for r in asset_records.values()], cat_needed[asset_id], f"CAT {asset_id.hex()[:12]}…")
        cat_selection[asset_id] = (chosen, asset_records)
    total = len(xch_coins) + sum(len(c) for c, _ in cat_selection.values())
    if total > MAX_COINS_PER_PROPOSAL:
        raise MultisigError(f"proposal would spend {total} funds coins; the limit is {MAX_COINS_PER_PROPOSAL}")
    if (action_conditions or action_builder) and not xch_coins:
        # Conditions need a coin to run on. Take the smallest unspent XCH coin at
        # the deposit address and let it carry them, changing nothing else.
        spare = [record_coin(r) for r in by_ph.get(deposit_ph, [])]
        if not spare:
            raise MultisigError("the lock holds no XCH coin to run these conditions on")
        xch_coins = [min(spare, key=lambda c: int(c.amount))]
        total += 1
    if (total == 0 and successor is None and not outputs and not action_conditions
            and not action_builder and not singleton_spends):
        raise MultisigError("the proposal does nothing")

    # Conditions that belong to ONE asset's spend rather than to the proposal at
    # large. An offer needs this: the announcements that bind what the lock wants
    # back have to sit on the spend that gives up the asset, so that spending
    # that asset elsewhere takes the offer down with it. The builder runs here
    # because it needs to know which coins were selected -- an offer's nonce is
    # derived from exactly those coin ids -- and nothing has been written yet.
    asset_conditions: dict[bytes32 | None, list[Any]] = {}
    if asset_conditions_builder is not None:
        selected: dict[bytes32 | None, list[Coin]] = {None: list(xch_coins)}
        for asset_id, (chosen, _records) in cat_selection.items():
            selected[asset_id] = list(chosen)
        asset_conditions = dict(asset_conditions_builder(selected) or {})
        for key, conds_for_asset in asset_conditions.items():
            if conds_for_asset and not selected.get(key):
                name = "XCH" if key is None else f"CAT {key.hex()[:12]}…"
                raise MultisigError(f"conditions were asked for on {name}, which this proposal does not spend")

    funds: list[FundsSpend] = []
    riders: list[CoinSpend] = []
    change_summary: list[dict[str, Any]] = []
    # The builder runs now, not earlier: a launcher coin is derived from the coin
    # that pays for it, so its conditions cannot be written until that coin exists.
    if action_builder is not None:
        if not xch_coins:
            raise MultisigError("this action needs one of the lock's XCH coins to build from")
        built_conditions, riders = action_builder(xch_coins[0])
        action_conditions = list(action_conditions or []) + list(built_conditions)
    lineage_cache: dict[bytes32, LineageProof] = {}
    if xch_coins:
        first, rest = xch_coins[0], xch_coins[1:]
        conds: list[Any] = [[CREATE_COIN, o.puzzle_hash, o.amount] + ([list(o.memos)] if o.memos else []) for o in outputs if o.asset_id is None]
        change = sum(int(c.amount) for c in xch_coins) - xch_needed
        if change > 0:
            conds.append([CREATE_COIN, deposit_ph, change])
            change_summary.append({"asset_id": None, "amount": change})
        if lock_fee > 0:
            conds.append([RESERVE_FEE, lock_fee])
        conds.extend(asset_conditions.get(None, []))
        # Safe's `data`. Appended to the first XCH coin's delegated puzzle, whose
        # hash the singleton names in its own conditions, which is what the owners
        # sign -- so these are approved, not merely carried.
        conds.extend(action_conditions or [])
        funds.append(FundsSpend("xch", None, first, None, Program.to((1, conds))))
        for coin in rest:
            funds.append(FundsSpend("xch", None, coin, None, Program.to((1, []))))
    for asset_id, (chosen, asset_records) in cat_selection.items():
        first, rest = chosen[0], chosen[1:]
        conds = [
            [CREATE_COIN, o.puzzle_hash, o.amount, [o.puzzle_hash]] if o.hint else [CREATE_COIN, o.puzzle_hash, o.amount]
            for o in outputs if o.asset_id == asset_id
        ]
        change = sum(int(c.amount) for c in chosen) - cat_needed[asset_id]
        if change > 0:
            conds.append([CREATE_COIN, deposit_ph, change, [deposit_ph]])
            change_summary.append({"asset_id": asset_id.hex(), "amount": change})
        conds.extend(asset_conditions.get(asset_id, []))
        funds.append(FundsSpend("cat", asset_id, first, cat_lineage_proof(node, first, asset_records[first.name()], lineage_cache), Program.to((1, conds))))
        for coin in rest:
            funds.append(FundsSpend("cat", asset_id, coin, cat_lineage_proof(node, coin, asset_records[coin.name()], lineage_cache), Program.to((1, []))))

    # Singletons the lock owns join the funds list here, after coin selection,
    # because they are not spendable value: a DID recreates itself at the same
    # amount, so counting it as XCH would make the change arithmetic wrong. From
    # this point it is an ordinary authorised spend and the loop below announces
    # it like any other.
    funds.extend(singleton_spends or [])

    # ── the singleton's delegated puzzle: the vote's content ──
    next_policy = successor or policy
    next_inner_hash = next_policy.inner_puzzle_hash()
    memos: list[Any] = [launcher_id]
    if successor is not None:
        memos = [launcher_id, VAULT_TAG, successor.encode()]
    singleton_conds: list[Any] = [[CREATE_COIN, next_inner_hash, SINGLETON_AMOUNT, memos]]
    for f in funds:
        # Authorize exactly this funds coin with exactly this delegated puzzle…
        singleton_conds.append([CREATE_PUZZLE_ANNOUNCEMENT, funds_announcement(f.coin, f.delegated_puzzle)])
        # …and refuse to run unless that funds coin is spent alongside ('$' is
        # the announcement p2_singleton_via_delegated_puzzle always makes).
        singleton_conds.append([ASSERT_COIN_ANNOUNCEMENT, announcement_id(f.coin.name(), b"$")])
    # The lock always announces its nonce, whether or not a sponsor was named at
    # proposal time. That announcement is the handle a fee spend binds itself to:
    # a wallet coin can assert it at EXECUTE time, long after the owners signed,
    # and still be useless in any other bundle. Nothing asserts it back, so the
    # lock's spend does not depend on a fee spend existing -- which is what lets
    # the fee be attached by whoever pushes rather than fixed by whoever
    # proposed. It costs one condition and is inert when no one binds to it.
    singleton_conds.append([CREATE_COIN_ANNOUNCEMENT, nonce])

    sponsor_nonce = hashlib.sha256(nonce + b"sponsor").digest()
    planned_sponsor: PlannedSponsor | None = None
    if sponsor is not None:
        sponsor_coin, sponsor_puzzle = sponsor
        sponsor_key = sponsor_key_of(sponsor_puzzle, sponsor_coin)
        if int(sponsor_coin.amount) < fee:
            raise MultisigError(f"sponsor coin holds {int(sponsor_coin.amount)} mojos; the fee is {fee}")
        singleton_conds.append([ASSERT_COIN_ANNOUNCEMENT, announcement_id(sponsor_coin.name(), sponsor_nonce)])
        sconds: list[Any] = []
        sponsor_change = int(sponsor_coin.amount) - fee
        if sponsor_change > 0:
            sconds.append([CREATE_COIN, sponsor_coin.puzzle_hash, sponsor_change, [sponsor_coin.puzzle_hash]])
        if fee > 0:
            sconds.append([RESERVE_FEE, fee])
        sconds.append([ASSERT_MY_COIN_ID, sponsor_coin.name()])
        sconds.append([CREATE_COIN_ANNOUNCEMENT, sponsor_nonce])
        sconds.append([ASSERT_COIN_ANNOUNCEMENT, announcement_id(state.tip.name(), nonce)])
        planned_sponsor = PlannedSponsor(sponsor_coin, sponsor_puzzle, sponsor_key, Program.to((1, sconds)))
    delegated = Program.to((1, singleton_conds))

    hrp = network_config(network)["hrp"]
    summary = {
        "kind": ("rekey" if successor else "did" if action_builder is not None
                 else "publish" if singleton_spends
                 else "action" if action_conditions else "send"),
        "action_conditions": len(action_conditions or []),
        "riders": len(riders),
        "singleton_spends": len(singleton_spends or []),
        "nft_moves": len([f for f in (singleton_spends or []) if f.host is not None and f.host.layers == "nft"]),
        "outputs": [{"puzzle_hash": o.puzzle_hash.hex(), "address": encode_puzzle_hash(o.puzzle_hash, hrp), "amount": o.amount, "asset_id": o.asset_id.hex() if o.asset_id else None, "memo": o.memo} for o in outputs],
        "fee": fee,
        "fee_paid_by": "sponsor" if sponsor else "lock",
        "sponsor": {"coin_id": planned_sponsor.coin.name().hex(), "pubkey": pubkey_hex(planned_sponsor.pubkey), "amount": int(planned_sponsor.coin.amount), "outputs": []} if planned_sponsor else None,
        "change": change_summary,
        "coin_count": 1 + len(funds) + (1 if planned_sponsor else 0),
        "nonce": nonce.hex(),
        "successor": {"puzzle_hash": next_inner_hash.hex(), "address": encode_puzzle_hash(deposit_ph, hrp), "name": successor.name, "m": successor.m, "owners": successor.to_json()["owners"], "same_address": True} if successor else None,
    }
    plan = VaultPlan(network, launcher_id, policy, state.tip, state.lineage, delegated, funds,
                     planned_sponsor, summary, successor, riders)
    validate_vault_bundle(plan, plan.materialize(policy.keys[: policy.m]), None)
    return plan


# ─── Validation and shares ───────────────────────────────────────────────────


def expected_pairs(plan: VaultPlan, spends: list[CoinSpend]) -> list[tuple[G1Element, bytes]]:
    pairs: list[tuple[G1Element, bytes]] = []
    for spend in spends:
        conditions = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_CLVM_COST)
        if conditions.get(ConditionOpcode.AGG_SIG_UNSAFE):
            raise MultisigError("a vault spend must not use AGG_SIG_UNSAFE")
        for condition in conditions.get(AGG_SIG_ME, []):
            pairs.append((G1Element.from_bytes(condition.vars[0]), bytes(condition.vars[1]) + bytes(spend.coin.name()) + AGG_SIG_ME_DATA[plan.network]))
    return pairs


def validate_vault_bundle(plan: VaultPlan, spends: list[CoinSpend], signature: G2Element | None) -> dict[str, Any]:
    pairs = expected_pairs(plan, spends)
    allowed = {plan.message}
    if plan.sponsor:
        allowed.add(plan.sponsor.message(plan.network))
    for _key, message in pairs:
        if message not in allowed:
            raise MultisigError("a spend demands a signature the plan did not promise")
    expected = plan.policy.m + (1 if plan.sponsor else 0)
    if len(pairs) != expected:
        raise MultisigError(f"expected {expected} AGG_SIG_ME conditions, got {len(pairs)}")
    owner_keys = {bytes(k) for k in plan.policy.keys}
    for key, message in pairs:
        if message == plan.message and bytes(key) not in owner_keys:
            raise MultisigError("a non-owner key is asked to sign the vote")
    # The singleton must recreate itself, once, with the planned inner hash.
    singleton_conditions = conditions_dict_for_solution(spends[0].puzzle_reveal, spends[0].solution, MAX_CLVM_COST)
    next_inner = (plan.successor or plan.policy).inner_puzzle_hash()
    expected_full = vault_puzzle(plan.launcher_id, plan.successor or plan.policy).get_tree_hash()
    creates = [c for c in singleton_conditions.get(CREATE_COIN, []) if int.from_bytes(c.vars[1], "big") % 2 == 1]
    if len(creates) != 1 or bytes(creates[0].vars[0]) != bytes(expected_full):
        raise MultisigError("the singleton does not recreate itself with the planned policy")
    _ = next_inner
    if signature is not None and not AugSchemeMPL.aggregate_verify([k for k, _ in pairs], [m for _, m in pairs], signature):
        raise MultisigError("aggregated signature does not verify against the assembled spends")
    return {"agg_sig_count": len(pairs)}


def find_signed_subset(plan: VaultPlan, candidates: list[G1Element], signature: G2Element) -> tuple[list[G1Element], bool] | None:
    sponsor = (plan.sponsor.pubkey, plan.sponsor.message(plan.network)) if plan.sponsor else None
    return find_signed_roles(candidates, lambda _key: [plan.message], sponsor, signature)


# ─── Commands ────────────────────────────────────────────────────────────────


def node_for(payload: dict[str, Any], network: str, factory: Callable[[str], Node]) -> Node:
    return factory(str(payload.get("node_url") or network_config(network)["node_url"]))


def cmd_derive(payload: dict[str, Any], _factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    raw_policy = payload.get("policy") or payload
    if isinstance(raw_policy, dict) and not raw_policy.get("format"):
        # A lock minted today is a Chia vault: same custody hash the SDK, the
        # custody tool and the Cloud Wallet compute for the same owners.
        raw_policy = {**raw_policy, "format": FORMAT_MIPS}
    policy = Policy.from_json(raw_policy)
    out: dict[str, Any] = {"success": True, "network": network, "puzzle": PUZZLE_KIND, "policy": policy.to_json(), "inner_puzzle_hash": policy.inner_puzzle_hash().hex(), "merkle_root": policy.merkle_root().hex()}
    if payload.get("launcher_id"):
        launcher_id = hex32(payload["launcher_id"], "launcher_id")
        out["launcher_id"] = launcher_id.hex()
        out["deposit_puzzle_hash"] = deposit_puzzle_hash(launcher_id).hex()
        out["deposit_address"] = encode_puzzle_hash(deposit_puzzle_hash(launcher_id), network_config(network)["hrp"])
        out["singleton_puzzle_hash"] = vault_puzzle(launcher_id, policy).get_tree_hash().hex()
    return out


def cmd_launch(payload: dict[str, Any], _factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    raw_policy = payload.get("policy") or payload
    if isinstance(raw_policy, dict) and not raw_policy.get("format"):
        raw_policy = {**raw_policy, "format": FORMAT_MIPS}
    return build_launch(network, Policy.from_json(raw_policy), payload.get("coins"), parse_amount(payload.get("fee", 0), "fee"), payload.get("profile"))


def cmd_read(payload: dict[str, Any], factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    node = node_for(payload, network, factory)
    if payload.get("launcher_id"):
        launcher_id = hex32(payload["launcher_id"], "launcher_id")
    else:
        deposit_ph = profile.parse_target(payload, network)
        found = find_launcher_from_deposit(node, deposit_ph)
        if found is None:
            return {"success": True, "found": False, "network": network, "deposit_puzzle_hash": deposit_ph.hex()}
        launcher_id = found
    state = read_vault(node, launcher_id)
    return {"success": True, "found": True, "network": network, "puzzle": PUZZLE_KIND, **state.to_json(network)}


def _singleton_kind(node: Node, record: dict[str, Any]) -> dict[str, Any] | None:
    """Classify one hinted singleton coin as an NFT, a DID, or something else.

    The coin record gives only the singleton's outer puzzle hash, which says
    nothing about what is inside. The inside comes from the PARENT's spend: the
    reveal there is the puzzle this coin was created from, and uncurrying it
    tells NFT from DID. A coin whose parent we cannot read is reported as
    unknown rather than guessed at.
    """
    coin = record_coin(record)
    parent_id = bytes32(coin.parent_coin_info)
    parent = node.coin_record(parent_id)
    if not parent or not parent.get("spent_block_index"):
        return {"kind": "unknown", "reason": "parent spend not found"}
    try:
        reveal = node.puzzle_reveal(parent_id, int(parent["spent_block_index"]))
    except MultisigError:
        return {"kind": "unknown", "reason": "parent reveal unavailable"}

    try:
        from chia.wallet.nft_wallet.uncurry_nft import UncurriedNFT
        nft = UncurriedNFT.uncurry(*reveal.uncurry())
    except Exception:                                          # noqa: BLE001
        nft = None
    if nft is not None:
        out: dict[str, Any] = {"kind": "nft", "launcher_id": nft.singleton_launcher_id.hex()}
        try:
            owner = nft.owner_did
            out["owner_did"] = owner.hex() if owner else None
        except Exception:                                      # noqa: BLE001
            out["owner_did"] = None
        try:
            uris = [bytes(u).decode(errors="replace") for u in nft.data_uris.as_iter()]
            out["uris"] = uris[:4]
        except Exception:                                      # noqa: BLE001
            out["uris"] = []
        return out

    # `match_did_puzzle` returns an ITERATOR of the DID's curried arguments, or
    # None. The old code unpacked it as a (matched, curried) pair, which raises
    # either way and was swallowed here -- so no DID ever matched and every one
    # the lock held was reported as "not an NFT or DID singleton". The row the
    # assets list showed as unknown was a DID all along.
    try:
        from chia.wallet.did_wallet.did_wallet_puzzles import match_did_puzzle
        curried = match_did_puzzle(*reveal.uncurry())
    except Exception:                                          # noqa: BLE001
        curried = None
    if curried is not None:
        args = list(curried)
        # did_innerpuz is curried (p2, recovery_list_hash, needed, struct, metadata).
        struct = args[3] if len(args) > 3 else None
        owner = args[0] if args else None
        launcher_id = _struct_launcher_id(struct)
        return {
            "kind": "did",
            "launcher_id": launcher_id,
            "owner_puzzle_hash": _puzzle_hash_of(owner),
            "stage": "live",
            # The name lives in the launcher's key/value list and nowhere else,
            # so it has to be read from there however the DID was found.
            # Without this a DID lost its name the moment publishing made it
            # findable, which is exactly backwards.
            "name": _named_launcher(node, launcher_id),
        }
    return {"kind": "unknown", "reason": "not an NFT or DID singleton"}


def _named_launcher(node: Node, launcher_id: str | None) -> str | None:
    """The name Forge wrote into a launcher, found from the launcher id alone."""
    if not launcher_id:
        return None
    try:
        coin_id = bytes32.fromhex(launcher_id)
    except Exception:                                          # noqa: BLE001
        return None
    record = node.coin_record(coin_id)
    height = int((record or {}).get("spent_block_index") or 0)
    return _launcher_name(node, coin_id, height) if height > 0 else None


def _struct_launcher_id(struct: Program | None) -> str | None:
    """The launcher id inside a singleton struct ``(mod (launcher . launcher_ph))``."""
    try:
        return bytes32(struct.rest().first().as_atom()).hex() if struct is not None else None
    except Exception:                                          # noqa: BLE001
        return None


def _puzzle_hash_of(value: Program | None) -> str | None:
    """``create_innerpuz`` takes an owner as either a puzzle or its hash, so a
    DID read back off chain can carry either. Reduce both to the hash."""
    if value is None:
        return None
    try:
        atom = value.as_atom()
        if atom is not None and len(atom) == 32:
            return bytes32(atom).hex()
    except Exception:                                          # noqa: BLE001
        pass
    try:
        return value.get_tree_hash().hex()
    except Exception:                                          # noqa: BLE001
        return None


# How far back the launch scan looks. A lock's own spend history is short, and
# each entry costs at most two coin lookups.
MAX_LAUNCH_SCAN_COINS = 300


def _launched_dids(node: Node, lock_launcher_id: bytes32, deposit_ph: bytes32) -> list[dict[str, Any]]:
    """DIDs this lock launched that no hint points at yet.

    **A freshly launched singleton is not hinted to its owner.** The singleton
    launcher emits a bare ``CREATE_COIN`` with no memo, so the eve coin carries
    no hint; the hint only appears when that eve coin is spent and recreates
    itself with one. A DID the lock just created is therefore confirmed on chain
    and invisible to a hint scan, which is exactly how it can show as executed in
    the queue and missing from the assets list.

    It is still derivable, with no index and no memory of the proposal, because
    both halves are deterministic:

    * a launcher's coin id is ``Coin(payer, SINGLETON_LAUNCHER_HASH, 1)``, so
      every spent coin at the deposit address names one candidate launcher;
    * a DID owned by this lock has exactly one possible eve puzzle hash — the
      singleton wrapper around ``create_innerpuz`` curried with this lock's own
      deposit puzzle hash and that launcher id.

    So a hit is proof rather than a guess: the coin found sits at precisely the
    hash a DID owned by this lock, launched by that coin, would produce. A DID
    owned by anyone else cannot land here.
    """
    records = node.coin_records_by_puzzle_hashes([deposit_ph], include_spent=True)
    spent = [r for r in records if r.get("spent")]
    spent.sort(key=lambda r: int(r.get("spent_block_index") or 0), reverse=True)

    out: list[dict[str, Any]] = []
    for record in spent[:MAX_LAUNCH_SCAN_COINS]:
        payer = record_coin(record)
        launcher_id = Coin(payer.name(), SINGLETON_LAUNCHER_HASH, uint64(SINGLETON_AMOUNT)).name()
        launcher = node.coin_record(launcher_id)
        if not launcher or not launcher.get("spent"):
            continue
        # Two shapes are checked: the correct one, whose owner is the deposit
        # PUZZLE, and the inert one Forge shipped once, whose owner was curried
        # as the deposit puzzle's HASH. The second cannot be spent at all, and
        # saying so is far better than leaving it out and letting it look lost.
        for owner_as_hash in (False, True):
            eve = did_eve_coin(lock_launcher_id, launcher_id, owner_as_hash=owner_as_hash)
            eve_record = node.coin_record(eve.name())
            if not eve_record or eve_record.get("spent"):
                # Spent means the DID moved on and hinted itself; the hint scan has it.
                continue
            out.append({
                **coin_to_json(eve),
                "coin_id": eve.name().hex(),
                "confirmed_block_index": int(eve_record.get("confirmed_block_index") or 0),
                "kind": "did",
                "launcher_id": launcher_id.hex(),
                "owner_puzzle_hash": deposit_ph.hex(),
                # An eve singleton has not spent itself once, which is the step
                # that publishes its hint. Until then other wallets and indexers
                # cannot find it.
                "stage": "eve",
                "hinted": False,
                # False means no spend of this coin can ever run. See did_inner_puzzle.
                "spendable": not owner_as_hash,
                "name": _launcher_name(node, launcher_id, int(launcher.get("spent_block_index") or 0)),
            })
            break
    return out


def did_eve_coin(lock_launcher_id: bytes32, did_launcher_id: bytes32, owner_as_hash: bool = False) -> Coin:
    """The eve coin a DID owned by this lock must sit at.

    ``owner_as_hash`` rebuilds the broken shape Forge shipped once, where the
    owner was curried as a hash. That shape is inert -- no spend of it can run --
    but it exists on chain, so it has to be recognisable in order to be reported.
    """
    owner: Any = deposit_puzzle_hash(lock_launcher_id) if owner_as_hash else deposit_puzzle(lock_launcher_id)
    inner = create_innerpuz(owner, [], uint64(0), did_launcher_id, Program.to([]))
    return Coin(did_launcher_id, puzzle_for_singleton(did_launcher_id, inner).get_tree_hash(), uint64(SINGLETON_AMOUNT))


def resolve_owned_did(node: Node, lock_launcher_id: bytes32, launcher_id: bytes32) -> tuple[Coin, SingletonHost]:
    """The unspent coin of a DID this lock owns, with the layers to spend it.

    Only the eve coin is resolved, which is what "publish" acts on: the eve coin
    is the one no hint points at. Its puzzle hash is not read from the chain and
    trusted -- it is rebuilt from this lock's own deposit puzzle and this launcher
    id, and the coin is then looked up at exactly that hash. So a launcher id
    naming a DID somebody else owns resolves to nothing rather than to a spend
    the lock cannot make.
    """
    launcher = node.coin_record(launcher_id)
    if not launcher:
        raise MultisigError(f"launcher {launcher_id.hex()} not found")
    if not launcher.get("spent"):
        raise MultisigError("that launcher has not been spent; there is no singleton yet")
    launcher_coin = record_coin(launcher)

    inner = did_inner_puzzle(deposit_puzzle(lock_launcher_id), launcher_id)
    eve = did_eve_coin(lock_launcher_id, launcher_id)
    record = node.coin_record(eve.name())
    if not record:
        inert = node.coin_record(did_eve_coin(lock_launcher_id, launcher_id, owner_as_hash=True).name())
        if inert is not None:
            raise MultisigError(
                "that DID was launched with its owner curried as a puzzle hash instead of a "
                "puzzle, so no spend of it can ever run: mode 1 tries to run an atom as a "
                "program and mode 0 is closed by an empty recovery list. It cannot be "
                "published, moved or used to mint. Create a new one.")
        raise MultisigError("this lock does not own a DID with that launcher id")
    if record.get("spent"):
        raise MultisigError("that DID has already been spent once; it is published already")

    lineage = SingletonLineageProof(
        bytes32(launcher_coin.parent_coin_info), None, uint64(launcher_coin.amount))
    return eve, SingletonHost(launcher_id, inner, lineage)


def publish_did_spend(coin: Coin, host: SingletonHost, owner_ph: bytes32, extra: list[Any] | None = None) -> FundsSpend:
    """Spend a DID to itself so it writes the hint that makes it findable.

    A launched singleton carries no hint: the launcher emits a bare
    ``CREATE_COIN``. Wallets, explorers and Forge's own scan all search by hint,
    so until the DID spends itself once it is real but invisible. This is that
    spend, and it changes nothing else -- the DID recreates itself at the same
    inner puzzle hash, with the same owner, at the same amount. The only new
    thing on chain is the memo naming its owner.

    ``extra`` rides along in the same spend. An NFT moving under this DID needs
    the DID to announce that NFT's launcher id, and a DID can only say so while
    it is being spent -- so the announcement goes here rather than needing a
    second spend of the same coin, which is impossible anyway.
    """
    conditions: list[Any] = [[CREATE_COIN, host.inner_puzzle.get_tree_hash(), int(coin.amount), [owner_ph]]]
    conditions.extend(extra or [])
    return FundsSpend("did", None, coin, None, Program.to((1, conditions)), host)


def _launcher_name(node: Node, launcher_id: bytes32, height: int) -> str | None:
    """The name Forge wrote into the launcher's key/value list at launch."""
    try:
        _, solution = node.puzzle_and_solution(launcher_id, height)
        entries = list(solution.as_iter())
        if len(entries) < 3:
            return None
        for pair in entries[2].as_iter():
            # Each entry is an improper cons, (tag . value), so it is read with
            # first/rest; iterating it as a list yields nothing.
            if not pair.pair:
                continue
            if pair.first().as_atom() != DID_TAG:
                continue
            return bytes(pair.rest().as_atom() or b"").decode("utf-8", "replace") or None
    except Exception:                                          # noqa: BLE001
        return None
    return None


def scan_singletons(node: Node, lock_launcher_id: bytes32, deposit_ph: bytes32) -> list[dict[str, Any]]:
    """Every singleton this lock owns, classified.

    Two sources, because one is not enough. Hints find singletons that were
    SENT here, and DIDs that have spent themselves at least once. They cannot
    find a singleton this lock launched and has not moved, so those are derived
    from the lock's own spend history — see :func:`_launched_dids`.

    Hinted coins include ordinary payments, so only odd-amount coins are
    considered: a singleton always carries an odd amount, and the launcher's
    own even coin is not one.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in node.coin_records_by_hint(deposit_ph):
        if record.get("spent"):
            continue
        coin = record_coin(record)
        if coin.amount % 2 == 0:
            continue
        if coin.puzzle_hash == deposit_ph:
            # A 1-mojo coin sitting at the address itself: the launch pointer, or
            # an odd payment. It is XCH the balance already reports, not a
            # singleton, and listing it as one put a permanent "unknown" row in
            # the assets list.
            continue
        entry = {
            **coin_to_json(coin),
            "coin_id": coin.name().hex(),
            "confirmed_block_index": int(record.get("confirmed_block_index") or 0),
            "hinted": True,
        }
        entry.update(_singleton_kind(node, record) or {})
        seen.add(entry["coin_id"])
        out.append(entry)

    for entry in _launched_dids(node, lock_launcher_id, deposit_ph):
        if entry["coin_id"] in seen:
            continue
        seen.add(entry["coin_id"])
        out.append(entry)
    return out


def cmd_balance(payload: dict[str, Any], factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    node = node_for(payload, network, factory)
    launcher_id = hex32(payload.get("launcher_id"), "launcher_id")
    deposit = deposit_puzzle(launcher_id)
    deposit_ph = deposit.get_tree_hash()
    asset_ids: list[bytes32] = []
    for raw in payload.get("asset_ids") or []:
        asset_id = hex32(raw, "asset_id")
        if asset_id != bytes32.zeros and asset_id not in asset_ids:
            asset_ids.append(asset_id)
    outer = {construct_cat_puzzle(CAT_MOD, asset_id, deposit).get_tree_hash(): asset_id for asset_id in asset_ids}
    records = node.coin_records_by_puzzle_hashes([deposit_ph] + list(outer))
    xch, cats = [], {a: [] for a in asset_ids}
    for record in records:
        if record.get("spent"):
            continue
        coin = record_coin(record)
        entry = {**coin_to_json(coin), "coin_id": coin.name().hex(), "confirmed_block_index": int(record.get("confirmed_block_index") or 0), "timestamp": int(record.get("timestamp") or 0)}
        if coin.puzzle_hash == deposit_ph:
            xch.append(entry)
        elif coin.puzzle_hash in outer:
            cats[outer[coin.puzzle_hash]].append(entry)
    # NFTs and DIDs are singletons owned by this address, not coins AT it, so they
    # are found by hint. Skipped unless asked for: it costs a parent lookup each.
    singletons = scan_singletons(node, launcher_id, deposit_ph) if payload.get("include_singletons") else []
    # A DID hinted here but owned by another puzzle is not this lock's to use: the
    # owners cannot spend it and cannot mint under it. It belongs with the other
    # singletons, not in the DID list, where its presence read as capability the
    # lock does not have.
    owned_did = lambda x: x.get("kind") == "did" and x.get("owner_puzzle_hash") == deposit_ph.hex()
    return {
        "success": True,
        "network": network,
        "puzzle_hash": deposit_ph.hex(),
        "address": encode_puzzle_hash(deposit_ph, network_config(network)["hrp"]),
        "xch": {"balance": sum(c["amount"] for c in xch), "coins": xch},
        "cats": [{"asset_id": a.hex(), "balance": sum(c["amount"] for c in coins), "coins": coins} for a, coins in cats.items() if coins],
        "nfts": [x for x in singletons if x.get("kind") == "nft"],
        "dids": [x for x in singletons if owned_did(x)],
        "other_singletons": [x for x in singletons if x.get("kind") == "unknown" or (x.get("kind") == "did" and not owned_did(x))],
    }


def state_from_payload(node: Node, payload: dict[str, Any]) -> VaultState:
    launcher_id = hex32(payload.get("launcher_id"), "launcher_id")
    return read_vault(node, launcher_id)


def cmd_propose(payload: dict[str, Any], factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    config = network_config(network)
    node = node_for(payload, network, factory)
    state = state_from_payload(node, payload)
    fee = parse_amount(payload.get("fee", 0), "fee")
    raw_successor = payload.get("successor")
    if isinstance(raw_successor, dict) and not raw_successor.get("format"):
        # A re-key rewrites the owners, not the composition: a MIPS lock stays a
        # MIPS lock and a legacy lock stays legacy, or its address would move.
        raw_successor = {**raw_successor, "format": state.policy.fmt}
    successor = Policy.from_json(raw_successor) if raw_successor is not None else None
    raw_outputs = payload.get("outputs") or []
    outputs = parse_outputs(raw_outputs, config["hrp"]) if raw_outputs else []
    action_conditions = parse_conditions(payload.get("conditions"))
    # A DID the LOCK launches: its coin is the launcher's parent, its owners approve
    # it, and the DID's owner is the lock's own deposit puzzle from the first coin.
    # Publishing a DID the lock already owns: spend it to itself so it writes the
    # hint. It is not a new DID, so it takes no name and creates no launcher.
    # Singletons the lock spends in this proposal: DIDs being published, DIDs
    # approving an NFT that is moving under them, and the NFTs themselves. They
    # are collected per launcher because a coin can only be spent once — a DID
    # that is both published and giving its approval does both in one spend.
    deposit_ph_here = deposit_puzzle_hash(state.launcher_id)
    did_coins: dict[bytes32, tuple[Coin, SingletonHost]] = {}
    did_extra: dict[bytes32, list[Any]] = {}

    publish_raw = payload.get("publish_did")
    publishes = publish_raw if isinstance(publish_raw, list) else [publish_raw]
    for entry in publishes:
        if not isinstance(entry, dict) or not entry.get("launcher_id"):
            continue
        launcher = hex32(entry["launcher_id"], "launcher_id")
        if launcher in did_coins:
            raise MultisigError("the same DID is named twice in one proposal")
        # The eve coin specifically: publishing is the DID's first spend, and
        # asking for it twice is a mistake worth reporting.
        did_coins[launcher] = resolve_owned_did(node, state.launcher_id, launcher)
        did_extra.setdefault(launcher, [])

    # NFTs the lock moves: to an address, under a DID, or both.
    nft_spends: list[FundsSpend] = []
    nft_raw = payload.get("nft")
    if nft_raw:
        import vault_nft

        moves = vault_nft.parse_moves(nft_raw, config["hrp"])

        def _did_inner_hash(launcher: bytes32) -> bytes32:
            # Whichever coin of that DID is unspent now — it is the one that has
            # to be spent alongside to say yes, whether or not it was ever
            # published.
            if launcher not in did_coins:
                did_coins[launcher] = vault_nft.resolve_did_now(node, state.launcher_id, launcher)
                did_extra.setdefault(launcher, [])
            return did_coins[launcher][1].inner_puzzle.get_tree_hash()

        nft_spends, approvals = vault_nft.build_nft_spends(node, state, moves, _did_inner_hash)
        for did_launcher in approvals:
            for move in moves:
                if move.new_owner == did_launcher:
                    did_extra[did_launcher].append(vault_nft.approval_condition(move.launcher_id))

    singleton_spends: list[FundsSpend] = [
        publish_did_spend(coin, host, deposit_ph_here, did_extra.get(launcher) or None)
        for launcher, (coin, host) in did_coins.items()
    ]
    singleton_spends.extend(nft_spends)

    did_raw = payload.get("did")
    action_builder = None
    action_value = 0
    if isinstance(did_raw, dict) and str(did_raw.get("name") or "").strip():
        action_builder = did_action(deposit_puzzle(state.launcher_id), str(did_raw["name"]))
        action_value = SINGLETON_AMOUNT
    if isinstance(payload.get("offer"), dict):
        # An offer writes its own outputs and needs no fee: the lock gives up
        # coins and gets paid, and whoever takes it pays to push it.
        if outputs or successor is not None or action_conditions or action_builder is not None or singleton_spends:
            raise MultisigError("an offer proposal cannot also carry outputs, conditions, a DID or a re-key")
        if fee:
            raise MultisigError("an offer carries no fee of its own; the taker pushes it")
    elif not outputs and successor is None and not action_conditions and action_builder is None and not singleton_spends:
        raise MultisigError("a proposal needs outputs, conditions, a DID, a DID to publish, or a new policy")

    sponsor: tuple[Coin, Program] | None = None
    sponsor_raw = payload.get("sponsor")
    if isinstance(sponsor_raw, dict) and isinstance(sponsor_raw.get("coins"), list):
        candidates: list[tuple[Coin, Program]] = []
        for entry in sponsor_raw["coins"]:
            if not isinstance(entry, dict):
                continue
            reveal = strip0x(entry.get("puzzle") or entry.get("puzzle_reveal") or "")
            if not reveal:
                continue
            coin = coin_from_json(entry.get("coin"))
            if int(coin.amount) >= fee:
                candidates.append((coin, Program.from_bytes(bytes.fromhex(reveal))))
        if not candidates:
            raise MultisigError(f"the proposer's wallet has no XCH coin covering the {fee} mojo fee")
        sponsor = max(candidates, key=lambda pair: int(pair[0].amount))
    elif successor is not None and fee > 0 and not outputs:
        # A pure re-key spends no funds coin, so only a sponsor can pay a fee.
        raise MultisigError("a re-key with a fee needs the proposer's wallet to sponsor it")

    nonce_raw = strip0x(payload.get("nonce"))
    nonce = bytes.fromhex(nonce_raw) if nonce_raw else None
    offer_raw = payload.get("offer")
    if isinstance(offer_raw, dict):
        # An offer is built by its own composer, which needs to know which coins
        # were selected before it can write anything. Everything else about the
        # plan -- the singleton spend, the announcements, the one signature the
        # owners give -- is the same as a payment's, which is what lets an offer
        # travel through the queue like any other proposal.
        import vault_offer
        plan = vault_offer.build_offer_plan(
            node, network, state,
            vault_offer.parse_side(offer_raw.get("offered"), "offered"),
            vault_offer.parse_requested(offer_raw.get("requested")),
            nonce,
        )
    else:
        plan = build_vault_plan(node, network, state, outputs, fee, sponsor, successor,
                                nonce, action_conditions,
                                action_value, action_builder, singleton_spends)
    return {
        "success": True,
        "plan": plan.to_json(),
        "messages": [plan.message.hex()],
        "coin_ids": [c.hex() for c in plan.all_coin_ids()],
        "state": state.to_json(network),
    }


def spends_needing_signature(spends: list[CoinSpend], keys: list[G1Element]) -> list[CoinSpend]:
    """The spends that actually ask one of ``keys`` for a signature.

    A lock's bundle is mostly things a wallet has no business being handed. The
    funds coins are authorised by the singleton's announcement and carry no
    signature condition at all; a launcher spend has neither a signature nor a
    puzzle any wallet recognises. Only the singleton spend, where the revealed
    members emit ``AGG_SIG_ME``, needs the owner.

    Sending the rest is not merely wasteful. A wallet asked to sign a coin whose
    puzzle it cannot interpret has to decide what to do with it, and Sage was
    seen to crash outright on the launcher spend — no prompt, no signature, no
    error. So the wallet is handed exactly what it is being asked to sign.

    Nothing is lost by narrowing it: ``AGG_SIG_ME`` binds the message to its own
    coin, so a signature over the singleton spend is complete on its own, and the
    service still validates the whole bundle before and after.
    """
    wanted = {bytes(key) for key in keys}
    out: list[CoinSpend] = []
    for spend in spends:
        try:
            conditions = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_CLVM_COST)
        except Exception:                                      # noqa: BLE001
            continue                                           # unreadable here is unsignable there
        for opcode in (ConditionOpcode.AGG_SIG_ME, ConditionOpcode.AGG_SIG_UNSAFE):
            if any(cvp.vars and bytes(cvp.vars[0]) in wanted for cvp in conditions.get(opcode, [])):
                out.append(spend)
                break
    return out


def signature_keys(spends: list[CoinSpend]) -> set[bytes]:
    """Every distinct key the given spends ask for a signature from."""
    keys: set[bytes] = set()
    for spend in spends:
        try:
            conditions = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_CLVM_COST)
        except Exception:                                      # noqa: BLE001
            continue
        for opcode in (ConditionOpcode.AGG_SIG_ME, ConditionOpcode.AGG_SIG_UNSAFE):
            for cvp in conditions.get(opcode, []):
                if cvp.vars:
                    keys.add(bytes(cvp.vars[0]))
    return keys


def cmd_sign_request(payload: dict[str, Any], _factory: Callable[[str], Node]) -> dict[str, Any]:
    plan = VaultPlan.from_json(payload.get("plan"))
    signer = parse_pubkey(payload.get("signer"), "signer")
    selection = plan.policy.signing_selection(signer)
    spends = plan.materialize(selection)
    # The whole bundle is checked here, as always: what the owners approve has to
    # hold together. Only what to SHOW the wallet is narrowed.
    validate_vault_bundle(plan, spends, None)

    keys = list(selection)
    if plan.sponsor is not None:
        # The proposer's own coin pays the fee and carries its own AGG_SIG_ME on
        # the wallet's synthetic key, so that spend belongs in front of them too.
        keys.append(plan.sponsor.pubkey)
    to_sign = spends_needing_signature(spends, keys)
    if not to_sign:
        raise MultisigError("this proposal asks nothing of that signer")

    # Whether the wallet must sign PARTIALLY is decided here, not by the caller.
    #
    # Partial signing exists so a wallet signs its own owner's condition and
    # leaves another owner's alone. It is the rarer request and the one wallets
    # handle worst. Whether it is needed is a fact about the spends being sent —
    # do they ask for more than one key — and this is the only place that fact is
    # known for certain. Leaving it to the client made it depend on whether the
    # wallet's key list had loaded yet, so the same proposal asked one way or the
    # other depending on timing, which is exactly the kind of difference that
    # makes a failure look random.
    required = signature_keys(to_sign)
    partial = len(required) > 1

    return {
        "success": True,
        "signer": pubkey_hex(signer),
        "selected_pubkeys": [pubkey_hex(k) for k in selection],
        "coin_spends": [spend_to_json(s) for s in to_sign],
        "bundle_spends": len(spends),
        "partial": partial,
        "required_keys": [key.hex() for key in sorted(required)],
        "messages": [plan.message.hex()],
    }


def cmd_verify_share(payload: dict[str, Any], _factory: Callable[[str], Node]) -> dict[str, Any]:
    plan = VaultPlan.from_json(payload.get("plan"))
    raw_sig = strip0x(payload.get("signature"))
    if len(raw_sig) != 192:
        raise MultisigError("signature must be a 96-byte BLS G2 element")
    signature = G2Element.from_bytes(bytes.fromhex(raw_sig))
    candidates_raw = payload.get("candidates")
    candidates = [parse_pubkey(c, "candidate") for c in candidates_raw] if isinstance(candidates_raw, list) and candidates_raw else list(plan.policy.keys)
    owner_keys = {bytes(k) for k in plan.policy.keys}
    for key in candidates:
        if bytes(key) not in owner_keys:
            raise MultisigError(f"{pubkey_hex(key)} is not an owner of this lock")
    found = find_signed_subset(plan, candidates, signature)
    if found is None:
        raise MultisigError("signature does not verify for any owner key over this proposal")
    owners, sponsor_signed = found
    if len(owners) > plan.policy.m:
        raise MultisigError(f"signature covers {len(owners)} owner keys, more than the threshold of {plan.policy.m}")
    return {"success": True, "keys": [pubkey_hex(k) for k in owners], "owner_keys": [pubkey_hex(k) for k in owners], "sponsor_signed": sponsor_signed, "signature": bytes(signature).hex()}


def build_execution_fee(plan: VaultPlan, coins_raw: Any, fee: int) -> dict[str, Any]:
    """A wallet spend that pays the fee for an already-signed proposal.

    This is the piece that makes "whoever pushes pays" true rather than
    aspirational. The owners' signatures cover the lock's delegated puzzle, and
    this spend is not in it -- so it can be built and signed at execute time, by
    somebody who was not the proposer, without invalidating anything.

    It is bound one way only, and deliberately: the fee spend asserts the lock's
    own announcement, so it is worthless in any other bundle and cannot be lifted
    out of the mempool to pay for somebody else's transaction. The lock does not
    assert the fee spend in return, because it could not -- the owners signed
    before this coin was chosen. The cost of that asymmetry is that the lock's
    spend can be pushed with no fee attached, which loses nobody anything: it
    simply waits.
    """
    if fee <= 0:
        raise MultisigError("a fee spend needs a positive fee")
    wallet_coins: list[tuple[Coin, Program]] = []
    for entry in coins_raw or []:
        if not isinstance(entry, dict):
            continue
        reveal = strip0x(entry.get("puzzle") or entry.get("puzzle_reveal") or "")
        if not reveal:
            continue
        coin = coin_from_json(entry.get("coin"))
        puzzle = Program.from_bytes(bytes.fromhex(reveal))
        sponsor_key_of(puzzle, coin)          # refuses a coin this wallet cannot sign
        wallet_coins.append((coin, puzzle))
    if not wallet_coins:
        raise MultisigError("the wallet reported no XCH coin with a puzzle reveal")

    candidates = [pair for pair in wallet_coins if int(pair[0].amount) >= fee]
    if not candidates:
        largest = max(int(pair[0].amount) for pair in wallet_coins)
        raise MultisigError(f"no wallet coin covers a fee of {fee} mojos; the largest holds {largest}")
    coin, puzzle = min(candidates, key=lambda pair: int(pair[0].amount))

    nonce = bytes.fromhex(str(plan.summary.get("nonce") or ""))
    if len(nonce) != 32:
        raise MultisigError("this proposal carries no nonce to bind a fee spend to")

    conditions: list[Any] = []
    change = int(coin.amount) - fee
    if change > 0:
        conditions.append([CREATE_COIN, coin.puzzle_hash, change, [coin.puzzle_hash]])
    conditions.append([RESERVE_FEE, fee])
    conditions.append([ASSERT_MY_COIN_ID, coin.name()])
    # The binding. Without it this spend would pay a fee for anything at all.
    conditions.append([ASSERT_COIN_ANNOUNCEMENT, announcement_id(plan.tip.name(), nonce)])
    spend = make_spend(coin, puzzle, standard_solution(conditions))
    return {
        "success": True,
        "fee": fee,
        "coin_id": coin.name().hex(),
        "change": max(0, change),
        "pubkey": pubkey_hex(sponsor_key_of(puzzle, coin)),
        "coin_spends": [spend_to_json(spend)],
    }


def cmd_fee_spend(payload: dict[str, Any], _factory: Callable[[str], Node]) -> dict[str, Any]:
    plan = VaultPlan.from_json(payload.get("plan"))
    return build_execution_fee(plan, payload.get("coins"), parse_amount(payload.get("fee", 0), "fee"))


def cmd_assemble(payload: dict[str, Any], factory: Callable[[str], Node]) -> dict[str, Any]:
    plan = VaultPlan.from_json(payload.get("plan"))
    shares = parse_shares(payload.get("shares"))
    if not shares:
        raise MultisigError("no signature shares to assemble")
    chosen = choose_shares(plan.policy.safe(), shares, plan.sponsor is not None)
    if chosen is None:
        detail = " (the proposer's fee signature is also required)" if plan.sponsor else ""
        raise MultisigError(f"cannot reach the threshold of {plan.policy.m} from the shares collected{detail}")
    keys, signatures = chosen
    spends = plan.materialize(keys)
    aggregate = AugSchemeMPL.aggregate(signatures)
    # The plan is validated on its own, before any fee spend joins it: what the
    # owners approved has to hold up by itself, and a fee spend must not be able
    # to change that judgement.
    check = validate_vault_bundle(plan, spends, aggregate)

    # A fee attached at push time by whoever is pushing. It is not part of the
    # plan and no owner signed it; it carries its own signature and binds itself
    # to this lock spend by asserting its announcement.
    fee_spends = [spend_from_json(entry) for entry in payload.get("fee_spends") or []]
    if fee_spends:
        fee_signature_raw = strip0x(str(payload.get("fee_signature") or ""))
        if not fee_signature_raw:
            raise MultisigError("a fee spend needs its own signature")
        aggregate = AugSchemeMPL.aggregate([aggregate, G2Element.from_bytes(bytes.fromhex(fee_signature_raw))])
        spends = spends + fee_spends
    bundle = WalletSpendBundle(spends, aggregate)
    is_offer = plan.summary.get("kind") == "offer"
    if is_offer and (fee_spends or payload.get("push")):
        # An offer's announcements are unsatisfied by design -- that is what the
        # taker satisfies by paying. There is nothing here for a node to accept
        # and nothing for a fee to attach to.
        raise MultisigError("an offer is not a transaction: it cannot be pushed, and it takes no fee")
    next_policy = plan.successor or plan.policy
    next_tip = Coin(plan.tip.name(), vault_puzzle(plan.launcher_id, next_policy).get_tree_hash(), uint64(SINGLETON_AMOUNT))
    result: dict[str, Any] = {
        "success": True,
        "signers": [pubkey_hex(k) for k in keys],
        "agg_sig_count": check["agg_sig_count"],
        "spend_bundle": {"coin_spends": [spend_to_json(s) for s in spends], "aggregated_signature": bytes(aggregate).hex()},
        "spend_bundle_id": bundle.name().hex(),
        "next": {
            "policy": next_policy.to_json(),
            "tip": {"coin": coin_to_json(next_tip), "coin_id": next_tip.name().hex(), "lineage_proof": {"parent_name": plan.tip.name().hex(), "inner_puzzle_hash": plan.policy.inner_puzzle_hash().hex(), "amount": SINGLETON_AMOUNT}},
        },
    }
    if is_offer:
        import vault_offer
        offer = vault_offer.assemble_offer(plan, spends, aggregate)
        result["offer"] = {
            "text": offer.to_bech32(),
            "id": offer.name().hex(),
            # The coins the offer creates when it is taken. Their existence on
            # chain is the difference between "somebody took it" and "the owners
            # spent those coins on something else", which is otherwise invisible:
            # either way the offered coins are gone.
            "settlement_coin_ids": [c.name().hex() for group in offer.get_offered_coins().values() for c in group],
            "offered": {("xch" if k is None else k.hex()): int(v) for k, v in offer.get_offered_amounts().items()},
            "requested": {("xch" if k is None else k.hex()): sum(int(p.amount) for p in v) for k, v in offer.get_requested_payments().items()},
        }
        return result
    if payload.get("push"):
        node = node_for(payload, plan.network, factory)
        pushed = node.push_tx(bundle)
        status = str(pushed.get("status") or "").upper()
        ok = bool(pushed.get("success")) and status in ("", "SUCCESS", "PENDING")
        result["push"] = {"ok": ok, "status": status or None, "raw": pushed}
        if not ok:
            result["success"] = False
            result["error"] = f"push_tx rejected: {pushed.get('error') or status or json.dumps(pushed)[:300]}"
    return result


def cmd_status(payload: dict[str, Any], factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    node = node_for(payload, network, factory)
    coin_ids = [hex32(c, "coin_id") for c in payload.get("coin_ids") or []]
    if not coin_ids:
        raise MultisigError("no coin ids to check")
    coins = []
    for coin_id in coin_ids:
        record = node.coin_record(coin_id)
        coins.append({"coin_id": coin_id.hex(), "found": record is not None, "spent": bool(record and record.get("spent")), "spent_block_index": int((record or {}).get("spent_block_index") or 0)})
    return {"success": True, "coins": coins, "all_spent": all(c["spent"] for c in coins), "any_spent": any(c["spent"] for c in coins)}


COMMANDS: dict[str, Callable[[dict[str, Any], Callable[[str], Node]], dict[str, Any]]] = {
    "derive": cmd_derive,
    "launch": cmd_launch,
    "read": cmd_read,
    "balance": cmd_balance,
    "propose": cmd_propose,
    "sign-request": cmd_sign_request,
    "verify-share": cmd_verify_share,
    "assemble": cmd_assemble,
    "fee-spend": cmd_fee_spend,
    "status": cmd_status,
}


def run(command: str, payload: dict[str, Any], factory: Callable[[str], Node] = Node) -> dict[str, Any]:
    handler = COMMANDS.get(command)
    if handler is None:
        raise MultisigError(f"unknown command {command!r}")
    return handler(payload, factory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="strict")
        except (AttributeError, ValueError):
            pass
    raw = sys.stdin.read().strip()
    try:
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise MultisigError("stdin payload must be a JSON object")
        result = run(args.command, payload)
    except MultisigError as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
