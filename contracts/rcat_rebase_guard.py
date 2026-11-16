"""rCAT rebase payout collisions, for the issuer's rebase tooling.

`forge_action_rebase.rue` (V17, V18) pays, from the rebased rCAT reserve, the dev fee owed (to
protocol_puzzle_hash), the DAO fee owed (to dao_puzzle_hash) and, on a downward rebase, the surplus
(to H), each as its own CREATE_COIN when its amount is positive. Two CREATE_COINs from one coin with
the same (puzzle hash, amount) are one coin id twice: consensus refuses the bundle as
DUPLICATE_OUTPUT. The contracts are frozen, so the tooling checks before it builds.

The JavaScript twin is src/lib/rcatPayouts.js (`rebaseCollision`); api/__checks__/rcatPayouts.check.mjs
runs both on the same vectors. Pure Python, no chia imports.
"""

from __future__ import annotations


def _bare(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value or "").strip().lower().removeprefix("0x")


def rebase_collision(protocol_ph, dao_ph, hidden, fee_owed: int, dao_owed: int, old: int, new: int) -> dict | None:
    """None when the rebase's payouts are distinct, else {kind, outputs, fix}."""
    surplus = old - new if old > new else 0
    outputs = [o for o in (("dev", _bare(protocol_ph), int(fee_owed)),
                           ("dao", _bare(dao_ph), int(dao_owed)),
                           ("surplus", _bare(hidden), int(surplus))) if o[2] > 0]
    for i in range(len(outputs)):
        for j in range(i + 1, len(outputs)):
            a, b = outputs[i], outputs[j]
            if a[1] != b[1] or a[2] != b[2]:
                continue
            pair = {a[0], b[0]}
            if "surplus" in pair:
                kind = "h-is-dev" if "dev" in pair else "h-is-dao"
                fix = (f"Rebase to {new - 1} or {new + 1} instead (one unit away, so the surplus differs from the owed fee), "
                       "or run a collect on this pool in an earlier block first (it pays the owed fees, so the rebase pays none).")
            else:
                kind = "dao-is-dev"
                fix = ("Run a collect on this pool in an earlier block first (it pays the owed dev and DAO fees, so the rebase "
                       "pays none); rebase must be the first action of its spend, so the two cannot share a block's spend.")
            return {"kind": kind, "outputs": [{"who": o[0], "puzzle_hash": o[1], "amount": str(o[2])} for o in (a, b)], "fix": fix}
    return None


def refuse_colliding_rebase(protocol_ph, dao_ph, hidden, fee_owed: int, dao_owed: int, old: int, new: int,
                            collect_hint: str = "") -> None:
    """Raise SystemExit naming the fix when the rebase would hit DUPLICATE_OUTPUT. Nothing is built or pushed."""
    hit = rebase_collision(protocol_ph, dao_ph, hidden, fee_owed, dao_owed, old, new)
    if hit is None:
        return
    a, b = hit["outputs"]
    raise SystemExit(
        f"rebase refused before building ({hit['kind']}): it would pay {a['who']} and {b['who']} the same "
        f"{a['amount']} to the same puzzle hash {a['puzzle_hash'][:16]}..., which consensus refuses as DUPLICATE_OUTPUT. "
        f"{hit['fix']}" + (f" Collect with: {collect_hint}" if collect_hint else ""))


if __name__ == "__main__":   # vectors in, verdicts out (api/__checks__/rcatPayouts.check.mjs)
    import json
    import sys

    cases = json.load(sys.stdin)
    print(json.dumps([rebase_collision(c["protocol"], c["dao"], c["hidden"], int(c["fee"]), int(c["dao_owed"]),
                                       int(c["old"]), int(c["new"])) for c in cases]))
