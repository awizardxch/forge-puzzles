#!/usr/bin/env python3
"""forge_lp_tail_spend_builder.py"""
from __future__ import annotations
import sys
from pathlib import Path


def _load_mod(contracts_dir, filename):
    from chia.types.blockchain_format.program import Program
    data = bytes.fromhex((contracts_dir / "compiled" / filename).read_text(encoding="utf-8").strip())
    return Program.from_bytes(data)


def _strip_0x(val):
    s = str(val)
    return s[2:] if s.startswith("0x") else s


def _hex_to_bytes(h):
    return bytes.fromhex(_strip_0x(h))


def build_tail_puzzle(contracts_dir, launcher_id, tail_version=1):
    from chia.types.blockchain_format.program import Program
    mod = _load_mod(contracts_dir, "forge_lp_cat_tail.clvm.hex")
    return mod.curry(Program.to(_hex_to_bytes(launcher_id)), Program.to(tail_version))


def build_tail_solution(singleton_announcement_id, lp_delta=None):
    from chia.types.blockchain_format.program import Program
    ann = singleton_announcement_id if isinstance(singleton_announcement_id, bytes) else _hex_to_bytes(singleton_announcement_id)
    # New simplified TAIL only needs the announcement ID (lp_delta removed from struct).
    # lp_delta param kept for backward compat but ignored.
    return Program.to([ann])


def lp_cat_outer_puzzle(asset_id, inner_puzzle):
    from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
    try:
        from chia.types.blockchain_format.sized_bytes import bytes32
    except ImportError:
        from chia_rs.sized_bytes import bytes32
    return construct_cat_puzzle(CAT_MOD, bytes32.from_hexstr(asset_id), inner_puzzle)


def lp_cat_outer_puzzle_hash(asset_id, inner_puzzle_hash):
    from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
    try:
        from chia.types.blockchain_format.sized_bytes import bytes32
    except ImportError:
        from chia_rs.sized_bytes import bytes32
    ph = bytes32.from_hexstr(inner_puzzle_hash)
    return construct_cat_puzzle(CAT_MOD, bytes32.from_hexstr(asset_id), ph).get_tree_hash().hex()