#!/usr/bin/env python3
"""A pool's own maintenance spends, built for a connected wallet: `collect` and `dao_fee`.

    collect   pays the dev fee owed (state fees_owed) to the pool's curried protocol
              recipient and the DAO fee owed (state dao_owed) to its curried DAO
              recipient, and zeroes both (forge_action_collect). Permissionless: the
              caller picks only WHEN; where and how much are the pool's.
    dao_fee   lowers the DAO fee (forge_action_dao_fee). Only a coin AT the DAO
              recipient can authorize it: that coin's spend sends the mode-23 message
              naming the new rate to this pool coin, so the payer must be the DAO.

The dev (protocol) fee rate is the pool's CONFIG, curried at creation: no leaf moves it.

stdin JSON:
    {kind: "collect" | "dao-fee", pool: <V16 snapshot>, height, new_bps?,
     payer?: {coin: {parent_coin_info, puzzle_hash, amount}, puzzle_reveal},
     network_fee?, aggregated_signature?, receiver_only?, sender_puzzle_hash?}

`receiver_only` (dao-fee): the DAO recipient is a lock, whose own proposal sends the
message (owner, 2026-10-11). Only the pool's half is built -- no payer, no signature --
and returned as `pool_spends` with the message to sign into the lock's conditions and an
estimate of the pool half's cost. The leaf still runs, so a change it refuses is refused
here; consensus validation waits for the lock's half at execution.

`payer` pays the network fee (a standard wallet coin; its change goes back to it). A
collect with no network fee needs no payer at all. Without a signature the answer is the
payer's spend to sign, the bundle's cost and the successor snapshot; with one it is also
the finished bundle. The same inputs always build the same bundle, so the page signs
what the router will push.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia_rs import Coin, G2Element, SpendBundle  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import forge_v16_driver as drv  # noqa: E402
import forge_v16_offer as off  # noqa: E402

PROTOCOL = 17
SEND_MESSAGE = 66
MODE_PUZZLE_TO_COIN = 0b010111   # sender committed by puzzle hash, receiver by coin id (23)
CREATE_COIN = 51
RESERVE_FEE = 52


class Refused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _b32(value: Any) -> bytes32:
    text = str(value or "").lower().removeprefix("0x")
    if len(text) != 64:
        raise Refused("BAD_REQUEST", f"expected 32 bytes of hex, got {text[:16]!r}")
    return bytes32.fromhex(text)


def dao_fee_message(new_bps: int) -> bytes32:
    """forge_action_common.rue: tree_hash(["forge-dao-fee-v1", new_bps])."""
    return bytes32(Program.to([b"forge-dao-fee-v1", new_bps]).get_tree_hash())


def owed_indices(pool: drv.V16Pool) -> list[int]:
    """Asset indices with a dev or DAO fee owed: the collect leaf asserts one or the other is positive."""
    fees, dao = pool.state[2], pool.state[6]
    return [i for i in range(len(fees)) if int(fees[i]) > 0 or int(dao[i]) > 0]


def build(payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = payload.get("pool") or {}
    if int(snapshot.get("protocol_version") or 0) != PROTOCOL:
        raise Refused("WRONG_REVISION", f"this builder spends protocol {PROTOCOL} pools only")
    pool = off.snapshot_to_pool(snapshot)
    kind = str(payload.get("kind") or "")
    height = int(payload.get("height") or 0)
    if height <= 0:
        raise Refused("BAD_REQUEST", "height is the chain's last transaction block")
    fee = int(payload.get("network_fee") or 0)
    if fee < 0:
        raise Refused("BAD_REQUEST", "network_fee is a whole number of mojos")

    if kind == "collect":
        indices = owed_indices(pool)
        if not indices:
            raise Refused("NOTHING_OWED", "this pool owes no dev or DAO fee yet")
        leaf, solution = "forge_action_collect", [height, indices]
        message = None
    elif kind == "dao-fee":
        new_bps = int(payload.get("new_bps"))
        current = int(pool.state[5])
        if pool.dao_ph == bytes32(b"\x00" * 32):
            raise Refused("NO_DAO", "this pool has no DAO recipient")
        if not 0 <= new_bps < current:
            raise Refused("BAD_REQUEST", f"the DAO fee only goes down: pick 0 to {current - 1} bps (now {current})")
        leaf, solution = "forge_action_dao_fee", [height, new_bps]
        message = dao_fee_message(new_bps)
    else:
        raise Refused("BAD_REQUEST", "kind is collect or dao-fee")

    if payload.get("receiver_only"):
        return receiver_only(pool, kind, leaf, solution, message, payload)

    payer_spends = []
    payer = payload.get("payer")
    if payer:
        coin_in = payer["coin"]
        coin = Coin(_b32(coin_in["parent_coin_info"]), _b32(coin_in["puzzle_hash"]), uint64(int(coin_in["amount"])))
        puzzle = Program.from_bytes(bytes.fromhex(str(payer["puzzle_reveal"]).removeprefix("0x")))
        if puzzle.get_tree_hash() != coin.puzzle_hash:
            raise Refused("BAD_REQUEST", "the payer's puzzle reveal is not its coin's puzzle")
        if int(coin.amount) < fee:
            raise Refused("PAYER_TOO_SMALL", f"the paying coin holds {int(coin.amount)} mojos; the network fee is {fee}")
        if message is not None and coin.puzzle_hash != pool.dao_ph:
            raise Refused("NOT_DAO", "only a coin at the pool's DAO recipient can lower its DAO fee")
        conditions = []
        if message is not None:
            conditions.append([SEND_MESSAGE, MODE_PUZZLE_TO_COIN, message, pool.coin.name()])
        change = int(coin.amount) - fee
        if change > 0:
            conditions.append([CREATE_COIN, coin.puzzle_hash, change, [coin.puzzle_hash]])
        if fee > 0:
            conditions.append([RESERVE_FEE, fee])
        payer_spends.append(make_spend(coin, puzzle, drv.p2_delegated_solution(conditions)))
    elif message is not None:
        raise Refused("NOT_DAO", "lowering the DAO fee needs a coin at the DAO recipient to authorize it")
    elif fee > 0:
        raise Refused("BAD_REQUEST", "a network fee needs a paying coin")

    pool_bundle, new_state = drv.spend_action(pool, leaf, solution, extra_spends=payer_spends)
    unsigned = SpendBundle(pool_bundle.coin_spends, G2Element())
    try:
        conds, _ = drv.validate(unsigned)
    except drv.Rejected as exc:
        raise Refused("INVALID", f"the {kind} bundle fails consensus validation: {exc}") from exc
    after = pool.advance(drv.state_to_list(new_state))
    out: dict[str, Any] = {
        "success": True,
        "kind": kind,
        "cost": int(conds.cost),
        "network_fee": str(fee),
        "payer_spends": [cs.to_json_dict() for cs in payer_spends],
        "pool_coin_id": pool.coin.name().hex(),
        "successor": off.pool_to_snapshot(after),
        "successor_coin_id": after.coin.name().hex(),
        "paid": {
            "fees_owed": [str(int(x)) for x in pool.state[2]],
            "dao_owed": [str(int(x)) for x in pool.state[6]],
            "protocol_puzzle_hash": pool.protocol_ph.hex(),
            "dao_puzzle_hash": pool.dao_ph.hex(),
        } if kind == "collect" else None,
    }
    signature = str(payload.get("aggregated_signature") or "").strip().lower().removeprefix("0x")
    if signature:
        bundle = SpendBundle(pool_bundle.coin_spends, G2Element.from_bytes(bytes.fromhex(signature)))
        out["bundle"] = bundle.to_json_dict()
        out["transaction_id"] = bundle.name().hex()
    elif not payer_spends:
        # Nothing to sign (a collect with no network fee): the bundle is already whole.
        out["bundle"] = unsigned.to_json_dict()
        out["transaction_id"] = unsigned.name().hex()
    return out


# Chia's cost model, for a half-bundle that cannot be validated alone: CLVM cost, plus
# 12,000 per byte of puzzle and solution, plus each CREATE_COIN and AGG_SIG.
COST_PER_BYTE = 12_000
CREATE_COIN_COST = 1_800_000
AGG_SIG_COST = 1_200_000
AGG_SIG_OPCODES = set(range(43, 51))


def estimate_cost(coin_spends) -> int:
    total = 0
    for cs in coin_spends:
        puzzle = Program.from_bytes(bytes(cs.puzzle_reveal))
        solution = Program.from_bytes(bytes(cs.solution))
        clvm_cost, output = puzzle.run_with_cost(drv.MAX_COST, solution)
        total += clvm_cost + COST_PER_BYTE * (len(bytes(cs.puzzle_reveal)) + len(bytes(cs.solution)))
        for condition in output.as_iter():
            opcode = condition.first().as_int()
            if opcode == CREATE_COIN:
                total += CREATE_COIN_COST
            elif opcode in AGG_SIG_OPCODES:
                total += AGG_SIG_COST
    return total


def receiver_only(pool: drv.V16Pool, kind: str, leaf: str, solution: list, message, payload: dict[str, Any]) -> dict[str, Any]:
    if kind != "dao-fee" or message is None:
        raise Refused("BAD_REQUEST", "receiver_only builds a DAO fee change only")
    sender = payload.get("sender_puzzle_hash")
    if sender and _b32(sender) != pool.dao_ph:
        raise Refused("NOT_DAO", "this lock is not the pool's DAO recipient")
    pool_bundle, new_state = drv.spend_action(pool, leaf, solution)
    after = pool.advance(drv.state_to_list(new_state))
    return {
        "success": True,
        "kind": kind,
        "receiver_only": True,
        "message": message.hex(),
        "pool_coin_id": pool.coin.name().hex(),
        "condition": [SEND_MESSAGE, MODE_PUZZLE_TO_COIN, "0x" + message.hex(), "0x" + pool.coin.name().hex()],
        "current_bps": int(pool.state[5]),
        "cost": estimate_cost(pool_bundle.coin_spends),
        "pool_spends": [cs.to_json_dict() for cs in pool_bundle.coin_spends],
        "successor": off.pool_to_snapshot(after),
        "successor_coin_id": after.coin.name().hex(),
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        print(json.dumps(build(payload)))
    except Refused as exc:
        print(json.dumps({"success": False, "code": exc.code, "error": str(exc)}))
    except Exception as exc:  # noqa: BLE001 -- the caller reads the reason
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "code": "BUILD_FAILED", "error": f"{type(exc).__name__}: {exc}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
