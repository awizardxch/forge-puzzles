"""Forge multisig: an M-of-N safe built on CNI's ``p2_m_of_n_delegate_direct``.

The puzzle is Chia Network's own, shipped unchanged in ``chia_puzzles_py`` and
deployed on chain since the first wallets. It is curried with ``(M, pubkeys)``
and solved with ``(selectors, delegated_puzzle, delegated_solution)``: the
selectors pick exactly M of the N keys and the puzzle emits one
``AGG_SIG_UNSAFE(pubkey, sha256tree(delegated_puzzle))`` per selected key, then
runs the delegated puzzle for its conditions.

Two properties of that puzzle shape every decision in this file:

* The message a signer signs is the delegated puzzle's tree hash and nothing
  else — no coin id, no genesis challenge. So every delegated puzzle this tool
  builds carries ``ASSERT_MY_COIN_ID``; a signature is then only good for the
  one coin it was written for, and once that coin is spent the signature is
  dead. Without it a signature over "pay 1 XCH to Alice" would be replayable
  against every coin the safe ever holds.
* The selectors are part of the solution and M must be met exactly, so the
  set of signers is fixed at assembly time, not at proposal time. A signer is
  therefore handed a *signing view* of the spends whose selectors include their
  key; the signature they return is over the delegated puzzle hash and stays
  valid whichever final selector set is chosen, as long as it includes them.

The safe's address is the puzzle hash of the curried puzzle. That means owners
and threshold are fixed for the life of the address; changing them is a
payment to a new safe. The CNI vault member puzzles (``M_OF_N`` over a merkle
tree inside a singleton) lift that restriction and are the intended successor
once Sage signs for them; the plan format here is versioned for that reason.

Every command reads one JSON object on stdin and prints one JSON object on the
last line of stdout, like the other ``contracts/`` builders. The Node routes in
``api/multisig-*.js`` are the only callers.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import CoinSpend, make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.wallet.cat_wallet.cat_utils import (
    CAT_MOD,
    LineageProof,
    SpendableCAT,
    construct_cat_puzzle,
    match_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
)
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import MOD as STANDARD_MOD, puzzle_hash_for_synthetic_public_key
from chia.wallet.puzzles.p2_m_of_n_delegate_direct import MOD as M_OF_N_MOD
from chia.wallet.uncurried_puzzle import uncurry_puzzle
from chia.wallet.wallet_spend_bundle import WalletSpendBundle
from chia_rs import AugSchemeMPL, G1Element, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

PLAN_VERSION = 1
PUZZLE_KIND = "p2_m_of_n_delegate_direct"
MAX_CLVM_COST = 11_000_000_000
# Above this a proposal is too big to sign comfortably in a wallet prompt and
# too costly to be worth a single block slot. Consolidate first.
MAX_COINS_PER_PROPOSAL = 40
MAX_OWNERS = 16

NETWORKS: dict[str, dict[str, str]] = {
    "testnet11": {"hrp": "txch", "node_url": "https://testnet11.api.coinset.org"},
    "mainnet": {"hrp": "xch", "node_url": "https://api.coinset.org"},
}

# What consensus appends to an AGG_SIG_ME message: the network's genesis
# challenge. The sponsor spend (the proposer's own wallet coin paying the fee)
# uses the standard puzzle, which signs with AGG_SIG_ME.
AGG_SIG_ME_DATA: dict[str, bytes] = {
    "mainnet": bytes.fromhex("ccd5bb71183532bff220ba46c268991a3ff07eb358e8255a65c30a2dce0e5fbb"),
    "testnet11": bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615"),
}
AGG_SIG_ME = ConditionOpcode.AGG_SIG_ME

AGG_SIG_UNSAFE = ConditionOpcode.AGG_SIG_UNSAFE
CREATE_COIN = ConditionOpcode.CREATE_COIN
RESERVE_FEE = ConditionOpcode.RESERVE_FEE
CREATE_COIN_ANNOUNCEMENT = ConditionOpcode.CREATE_COIN_ANNOUNCEMENT
ASSERT_COIN_ANNOUNCEMENT = ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT
ASSERT_MY_COIN_ID = ConditionOpcode.ASSERT_MY_COIN_ID


class MultisigError(Exception):
    """A caller mistake or a chain state that makes the request impossible."""


# ─── Encoding helpers ────────────────────────────────────────────────────────


def strip0x(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith("0x") else text


def hex32(value: Any, what: str) -> bytes32:
    raw = strip0x(value)
    if len(raw) != 64:
        raise MultisigError(f"{what} must be 32 bytes of hex, got {len(raw)} chars")
    try:
        return bytes32.fromhex(raw)
    except ValueError as exc:
        raise MultisigError(f"{what} is not valid hex") from exc


def parse_pubkey(value: Any, what: str = "owner public key") -> G1Element:
    raw = strip0x(value)
    if len(raw) != 96:
        raise MultisigError(f"{what} must be a 48-byte BLS G1 key (96 hex chars), got {len(raw)}")
    try:
        return G1Element.from_bytes(bytes.fromhex(raw))
    except Exception as exc:  # chia_rs raises a plain Exception on a bad point
        raise MultisigError(f"{what} is not a valid BLS public key: {exc}") from exc


def pubkey_hex(key: G1Element) -> str:
    return bytes(key).hex()


def parse_amount(value: Any, what: str) -> int:
    try:
        amount = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise MultisigError(f"{what} must be an integer number of mojos") from exc
    if amount < 0:
        raise MultisigError(f"{what} cannot be negative")
    return amount


def network_config(name: Any) -> dict[str, str]:
    key = str(name or "testnet11").strip().lower()
    if key not in NETWORKS:
        raise MultisigError(f"unknown network {key!r}; expected one of {sorted(NETWORKS)}")
    return NETWORKS[key]


def coin_to_json(coin: Coin) -> dict[str, Any]:
    return {
        "parent_coin_info": coin.parent_coin_info.hex(),
        "puzzle_hash": coin.puzzle_hash.hex(),
        "amount": int(coin.amount),
    }


def coin_from_json(value: Any) -> Coin:
    if not isinstance(value, dict):
        raise MultisigError("coin must be an object")
    return Coin(
        hex32(value.get("parent_coin_info"), "coin.parent_coin_info"),
        hex32(value.get("puzzle_hash"), "coin.puzzle_hash"),
        uint64(parse_amount(value.get("amount"), "coin.amount")),
    )


def spend_to_json(spend: CoinSpend) -> dict[str, Any]:
    return {
        "coin": coin_to_json(spend.coin),
        "puzzle_reveal": bytes(spend.puzzle_reveal).hex(),
        "solution": bytes(spend.solution).hex(),
    }


# ─── Node access ─────────────────────────────────────────────────────────────


class Node:
    """Thin coinset/full-node REST client. Tests replace ``rpc`` outright."""

    def __init__(self, node_url: str, timeout: int = 30) -> None:
        self.node_url = node_url.rstrip("/")
        self.timeout = timeout

    def rpc(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.node_url}/{route}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                # coinset's edge filters the default urllib agent.
                "User-Agent": "Mozilla/5.0 (aWizard-Forge/1.0)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise MultisigError(f"node {route} HTTP {exc.code}: {detail[:400]}") from exc
        except urllib.error.URLError as exc:
            raise MultisigError(f"node {route} unreachable: {exc.reason}") from exc
        if not isinstance(body, dict):
            raise MultisigError(f"node {route} returned a non-object body")
        return body

    def coin_records_by_puzzle_hashes(
        self, puzzle_hashes: list[bytes32], include_spent: bool = False
    ) -> list[dict[str, Any]]:
        """Coins at these puzzle hashes.

        ``include_spent`` is off by default because balances only care about
        unspent coins. It is on when a caller needs the address's HISTORY --
        a singleton launched from one of these coins is derived from the spend
        that paid for it, so the spent coin is the only handle on it.
        """
        body = self.rpc(
            "get_coin_records_by_puzzle_hashes",
            {"puzzle_hashes": [ph.hex() for ph in puzzle_hashes], "include_spent_coins": bool(include_spent)},
        )
        if not body.get("success", True):
            raise MultisigError(f"get_coin_records_by_puzzle_hashes failed: {body.get('error')}")
        records = body.get("coin_records") or []
        return [record for record in records if isinstance(record, dict)]

    def coin_records_by_hint(self, hint: bytes32) -> list[dict[str, Any]]:
        """Coins that name ``hint`` in a memo.

        A singleton owned by an address -- an NFT, a DID -- is not FOUND at that
        address: its coin sits at the singleton's own puzzle hash, which changes
        every time the inner puzzle does. What ties it to an owner is the hint the
        transfer wrote, which is why wallets discover NFTs this way and why the
        puzzle-hash scan that finds XCH and CATs cannot see them.
        """
        body = self.rpc(
            "get_coin_records_by_hint",
            {"hint": hint.hex(), "include_spent_coins": False},
        )
        if not body.get("success", True):
            raise MultisigError(f"get_coin_records_by_hint failed: {body.get('error')}")
        records = body.get("coin_records") or []
        return [record for record in records if isinstance(record, dict)]

    def coin_record(self, coin_id: bytes32) -> dict[str, Any] | None:
        body = self.rpc("get_coin_record_by_name", {"name": coin_id.hex()})
        record = body.get("coin_record")
        return record if isinstance(record, dict) else None

    def puzzle_reveal(self, coin_id: bytes32, height: int) -> Program:
        return self.puzzle_and_solution(coin_id, height)[0]

    def puzzle_and_solution(self, coin_id: bytes32, height: int) -> tuple[Program, Program]:
        """Both halves of a spend. The solution is what carries a launcher's
        key/value list, which is where a launched singleton's name lives."""
        body = self.rpc("get_puzzle_and_solution", {"coin_id": coin_id.hex(), "height": int(height)})
        spend = body.get("coin_solution") or body.get("coin_spend")
        if not isinstance(spend, dict) or not spend.get("puzzle_reveal"):
            raise MultisigError(f"no puzzle reveal for coin {coin_id.hex()} at height {height}")
        return (
            Program.from_bytes(bytes.fromhex(strip0x(spend["puzzle_reveal"]))),
            Program.from_bytes(bytes.fromhex(strip0x(spend.get("solution") or "80"))),
        )

    def push_tx(self, bundle: WalletSpendBundle) -> dict[str, Any]:
        return self.rpc("push_tx", {"spend_bundle": bundle.to_json_dict()})


def record_coin(record: dict[str, Any]) -> Coin:
    return coin_from_json(record.get("coin"))


# ─── The safe ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Safe:
    m: int
    pubkeys: tuple[G1Element, ...]

    @staticmethod
    def from_json(value: dict[str, Any]) -> "Safe":
        raw_keys = value.get("pubkeys") or value.get("owners")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise MultisigError("a safe needs a non-empty owners list")
        keys: list[G1Element] = []
        for index, entry in enumerate(raw_keys):
            raw = entry.get("pubkey") if isinstance(entry, dict) else entry
            keys.append(parse_pubkey(raw, f"owner {index + 1}"))
        if len(keys) > MAX_OWNERS:
            raise MultisigError(f"at most {MAX_OWNERS} owners")
        if len({bytes(k) for k in keys}) != len(keys):
            raise MultisigError("owner public keys must be distinct")
        try:
            m = int(value.get("m", value.get("threshold")))
        except (TypeError, ValueError) as exc:
            raise MultisigError("threshold must be an integer") from exc
        if m < 1 or m > len(keys):
            raise MultisigError(f"threshold must be between 1 and {len(keys)}")
        return Safe(m, tuple(keys))

    @property
    def n(self) -> int:
        return len(self.pubkeys)

    def puzzle(self) -> Program:
        return M_OF_N_MOD.curry(self.m, [bytes(k) for k in self.pubkeys])

    def puzzle_hash(self) -> bytes32:
        return self.puzzle().get_tree_hash()

    def key_index(self, key: G1Element) -> int:
        for index, candidate in enumerate(self.pubkeys):
            if bytes(candidate) == bytes(key):
                return index
        raise MultisigError(f"{pubkey_hex(key)} is not an owner of this safe")

    def selectors_for(self, keys: list[G1Element]) -> list[int]:
        chosen = {self.key_index(k) for k in keys}
        if len(chosen) != self.m:
            raise MultisigError(f"selectors must pick exactly {self.m} distinct owners, got {len(chosen)}")
        return [1 if index in chosen else 0 for index in range(self.n)]

    def signing_selectors(self, signer: G1Element) -> list[int]:
        """Selectors that include ``signer`` plus the first M-1 other owners.

        Only used for the signing view. Which other keys are selected is
        irrelevant to the signature, but the puzzle refuses to run unless
        exactly M are, so some choice has to be made.
        """
        signer_index = self.key_index(signer)
        chosen = [signer_index]
        for index in range(self.n):
            if len(chosen) == self.m:
                break
            if index != signer_index:
                chosen.append(index)
        return [1 if index in chosen else 0 for index in range(self.n)]

    def to_json(self) -> dict[str, Any]:
        return {"m": self.m, "pubkeys": [pubkey_hex(k) for k in self.pubkeys]}


def cat_outer_puzzle_hash(asset_id: bytes32, inner: Program) -> bytes32:
    return construct_cat_puzzle(CAT_MOD, asset_id, inner).get_tree_hash()


def m_of_n_solution(selectors: list[int], delegated_puzzle: Program, delegated_solution: Program) -> Program:
    return Program.to([selectors, delegated_puzzle, delegated_solution])


# ─── Plans ───────────────────────────────────────────────────────────────────
#
# A plan is everything needed to rebuild the coin spends for any selector set:
# the coins, their lineage proofs, and each spend's delegated puzzle. The
# delegated puzzle hash is the message an owner signs; it is fixed once the
# plan is built and does not depend on who ends up signing.


@dataclass
class PlannedSpend:
    kind: str  # "xch" | "cat"
    asset_id: bytes32 | None
    coin: Coin
    lineage_proof: LineageProof | None
    delegated_puzzle: Program
    delegated_solution: Program

    @property
    def message(self) -> bytes32:
        return self.delegated_puzzle.get_tree_hash()

    def to_json(self) -> dict[str, Any]:
        lineage = None
        if self.lineage_proof is not None:
            lineage = {
                "parent_name": self.lineage_proof.parent_name.hex(),
                "inner_puzzle_hash": self.lineage_proof.inner_puzzle_hash.hex(),
                "amount": int(self.lineage_proof.amount),
            }
        return {
            "kind": self.kind,
            "asset_id": self.asset_id.hex() if self.asset_id else None,
            "coin": coin_to_json(self.coin),
            "coin_id": self.coin.name().hex(),
            "lineage_proof": lineage,
            "delegated_puzzle": bytes(self.delegated_puzzle).hex(),
            "delegated_solution": bytes(self.delegated_solution).hex(),
            "message": self.message.hex(),
        }

    @staticmethod
    def from_json(value: dict[str, Any]) -> "PlannedSpend":
        kind = str(value.get("kind"))
        if kind not in ("xch", "cat"):
            raise MultisigError(f"unknown spend kind {kind!r}")
        lineage = value.get("lineage_proof")
        proof = None
        if isinstance(lineage, dict):
            proof = LineageProof(
                hex32(lineage.get("parent_name"), "lineage parent_name"),
                hex32(lineage.get("inner_puzzle_hash"), "lineage inner_puzzle_hash"),
                uint64(parse_amount(lineage.get("amount"), "lineage amount")),
            )
        asset_id = hex32(value.get("asset_id"), "asset_id") if kind == "cat" else None
        if kind == "cat" and proof is None:
            raise MultisigError("a CAT spend needs a lineage proof")
        return PlannedSpend(
            kind=kind,
            asset_id=asset_id,
            coin=coin_from_json(value.get("coin")),
            lineage_proof=proof,
            delegated_puzzle=Program.from_bytes(bytes.fromhex(strip0x(value.get("delegated_puzzle")))),
            delegated_solution=Program.from_bytes(bytes.fromhex(strip0x(value.get("delegated_solution")))),
        )


@dataclass
class PlannedSponsor:
    """The proposer's own wallet coin, riding along to pay the fee (and, for
    a re-key, the successor's manifest mojo) so the lock need not hold XCH.

    It is a standard-puzzle spend signed by the proposer's wallet with
    AGG_SIG_ME, linked both ways to the lock's primary spend by announcement,
    so neither half can be broadcast without the other.
    """
    coin: Coin
    puzzle_reveal: Program
    pubkey: G1Element
    delegated_puzzle: Program

    def message(self, network: str) -> bytes:
        return bytes(self.delegated_puzzle.get_tree_hash()) + bytes(self.coin.name()) + AGG_SIG_ME_DATA[network]

    def solution(self) -> Program:
        return Program.to([[], self.delegated_puzzle, []])

    def to_json(self) -> dict[str, Any]:
        return {
            "coin": coin_to_json(self.coin),
            "coin_id": self.coin.name().hex(),
            "puzzle_reveal": bytes(self.puzzle_reveal).hex(),
            "pubkey": pubkey_hex(self.pubkey),
            "delegated_puzzle": bytes(self.delegated_puzzle).hex(),
        }

    @staticmethod
    def from_json(value: Any) -> "PlannedSponsor":
        if not isinstance(value, dict):
            raise MultisigError("sponsor must be an object")
        coin = coin_from_json(value.get("coin"))
        reveal = Program.from_bytes(bytes.fromhex(strip0x(value.get("puzzle_reveal"))))
        return PlannedSponsor(coin, reveal, sponsor_key_of(reveal, coin), Program.from_bytes(bytes.fromhex(strip0x(value.get("delegated_puzzle")))))


def sponsor_key_of(puzzle_reveal: Program, coin: Coin) -> G1Element:
    """The synthetic key a standard-puzzle reveal was curried with; checked against the coin."""
    if puzzle_reveal.get_tree_hash() != coin.puzzle_hash:
        raise MultisigError(f"sponsor puzzle reveal does not match coin {coin.name().hex()}")
    mod, args = puzzle_reveal.uncurry()
    if mod != STANDARD_MOD:
        raise MultisigError("the sponsor coin must sit at a standard wallet address")
    try:
        return G1Element.from_bytes(bytes(args.first().as_atom()))
    except Exception as exc:  # noqa: BLE001
        raise MultisigError("sponsor puzzle has no key") from exc


@dataclass
class Plan:
    network: str
    safe: Safe
    spends: list[PlannedSpend]
    summary: dict[str, Any]
    sponsor: PlannedSponsor | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": PLAN_VERSION,
            "puzzle": PUZZLE_KIND,
            "network": self.network,
            "safe": self.safe.to_json(),
            "safe_puzzle_hash": self.safe.puzzle_hash().hex(),
            "spends": [spend.to_json() for spend in self.spends],
            "summary": self.summary,
            "sponsor": self.sponsor.to_json() if self.sponsor else None,
        }

    @staticmethod
    def from_json(value: Any) -> "Plan":
        if not isinstance(value, dict):
            raise MultisigError("plan must be an object")
        if int(value.get("version", 0)) != PLAN_VERSION:
            raise MultisigError(f"unsupported plan version {value.get('version')}")
        if value.get("puzzle", PUZZLE_KIND) != PUZZLE_KIND:
            raise MultisigError(f"unsupported plan puzzle {value.get('puzzle')}")
        safe = Safe.from_json(value.get("safe") or {})
        declared = strip0x(value.get("safe_puzzle_hash"))
        if declared and declared != safe.puzzle_hash().hex():
            raise MultisigError("plan safe_puzzle_hash does not match its owners and threshold")
        spends_raw = value.get("spends")
        if not isinstance(spends_raw, list) or not spends_raw:
            raise MultisigError("plan has no spends")
        sponsor_raw = value.get("sponsor")
        return Plan(
            network=str(value.get("network") or "testnet11"),
            safe=safe,
            spends=[PlannedSpend.from_json(s) for s in spends_raw],
            summary=dict(value.get("summary") or {}),
            sponsor=PlannedSponsor.from_json(sponsor_raw) if sponsor_raw else None,
        )

    def materialize(self, selectors: list[int]) -> list[CoinSpend]:
        """Rebuild every coin spend for one selector set, in plan order."""
        if len(selectors) != self.safe.n or sum(1 for s in selectors if s) != self.safe.m:
            raise MultisigError("selectors must have one entry per owner and select exactly M")
        inner_puzzle = self.safe.puzzle()
        result: dict[bytes32, CoinSpend] = {}

        for spend in self.spends:
            if spend.kind == "xch":
                solution = m_of_n_solution(selectors, spend.delegated_puzzle, spend.delegated_solution)
                result[spend.coin.name()] = make_spend(spend.coin, inner_puzzle, solution)

        # CAT coins spend as a ring per asset, so they have to be built together.
        by_asset: dict[bytes32, list[PlannedSpend]] = {}
        for spend in self.spends:
            if spend.kind == "cat":
                by_asset.setdefault(spend.asset_id, []).append(spend)  # type: ignore[arg-type]
        for asset_id, group in by_asset.items():
            spendables = [
                SpendableCAT(
                    spend.coin,
                    asset_id,
                    inner_puzzle,
                    m_of_n_solution(selectors, spend.delegated_puzzle, spend.delegated_solution),
                    lineage_proof=spend.lineage_proof,  # type: ignore[arg-type]
                )
                for spend in group
            ]
            bundle = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendables)
            for coin_spend in bundle.coin_spends:
                result[coin_spend.coin.name()] = coin_spend

        ordered = [result[spend.coin.name()] for spend in self.spends]
        if self.sponsor:
            ordered.append(make_spend(self.sponsor.coin, self.sponsor.puzzle_reveal, self.sponsor.solution()))
        return ordered

    def all_coin_ids(self) -> list[bytes32]:
        ids = [spend.coin.name() for spend in self.spends]
        if self.sponsor:
            ids.append(self.sponsor.coin.name())
        return ids


# ─── Proposal building ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Output:
    puzzle_hash: bytes32
    amount: int
    asset_id: bytes32 | None
    memo: str
    # On-chain memos for the created coin (XCH only). A re-key uses this to
    # create the successor lock's manifest coin inside the very spend that
    # moves the funds, so the successor is described on chain the moment it
    # owns anything.
    memos: tuple[bytes, ...] = ()
    # Whether a CAT payment hints the coin to the puzzle hash it lands on. True
    # for a payment, because that hint is how the recipient's wallet finds it.
    # False for a coin paid into the settlement puzzle to back an offer: nobody
    # owns that coin, and a hint on it names a puzzle no wallet is watching.
    hint: bool = True


def parse_outputs(raw: Any, hrp: str) -> list[Output]:
    if not isinstance(raw, list) or not raw:
        raise MultisigError("a proposal needs at least one output")
    outputs: list[Output] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise MultisigError(f"output {index + 1} must be an object")
        label = f"output {index + 1}"
        if entry.get("address"):
            address = str(entry["address"]).strip().lower()
            expected_prefix = f"{hrp}1"
            if not address.startswith(expected_prefix):
                raise MultisigError(f"{label}: address must start with {expected_prefix}")
            try:
                puzzle_hash = decode_puzzle_hash(address)
            except Exception as exc:
                raise MultisigError(f"{label}: invalid address ({exc})") from exc
        else:
            puzzle_hash = hex32(entry.get("puzzle_hash"), f"{label} puzzle_hash")
        amount = parse_amount(entry.get("amount"), f"{label} amount")
        if amount == 0:
            raise MultisigError(f"{label}: amount must be positive")
        asset_raw = strip0x(entry.get("asset_id"))
        asset_id = None if not asset_raw or asset_raw == "0" * 64 else hex32(asset_raw, f"{label} asset_id")
        outputs.append(Output(puzzle_hash, amount, asset_id, str(entry.get("memo") or "")[:120]))
    return outputs


def select_coins(coins: list[Coin], needed: int, what: str) -> list[Coin]:
    """Largest-first until covered. Keeps the spend small and the change lump big."""
    if needed <= 0:
        return []
    chosen: list[Coin] = []
    total = 0
    for coin in sorted(coins, key=lambda c: (-int(c.amount), c.name())):
        chosen.append(coin)
        total += int(coin.amount)
        if total >= needed:
            return chosen
    available = sum(int(c.amount) for c in coins)
    raise MultisigError(f"insufficient {what}: need {needed} mojos, safe holds {available}")


def announcement_id(coin_id: bytes32, message: bytes) -> bytes32:
    return bytes32(hashlib.sha256(coin_id + message).digest())


def cat_lineage_proof(node: Node, coin: Coin, record: dict[str, Any], cache: dict[bytes32, LineageProof]) -> LineageProof:
    parent_id = coin.parent_coin_info
    if parent_id in cache:
        return cache[parent_id]
    parent = node.coin_record(parent_id)
    if parent is None:
        raise MultisigError(f"parent coin {parent_id.hex()} not found for CAT coin {coin.name().hex()}")
    parent_coin = record_coin(parent)
    spent_height = int(parent.get("spent_block_index") or record.get("confirmed_block_index") or 0)
    if spent_height <= 0:
        raise MultisigError(f"parent coin {parent_id.hex()} has no spend height")
    reveal = node.puzzle_reveal(parent_id, spent_height)
    matched = match_cat_puzzle(uncurry_puzzle(reveal))
    if matched is None:
        raise MultisigError(
            f"CAT coin {coin.name().hex()} has a non-CAT parent; a CAT minted straight into the safe cannot be spent from here"
        )
    _mod_hash, _tail_hash, inner_puzzle = list(matched)
    proof = LineageProof(parent_coin.parent_coin_info, inner_puzzle.get_tree_hash(), parent_coin.amount)
    cache[parent_id] = proof
    return proof


def build_plan(
    node: Node,
    network: str,
    safe: Safe,
    outputs: list[Output],
    fee: int,
    nonce: bytes | None = None,
    sponsor: tuple[Coin, Program] | None = None,
    sponsor_outputs: list[Output] | None = None,
) -> Plan:
    inner_puzzle = safe.puzzle()
    safe_ph = safe.puzzle_hash()
    nonce = nonce or os.urandom(32)
    sponsor_outputs = sponsor_outputs or []
    if sponsor_outputs and sponsor is None:
        raise MultisigError("sponsor outputs need a sponsor coin")

    # With a sponsor the lock pays no fee: the proposer's coin does, and it
    # also carries any sponsor outputs (a re-key's manifest mojo).
    lock_fee = 0 if sponsor else fee
    xch_needed = sum(o.amount for o in outputs if o.asset_id is None) + lock_fee
    cat_needed: dict[bytes32, int] = {}
    for output in outputs:
        if output.asset_id is not None:
            cat_needed[output.asset_id] = cat_needed.get(output.asset_id, 0) + output.amount

    puzzle_hashes = [safe_ph] + [cat_outer_puzzle_hash(asset_id, inner_puzzle) for asset_id in cat_needed]
    records = node.coin_records_by_puzzle_hashes(puzzle_hashes)
    by_ph: dict[bytes32, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("spent"):
            continue
        coin = record_coin(record)
        by_ph.setdefault(coin.puzzle_hash, []).append(record)

    xch_records = {record_coin(r).name(): r for r in by_ph.get(safe_ph, [])}
    xch_coins = select_coins([record_coin(r) for r in xch_records.values()], xch_needed, "XCH")

    cat_selection: dict[bytes32, tuple[list[Coin], dict[bytes32, dict[str, Any]]]] = {}
    for asset_id, needed in cat_needed.items():
        outer_ph = cat_outer_puzzle_hash(asset_id, inner_puzzle)
        asset_records = {record_coin(r).name(): r for r in by_ph.get(outer_ph, [])}
        chosen = select_coins([record_coin(r) for r in asset_records.values()], needed, f"CAT {asset_id.hex()[:12]}…")
        cat_selection[asset_id] = (chosen, asset_records)

    total_coins = len(xch_coins) + sum(len(chosen) for chosen, _ in cat_selection.values())
    if total_coins == 0:
        raise MultisigError("nothing to spend: the proposal moves no value")
    if total_coins > MAX_COINS_PER_PROPOSAL:
        raise MultisigError(f"proposal would spend {total_coins} coins; the limit is {MAX_COINS_PER_PROPOSAL}. Consolidate first.")

    spends: list[PlannedSpend] = []
    primary: tuple[bytes32, bytes] | None = None  # (coin id, nonce)
    change_summary: list[dict[str, Any]] = []
    sponsor_nonce = hashlib.sha256(nonce + b"sponsor").digest()

    def link(conditions: list[Any], coin: Coin) -> None:
        """Bind every spend to the one that carries the outputs."""
        nonlocal primary
        conditions.append([ASSERT_MY_COIN_ID, coin.name()])
        if primary is None:
            primary = (coin.name(), nonce)
            conditions.append([CREATE_COIN_ANNOUNCEMENT, nonce])
            if sponsor is not None:
                # The lock's primary spend will not run without the sponsor.
                conditions.append([ASSERT_COIN_ANNOUNCEMENT, announcement_id(sponsor[0].name(), sponsor_nonce)])
        else:
            conditions.append([ASSERT_COIN_ANNOUNCEMENT, announcement_id(*primary)])

    def delegated(conditions: list[Any]) -> tuple[Program, Program]:
        return Program.to((1, conditions)), Program.to(0)

    # XCH first: it carries the fee, and when the proposal is XCH-only it is
    # the primary spend as well.
    if xch_coins:
        first, rest = xch_coins[0], xch_coins[1:]
        conditions: list[Any] = []
        for output in outputs:
            if output.asset_id is None:
                if output.memos:
                    conditions.append([CREATE_COIN, output.puzzle_hash, output.amount, list(output.memos)])
                else:
                    conditions.append([CREATE_COIN, output.puzzle_hash, output.amount])
        change = sum(int(c.amount) for c in xch_coins) - xch_needed
        if change > 0:
            conditions.append([CREATE_COIN, safe_ph, change])
            change_summary.append({"asset_id": None, "amount": change})
        if lock_fee > 0:
            conditions.append([RESERVE_FEE, lock_fee])
        link(conditions, first)
        dp, ds = delegated(conditions)
        spends.append(PlannedSpend("xch", None, first, None, dp, ds))
        for coin in rest:
            conditions = []
            link(conditions, coin)
            dp, ds = delegated(conditions)
            spends.append(PlannedSpend("xch", None, coin, None, dp, ds))

    lineage_cache: dict[bytes32, LineageProof] = {}
    for asset_id, (chosen, asset_records) in cat_selection.items():
        first, rest = chosen[0], chosen[1:]
        conditions = []
        for output in outputs:
            if output.asset_id == asset_id:
                # The hint memo is what lets the recipient's wallet find the CAT.
                conditions.append([CREATE_COIN, output.puzzle_hash, output.amount, [output.puzzle_hash]])
        change = sum(int(c.amount) for c in chosen) - cat_needed[asset_id]
        if change > 0:
            conditions.append([CREATE_COIN, safe_ph, change, [safe_ph]])
            change_summary.append({"asset_id": asset_id.hex(), "amount": change})
        link(conditions, first)
        dp, ds = delegated(conditions)
        proof = cat_lineage_proof(node, first, asset_records[first.name()], lineage_cache)
        spends.append(PlannedSpend("cat", asset_id, first, proof, dp, ds))
        for coin in rest:
            conditions = []
            link(conditions, coin)
            dp, ds = delegated(conditions)
            proof = cat_lineage_proof(node, coin, asset_records[coin.name()], lineage_cache)
            spends.append(PlannedSpend("cat", asset_id, coin, proof, dp, ds))

    planned_sponsor: PlannedSponsor | None = None
    if sponsor is not None:
        if primary is None:
            raise MultisigError("nothing to spend from the lock: a sponsor cannot authorize on its own")
        sponsor_coin, sponsor_puzzle = sponsor
        sponsor_key = sponsor_key_of(sponsor_puzzle, sponsor_coin)
        sponsor_needed = fee + sum(o.amount for o in sponsor_outputs)
        if int(sponsor_coin.amount) < sponsor_needed:
            raise MultisigError(f"sponsor coin holds {int(sponsor_coin.amount)} mojos; the fee and sponsor outputs need {sponsor_needed}")
        conds: list[Any] = []
        for output in sponsor_outputs:
            conds.append([CREATE_COIN, output.puzzle_hash, output.amount, list(output.memos)] if output.memos else [CREATE_COIN, output.puzzle_hash, output.amount])
        sponsor_change = int(sponsor_coin.amount) - sponsor_needed
        if sponsor_change > 0:
            conds.append([CREATE_COIN, sponsor_coin.puzzle_hash, sponsor_change, [sponsor_coin.puzzle_hash]])
        if fee > 0:
            conds.append([RESERVE_FEE, fee])
        conds.append([ASSERT_MY_COIN_ID, sponsor_coin.name()])
        conds.append([CREATE_COIN_ANNOUNCEMENT, sponsor_nonce])
        conds.append([ASSERT_COIN_ANNOUNCEMENT, announcement_id(*primary)])
        planned_sponsor = PlannedSponsor(sponsor_coin, sponsor_puzzle, sponsor_key, Program.to((1, conds)))

    summary = {
        "outputs": [
            {
                "puzzle_hash": o.puzzle_hash.hex(),
                "address": encode_puzzle_hash(o.puzzle_hash, network_config(network)["hrp"]),
                "amount": o.amount,
                "asset_id": o.asset_id.hex() if o.asset_id else None,
                "memo": o.memo,
            }
            for o in list(outputs) + list(sponsor_outputs)
        ],
        "fee": fee,
        "fee_paid_by": "sponsor" if sponsor else "lock",
        "sponsor": {
            "coin_id": planned_sponsor.coin.name().hex(),
            "pubkey": pubkey_hex(planned_sponsor.pubkey),
            "amount": int(planned_sponsor.coin.amount),
            "outputs": [o.amount for o in sponsor_outputs],
        } if planned_sponsor else None,
        "change": change_summary,
        "coin_count": total_coins + (1 if planned_sponsor else 0),
        "nonce": nonce.hex(),
    }
    plan = Plan(network, safe, spends, summary, planned_sponsor)
    # Never hand out a plan that cannot run. A selector set is arbitrary here.
    validate_bundle(plan, plan.materialize(safe.selectors_for(list(safe.pubkeys[: safe.m]))), None)
    return plan


# ─── Signatures ──────────────────────────────────────────────────────────────


def expected_signature_pairs(plan: Plan, coin_spends: list[CoinSpend]) -> list[tuple[G1Element, bytes]]:
    """Every (key, message) the assembled bundle demands a signature for.

    Lock spends emit AGG_SIG_UNSAFE over the delegated puzzle hash. The
    sponsor spend, if any, emits AGG_SIG_ME, whose message consensus extends
    with the coin id and the network's genesis challenge; the pair is built
    the same way here so the aggregate can be checked before broadcast.
    """
    pairs: list[tuple[G1Element, bytes]] = []
    lock_count = len(plan.spends)
    for index, spend in enumerate(coin_spends):
        conditions = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_CLVM_COST)
        is_sponsor = plan.sponsor is not None and index == lock_count
        for condition in conditions.get(AGG_SIG_UNSAFE, []):
            if is_sponsor:
                raise MultisigError("the sponsor spend must not emit AGG_SIG_UNSAFE")
            pairs.append((G1Element.from_bytes(condition.vars[0]), bytes(condition.vars[1])))
        for condition in conditions.get(AGG_SIG_ME, []):
            if not is_sponsor:
                raise MultisigError("unexpected AGG_SIG_ME in a safe spend")
            pairs.append((G1Element.from_bytes(condition.vars[0]), bytes(condition.vars[1]) + bytes(spend.coin.name()) + AGG_SIG_ME_DATA[plan.network]))
    return pairs


def validate_bundle(plan: Plan, coin_spends: list[CoinSpend], signature: G2Element | None) -> dict[str, Any]:
    """Run every spend and check that what it emits is what the plan promised.

    A wallet only ever signs the delegated puzzle hash, so this is the place
    where "the puzzle does what the summary says" is actually enforced: the
    AGG_SIG messages must be the plan's messages, and every spend must bind
    its own coin id. With a signature, the aggregate must verify too.
    """
    pairs = expected_signature_pairs(plan, coin_spends)
    messages = {bytes(spend.message) for spend in plan.spends}
    if plan.sponsor:
        messages.add(plan.sponsor.message(plan.network))
    for _key, message in pairs:
        if bytes(message) not in messages:
            raise MultisigError("a spend emits a signature message that is not in the plan")
    expected_count = len(plan.spends) * plan.safe.m + (1 if plan.sponsor else 0)
    if len(pairs) != expected_count:
        raise MultisigError(f"expected {expected_count} AGG_SIG conditions, puzzle emitted {len(pairs)}")

    expected_coins = [spend.coin for spend in plan.spends] + ([plan.sponsor.coin] if plan.sponsor else [])
    if len(coin_spends) != len(expected_coins):
        raise MultisigError("assembled bundle has the wrong number of spends")
    for spend, coin in zip(coin_spends, expected_coins):
        conditions = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_CLVM_COST)
        my_ids = [bytes(c.vars[0]) for c in conditions.get(ASSERT_MY_COIN_ID, [])]
        if coin.name() not in my_ids:
            raise MultisigError("a spend does not assert its own coin id; refusing a replayable signature")

    if signature is not None:
        if not AugSchemeMPL.aggregate_verify([k for k, _ in pairs], [m for _, m in pairs], signature):
            raise MultisigError("aggregated signature does not verify against the assembled spends")
    return {"agg_sig_count": len(pairs)}


def find_signed_roles(
    owner_candidates: list[G1Element],
    owner_messages: Callable[[G1Element], list[bytes]],
    sponsor: tuple[G1Element, bytes] | None,
    signature: G2Element,
) -> tuple[list[G1Element], bool] | None:
    """Which owner keys, and whether the sponsor, ``signature`` covers.

    Roles, not keys: with Sage an owner's key is often also the key of the
    wallet coin sponsoring the fee, so the same key may sign two different
    messages. Each owner slot carries the vote messages; the sponsor slot
    carries the fee coin's AGG_SIG_ME. Subsets are tried smallest-first so a
    single-role share is recognized as such.
    """
    seen: set[bytes] = set()
    owners: list[G1Element] = []
    for key in owner_candidates:
        if bytes(key) not in seen:
            seen.add(bytes(key))
            owners.append(key)
    slots: list[tuple[str, G1Element, list[bytes]]] = [("owner", key, owner_messages(key)) for key in owners]
    if sponsor is not None:
        slots.append(("sponsor", sponsor[0], [sponsor[1]]))
    for size in range(1, len(slots) + 1):
        for subset in itertools.combinations(slots, size):
            keys = [key for _role, key, msgs in subset for _ in msgs]
            msgs = [m for _role, _key, ms in subset for m in ms]
            if AugSchemeMPL.aggregate_verify(keys, msgs, signature):
                return ([key for role, key, _ in subset if role == "owner"], any(role == "sponsor" for role, _, _ in subset))
    return None


def find_signed_subset(plan: Plan, candidates: list[G1Element], signature: G2Element) -> tuple[list[G1Element], bool] | None:
    lock_messages = [bytes(spend.message) for spend in plan.spends]
    sponsor = (plan.sponsor.pubkey, plan.sponsor.message(plan.network)) if plan.sponsor else None
    return find_signed_roles(candidates, lambda _key: lock_messages, sponsor, signature)


def choose_shares(
    safe: Safe,
    shares: list[tuple[list[G1Element], bool, G2Element]],
    need_sponsor: bool = False,
) -> tuple[list[G1Element], list[G2Element]] | None:
    """Pick disjoint shares whose owner keys are exactly M distinct owners.

    Each share is (owner keys it covers, whether it also carries the sponsor's
    fee-coin signature, aggregate). With a sponsor, exactly one chosen share
    must carry that signature; it never counts towards M.
    """
    indexed = []
    for keys, has_sponsor, sig in shares:
        try:
            owner_indexes = frozenset(safe.key_index(key) for key in keys)
        except MultisigError:
            continue
        indexed.append((owner_indexes, has_sponsor, keys, sig))
    for size in range(1, len(indexed) + 1):
        for combo in itertools.combinations(indexed, size):
            union: set[int] = set()
            disjoint = True
            sponsors = 0
            for idx_set, has_sponsor, _, _ in combo:
                if union & idx_set:
                    disjoint = False
                    break
                union |= idx_set
                sponsors += 1 if has_sponsor else 0
            if disjoint and len(union) == safe.m and sponsors == (1 if need_sponsor else 0):
                return ([k for _, _, keys, _ in combo for k in keys], [sig for _, _, _, sig in combo])
    return None


def parse_shares(raw: Any) -> list[tuple[list[G1Element], bool, G2Element]]:
    shares: list[tuple[list[G1Element], bool, G2Element]] = []
    for share in raw or []:
        if not isinstance(share, dict):
            continue
        keys = [parse_pubkey(k, "share key") for k in share.get("keys") or []]
        raw_sig = strip0x(share.get("signature"))
        sponsor_signed = bool(share.get("sponsor_signed", share.get("sponsorSigned", False)))
        if len(raw_sig) == 192 and (keys or sponsor_signed):
            shares.append((keys, sponsor_signed, G2Element.from_bytes(bytes.fromhex(raw_sig))))
    return shares


# ─── Commands ────────────────────────────────────────────────────────────────


def cmd_derive(payload: dict[str, Any], _node_factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    config = network_config(network)
    safe = Safe.from_json(payload)
    inner = safe.puzzle()
    ph = safe.puzzle_hash()
    return {
        "success": True,
        "network": network,
        "puzzle": PUZZLE_KIND,
        "mod_hash": M_OF_N_MOD.get_tree_hash().hex(),
        "m": safe.m,
        "n": safe.n,
        "pubkeys": [pubkey_hex(k) for k in safe.pubkeys],
        # Each owner's own wallet address. Sage reports its derivations' synthetic
        # keys, and the standard puzzle curried with that key is the address the
        # wallet shows for it — so this is where a co-owner would be paid directly.
        "owners": [
            {"pubkey": pubkey_hex(k), "address": encode_puzzle_hash(puzzle_hash_for_synthetic_public_key(k), config["hrp"])}
            for k in safe.pubkeys
        ],
        "puzzle_hash": ph.hex(),
        "address": encode_puzzle_hash(ph, config["hrp"]),
        "puzzle_reveal": bytes(inner).hex(),
    }


def cmd_balance(payload: dict[str, Any], node_factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    config = network_config(network)
    node = node_factory(str(payload.get("node_url") or config["node_url"]))
    safe = Safe.from_json(payload)
    inner = safe.puzzle()
    safe_ph = safe.puzzle_hash()

    asset_ids: list[bytes32] = []
    seen: set[bytes32] = set()
    for raw in payload.get("asset_ids") or []:
        asset_id = hex32(raw, "asset_id")
        if asset_id not in seen and asset_id != bytes32.zeros:
            seen.add(asset_id)
            asset_ids.append(asset_id)

    outer = {cat_outer_puzzle_hash(asset_id, inner): asset_id for asset_id in asset_ids}
    records = node.coin_records_by_puzzle_hashes([safe_ph] + list(outer))

    xch_coins: list[dict[str, Any]] = []
    cats: dict[bytes32, list[dict[str, Any]]] = {asset_id: [] for asset_id in asset_ids}
    for record in records:
        if record.get("spent"):
            continue
        coin = record_coin(record)
        entry = {
            **coin_to_json(coin),
            "coin_id": coin.name().hex(),
            "confirmed_block_index": int(record.get("confirmed_block_index") or 0),
            "timestamp": int(record.get("timestamp") or 0),
        }
        if coin.puzzle_hash == safe_ph:
            xch_coins.append(entry)
        elif coin.puzzle_hash in outer:
            cats[outer[coin.puzzle_hash]].append(entry)

    return {
        "success": True,
        "network": network,
        "puzzle_hash": safe_ph.hex(),
        "address": encode_puzzle_hash(safe_ph, config["hrp"]),
        "xch": {"balance": sum(c["amount"] for c in xch_coins), "coins": xch_coins},
        "cats": [
            {"asset_id": asset_id.hex(), "balance": sum(c["amount"] for c in coins), "coins": coins}
            for asset_id, coins in cats.items()
            if coins
        ],
    }


def successor_output(successor_raw: Any, hrp: str) -> tuple[Output, dict[str, Any]]:
    """The 1-mojo manifest coin for a re-key's successor lock.

    Imported lazily: multisig_profile imports this module for its helpers, and
    the manifest encoding is the one thing this module needs back from it.
    """
    import multisig_profile as profile

    policy = profile.normalize_safes([successor_raw])
    if len(policy) != 1 or "owners" not in policy[0]:
        raise MultisigError("a re-key needs the successor's full policy: name, threshold, owners")
    target = profile.policy_puzzle_hash(policy[0])
    memos = (bytes(target), profile.MANIFEST_TAG, profile.encode_safe(policy[0]))
    info = {
        "puzzle_hash": target.hex(),
        "address": encode_puzzle_hash(target, hrp),
        "name": policy[0]["name"],
        "m": policy[0]["m"],
        "owners": policy[0]["owners"],
    }
    return Output(target, profile.PROFILE_AMOUNT, None, "manifest", memos), info


def cmd_propose(payload: dict[str, Any], node_factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    config = network_config(network)
    node = node_factory(str(payload.get("node_url") or config["node_url"]))
    safe = Safe.from_json(payload.get("safe") or payload)
    outputs: list[Output] = []
    fee = parse_amount(payload.get("fee", 0), "fee")
    nonce_raw = strip0x(payload.get("nonce"))
    nonce = bytes.fromhex(nonce_raw) if nonce_raw else None

    successor_info = None
    if payload.get("successor") is not None:
        manifest_output, successor_info = successor_output(payload["successor"], config["hrp"])
        if manifest_output.puzzle_hash == safe.puzzle_hash():
            raise MultisigError("the successor has the same owners and threshold as this lock; nothing to change")
        # Outputs addressed "successor" go to the successor lock: the caller
        # cannot know that address before the policy is derived here.
        raw_outputs = [
            {**entry, "address": None, "puzzle_hash": manifest_output.puzzle_hash.hex()}
            if isinstance(entry, dict) and str(entry.get("address") or "").strip().lower() == "successor"
            else entry
            for entry in (payload.get("outputs") or [])
        ]
        outputs = parse_outputs(raw_outputs, config["hrp"]) if raw_outputs else []
        outputs = [manifest_output] + outputs
    elif payload.get("outputs"):
        outputs = parse_outputs(payload.get("outputs"), config["hrp"])
    if not outputs:
        raise MultisigError("a proposal needs at least one output")

    # A sponsor: the proposer's own wallet coins. The largest one that covers
    # the fee (plus, for a re-key, the manifest mojo) rides along and pays.
    sponsor: tuple[Coin, Program] | None = None
    sponsor_outputs: list[Output] = []
    sponsor_raw = payload.get("sponsor")
    if isinstance(sponsor_raw, dict) and isinstance(sponsor_raw.get("coins"), list):
        if successor_info is not None:
            # The manifest mojo comes from the sponsor, not the lock.
            sponsor_outputs = [outputs[0]]
            outputs = outputs[1:]
        needed = fee + sum(o.amount for o in sponsor_outputs)
        candidates: list[tuple[Coin, Program]] = []
        for entry in sponsor_raw["coins"]:
            if not isinstance(entry, dict):
                continue
            reveal = strip0x(entry.get("puzzle") or entry.get("puzzle_reveal") or "")
            if not reveal:
                continue
            coin = coin_from_json(entry.get("coin"))
            if int(coin.amount) >= needed:
                candidates.append((coin, Program.from_bytes(bytes.fromhex(reveal))))
        if not candidates:
            raise MultisigError(f"the proposer's wallet has no XCH coin covering {needed} mojos for the fee{' and the manifest' if sponsor_outputs else ''}")
        sponsor = max(candidates, key=lambda pair: int(pair[0].amount))
        sponsor_key_of(sponsor[1], sponsor[0])
        if not outputs and not payload.get("outputs"):
            pass

    plan = build_plan(node, network, safe, outputs, fee, nonce, sponsor, sponsor_outputs)
    if successor_info is not None:
        plan.summary["kind"] = "rekey"
        plan.summary["successor"] = successor_info
    return {
        "success": True,
        "plan": plan.to_json(),
        "messages": [spend.message.hex() for spend in plan.spends],
        "coin_ids": [coin_id.hex() for coin_id in plan.all_coin_ids()],
    }


def cmd_sign_request(payload: dict[str, Any], _node_factory: Callable[[str], Node]) -> dict[str, Any]:
    plan = Plan.from_json(payload.get("plan"))
    signer = parse_pubkey(payload.get("signer"), "signer")
    selectors = plan.safe.signing_selectors(signer)
    coin_spends = plan.materialize(selectors)
    validate_bundle(plan, coin_spends, None)
    selected = [pubkey_hex(k) for k, flag in zip(plan.safe.pubkeys, selectors) if flag]
    return {
        "success": True,
        "signer": pubkey_hex(signer),
        "selected_pubkeys": selected,
        "coin_spends": [spend_to_json(s) for s in coin_spends],
        "messages": [spend.message.hex() for spend in plan.spends],
    }


def cmd_verify_share(payload: dict[str, Any], _node_factory: Callable[[str], Node]) -> dict[str, Any]:
    plan = Plan.from_json(payload.get("plan"))
    raw_sig = strip0x(payload.get("signature"))
    if len(raw_sig) != 192:
        raise MultisigError("signature must be a 96-byte BLS G2 element (192 hex chars)")
    try:
        signature = G2Element.from_bytes(bytes.fromhex(raw_sig))
    except Exception as exc:
        raise MultisigError(f"signature is not a valid BLS G2 element: {exc}") from exc

    candidates_raw = payload.get("candidates")
    candidates = (
        [parse_pubkey(c, "candidate") for c in candidates_raw]
        if isinstance(candidates_raw, list) and candidates_raw
        else list(plan.safe.pubkeys)
    )
    for key in candidates:
        plan.safe.key_index(key)
    found = find_signed_subset(plan, candidates, signature)
    if found is None:
        raise MultisigError("signature does not verify for any owner key over this proposal's spends")
    owner_keys, sponsor_signed = found
    if len(owner_keys) > plan.safe.m:
        raise MultisigError(f"signature covers {len(owner_keys)} owner keys, more than the threshold of {plan.safe.m}; it cannot be used")
    return {
        "success": True,
        "keys": [pubkey_hex(k) for k in owner_keys],
        "owner_keys": [pubkey_hex(k) for k in owner_keys],
        "sponsor_signed": sponsor_signed,
        "signature": bytes(signature).hex(),
    }


def cmd_assemble(payload: dict[str, Any], node_factory: Callable[[str], Node]) -> dict[str, Any]:
    plan = Plan.from_json(payload.get("plan"))
    shares = parse_shares(payload.get("shares"))
    if not shares:
        raise MultisigError("no signature shares to assemble")

    chosen = choose_shares(plan.safe, shares, plan.sponsor is not None)
    if chosen is None:
        signed = {pubkey_hex(k) for keys, _, _ in shares for k in keys}
        detail = " (the proposer's wallet signature for the fee coin is also required)" if plan.sponsor else ""
        raise MultisigError(
            f"cannot reach the threshold of {plan.safe.m} from the {len(signed)} signed key(s) with non-overlapping shares{detail}"
        )
    keys, signatures = chosen
    selectors = plan.safe.selectors_for(keys)
    coin_spends = plan.materialize(selectors)
    aggregate = AugSchemeMPL.aggregate(signatures)
    check = validate_bundle(plan, coin_spends, aggregate)
    bundle = WalletSpendBundle(coin_spends, aggregate)

    result: dict[str, Any] = {
        "success": True,
        "signers": [pubkey_hex(k) for k in keys],
        "selectors": selectors,
        "agg_sig_count": check["agg_sig_count"],
        "spend_bundle": {
            "coin_spends": [spend_to_json(s) for s in coin_spends],
            "aggregated_signature": bytes(aggregate).hex(),
        },
        "spend_bundle_id": bundle.name().hex(),
    }

    if payload.get("push"):
        config = network_config(plan.network)
        node = node_factory(str(payload.get("node_url") or config["node_url"]))
        pushed = node.push_tx(bundle)
        status = str(pushed.get("status") or "").upper()
        ok = bool(pushed.get("success")) and status in ("", "SUCCESS", "PENDING")
        result["push"] = {"ok": ok, "status": status or None, "raw": pushed}
        if not ok:
            result["success"] = False
            result["error"] = f"push_tx rejected: {pushed.get('error') or status or json.dumps(pushed)[:300]}"
    return result


def cmd_status(payload: dict[str, Any], node_factory: Callable[[str], Node]) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    config = network_config(network)
    node = node_factory(str(payload.get("node_url") or config["node_url"]))
    coin_ids = [hex32(c, "coin_id") for c in payload.get("coin_ids") or []]
    if not coin_ids:
        raise MultisigError("no coin ids to check")
    coins = []
    for coin_id in coin_ids:
        record = node.coin_record(coin_id)
        coins.append({
            "coin_id": coin_id.hex(),
            "found": record is not None,
            "spent": bool(record and record.get("spent")),
            "spent_block_index": int((record or {}).get("spent_block_index") or 0),
        })
    return {
        "success": True,
        "coins": coins,
        "all_spent": all(c["spent"] for c in coins),
        "any_spent": any(c["spent"] for c in coins),
    }


COMMANDS: dict[str, Callable[[dict[str, Any], Callable[[str], Node]], dict[str, Any]]] = {
    "derive": cmd_derive,
    "balance": cmd_balance,
    "propose": cmd_propose,
    "sign-request": cmd_sign_request,
    "verify-share": cmd_verify_share,
    "assemble": cmd_assemble,
    "status": cmd_status,
}


def run(command: str, payload: dict[str, Any], node_factory: Callable[[str], Node] = Node) -> dict[str, Any]:
    handler = COMMANDS.get(command)
    if handler is None:
        raise MultisigError(f"unknown command {command!r}; expected one of {sorted(COMMANDS)}")
    return handler(payload, node_factory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args()
    # UTF-8 on both pipes whatever the console code page is: labels and names
    # may carry emoji, and JSON output must survive the trip back unchanged.
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
    except Exception as exc:  # noqa: BLE001 — the route needs a JSON line either way
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
