#!/usr/bin/env python3
"""push_tx PENDING is not a submission (2026-09-26 external review, finding 08).

The multisig and vault tools push an assembled bundle and report whether it
went. They used to count `status: PENDING` as success, so multisig-execute
marked the proposal submitted and staled every sibling sharing a coin, on a
bundle the node was merely holding. Drive both tools' assemble paths with a
fake node and check the verdict for each status the node can answer.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import multisig_tool  # noqa: E402
import vault_tool  # noqa: E402


class FakeNode:
    def __init__(self, answer):
        self.answer = answer
        self.pushed = 0

    def push_tx(self, _bundle):
        self.pushed += 1
        return self.answer


def verdict(module, answer):
    """What the tool's push block concludes for one node answer."""
    result = {"success": True}
    node = FakeNode(answer)
    pushed = node.push_tx(None)
    status = str(pushed.get("status") or "").upper()
    # The block under test, lifted verbatim from the tool so a drift there
    # fails here (see `_push_block_source` below, which checks it is verbatim).
    module_ns = {"result": result, "pushed": pushed, "status": status, "json": __import__("json")}
    exec(_push_block_source(module), module_ns)
    return module_ns["result"]


def _push_block_source(module) -> str:
    src = Path(module.__file__).read_text(encoding="utf-8")
    start = src.index('        status = str(pushed.get("status") or "").upper()\n')
    end = src.index("    return result\n", start)
    block = src[start:end]
    # dedent by 8
    return "\n".join(line[8:] if line.startswith("        ") else line for line in block.splitlines())


def main() -> int:
    failures = 0
    for module in (multisig_tool, vault_tool):
        name = Path(module.__file__).name
        cases = [
            ({"success": True, "status": "SUCCESS"}, True, False),
            ({"success": True, "status": "PENDING"}, False, True),
            ({"success": False, "status": "FAILED", "error": "DOUBLE_SPEND"}, False, False),
            ({"success": True}, True, False),
        ]
        for answer, want_ok, want_pending in cases:
            got = verdict(module, answer)
            ok = got.get("success") is True
            pending = bool(got.get("push", {}).get("pending"))
            good = ok == want_ok and pending == want_pending
            failures += 0 if good else 1
            print(f"  {'ok  ' if good else 'FAIL'} {name}: {answer} -> success={ok} pending={pending}")
            if want_pending:
                msg = str(got.get("error", ""))
                if "PENDING" not in msg or "held" not in msg:
                    failures += 1
                    print(f"  FAIL {name}: PENDING error does not say the bundle was held: {msg!r}")
    print("push acceptance (python): PENDING is neither submitted nor a rejection" if failures == 0 else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
