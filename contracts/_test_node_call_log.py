"""Each node call is logged with its time, its attempts and the host that answered.

The Forge Protocol Fee lock's balance read spent 1-11 s in its node calls on the
router while the same calls took 0.1-0.4 s from a desk, and the header said only
how long the calls took together (owner, 2026-10-24: "add the per-call breakdown
too"). The node client retries a refused read (0.6 s, then 1.2 s) and then asks each
failover node, so a slow call is either one slow node or a node refusing. Each call
now records which: its route, time, attempts (retries and failover nodes included)
and the host of its last attempt. The router puts them in Server-Timing (n1, n2, ...).
"""

from __future__ import annotations

import io
import json
import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import multisig_tool as tool

passed = failed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if ok:
        passed += 1
    else:
        failed += 1


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


refusals: dict[str, int] = {}
stalls: dict[str, list[float]] = {}          # per host, how long each next request takes
answers_4xx: dict[str, int] = {}


def fake_urlopen(request, timeout=0):
    """primary.test refuses as many times as ``refusals`` says; backup.test answers.
    A host with a ``stalls`` entry waits that long before its answer."""
    host = request.full_url.split("/")[2]
    with stall_lock:
        wait = stalls[host].pop(0) if stalls.get(host) else 0
    if wait:
        time.sleep(wait)
    if answers_4xx.get(host, 0) > 0:
        answers_4xx[host] -= 1
        raise urllib.error.HTTPError(request.full_url, 400, "bad request", {}, io.BytesIO(b"bad request"))
    if refusals.get(host, 0) > 0:
        refusals[host] -= 1
        raise urllib.error.HTTPError(request.full_url, 503, "upstream connect error", {}, io.BytesIO(b"upstream connect error"))
    return Response(json.dumps({"success": True, "coin_records": [], "host": host}).encode())


import threading
import time
stall_lock = threading.Lock()


tool.urllib.request.urlopen = fake_urlopen
tool.Node.RETRY_PAUSE_S = 0
tool.NODE_LOG.clear()

node = tool.Node("https://primary.test", failover_urls=["https://backup.test"])

print("one entry per call")
node.rpc("get_coin_records_by_hint", {"hint": "00"})
entry = tool.NODE_LOG[-1]
check("a call answered first time: one attempt, the primary host", entry["route"] == "get_coin_records_by_hint" and entry["attempts"] == 1
      and entry["host"] == "primary.test" and entry["ok"] is True, str(entry))

refusals["primary.test"] = 2
node.rpc("get_coin_records_by_names", {"names": []})
entry = tool.NODE_LOG[-1]
check("a call the primary refused twice: three attempts, still the primary that answered",
      entry["attempts"] == 3 and entry["host"] == "primary.test" and entry["ok"] is True, str(entry))

refusals["primary.test"] = 3
node.rpc("get_puzzle_and_solution", {"coin_id": "00", "height": 1})
entry = tool.NODE_LOG[-1]
check("a call the primary never answered: its three tries and the failover, which answered",
      entry["attempts"] == 4 and entry["host"] == "backup.test" and entry["ok"] is True, str(entry))

refusals["primary.test"], refusals["backup.test"] = 3, 1
try:
    node.rpc("get_coin_records_by_parent_ids", {"parent_ids": []})
    raised = False
except tool.MultisigError:
    raised = True
entry = tool.NODE_LOG[-1]
check("a call no node answered is logged as failed, with every attempt", raised and entry["ok"] is False and entry["attempts"] == 4, str(entry))

print("\nthe run's timing")
timing = tool.run_timing(0.0)
check("carries the log, and the totals still add up", [e["route"] for e in timing["node_log"]][-4:] == [
    "get_coin_records_by_hint", "get_coin_records_by_names", "get_puzzle_and_solution", "get_coin_records_by_parent_ids"]
      and timing["node_calls"] >= 4, str(timing["node_calls"]))
for _ in range(tool.MAX_NODE_LOG + 5):
    node.rpc("get_coin_records_by_hint", {"hint": "00"})
check("the log keeps only the newest calls", len(tool.NODE_LOG) == tool.MAX_NODE_LOG)
check("an attempt outside a call (no log open) does not break", node._rpc_at("https://primary.test", "x", {}, 1)["success"] is True)

# -- Hedged reads (owner, 2026-10-24: "add the hedged reads too"): from the router about
# one coinset read in five took 2-20 s on its first and only try. A read not answered by
# the hedge delay is asked again; the first answer wins.
print("\nhedged reads")
import os
import subprocess
os.environ["FORGE_NODE_HEDGE_MS"] = "200"


def timed(n, route="get_coin_records_by_hint"):
    started = time.monotonic()
    try:
        body = n.rpc(route, {"hint": "00"})
    except tool.MultisigError as exc:
        body = exc
    return body, time.monotonic() - started, tool.NODE_LOG[-1]


stalls["primary.test"] = [2.0]
body, took, entry = timed(node)
check("a primary that stalls is asked of the failover node, which answers first",
      took < 1.0 and body.get("host") == "backup.test" and entry["hedged"] and entry["attempts"] == 2 and entry["host"] == "backup.test",
      f"{took:.2f}s {entry}")

alone = tool.Node("https://primary.test", failover_urls=[])
stalls["primary.test"] = [2.0, 0]
body, took, entry = timed(alone)
check("with no failover node, the same node is asked again", took < 1.0 and entry["hedged"] and entry["host"] == "primary.test", f"{took:.2f}s")

body, took, entry = timed(node)
check("a read answered in time is asked once, not hedged", entry["attempts"] == 1 and not entry["hedged"] and entry["host"] == "primary.test")

answers_4xx["primary.test"] = 1
body, took, entry = timed(node)
check("a 4xx is the node's answer: raised at once, not hedged or retried",
      isinstance(body, tool.MultisigError) and "HTTP 400" in str(body) and entry["attempts"] == 1 and not entry["hedged"] and took < 0.2, str(body))

stalls["primary.test"], stalls["backup.test"] = [2.0], [0.3]
refusals["backup.test"] = 1
body, took, entry = timed(node)
check("a hedge that is refused does not win: the stalled primary's answer is still taken",
      body.get("host") == "primary.test" and entry["hedged"] and 1.9 < took < 3.0, f"{took:.2f}s")

os.environ["FORGE_NODE_HEDGE_MS"] = "0"
stalls["primary.test"] = [0.6]
body, took, entry = timed(node)
check("FORGE_NODE_HEDGE_MS=0 turns hedging off", took >= 0.5 and not entry["hedged"] and entry["attempts"] == 1, f"{took:.2f}s")
os.environ["FORGE_NODE_HEDGE_MS"] = "200"

# The request left behind must not hold the tool open after the answer is in.
probe = f'''
import sys, time, io, json
sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parent)!r})
import multisig_tool as tool
class R(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False
def fake(request, timeout=0):
    if "slow.test" in request.full_url: time.sleep(20)
    return R(json.dumps({{"success": True}}).encode())
tool.urllib.request.urlopen = fake
tool.Node("https://slow.test", failover_urls=["https://fast.test"]).rpc("get_coin_records_by_hint", {{}})
'''
started = time.monotonic()
done = subprocess.run([sys.executable, "-c", probe], env={**os.environ, "FORGE_NODE_HEDGE_MS": "200"}, timeout=60)
check("the stalled request left behind does not keep the process alive", done.returncode == 0 and time.monotonic() - started < 10,
      f"exited in {time.monotonic() - started:.1f}s")

print(f"\n{passed}/{passed + failed} node call log checks passed")
sys.exit(1 if failed else 0)
