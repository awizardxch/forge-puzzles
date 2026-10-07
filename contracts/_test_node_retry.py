"""Node reads retry a dropped connection; pushes never do.

The lock page showed "node get_coin_records_by_puzzle_hashes HTTP 503: upstream
connect error" for a balance coinset answered a moment later (mainnet,
2026-10-05). Node.rpc now asks a read again after a 5xx, a 429 or a reset, up to
twice. A push_tx is never repeated: one that timed out may have landed, and the
callers already treat that case. A 4xx is the node's real answer and is not asked
again.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import multisig_tool as tool

PASSED = 0
FAILED: list[str] = []


def check(name: str, got: object, want: object) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}\n     got  {got}\n     want {want}")


class Flaky(tool.Node):
    RETRY_PAUSE_S = 0  # no waiting in a test

    def __init__(self, failures: int, error=tool._Transient) -> None:
        super().__init__("http://fake")
        self.failures = failures
        self.error = error
        self.calls = 0

    def _rpc_once(self, route, payload):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error(f"node {route} HTTP 503: upstream connect error")
        return {"success": True, "route": route}


def outcome(node: Flaky, route: str):
    try:
        return node.rpc(route, {})["success"], node.calls
    except tool.MultisigError as exc:
        return "error", node.calls, str(exc)


check("one dropped read is asked again and answers", outcome(Flaky(1), "get_coin_records_by_puzzle_hashes"), (True, 2))
check("two dropped reads, still answered on the third", outcome(Flaky(2), "get_coin_records_by_puzzle_hashes"), (True, 3))
gave_up = outcome(Flaky(3), "get_coin_records_by_puzzle_hashes")
check("three dropped reads give up after three calls", gave_up[:2], ("error", 3))
check("and report the node's own message", gave_up[2].startswith("node get_coin_records_by_puzzle_hashes HTTP 503"), True)
check("a push is never repeated", outcome(Flaky(1), "push_tx")[:2], ("error", 1))
check("a 4xx is the node's answer, not asked again", outcome(Flaky(1, tool.MultisigError), "get_coin_record_by_name")[:2], ("error", 1))

print(f"node retry: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
