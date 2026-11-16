"""The fee keeper's key: its public key, and its signature over the one spend that pays a fee.

The keeper (api/_feeKeeper.js) collects pools' owed fees. A collect needs no signature; only
the coin that pays a network fee does, when the mempool is too full for a free spend. That
coin sits at the standard address of one synthetic key, FORGE_KEEPER_SK, which this script
reads from the environment and nothing else ever holds: not the Node process, not a file,
not a log line.

What it will sign is narrow on purpose. Every spend must be of a coin AT the keeper's own
address, and may only pay that same address back and reserve a fee. A request to sign
anything else is refused: a keeper key that leaks through a bug upstream can lose its own
small balance to fees and nothing more.

stdin:  {"action": "pubkey"}
        {"action": "sign", "network": "mainnet" | "testnet11", "spends": [coin spend json, ...]}
stdout: {"success": true, "pubkey": hex} | {"success": true, "signature": hex}
"""

from __future__ import annotations

import json
import os
import sys

from chia_rs import AugSchemeMPL, CoinSpend, G2Element, PrivateKey
from chia.types.blockchain_format.program import Program
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key

GENESIS = {
    "mainnet": "ccd5bb71183532bff220ba46c268991a3ff07eb358e8255a65c30a2dce0e5fbb",
    "testnet11": "37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615",
}
AGG_SIG_ME, CREATE_COIN, RESERVE_FEE = 50, 51, 52
MAX_COST = 11_000_000_000


class Refused(Exception):
    pass


def secret_key() -> PrivateKey:
    raw = os.environ.get("FORGE_KEEPER_SK", "").strip().lower().removeprefix("0x")
    if len(raw) != 64:
        raise Refused("FORGE_KEEPER_SK is not set (a 32-byte synthetic secret key, hex)")
    return PrivateKey.from_bytes(bytes.fromhex(raw))


def sign(payload: dict) -> str:
    network = str(payload.get("network") or "")
    if network not in GENESIS:
        raise Refused("network is mainnet or testnet11")
    sk = secret_key()
    pk = sk.get_g1()
    own = puzzle_for_synthetic_public_key(pk).get_tree_hash()
    signatures = []
    spends = payload.get("spends") or []
    if not spends:
        raise Refused("nothing to sign")
    for raw in spends:
        spend = CoinSpend.from_json_dict(raw)
        if spend.coin.puzzle_hash != own:
            raise Refused("a spend is not of a coin at the keeper's address")
        puzzle = Program.from_bytes(bytes(spend.puzzle_reveal))
        if puzzle.get_tree_hash() != own:
            raise Refused("a spend's puzzle is not the keeper's")
        _, conditions = puzzle.run_with_cost(MAX_COST, Program.from_bytes(bytes(spend.solution)))
        asked = []
        for cond in conditions.as_iter():
            op = cond.first().as_int()
            if op == AGG_SIG_ME:
                if bytes(cond.rest().first().as_atom()) != bytes(pk):
                    raise Refused("a spend asks for another key's signature")
                asked.append(bytes(cond.rest().rest().first().as_atom()))
            elif op == CREATE_COIN:
                if bytes(cond.rest().first().as_atom()) != bytes(own):
                    raise Refused("a spend pays an address other than the keeper's own")
            elif op != RESERVE_FEE:
                raise Refused(f"a spend carries condition {op}; the keeper signs change and a fee only")
        if len(asked) != 1:
            raise Refused("a spend must ask for exactly one signature")
        signatures.append(AugSchemeMPL.sign(sk, asked[0] + bytes(spend.coin.name()) + bytes.fromhex(GENESIS[network])))
    return bytes(AugSchemeMPL.aggregate(signatures) if signatures else G2Element()).hex()


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        action = str(payload.get("action") or "")
        if action == "pubkey":
            out = {"success": True, "pubkey": bytes(secret_key().get_g1()).hex()}
        elif action == "sign":
            out = {"success": True, "signature": sign(payload)}
        else:
            raise Refused("action is pubkey or sign")
    except Refused as exc:
        out = {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        out = {"success": False, "error": f"{type(exc).__name__}"}
    print(json.dumps(out))
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
