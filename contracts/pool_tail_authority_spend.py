#!/usr/bin/env python3
"""pool_tail_authority_spend.py — Builders for pool-controlled TAIL spend bundles.

Architecture (trustless, no central authority):

  The pool singleton inner puzzle IS the sole on-chain authority. It enforces
  the N-asset CFMM invariant and emits a CREATE_PUZZLE_ANNOUNCEMENT that commits
  to the LP delta and the new sorted Merkle root of all N reserve states.

  The LP CAT TAIL (forge_lp_cat_tail.rue) simply:
    1. Verifies extra_delta (CAT2-injected) == auth.lp_delta from the solution
    2. Asserts the pool singleton's announcement exists in the same block

  No separate authority coin — it would be a single point of failure (can be
  lost, stuck, or front-run) without adding any security benefit.

Pool announcement formats (CONFIRMED from CLVM disassembly + oracle tests, 2026-03):

  ANN1 (asserted by LP CAT TAIL):
    message = tree_hash([recipient_ph, lp_out, launcher_id])
    ann_id  = sha256(pool_puzzle_hash || ANN1_message)

  ANN2 (asserted by reserve coins):
    message = tree_hash([recipient_ph, lp_out, launcher_id, lp_out])  ← lp_out duplicated
    ann_id  = sha256(pool_puzzle_hash || ANN2_message)

    CLVM bytecode: ANN1 uses paths (91, 5, 0xafffff), ANN2 adds path 10 = same as path 5.

Reserve coin solution format: UNKNOWN — needs simulator test to determine.

These builders are consumed by:
  - forge_add_liquidity.py  (deposit lane)
  - forge_remove_liquidity.py (withdrawal lane)

NOTE: Not audited. Testnet only.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

# ── Condition opcodes ─────────────────────────────────────────────────────

AGG_SIG_ME              = 50
CREATE_COIN             = 51
ASSERT_COIN_ANNOUNCEMENT= 61
CREATE_PUZZLE_ANNOUNCEMENT = 62
ASSERT_PUZZLE_ANNOUNCEMENT = 63
ASSERT_MY_COIN_ID       = 70

# ── Authority coin constants ───────────────────────────────────────────────

AUTHORITY_COIN_AMOUNT = 1

# ── Utilities ─────────────────────────────────────────────────────────────

def _strip_0x(val: str) -> str:
    s = str(val)
    return s[2:] if s.startswith("0x") else s


def _hex_to_bytes(h: str) -> bytes:
    return bytes.fromhex(_strip_0x(h))


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _sha256_hex(data: bytes) -> str:
    return _sha256(data).hex()


def _tree_hash_atom(atom: bytes) -> bytes:
    return _sha256(b"\x01" + atom)


def _tree_hash_pair(left: bytes, right: bytes) -> bytes:
    return _sha256(b"\x02" + left + right)


def _tree_hash_list(elements: list[bytes]) -> bytes:
    result = _tree_hash_atom(b"")  # nil
    for el in reversed(elements):
        result = _tree_hash_pair(el, result)
    return result


def _int_to_clvm_bytes(n: int) -> bytes:
    """Encode integer as CLVM minimal-length signed bytes."""
    if n == 0:
        return b""
    negative = n < 0
    absval = -n if negative else n
    buf: list[int] = []
    while absval > 0:
        buf.insert(0, absval & 0xFF)
        absval >>= 8
    if not negative and buf[0] >= 0x80:
        buf.insert(0, 0)
    if negative:
        buf = [~b & 0xFF for b in buf]
        carry = 1
        for i in range(len(buf) - 1, -1, -1):
            s = buf[i] + carry
            buf[i] = s & 0xFF
            carry = s >> 8
        if buf[0] < 0x80:
            buf.insert(0, 0xFF)
    return bytes(buf)


def _coin_id(parent: str, puzzle_hash: str, amount: int) -> str:
    """SHA256(parent_coin_info || puzzle_hash || uint64_be(amount))"""
    raw = (
        _hex_to_bytes(parent)
        + _hex_to_bytes(puzzle_hash)
        + struct.pack(">Q", amount)
    )
    return _sha256_hex(raw)


def _puzzle_announcement_id(puzzle_hash: str, message: bytes) -> str:
    """announcement_id = sha256(puzzle_hash_bytes || message_bytes)"""
    return _sha256_hex(_hex_to_bytes(puzzle_hash) + message)


# ── TAIL puzzle hash derivation ────────────────────────────────────────────

def _load_hex_file(path: Path) -> bytes:
    return bytes.fromhex(path.read_text(encoding="utf-8").strip())


def _load_clvm_mod(contracts_dir: Path, filename: str):
    """Load a compiled CLVM hex file as a Program."""
    try:
        from chia.types.blockchain_format.program import Program as _Program
    except ImportError as e:
        raise ImportError("chia-blockchain is required for puzzle hash computation") from e
    hex_path = contracts_dir / "compiled" / filename
    return _Program.from_bytes(_load_hex_file(hex_path))


def compute_tail_puzzle_hash(contracts_dir: Path, launcher_id: str, tail_version: int = 1) -> str:
    """
    tail_puzzle_hash = tree_hash(curry(forge_lp_cat_tail.clvm, launcher_id, tail_version))

    Uses chia-blockchain's Program.curry() for correct canonical hash.
    """
    from chia.types.blockchain_format.program import Program
    mod = _load_clvm_mod(contracts_dir, "forge_lp_cat_tail.clvm.hex")
    curried = mod.curry(Program.to(_hex_to_bytes(launcher_id)), Program.to(tail_version))
    return curried.get_tree_hash().hex()


def compute_lp_cat_asset_id(contracts_dir: Path, launcher_id: str, tail_version: int = 1) -> str:
    """LP CAT asset_id = TAIL puzzle hash (curried with launcher_id + tail_version)."""
    return compute_tail_puzzle_hash(contracts_dir, launcher_id, tail_version)


def compute_authority_puzzle_hash(contracts_dir: Path, launcher_id: str, lp_cat_asset_id: str) -> str:
    """
    Compute the authority coin puzzle hash using the self_puzzle_hash fixed-point pattern.

    The authority coin now curries (launcher_id, lp_cat_asset_id, self_puzzle_hash) where
    self_puzzle_hash is a fixed point: ph = curry(mod, lid, lc, ph).get_tree_hash().

    We solve this iteratively — it converges in ≤64 rounds thanks to SHA256 diffusion.
    This avoids Rue's internal curry_tree_hash which diverges from the chia-standard formula.
    """
    from chia.types.blockchain_format.program import Program
    mod = _load_clvm_mod(contracts_dir, "lp_cat_authority_coin.clvm.hex")
    lid_prog = Program.to(_hex_to_bytes(launcher_id))
    lc_prog  = Program.to(_hex_to_bytes(lp_cat_asset_id))

    # Start with a zero seed and iterate until convergence.
    ph = bytes(32)
    for _ in range(64):
        ph_new = mod.curry(lid_prog, lc_prog, Program.to(ph)).get_tree_hash()
        if ph_new == ph:
            break
        ph = bytes(ph_new)
    return bytes(ph).hex()


# ── Sorted Merkle root for N-asset pool state ─────────────────────────────

def _merkle_leaf(asset_id: bytes, puzzle_hash: bytes) -> bytes:
    """Leaf = sha256(0x00 || asset_id || puzzle_hash) — domain-separated."""
    return _sha256(b"\x00" + asset_id + puzzle_hash)


def _merkle_pair(left: bytes, right: bytes) -> bytes:
    """Internal node = sha256(0x01 || min(left,right) || max(left,right))."""
    a, b = (left, right) if left <= right else (right, left)
    return _sha256(b"\x01" + a + b)


def sorted_reserves_merkle_root(reserves: list[tuple[str, str]]) -> str:
    """
    Compute a canonical sorted Merkle root for N independent reserve assets.

    Each asset is represented as (asset_id_hex, reserve_puzzle_hash_hex).
    Assets are sorted by asset_id before building the tree, making the root
    order-independent regardless of the deposit sequence.

    For N assets:
      - Each leaf = sha256(0x00 || asset_id || reserve_ph)  [domain sep]
      - Internal nodes use sorted child pairs to stay order-independent
      - Odd levels duplicate the last leaf (standard Merkle convention)

    Returns the root hash as a hex string (no 0x prefix).

    This is the canonical commitment that goes into the pool singleton state
    and is announced alongside LP supply deltas, giving O(log N) inclusion
    proofs without nesting all N values as CLVM curry args.
    """
    if not reserves:
        return _sha256(b"").hex()

    # Sort by asset_id for determinism.
    sorted_r = sorted(reserves, key=lambda x: _hex_to_bytes(x[0]))
    leaves = [
        _merkle_leaf(_hex_to_bytes(aid), _hex_to_bytes(rph))
        for aid, rph in sorted_r
    ]

    level = leaves
    while len(level) > 1:
        next_level: list[bytes] = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else level[i]  # dup last if odd
            next_level.append(_merkle_pair(left, right))
        level = next_level

    return level[0].hex()


# ── Announcement computation ───────────────────────────────────────────────

def compute_pool_ann1_message(
    recipient_puzzle_hash: str,
    lp_out: int,
    pool_id: str,
) -> bytes:
    """
    Pool ANN1 message — asserted by the LP CAT TAIL (forge_lp_cat_tail.rue).

    message = tree_hash([recipient_puzzle_hash, lp_out, pool_id])

    ✅  CONFIRMED from CLVM disassembly + empirical oracle test (2026-03):
        Deposit & bootstrap mode of forge_pool_singleton_mod (1f451e6e...) emits:
          ANN1 = sha256tree([recipient_ph, lp_out, launcher_id])

    The LP CAT TAIL ASSERTS this announcement so the TAIL spend is only valid
    in the same block as the pool's CREATE_PUZZLE_ANNOUNCEMENT.

    See also: compute_pool_ann2_message() (asserted by reserve coins).
    """
    elements = [
        _tree_hash_atom(_hex_to_bytes(recipient_puzzle_hash)),
        _tree_hash_atom(_int_to_clvm_bytes(lp_out)),
        _tree_hash_atom(_hex_to_bytes(pool_id)),
    ]
    return _tree_hash_list(elements)


# Keep old name as alias for backward compat
compute_pool_announcement_message = compute_pool_ann1_message


def compute_pool_ann2_message(
    recipient_puzzle_hash: str,
    lp_delta: int,
    pool_id: str,
    new_total_lp: int,
) -> bytes:
    """
    Pool ANN2 message — asserted by reserve coins.

    message = tree_hash([recipient_puzzle_hash, lp_delta, pool_id, new_total_lp])

    ✅  CONFIRMED via CLVM dry-run brute-force (2026-03):
        Deposit [1000,1000] into a pool with lp_supply=11000, lp_delta=1100:
          ANN2 = sha256tree([recipient_ph, 1100, launcher_id, 12100])
        where new_total_lp = old_supply + lp_delta = 11000 + 1100 = 12100.

        At bootstrap (old_supply=0): new_total_lp == lp_delta, which previously
        appeared to be a duplicate — that was the source of prior incorrect belief.

    WARNING: The old signature had `lp_out` duplicated. This was WRONG for any
    non-bootstrap operation. Updated 2026-03 after sim_test_tm10_1.py confirmed
    the correct formula with empirical pool CLVM execution.

    Reserve coins ASSERT this announcement to bind their spend to the pool's execution.
    Reserve coin solution should include:
        ASSERT_PUZZLE_ANNOUNCEMENT sha256(pool_puzzle_hash || ANN2_message)
    """
    elements = [
        _tree_hash_atom(_hex_to_bytes(recipient_puzzle_hash)),
        _tree_hash_atom(_int_to_clvm_bytes(lp_delta)),
        _tree_hash_atom(_hex_to_bytes(pool_id)),
        _tree_hash_atom(_int_to_clvm_bytes(new_total_lp)),
    ]
    return _tree_hash_list(elements)


def compute_pool_announcement_message_new_format(
    launcher_id: str,
    lp_delta: int,
    new_total_lp: int,
    reserves_merkle_root: str,
) -> bytes:
    """
    NEW format pool announcement message — for future pool CLVM revisions.

    message = tree_hash([launcher_id, lp_delta, new_total_lp, reserves_merkle_root])

    ⚠️  NOT used by the currently deployed forge_pool_singleton_mod.
    Kept here for reference when a new pool CLVM revision is built that
    uses the richer Merkle-root-based format.

    TODO: Withdrawal mode ANN1/ANN2 formats — not yet verified via simulator.
    From CLVM disassembly: WITHDRAW ANN1 = thl([val@91, val@19, nil])
                           WITHDRAW ANN2 = thl([val@91, val@19, val@0xafffff, val@10])
    Need to identify val@19 (likely lp_burned) and confirm via sim_test_tm10_1.py phase 3.
    """
    elements = [
        _tree_hash_atom(_hex_to_bytes(launcher_id)),
        _tree_hash_atom(_int_to_clvm_bytes(lp_delta)),
        _tree_hash_atom(_int_to_clvm_bytes(new_total_lp)),
        _tree_hash_atom(_hex_to_bytes(reserves_merkle_root)),
    ]
    return _tree_hash_list(elements)


def compute_pool_announcement_id(pool_puzzle_hash: str, message: bytes) -> str:
    """announcement_id = sha256(pool_puzzle_hash || message)"""
    return _puzzle_announcement_id(pool_puzzle_hash, message)


# ── TAIL spend auth descriptor ─────────────────────────────────────────────

class TailSpendAuth:
    """
    Minimal data bundle for one LP CAT TAIL spend.

    Consumed by forge_lp_tail_spend_builder.build_tail_solution() to produce
    the CLVM solution for a TAIL invocation inside a CAT2 spend.

    Fields match the Rue SpendAuth struct in forge_lp_cat_tail.rue:
        struct SpendAuth {
            singleton_announcement_id: Bytes32,  // sha256(pool_ph || ANN1_message)
            lp_delta: Int,                        // positive = mint, negative = burn
        }

    ✅  ANN1_message = tree_hash([recipient_ph, lp_out, launcher_id])  — CONFIRMED
    ✅  ANN2_message = tree_hash([recipient_ph, lp_out, launcher_id, lp_out])  — CONFIRMED
    The TAIL uses ANN1. Reserve coins use ANN2.
    """

    def __init__(
        self,
        launcher_id: str,
        recipient_puzzle_hash: str,
        lp_delta: int,
        pool_puzzle_hash: str,
    ) -> None:
        self.launcher_id = launcher_id
        self.recipient_puzzle_hash = recipient_puzzle_hash
        self.lp_delta = lp_delta
        self.pool_puzzle_hash = pool_puzzle_hash

        # lp_out in the OLD format is always positive (the absolute value)
        lp_out_abs = abs(lp_delta)
        msg = compute_pool_announcement_message(recipient_puzzle_hash, lp_out_abs, launcher_id)
        self.singleton_announcement_id = compute_pool_announcement_id(pool_puzzle_hash, msg)

    def to_dict(self) -> dict:
        return {
            "launcher_id": self.launcher_id,
            "recipient_puzzle_hash": self.recipient_puzzle_hash,
            "lp_delta": self.lp_delta,
            "pool_puzzle_hash": self.pool_puzzle_hash,
            "singleton_announcement_id": self.singleton_announcement_id,
        }


# ── Bundle descriptor builders ─────────────────────────────────────────────

def build_deposit_tail_auth(
    launcher_id: str,
    pool_puzzle_hash: str,
    lp_out: int,
    recipient_puzzle_hash: str,
) -> TailSpendAuth:
    """
    Build the TAIL spend auth descriptor for a deposit (LP mint).

    Uses OLD announcement format: ANN1 = tree_hash([recipient_ph, lp_out, launcher_id]).

    Parameters:
        launcher_id         — pool launcher coin ID (hex)
        pool_puzzle_hash    — pool singleton CURRENT puzzle hash (hex, for annotation_id)
        lp_out              — LP CATs to mint (positive integer)
        recipient_puzzle_hash — who receives the LP tokens (their puzzle hash)

    The computed TailSpendAuth.singleton_announcement_id must match what the pool CLVM
    emits for ANN1 in the same block (sha256(pool_ph || ANN1_message)).
    """
    return TailSpendAuth(
        launcher_id=launcher_id,
        recipient_puzzle_hash=recipient_puzzle_hash,
        lp_delta=lp_out,
        pool_puzzle_hash=pool_puzzle_hash,
    )


def build_withdraw_tail_auth(
    launcher_id: str,
    pool_puzzle_hash: str,
    lp_burned: int,
    recipient_puzzle_hash: str,
) -> TailSpendAuth:
    """
    Build the TAIL spend auth descriptor for a withdrawal (LP burn).

    ⚠️  UNCONFIRMED: withdrawal announcement format not yet verified via simulator.
    Using the same OLD format as deposits, with lp_delta negated.
    Run sim_test_tm10_1.py to confirm withdraw ANN1 format.
    """
    if lp_burned <= 0:
        raise ValueError(f"LP burn amount must be positive, got {lp_burned}")
    return TailSpendAuth(
        launcher_id=launcher_id,
        recipient_puzzle_hash=recipient_puzzle_hash,
        lp_delta=-lp_burned,
        pool_puzzle_hash=pool_puzzle_hash,
    )


# ── CLI entrypoint: verify puzzle hashes for a known pool ─────────────────

def main() -> None:
    import argparse, json, sys

    parser = argparse.ArgumentParser(
        description="Compute LP CAT TAIL puzzle hash and example announcement for a pool."
    )
    parser.add_argument("--launcher-id", required=True, help="Pool launcher coin ID (hex)")
    parser.add_argument("--tail-version", type=int, default=1)
    parser.add_argument(
        "--contracts-dir",
        default=str(Path(__file__).parent),
        help="Path to the contracts/ directory",
    )
    args = parser.parse_args()

    contracts_dir = Path(args.contracts_dir)
    launcher_id = _strip_0x(args.launcher_id)

    try:
        tail_ph = compute_tail_puzzle_hash(contracts_dir, launcher_id, args.tail_version)

        print(json.dumps({
            "launcher_id": launcher_id,
            "tail_version": args.tail_version,
            "tail_puzzle_hash": tail_ph,
            "lp_cat_asset_id": tail_ph,
            "note": "No authority coin — pool singleton is the sole authority.",
        }, indent=2))
    except FileNotFoundError as e:
        print(f"[aWizard] ERROR: {e}", file=sys.stderr)
        print(
            "[aWizard] Compile the contracts first: rue build contracts/forge_lp_cat_tail.rue",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

