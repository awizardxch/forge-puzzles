#!/usr/bin/env python3
"""forge_v14_price_history: a pool's reserves at every state, read back from the chain.

The walk runs on a lineage built by the real driver -- `make_pool`, then `advance`
through three more states -- served by a fake node that answers exactly what a full
node answers for those coins (coin records, puzzle reveals). Every state the walk
returns must be the state that coin was curried with, newest first, ending at the
launcher. It must stop at a coin the caller already holds, resume after a budget,
and refuse a reveal that is not this pool's V14 singleton.

Exit 0 all pass, 1 a failure.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

import forge_v14_driver as drv  # noqa: E402
import forge_v14_price_history as ph  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures += 1


def refused(fn, code):
    try:
        fn()
    except ph.HistoryError as exc:
        return exc.code == code, exc.code
    return False, "accepted"


CAT = bytes32(b"\xd0" * 32)
RESERVES = [[10_000_000, 20_000_000], [11_000_000, 18_200_000], [9_500_000, 21_100_000], [12_000_000, 16_700_000]]

# The lineage: the eve pool and three successors. Each coin's parent is the one before;
# the eve coin's parent is the launcher.
pools = [drv.make_pool([None, CAT], RESERVES[0], total_lp=5_000_000, leaves="forge", salt=0x61)]
for step, reserves in enumerate(RESERVES[1:], start=1):
    # Distinct oracle readings per state, so a walk that dropped or shifted them shows.
    pools.append(pools[-1].advance(drv.forge_state(
        reserves, 5_000_000, last_height=1_000 * step, cums=[7_000 * step], last_spot=[300 + step])))
LAUNCHER = pools[0].launcher_id.hex()
coins = [pool.coin for pool in pools]
TIP = coins[-1].name().hex()


class FakeNode:
    """Answers get_coin_record_by_name and get_puzzle_and_solution for the lineage."""

    def __init__(self, reveal_for=None):
        self.calls = 0
        self.reveal_for = reveal_for or {}

    def __call__(self, route, payload):
        self.calls += 1
        if route == "get_coin_record_by_name":
            name = payload["name"].removeprefix("0x")
            for index, coin in enumerate(coins):
                if coin.name().hex() == name:
                    spent = index < len(coins) - 1
                    return {"coin_record": {
                        "coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(),
                                 "puzzle_hash": "0x" + coin.puzzle_hash.hex(), "amount": 1},
                        "confirmed_block_index": 100 + index * 10, "timestamp": 1_000 + index,
                        "spent": spent, "spent_block_index": 105 + index * 10 if spent else 0}}
            return {"coin_record": None}
        if route == "get_puzzle_and_solution":
            name = payload["coin_id"].removeprefix("0x")
            index = next(i for i, coin in enumerate(coins) if coin.name().hex() == name)
            reveal = self.reveal_for.get(index) or puzzle_for_singleton(pools[index].launcher_id, pools[index].inner)
            return {"coin_solution": {"puzzle_reveal": "0x" + bytes(reveal).hex(), "solution": "0x80"}}
        raise AssertionError(route)


print("the walk:")
node = FakeNode()
out = ph.walk(node, LAUNCHER, TIP, set(), 50)
heights = [p["height"] for p in out["points"]]
check("walks every spent coin back to the launcher", out["reached"] == "genesis" and len(out["points"]) == 3,
      f"{out['reached']}, {len(out['points'])} points")
check("newest first, each at its own confirmed height", heights == [120, 110, 100], str(heights))
check("each point holds the reserves its coin was curried with",
      [list(map(int, p["reserves"])) for p in out["points"]] == [RESERVES[2], RESERVES[1], RESERVES[0]])
check("each point carries the oracle reading its coin was curried with",
      [p["oracle"] for p in out["points"]] == [
          {"last_height": 2_000, "cums": ["14000"], "last_spot": ["302"]},
          {"last_height": 1_000, "cums": ["7000"], "last_spot": ["301"]},
          {"last_height": 0, "cums": ["0"], "last_spot": ["0"]}],
      str([p["oracle"] for p in out["points"]]))
check("the unspent tip is reported, with when its state began",
      out["tip"] == {"coin_id": TIP, "height": 130, "timestamp": 1_003})
check("the eve coin's parent is the launcher, which ends the walk",
      out["points"][-1]["parent"] == LAUNCHER)

print("stopping and resuming:")
middle = coins[1].name().hex()
out = ph.walk(FakeNode(), LAUNCHER, TIP, {middle}, 50)
check("stops at a coin the caller already holds", out["reached"] == "known" and len(out["points"]) == 1,
      f"{out['reached']}, {len(out['points'])}")
out = ph.walk(FakeNode(), LAUNCHER, TIP, set(), 2)
check("a budget stops the walk and names where to resume",
      out["reached"] == "budget" and out["next"] == coins[0].name().hex() and len(out["points"]) == 2)
rest = ph.walk(FakeNode(), LAUNCHER, out["next"], set(), 50)
check("resuming from there finishes at the launcher",
      rest["reached"] == "genesis" and [int(r) for r in rest["points"][0]["reserves"]] == RESERVES[0])
full = ph.history({"launcher_id": LAUNCHER, "segments": [{"start": TIP, "budget": 1}, {"start": coins[1].name().hex(), "budget": 50}]},
                  rpc=FakeNode())
check("history() runs segments in order and never repeats a coin",
      [p["height"] for p in full["points"]] == [120, 110, 100]
      and [s["reached"] for s in full["segments"]] == ["budget", "genesis"], str(full["segments"]))
overlap = ph.history({"launcher_id": LAUNCHER, "segments": [{"start": TIP, "budget": 50}, {"start": coins[2].name().hex(), "budget": 50}]},
                     rpc=FakeNode())
check("a later segment stops at what an earlier one walked, so nothing is read twice",
      len(overlap["points"]) == 3 and overlap["segments"][1] == {"start": coins[2].name().hex(), "walked": 0, "reached": "known", "next": None},
      str(overlap["segments"][1]))

print("refusals:")
other = drv.make_pool([None, CAT], RESERVES[0], total_lp=5_000_000, leaves="forge", salt=0x62)
ok, code = refused(lambda: ph.walk(FakeNode({1: puzzle_for_singleton(other.launcher_id, other.inner)}),
                                   LAUNCHER, TIP, set(), 50), "REVEAL_MISMATCH")
check("a reveal that does not hash to the coin is refused", ok, code)
ok, code = refused(lambda: ph.state_from_reveal(puzzle_for_singleton(other.launcher_id, other.inner),
                                                pools[0].launcher_id), "NOT_THIS_POOL")
check("another singleton's reveal is not read as this pool", ok, code)
ok, code = refused(lambda: ph.state_from_reveal(puzzle_for_singleton(pools[0].launcher_id, Program.to(1)),
                                                pools[0].launcher_id), "NOT_V14")
check("a singleton that is not the V14 action layer is refused", ok, code)
# The same three curried arguments -- finalizer, merkle root, a well-formed state --
# around a different module: only the module hash tells it apart.
_, real_args = pools[0].inner.uncurry()
imposter = Program.to(1).curry(*real_args.as_iter())
ok, code = refused(lambda: ph.state_from_reveal(puzzle_for_singleton(pools[0].launcher_id, imposter),
                                                pools[0].launcher_id), "NOT_V14")
check("a look-alike that curries a valid state into another module is refused", ok, code)
ok, code = refused(lambda: ph.state_from_reveal(Program.to(1), pools[0].launcher_id), "NOT_THIS_POOL")
check("a puzzle that is not a singleton at all is refused", ok, code)

print(f"\n{'all passed' if failures == 0 else f'{failures} failed'}")
raise SystemExit(1 if failures else 0)
