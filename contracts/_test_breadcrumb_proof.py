"""breadcrumb_proof.py on a real testnet11 swap, replayed offline.

The chain answers for one real case are recorded once into
_breadcrumb_proof_fixture.json (public chain data: pool coins, a settlement spend,
and the protocol-fee payout it made). The suite replays them, so it needs no network,
and pins each rule by breaking it:
  - the true proof verifies;
  - another address, the same address inside the other asset, a payout coin from
    another block, and a pool that took the asset in are refused PROOF_MISMATCH;
  - a second source that disagrees, or cannot answer, refuses the proof;
  - a Spacescan source agreeing with the node is accepted.

Re-record (network): .venv/Scripts/python.exe contracts/_test_breadcrumb_proof.py --record
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.util.bech32m import encode_puzzle_hash  # noqa: E402

import breadcrumb_proof as bp  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "_breadcrumb_proof_fixture.json"
NODE = "https://testnet11.api.coinset.org"

# Pool d81c5e89 (TXCH/t8) at testnet11 block 4,773,135 released TXCH, and a settlement
# spend in that block paid it to the protocol-fee address (public pool config).
CASE = {
    "launcher_id": "d81c5e8936",   # completed from the fixture
    "payout_coin_id": "e67339332208e3fa555b3744057c38467d9c7b2d8480a850a887f80e7b7e51ca",
    "payee_puzzle_hash": "a4ae6557eb1b99907a31365101f3e6a2db33c5592a75473fdfc1d799e8508485",
}

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures += 1


def record() -> None:
    index = json.loads((Path(__file__).resolve().parent.parent / ".awizard" / "deployment-index.json").read_text(encoding="utf-8"))
    snap = next(b["poolSnapshot"] for plan in index.values() for b in (plan.get("batches") or {}).values()
                if (b.get("poolSnapshot") or {}).get("launcher_id", "").startswith(CASE["launcher_id"]))
    calls: dict[str, dict] = {}

    def recording(source: str, route: str, payload: dict) -> dict:
        answer = bp.http_fetch(source, route, payload)
        calls[f"{route} {json.dumps(payload, sort_keys=True)}"] = answer
        return answer

    payload = base_payload(snap)
    bp.verify(payload, recording)
    FIXTURE.write_text(json.dumps({"note": "testnet11 chain answers recorded by _test_breadcrumb_proof.py --record",
                                   "payload": payload, "calls": calls}, indent=1) + "\n", encoding="utf-8")
    print(f"recorded {len(calls)} answers to {FIXTURE.name}")


def base_payload(snap: dict) -> dict:
    return {
        "launcher_id": snap["launcher_id"], "tip_coin_id": snap["pool_coin_id"], "tip_height": int(snap["state"]["birth"]),
        "tip_state_reserves": snap["state"]["reserves"], "asset_ids": snap["asset_ids"], "network": "testnet11",
        "proof": {"address": encode_puzzle_hash(bytes.fromhex(CASE["payee_puzzle_hash"]), "txch"),
                  "payout_coin_id": CASE["payout_coin_id"], "tx_id": "as-reported"},
        "sources": [NODE],
    }


def replay(calls: dict, *, second: dict | None = None, second_fails: bool = False):
    """Answers from the fixture; a second source answers coin records from `second` (or the same)."""
    def fetch(source: str, route: str, payload: dict) -> dict:
        key = f"{route} {json.dumps(payload, sort_keys=True)}"
        if source != NODE:
            if second_fails:
                raise OSError("connection reset")
            if second is not None and key in second:
                return second[key]
        if key not in calls:
            raise AssertionError(f"not in the fixture: {key[:120]}")
        return calls[key]
    return fetch


def outcome(payload: dict, fetch) -> str:
    try:
        out = bp.verify(payload, fetch)
        return "verified" if out.get("verified") else "?"
    except bp.ProofError as exc:
        return exc.code


def main() -> int:
    if "--record" in sys.argv or not FIXTURE.exists():
        record()
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload, calls = data["payload"], data["calls"]

    out = bp.verify(payload, replay(calls))
    check("the true proof verifies", out.get("verified") is True and out["asset_id"] == "0" * 64, json.dumps(out)[:160])
    check("tx_id is kept as reported, not checked", out.get("tx_id") == "as-reported")

    wrong = copy.deepcopy(payload)
    wrong["proof"]["address"] = encode_puzzle_hash(bytes.fromhex("11" * 32), "txch")
    check("another address is refused", outcome(wrong, replay(calls)) == "PROOF_MISMATCH")

    mainnet = copy.deepcopy(payload)
    mainnet["proof"]["address"] = encode_puzzle_hash(bytes.fromhex(CASE["payee_puzzle_hash"]), "xch")
    check("the same payee on another network's prefix is refused", outcome(mainnet, replay(calls)) == "WRONG_NETWORK")

    took_in = copy.deepcopy(payload)
    # The pool's TXCH reserve one mojo above what it held before: it paid nothing out.
    before = [k for k in calls if k.startswith("get_puzzle_and_solution")]
    took_in["tip_state_reserves"] = [str(10 ** 20)] + payload["tip_state_reserves"][1:]
    check("a pool that did not pay that asset out is refused", outcome(took_in, replay(calls)) == "PROOF_MISMATCH",
          f"{len(before)} spends recorded")

    other_asset = copy.deepcopy(payload)
    other_asset["asset_ids"] = ["ab" * 32] + payload["asset_ids"][1:]
    check("an asset this pool does not hold is refused", outcome(other_asset, replay(calls)) == "PROOF_MISMATCH")

    other_block = copy.deepcopy(payload)
    other_block["tip_height"] = payload["tip_height"] + 1
    check("a proof for another block is refused", outcome(other_block, replay(calls)) == "PROOF_MISMATCH")

    # Only the tip's own record is off: the parent, payout and settlement all still
    # say this block, so the tip check is the one line that can refuse it.
    tip_key = next(k for k in calls if k.startswith("get_coin_record_by_name") and payload["tip_coin_id"] in k)
    shifted = copy.deepcopy(calls)
    shifted[tip_key]["coin_record"]["confirmed_block_index"] = payload["tip_height"] - 1
    check("a tip confirmed in another block is refused", outcome(payload, replay(shifted)) == "PROOF_MISMATCH")

    two = copy.deepcopy(payload)
    two["sources"] = [NODE, "https://second.example"]
    check("a second source agreeing: verified", outcome(two, replay(calls)) == "verified")
    payout_key = next(k for k in calls if k.startswith("get_coin_record_by_name") and CASE["payout_coin_id"] in k)
    lying = {payout_key: copy.deepcopy(calls[payout_key])}
    lying[payout_key]["coin_record"]["spent_block_index"] = 1
    check("a second source disagreeing: SOURCES_DISAGREE", outcome(two, replay(calls, second=lying)) == "SOURCES_DISAGREE")
    check("a second source that cannot answer: SOURCE_UNAVAILABLE",
          outcome(two, replay(calls, second_fails=True)) == "SOURCE_UNAVAILABLE")

    spacescan_first = copy.deepcopy(payload)
    spacescan_first["sources"] = ["spacescan:https://api.spacescan.io", NODE]
    check("Spacescan is never the source of reveals", outcome(spacescan_first, replay(calls)) == "NO_SOURCE")

    print(f"\n{'all breadcrumb-proof checks passed' if failures == 0 else f'{failures} breadcrumb-proof check(s) failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
