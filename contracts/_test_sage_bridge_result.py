#!/usr/bin/env python3
"""The Sage offer bridge reports a take as a success only when something was
broadcast (2026-09-26 external review, finding 09).

`take_offer` may return a signed bundle and no transaction id; the bridge then
submits it itself, and that submit may raise. The bridge used to emit
`success: True` regardless, and the router-taker recorded a pending
confirmation for a transaction that never existed. Driven here with a fake
SageRPC so every shape the wallet can answer is covered without a wallet.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sage_offer_bridge  # noqa: E402

FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        FAILED += 1


class FakeSage:
    def __init__(self, take_result, submit=None, submit_raises=None):
        self.take_result = take_result
        self.submit = submit
        self.submit_raises = submit_raises
        self.submitted = 0

    def import_offer(self, _offer):
        return {"offer_id": "imported"}

    def take_offer(self, _offer, _fee):
        return self.take_result

    def submit_transaction(self, _bundle):
        self.submitted += 1
        if self.submit_raises:
            raise RuntimeError(self.submit_raises)
        return self.submit


def run_take(fake: FakeSage) -> tuple[dict, int]:
    sage_offer_bridge.SageRPC = lambda host, port: fake  # noqa: ARG005 -- the bridge constructs it by keyword
    sys.stdin = io.StringIO(json.dumps({"action": "take", "offer": "offer1qqq", "fee": 0}))
    out = io.StringIO()
    with redirect_stdout(out):
        code = sage_offer_bridge.main()
    return json.loads(out.getvalue().strip().splitlines()[-1]), code


print("take outcomes:")
result, code = run_take(FakeSage({"transaction_id": "ab" * 32, "offer_id": "o1"}))
check("a take that returns a transaction id is a submitted success", result["success"] is True and result["state"] == "submitted" and code == 0, str(result)[:120])

result, code = run_take(FakeSage({"spend_bundle": {"coin_spends": []}}, submit={"transaction_id": "cd" * 32}))
check("a signed bundle the bridge submits itself is a submitted success", result["success"] is True and result["transaction_id"] == "cd" * 32, str(result)[:120])

result, code = run_take(FakeSage({"spend_bundle": {"coin_spends": []}}, submit_raises="mempool rejected the fee"))
check("a signed bundle whose submit raised is NOT a success", result["success"] is False and code == 1, str(result)[:120])
check("...and says it was signed but not submitted", result.get("state") == "signed-not-submitted" and "mempool rejected" in str(result.get("error")), str(result)[:160])

result, code = run_take(FakeSage({"spend_bundle": {"coin_spends": []}}, submit={}))
check("a submit that returned no transaction id is NOT a success", result["success"] is False and result.get("state") == "signed-not-submitted", str(result)[:120])

result, code = run_take(FakeSage({"offer_id": "o1"}))
check("a take with neither an id nor a bundle is NOT a success", result["success"] is False, str(result)[:120])

print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    raise SystemExit(1)
print("ALL PASSED -- the bridge's success means broadcast")
