#!/usr/bin/env python3
"""The fee keeper's key (forge_keeper_key.py): it signs the spend that pays a network fee from
its own address back to its own address, and refuses everything else. A throwaway key, offline."""
import json
import os
import subprocess
import sys
import pathlib

sys.stdout.reconfigure(encoding="utf-8")
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from chia_rs import AugSchemeMPL, G1Element, G2Element, PrivateKey  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402
from chia.types.blockchain_format.coin import Coin  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key  # noqa: E402

import forge_v16_driver as drv  # noqa: E402

GENESIS = bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615")
SK = PrivateKey.from_bytes(bytes([0x11]) * 31 + bytes([0x07]))
PK = SK.get_g1()
PUZZLE = puzzle_for_synthetic_public_key(PK)
OWN = PUZZLE.get_tree_hash()
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))


def run(payload, key=True):
    env = dict(os.environ)
    env.pop("FORGE_KEEPER_SK", None)
    if key:
        env["FORGE_KEEPER_SK"] = bytes(SK).hex()
    out = subprocess.run([sys.executable, str(HERE / "forge_keeper_key.py")], input=json.dumps(payload), capture_output=True, text=True, env=env)
    return json.loads(out.stdout.strip().splitlines()[-1])


def spend(conditions, puzzle=PUZZLE, puzzle_hash=None, amount=10_000):
    coin = Coin(bytes32(b"\x21" * 32), bytes32(puzzle_hash or puzzle.get_tree_hash()), uint64(amount))
    return coin, make_spend(coin, puzzle, drv.p2_delegated_solution(conditions))


def main() -> int:
    got = run({"action": "pubkey"})
    check("pubkey answers the key's public key", got.get("pubkey") == bytes(PK).hex())
    check("without FORGE_KEEPER_SK it refuses", run({"action": "pubkey"}, key=False).get("success") is False)

    coin, honest = spend([[51, OWN, 6_000, [OWN]], [52, 4_000]])
    got = run({"action": "sign", "network": "testnet11", "spends": [honest.to_json_dict()]})
    ok = got.get("success") is True
    if ok:
        delegated = Program.from_bytes(bytes(honest.solution)).rest().first()
        message = bytes(delegated.get_tree_hash()) + bytes(coin.name()) + GENESIS
        ok = AugSchemeMPL.verify(PK, message, G2Element.from_bytes(bytes.fromhex(got["signature"])))
    check("signs the fee spend: change to itself and RESERVE_FEE, a valid AGG_SIG_ME for this network", ok, str(got)[:80])
    wrong_net = run({"action": "sign", "network": "mainnet", "spends": [honest.to_json_dict()]})
    check("the mainnet signature is a different one (network bound)", wrong_net.get("signature") != got.get("signature"))

    refused = lambda label, s, why: check(label, (lambda r: r.get("success") is False and why in r.get("error", ""))(  # noqa: E731
        run({"action": "sign", "network": "testnet11", "spends": [s.to_json_dict()]})))
    refused("refuses a spend that pays another address", spend([[51, b"\xee" * 32, 6_000], [52, 4_000]])[1], "other than the keeper's own")
    refused("refuses a spend that sends a message", spend([[66, 23, b"x", b"\x01" * 32], [51, OWN, 6_000]])[1], "condition 66")
    refused("refuses a spend that makes an announcement", spend([[60, b"hello"], [51, OWN, 6_000]])[1], "condition 60")
    other = puzzle_for_synthetic_public_key(PrivateKey.from_bytes(bytes([0x22]) * 31 + bytes([0x01])).get_g1())
    refused("refuses a coin that is not at the keeper's address", spend([[51, OWN, 6_000]], puzzle=other)[1], "not of a coin at the keeper's address")
    check("refuses an empty request", run({"action": "sign", "network": "testnet11", "spends": []}).get("success") is False)
    check("refuses an unknown network", run({"action": "sign", "network": "x", "spends": [honest.to_json_dict()]}).get("success") is False)

    print(f"\n{sum(results)}/{len(results)} keeper key checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
