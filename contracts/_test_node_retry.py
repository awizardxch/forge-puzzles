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


# Failover nodes (MULTISIG_NODE_FAILOVER_URLS, 2026-10-17): a read this node never
# answers goes to each failover once; a push goes to every node at once, this node's
# answer stands unless it did not answer and a failover accepted the bundle.
PINNED = "https://pinned.example"
BACKUP = "https://backup.example"


class Fleet(tool.Node):
    RETRY_PAUSE_S = 0
    FAILOVER_PUSH_TIMEOUT_S = 2

    def __init__(self, answers: dict) -> None:
        super().__init__(PINNED, failover_urls=[BACKUP])
        self.answers = answers  # url -> body dict or exception class
        self.calls: list[tuple[str, str]] = []

    def _rpc_at(self, node_url, route, payload, timeout):
        self.calls.append((node_url, route))
        answer = self.answers[node_url]
        if isinstance(answer, type) and issubclass(answer, Exception):
            raise answer(f"node {route} HTTP 503: down")
        return answer


def run(node: Fleet, route: str):
    try:
        return node.rpc(route, {})
    except tool.MultisigError as exc:
        return f"error: {exc}"


fleet = Fleet({PINNED: tool._Transient, BACKUP: {"success": True, "coin_record": None}})
check("pinned down: the failover answers the read", run(fleet, "get_coin_record_by_name"), {"success": True, "coin_record": None})
check("after the pinned node's three tries", fleet.calls.count((PINNED, "get_coin_record_by_name")), 3)
fleet = Fleet({PINNED: tool.MultisigError, BACKUP: {"success": True}})
check("a 4xx from the pinned node is final", run(fleet, "get_coin_record_by_name").startswith("error"), True)
check("and no failover is asked", [c for c in fleet.calls if c[0] == BACKUP], [])
fleet = Fleet({PINNED: tool._Transient, BACKUP: tool._Transient})
check("no node answers: the read still fails", run(fleet, "get_coin_record_by_name").startswith("error"), True)
fleet = Fleet({PINNED: {"success": True, "status": "SUCCESS"}, BACKUP: {"success": False, "error": "DOUBLE_SPEND"}})
pushed = run(fleet, "push_tx")
check("a push goes to every node", sorted(c[0] for c in fleet.calls), [BACKUP, PINNED])
check("the pinned answer stands over a failover's DOUBLE_SPEND", pushed, {"success": True, "status": "SUCCESS"})
fleet = Fleet({PINNED: tool._Transient, BACKUP: {"success": True, "status": "SUCCESS"}})
check("pinned down, failover SUCCESS: the failover's answer is used", run(fleet, "push_tx"), {"success": True, "status": "SUCCESS"})
check("and the pinned node is pushed exactly once", fleet.calls.count((PINNED, "push_tx")), 1)
fleet = Fleet({PINNED: tool._Transient, BACKUP: {"success": True, "status": "PENDING"}})
check("a failover's PENDING is never success", run(fleet, "push_tx").startswith("error"), True)
check("failover URLs: https only, no credentials or query", tool.failover_node_urls("http://a.example, https://u:p@b.example, https://c.example/?key=1, https://d.example/rpc/, junk"), ["https://d.example/rpc"])
check("a Node for another network's node gets no failovers", tool.Node("https://elsewhere.example").failover_urls, [])

print(f"node retry: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
