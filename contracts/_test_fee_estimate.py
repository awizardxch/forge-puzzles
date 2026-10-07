"""The stand-in offer a trade's fee is measured with (forge_fee_estimate.py).

The router measures a trade's fee by dry-running the trade's own respond lane
around a stand-in of the trader's offer. The stand-in has to be the same shape as
what the wallet will sign, or the measurement is of a different bundle:

* one coin per offered asset, with change, as a wallet spends;
* the fee stated as RESERVE_FEE, on the XCH the offer gives up, or on an extra
  XCH coin when it gives up none -- the shape a CAT-paid trade really has;
* the requested payments notarized exactly as asked.

And the cost command must report what the mempool charges for a bundle.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.trading.offer import Offer
from chia_rs import G2Element, SpendBundle, get_conditions_from_spendbundle
from chia.consensus.default_constants import DEFAULT_CONSTANTS

import forge_fee_estimate as est

PASSED = 0
FAILED: list[str] = []


def check(name: str, got: object, want: object) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}\n     got  {got}\n     want {want}")


CAT_A = "a1" * 32
CAT_B = "b2" * 32
MAX_COST = 11_000_000_000


def reserved_fees(offer: Offer) -> list[int]:
    fees = []
    for spend in offer.to_spend_bundle().coin_spends:
        try:
            conditions = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_COST)
        except ValueError:
            continue  # a CAT spend does not run alone; the fee is only ever on XCH

        fees += [int.from_bytes(c.vars[0], "big") for c in conditions.get(ConditionOpcode.RESERVE_FEE, [])]
    return fees


def own_spends(offer: Offer) -> int:
    """The trader's own spends: an offer's bundle also carries one zero-parent
    spend per requested payment, which the settlement replaces."""
    return sum(1 for spend in offer.to_spend_bundle().coin_spends if bytes(spend.coin.parent_coin_info) != bytes(32))


def build(offered, requested, fee=1) -> Offer:
    result = est.standin({"offered": offered, "requested": requested, "fee": fee})
    check(f"stand-in builds for {offered}", result["success"], True)
    return Offer.from_bech32(result["offer"])


# ─── XCH-paid: the fee rides on the XCH coin given up ────────────────────────
xch = build([{"asset_id": None, "amount": "10000000000"}], [{"asset_id": CAT_A, "amount": "40"}], fee=7)
check("an XCH-paid offer spends one coin", own_spends(xch), 1)
check("and states its fee as RESERVE_FEE", reserved_fees(xch), [7])
check("it requests exactly what was asked", [(bytes(a).hex() if a else None, [int(p.amount) for p in ps]) for a, ps in xch.requested_payments.items()], [(CAT_A, [40])])
check("it gives up exactly the amount", xch.get_offered_amounts(), {None: 10_000_000_000})

# ─── CAT-paid: the fee needs its own XCH coin, as a wallet's does ────────────
cat = build([{"asset_id": CAT_A, "amount": "100"}], [{"asset_id": CAT_B, "amount": "200"}], fee=9)
check("a CAT-paid offer spends the CAT coin and an XCH coin for the fee", own_spends(cat), 2)
check("the fee coin states the fee", reserved_fees(cat), [9])
check("it gives up exactly the CAT", {(bytes(k).hex() if k else None): v for k, v in cat.get_offered_amounts().items()}, {CAT_A: 100})

# ─── a deposit: two assets given up, LP asked for ────────────────────────────
deposit = build([{"asset_id": None, "amount": "2000000000"}, {"asset_id": CAT_A, "amount": "10"}], [{"asset_id": CAT_B, "amount": "5"}])
check("a two-asset deposit spends one coin per asset, no extra fee coin", own_spends(deposit), 2)

# ─── the stand-in is never signed ────────────────────────────────────────────
check("the stand-in carries an empty signature", deposit.to_spend_bundle().aggregated_signature, G2Element())


def refused(payload) -> bool:
    try:
        est.standin(payload)
    except Exception:  # noqa: BLE001
        return True
    return False


check("no offered assets is refused", refused({"offered": [], "requested": [{"asset_id": None, "amount": "1"}]}), True)
check("a zero fee is refused (the real offer states one)", refused({"offered": [{"asset_id": None, "amount": "5"}], "requested": [{"asset_id": CAT_A, "amount": "1"}], "fee": 0}), True)
check("more than eight legs is refused", refused({"offered": [{"asset_id": None, "amount": "5"}] * 9, "requested": [{"asset_id": CAT_A, "amount": "1"}]}), True)

# ─── cost: what the mempool charges ──────────────────────────────────────────
# A standard spend that only creates a coin needs nothing else in its bundle, so
# both sides can measure it: the command, and chia_rs directly.
from chia.types.blockchain_format.coin import Coin
from chia.types.coin_spend import make_spend
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import solution_for_conditions
from chia_rs.sized_bytes import bytes32

inner, ph = est._standin_wallet()
plain_coin = Coin(bytes32(b"3" * 32), ph, 1_000)
plain = SpendBundle([make_spend(plain_coin, inner, solution_for_conditions([[51, bytes32(b"D" * 32), 990], [52, 10]]))], G2Element())
direct = get_conditions_from_spendbundle(plain, DEFAULT_CONSTANTS.MAX_BLOCK_COST_CLVM, DEFAULT_CONSTANTS, est.COST_RULES_HEIGHT).cost
measured = est.cost({"bundle": plain.to_json_dict()})
check("cost measures a whole bundle", measured["cost"] > 0, True)
check("and agrees with chia_rs on it exactly", measured["cost"], direct)
check("and counts its spends", measured["spends"], 1)

print(f"fee estimate: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
