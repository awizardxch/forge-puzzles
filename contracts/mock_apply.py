"""Run a spend bundle's puzzles for the mock node: which coins it creates.

The mock coinset (scripts/multisig-mock-node.mjs) cannot run CLVM. When a
bundle is pushed it hands the coin spends here and gets back, per spend, the
CREATE_COIN children so it can record them — enough for the vault's lineage
walk, balance reads and manifests to work in a dry run.
"""

from __future__ import annotations

import json
import sys

from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode
from chia.consensus.condition_tools import conditions_dict_for_solution

from multisig_tool import MAX_CLVM_COST, coin_from_json, strip0x


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    payload = json.loads(sys.stdin.read() or "{}")
    out = []
    try:
        for spend in payload.get("coin_spends") or []:
            coin = coin_from_json(spend["coin"])
            puzzle = Program.from_bytes(bytes.fromhex(strip0x(spend["puzzle_reveal"])))
            solution = Program.from_bytes(bytes.fromhex(strip0x(spend["solution"])))
            children = []
            for cond in conditions_dict_for_solution(puzzle, solution, MAX_CLVM_COST).get(ConditionOpcode.CREATE_COIN, []):
                children.append({"parent_coin_info": coin.name().hex(), "puzzle_hash": bytes(cond.vars[0]).hex(), "amount": int.from_bytes(cond.vars[1], "big")})
            out.append({"coin_id": coin.name().hex(), "children": children})
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({"success": True, "spends": out}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
