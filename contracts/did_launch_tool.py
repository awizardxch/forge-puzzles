#!/usr/bin/env python3
"""Launch a DID whose owner is a given puzzle hash, from the start.

Sage's `create_did` always mints to the wallet running it, so the only way to
get a DID onto a lock through Sage is to mint it and then transfer it. That
leaves the DID owned by a person before it is owned by the lock, takes two
transactions, and strands the DID if the second one does not land.

A DID's owner is not something bolted on afterwards: it is the `p2_puzzle_hash`
curried into the DID inner puzzle when the singleton is launched. So the launch
can name the lock directly and the DID belongs to it from its first coin. This
builds exactly that, unsigned:

  * pick the wallet coin that pays for it;
  * derive the launcher coin from that parent, because the DID's inner puzzle
    is curried with the launcher id and therefore depends on it;
  * curry the inner puzzle with the LOCK's deposit puzzle hash as owner;
  * emit the parent's spend (creating the launcher, the change and the fee) and
    the launcher's spend (creating the eve DID). The launcher spend needs no
    signature; the wallet signs the parent, which is the only approval needed.

The name rides in the launcher's key/value list, where the vault puts its
policy, so it is readable from chain rather than only in a wallet's local index.

    stdin:  {"action": "build", "network": "testnet11", "address": "txch1…"
             (or "owner_puzzle_hash"), "name": "…", "fee": 0,
             "coins": [{"coin": {...}, "puzzle": "<hex reveal>"}]}
    stdout: one JSON object; `success` false with `error` on failure.

Nothing here signs or broadcasts.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.coin import Coin  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.types.condition_opcodes import ConditionOpcode  # noqa: E402
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash  # noqa: E402
from chia.wallet.did_wallet.did_wallet_puzzles import create_innerpuz  # noqa: E402
from chia.wallet.puzzles.singleton_top_layer_v1_1 import (  # noqa: E402
    generate_launcher_coin,
    launch_conditions_and_coinsol,
)
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

DID_TAG = b"forge-did/1"
SINGLETON_AMOUNT = 1
HRP = {"mainnet": "xch", "testnet11": "txch"}


def _strip(value: str) -> str:
    return str(value or "").lower().removeprefix("0x")


def _coin(entry: dict) -> Coin:
    return Coin(bytes32.fromhex(_strip(entry["parent_coin_info"])),
                bytes32.fromhex(_strip(entry["puzzle_hash"])),
                uint64(int(entry["amount"])))


def build(payload: dict) -> dict[str, Any]:
    network = str(payload.get("network") or "testnet11")
    hrp = HRP.get(network, "txch")
    if payload.get("owner_puzzle_hash"):
        owner_ph = bytes32.fromhex(_strip(payload["owner_puzzle_hash"]))
    elif payload.get("address"):
        owner_ph = bytes32(decode_puzzle_hash(str(payload["address"]).strip()))
    else:
        raise ValueError("an owner address or puzzle hash is required")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("a name is required")
    fee = int(payload.get("fee") or 0)

    wallet_coins: list[tuple[Coin, Program]] = []
    for entry in payload.get("coins") or []:
        if not isinstance(entry, dict):
            continue
        reveal = _strip(entry.get("puzzle") or entry.get("puzzle_reveal") or "")
        if not reveal:
            continue
        wallet_coins.append((_coin(entry["coin"]), Program.from_bytes(bytes.fromhex(reveal))))
    if not wallet_coins:
        raise ValueError("the wallet reported no XCH coin with a puzzle reveal")

    needed = SINGLETON_AMOUNT + fee
    candidates = [pair for pair in wallet_coins if int(pair[0].amount) >= needed]
    if not candidates:
        raise ValueError(f"no wallet coin covers {needed} mojos (the DID coin plus the fee)")
    parent, parent_puzzle = max(candidates, key=lambda pair: int(pair[0].amount))

    # The inner puzzle is curried with the launcher id, and the launcher id comes
    # from the parent, so the parent has to be chosen before the DID exists even
    # as a hash.
    launcher_coin = generate_launcher_coin(parent, uint64(SINGLETON_AMOUNT))
    launcher_id = launcher_coin.name()
    inner = create_innerpuz(owner_ph, [], uint64(0), launcher_id, Program.to([]))
    comment = Program.to([(DID_TAG, name.encode())])
    launch_conditions, launcher_spend = launch_conditions_and_coinsol(
        parent, inner, comment, uint64(SINGLETON_AMOUNT))

    conditions: list[Any] = list(launch_conditions)
    change = int(parent.amount) - needed
    if change > 0:
        conditions.append([ConditionOpcode.CREATE_COIN, parent.puzzle_hash, change])
    if fee > 0:
        conditions.append([ConditionOpcode.RESERVE_FEE, fee])

    parent_spend = make_spend(parent, parent_puzzle, Program.to((1, conditions)))
    spends = [parent_spend, launcher_spend]
    return {
        "success": True,
        "network": network,
        "launcher_id": launcher_id.hex(),
        "did_id": encode_puzzle_hash(launcher_id, "did:chia:"),
        "owner_puzzle_hash": owner_ph.hex(),
        "owner_address": encode_puzzle_hash(owner_ph, hrp),
        "name": name,
        "fee": fee,
        "coin_spends": [
            {
                "coin": {
                    "parent_coin_info": "0x" + spend.coin.parent_coin_info.hex(),
                    "puzzle_hash": "0x" + spend.coin.puzzle_hash.hex(),
                    "amount": int(spend.coin.amount),
                },
                "puzzle_reveal": "0x" + bytes(spend.puzzle_reveal).hex(),
                "solution": "0x" + bytes(spend.solution).hex(),
            }
            for spend in spends
        ],
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        action = str(payload.get("action", "")).lower()
        if action != "build":
            raise ValueError("action must be build")
        print(json.dumps(build(payload)))
        return 0
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
