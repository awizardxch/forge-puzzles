"""Check a breadcrumb's proof of transaction on chain, across every source configured.

A breadcrumb may carry `proof: {address, payout_coin_id, tx_id?}`: who the swap that
made the pool's new tip paid. Chia keeps no spend-bundle id on chain -- a block holds
coin spends, not the bundles they came in -- so the proof is checked as what the chain
can show, all in the block that created the tip:

1. The tip exists at `tip_height`, and its parent is the pool coin it replaced.
2. The payout coin was created in that same block, by a settlement-payments coin
   (the offer settlement puzzle, bare for XCH or inside a CAT) spent there.
3. Running that settlement spend creates exactly the payout coin, and the payout
   coin's puzzle hash is the address itself (XCH) or the address inside that CAT.
4. The asset paid out is one this pool released in that spend: its reserve in the
   state the parent coin revealed is greater than in the tip's state.

Every coin record read here must come back identical from each source given; one
source that disagrees, or cannot answer, refuses the proof rather than picking a
side. `tx_id` is kept as the sender reported it and never checked: nothing on chain
carries one.

stdin:  JSON {launcher_id, tip_coin_id, tip_height, tip_state_reserves[], asset_ids[],
              network, proof: {address, payout_coin_id, tx_id?}, sources: [url...]}
stdout: JSON {success, verified: true, asset_id, amount, address, payout_coin_id, tx_id?}
        or {success: false, code, error}
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.util.bech32m import decode_puzzle_hash  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import match_cat_puzzle  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD_HASH  # noqa: E402
from chia.wallet.uncurried_puzzle import uncurry_puzzle  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

from untrusted_clvm import parse_untrusted_hex, run_capped  # noqa: E402

import forge_network as _forge_network  # noqa: E402
from forge_v15_price_history import state_from_reveal  # noqa: E402
from wallet_holdings import cat_puzzle_hash  # noqa: E402

XCH = "0" * 64
Fetch = Callable[[str, str, dict], dict]


class ProofError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _strip(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith("0x") else text


def _spacescan_record(base: str, coin_id: str) -> dict:
    """Spacescan's coin info, as a full node's get_coin_record_by_name would answer."""
    headers = {"User-Agent": "aWizard-Forge/1.0"}
    key = os.environ.get("FORGE_SPACESCAN_API_KEY")
    if key:
        headers["x-api-key"] = key
    request = urllib.request.Request(f"{base.rstrip('/')}/coin/info/0x{coin_id}", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read())
    coin = body.get("coin") or {}
    if not coin.get("coin_name"):
        return {"success": False}
    parent = next((c.get("coin_name") for c in body.get("coins") or [] if c.get("cointype") == "parent"), "")
    return {"success": True, "coin_record": {
        "coin": {"parent_coin_info": "0x" + _strip(parent), "puzzle_hash": "0x" + _strip((coin.get("receiver") or {}).get("address_hex")),
                 "amount": int(coin.get("amount_mojo") or 0)},
        "confirmed_block_index": int(coin.get("confirmed_block") or 0),
        "spent_block_index": int(coin.get("spent_block") or 0),
    }}


def http_fetch(source: str, route: str, payload: dict) -> dict:
    """A full node's RPC, or `spacescan:<base>` for coin records from Spacescan."""
    if source.startswith("spacescan:"):
        if route != "get_coin_record_by_name":
            raise ProofError("Spacescan is read for coin records only", "BAD_SOURCE")
        return _spacescan_record(source[len("spacescan:"):], _strip(payload["name"]))
    request = urllib.request.Request(
        f"{source.rstrip('/')}/{route}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "aWizard-Forge/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _record_key(record: dict | None) -> tuple | None:
    """What two sources must agree on about a coin."""
    if not record:
        return None
    coin = record["coin"]
    return (_strip(coin["parent_coin_info"]), _strip(coin["puzzle_hash"]), int(coin["amount"]),
            int(record.get("confirmed_block_index") or 0), int(record.get("spent_block_index") or 0))


class Consensus:
    """Reads that every source must answer identically."""

    def __init__(self, sources: list[str], fetch: Fetch = http_fetch) -> None:
        if not sources or sources[0].startswith("spacescan:"):
            raise ProofError("the first source must be a full node", "NO_SOURCE")
        self.sources = sources
        self.fetch = fetch

    def coin_record(self, coin_id: str) -> dict | None:
        answers = []
        for source in self.sources:
            try:
                answer = self.fetch(source, "get_coin_record_by_name", {"name": "0x" + coin_id})
            except Exception as exc:  # noqa: BLE001
                raise ProofError(f"{source} did not answer: {exc}", "SOURCE_UNAVAILABLE") from exc
            answers.append(answer.get("coin_record") if answer.get("success") else None)
        keys = {_record_key(a) for a in answers}
        if len(keys) != 1:
            raise ProofError(f"the sources disagree about coin {coin_id[:12]}", "SOURCES_DISAGREE")
        return answers[0]

    def spend(self, coin_id: str, height: int) -> tuple[Program, Program]:
        """A puzzle reveal checks itself against the agreed puzzle hash, so one source suffices."""
        answer = self.fetch(self.sources[0], "get_puzzle_and_solution", {"coin_id": "0x" + coin_id, "height": height})
        if not answer.get("success"):
            raise ProofError(f"no spend for {coin_id[:12]} at {height}", "NO_SPEND")
        spend = answer["coin_solution"]
        # Untrusted CLVM from a node: parsed without back-references (audit U3).
        return parse_untrusted_hex(spend["puzzle_reveal"]), parse_untrusted_hex(spend["solution"])


def coin_of(record: dict) -> Coin:
    coin = record["coin"]
    return Coin(bytes32.fromhex(_strip(coin["parent_coin_info"])), bytes32.fromhex(_strip(coin["puzzle_hash"])),
                int(coin["amount"]))


def settlement_asset(puzzle: Program) -> str | None:
    """XCH for the bare settlement puzzle, the asset id for one inside a CAT, else None."""
    if puzzle.get_tree_hash() == OFFER_MOD_HASH:
        return XCH
    matched = match_cat_puzzle(uncurry_puzzle(puzzle))
    if matched is None:
        return None
    _mod, tail_hash, inner = matched
    return _strip(bytes(tail_hash.as_atom()).hex()) if inner.get_tree_hash() == OFFER_MOD_HASH else None


def verify(payload: dict[str, Any], fetch: Fetch = http_fetch) -> dict[str, Any]:
    launcher_id = _strip(payload.get("launcher_id"))
    tip_id = _strip(payload.get("tip_coin_id"))
    tip_height = int(payload.get("tip_height") or 0)
    proof = payload.get("proof") or {}
    payout_id = _strip(proof.get("payout_coin_id"))
    assets = [_strip(a) for a in payload.get("asset_ids") or []]
    tip_reserves = [int(r) for r in payload.get("tip_state_reserves") or []]
    if len(payout_id) != 64 or len(tip_id) != 64 or tip_height <= 0 or len(assets) != len(tip_reserves) or not assets:
        raise ProofError("the proof needs payout_coin_id, and the tip, its height, state and assets", "BAD_PROOF")

    network = _forge_network.NETWORKS.get(str(payload.get("network") or ""), None)
    if network is None:
        raise ProofError("unknown network", "BAD_PROOF")
    address = str(proof.get("address") or "").strip()
    if not address.startswith(network["hrp"] + "1"):
        raise ProofError(f"the address is not a {network['hrp']} address", "WRONG_NETWORK")
    try:
        address_ph = bytes32(decode_puzzle_hash(address))
    except Exception as exc:  # noqa: BLE001
        raise ProofError("the address does not decode", "BAD_PROOF") from exc

    chain = Consensus([str(s) for s in payload.get("sources") or []], fetch)

    # 1. The tip, and the pool coin it replaced.
    tip = chain.coin_record(tip_id)
    if not tip or int(tip["confirmed_block_index"]) != tip_height:
        raise ProofError("the tip is not a coin confirmed at that height", "PROOF_MISMATCH")
    parent_id = _strip(tip["coin"]["parent_coin_info"])
    parent = chain.coin_record(parent_id)
    if not parent or int(parent.get("spent_block_index") or 0) != tip_height:
        raise ProofError("the tip's parent was not spent in that block", "PROOF_MISMATCH")
    parent_puzzle, _ = chain.spend(parent_id, tip_height)
    if parent_puzzle.get_tree_hash() != coin_of(parent).puzzle_hash:
        raise ProofError("the parent's reveal does not hash to its coin", "PROOF_MISMATCH")
    before = state_from_reveal(parent_puzzle, bytes32.fromhex(launcher_id))
    before_reserves = [int(r) for r in before[0]]
    if len(before_reserves) != len(tip_reserves):
        raise ProofError("the parent's state has another asset count", "PROOF_MISMATCH")

    # 2. The payout, made in the same block by a settlement spend.
    payout = chain.coin_record(payout_id)
    if not payout or int(payout["confirmed_block_index"]) != tip_height:
        raise ProofError("the payout coin was not created in the tip's block", "PROOF_MISMATCH")
    payout_coin = coin_of(payout)
    if bytes(payout_coin.name()).hex() != payout_id:
        raise ProofError("the payout record is not that coin", "PROOF_MISMATCH")
    maker_id = bytes(payout_coin.parent_coin_info).hex()
    maker = chain.coin_record(maker_id)
    if not maker or int(maker.get("spent_block_index") or 0) != tip_height:
        raise ProofError("the payout's creator was not spent in the tip's block", "PROOF_MISMATCH")
    maker_puzzle, maker_solution = chain.spend(maker_id, tip_height)
    if maker_puzzle.get_tree_hash() != coin_of(maker).puzzle_hash:
        raise ProofError("the creator's reveal does not hash to its coin", "PROOF_MISMATCH")
    asset = settlement_asset(maker_puzzle)
    if asset is None:
        raise ProofError("the payout was not made by an offer settlement", "PROOF_MISMATCH")

    # 3. It creates exactly the payout, to the address.
    created = [(bytes(c.rest().first().atom), c.rest().rest().first().as_int())
               for c in run_capped(maker_puzzle, maker_solution).as_iter() if c.first().as_int() == 51]
    if (bytes(payout_coin.puzzle_hash), payout_coin.amount) not in created:
        raise ProofError("the settlement spend does not create the payout coin", "PROOF_MISMATCH")
    expected_ph = address_ph if asset == XCH else cat_puzzle_hash(bytes32.fromhex(asset), address_ph)
    if payout_coin.puzzle_hash != expected_ph:
        raise ProofError("the payout does not pay that address", "PROOF_MISMATCH")

    # 4. This pool released that asset in that spend.
    if asset not in assets:
        raise ProofError("the payout's asset is not one this pool holds", "PROOF_MISMATCH")
    slot = assets.index(asset)
    if not tip_reserves[slot] < before_reserves[slot]:
        raise ProofError("this pool did not pay that asset out in that spend", "PROOF_MISMATCH")

    out = {"success": True, "verified": True, "asset_id": asset, "amount": str(payout_coin.amount),
           "address": address, "payout_coin_id": payout_id, "sources": len(chain.sources)}
    tx_id = proof.get("tx_id")
    if tx_id is not None:
        out["tx_id"] = str(tx_id)[:128]
    return out


def main() -> int:
    try:
        print(json.dumps(verify(json.loads(sys.stdin.read() or "{}"))))
        return 0
    except ProofError as exc:
        print(json.dumps({"success": False, "error": str(exc), "code": exc.code}))
        return 1
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}", "code": "PROOF_ERROR"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
