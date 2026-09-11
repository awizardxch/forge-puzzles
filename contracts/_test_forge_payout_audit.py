#!/usr/bin/env python3
"""What a swap pays out, and who can influence it.

The router builds the bundle, so the trader's protection cannot rest on the
router behaving. It rests on two things: the Offer's notarised payment, which the
settlement spend must satisfy, and the protocol fee, which the pool derives from
its own config and the reserve refuses to split any other way.

These probes push on the parts the router DOES control -- the router fee rate and
the surplus -- and check the invariants that must hold regardless:

  * the trader is never paid less than they notarised, at any fee rate;
  * the protocol fee reaches the configured recipient, exactly;
  * nothing is invented or lost: trader + protocol + router == what the curve
    released.

The last one is what makes the other two meaningful. A bundle that simply burned
the surplus would satisfy "trader gets their minimum" while quietly destroying
value, so the sum is checked every time.
"""
import sys

sys.path.insert(0, ".")

from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from _forge_testkit import USER_PH, audit, cat_maker_spend
from _test_forge_transition import (
    ASSETS, FEE_BPS, NONCE, TREASURY, build_pool, external_of, make_offer, payouts_in,
)
from forge_math import swap_output
from forge_transition import build_transition
from forge_offer import MODE_SWAP

ROUTER_PH = bytes32.fromhex("d0" * 32)
PROTOCOL_BPS = 25


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def settle(pool, amount_in, notarised, dev_bps, salt):
    """Build and audit one swap; return what each party actually received."""
    spends = cat_maker_spend(ASSETS[0], amount_in, salt)
    offer = make_offer(spends, {ASSETS[1]: [
        NotarizedPayment(USER_PH, uint64(notarised), [], NONCE)]})
    result = build_transition(pool, offer, MODE_SWAP, ROUTER_PH, dev_bps)
    problems, checks = audit(result.bundle, external_of(pool, spends))
    paid = payouts_in(result.bundle, ASSETS[1], ROUTER_PH)
    router = paid.get(ROUTER_PH, 0)
    return paid[USER_PH], paid[TREASURY], router, result.dev_fee_collected, problems, checks


def main() -> int:
    results = []
    pool, _ = build_pool(PROTOCOL_BPS)
    reserves = [int(r[2]) for r in pool.state[0]]

    amount_in = 100_000
    released = swap_output(reserves[0], reserves[1], amount_in, FEE_BPS)
    protocol_fee = released * PROTOCOL_BPS // 10_000
    traders_share = released - protocol_fee

    print(f"a swap of {amount_in} releases {released}; the protocol takes {protocol_fee}")
    print()

    # The trader notarises less than the full share, leaving a surplus the router
    # may draw its fee from. This is the ordinary case.
    notarised = traders_share - 400
    for dev_bps, label in ((0, "no router fee"), (42, "the usual 42 bps")):
        trader, treasury, router, reported, problems, checks = settle(
            pool, amount_in, notarised, dev_bps, 0xE1)
        results.append(check(f"{label}: bundle audits clean ({checks} assertions)", not problems))
        results.append(check(f"{label}: the trader gets at least what they notarised",
                             trader >= notarised, f"{trader} >= {notarised}"))
        results.append(check(f"{label}: the protocol recipient is paid exactly",
                             treasury == protocol_fee, f"{treasury} == {protocol_fee}"))
        results.append(check(f"{label}: nothing invented or lost",
                             trader + treasury + router == released,
                             f"{trader} + {treasury} + {router} == {released}"))
        results.append(check(f"{label}: the reported fee is what was paid",
                             router == reported, f"{router} == {reported}"))

    print()
    print("a hostile router cannot reach past the surplus:")
    # The router controls only the rate it asks for. At an absurd rate the fee
    # must still stop at the surplus -- the trader's notarised amount is floor.
    for dev_bps in (5_000, 10_000, 100_000):
        trader, treasury, router, _reported, problems, _ = settle(
            pool, amount_in, notarised, dev_bps, 0xE2)
        ok = trader >= notarised and not problems
        results.append(check(f"at {dev_bps} bps the trader still gets their minimum",
                             ok, f"trader={trader} floor={notarised} router={router}"))
        results.append(check(f"at {dev_bps} bps value is still conserved",
                             trader + treasury + router == released))

    print()
    print("with no surplus there is nothing for the router to take:")
    # The trader notarises the whole of their share, so surplus is zero.
    trader, treasury, router, reported, problems, _ = settle(
        pool, amount_in, traders_share, 42, 0xE3)
    results.append(check("the router collects nothing", router == 0 and reported == 0))
    results.append(check("the trader receives their whole share",
                         trader == traders_share, f"{trader} == {traders_share}"))
    results.append(check("value is conserved", trader + treasury + router == released))

    print()
    print("an Offer asking for more than the curve gives is refused:")
    try:
        settle(pool, amount_in, traders_share + 1, 0, 0xE4)
        results.append(check("over-asking is refused", False, "it settled"))
    except Exception:
        results.append(check("over-asking is refused", True))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} payout probes passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
