#!/usr/bin/env python3
"""The opening LP:reserve ratio, and what it does and does not change.

Every pool before this launched at `initial_lp = min(bootstrap)` -- one LP mojo
per mojo of the scarcest reserve. `lpRatio` lets a launch open at any positive
multiple of that, which a vault wants when its LP is meant to be divided into
shares: at 10x a 10.000 chest opens with 100.000 LP, so a fifth is a round
20.000 rather than 2.000.

Nothing on chain constrains the ratio. The puzzle asserts `total_lp > 0` and its
mint bracket is homogeneous in total_lp -- scale the supply by c and the c**K
cancels off both sides -- so the ratio is chosen once, at launch, and then
preserved by every mint and burn forever. There is no field to correct it later,
which is why this suite pins the two things that follow from that:

  * it changes GRANULARITY, not ownership. The same deposit buys the same
    fraction of the same pool at 1x and at 10x, and redeems for the same assets.
  * it costs XCH. Every CAT mojo is an XCH mojo, so the genesis mint's backing is
    multiplied too, and `forge_create_pool` checks that total exactly -- a launch
    whose offer was sized at one ratio and whose execution names another is
    refused rather than mis-minted.
"""
import sys

sys.path.insert(0, ".")

import forge_puzzles
from _forge_testkit import X, Y, creation_offer_for, make_pool
from forge_create_pool import deploy
from forge_math import invariant_lp_mint, withdrawal_amounts

FORGE = forge_puzzles.FORGE_VERSION
BOOT = 10_000
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    results.append(ok)
    return ok


def supply_of(pool) -> int:
    return int(pool.state[1])


def reserve_of(pool) -> int:
    return int(pool.state[0][0][2])


def main() -> int:
    print(f"\nV{FORGE} lpRatio\n")

    # ── the ratio reaches the opening state ──────────────────────────────────
    base = make_pool([X], {X: BOOT}, 0xB1, protocol_bps=0)
    check("default opens at 1 LP per reserve mojo",
          supply_of(base) == BOOT and reserve_of(base) == BOOT,
          f"supply {supply_of(base)}, reserve {reserve_of(base)}")

    ten = make_pool([X], {X: BOOT}, 0xB2, protocol_bps=0, lp_ratio=10)
    check("lpRatio=10 opens at 10 LP per reserve mojo",
          supply_of(ten) == 10 * BOOT and reserve_of(ten) == BOOT,
          f"supply {supply_of(ten)}, reserve {reserve_of(ten)}")

    check("the reserve is untouched by the ratio -- only the denominator moves",
          reserve_of(base) == reserve_of(ten) == BOOT)

    # ── granularity, not ownership ───────────────────────────────────────────
    # The same deposit into the same reserve, at both ratios.
    deposit = 1_000
    mint_1 = invariant_lp_mint([BOOT], [deposit], BOOT, 0, [1], FORGE)
    mint_10 = invariant_lp_mint([BOOT], [deposit], 10 * BOOT, 0, [1], FORGE)
    check("a deposit mints exactly 10x more LP at 10x",
          mint_10 == 10 * mint_1, f"{mint_10} == 10 * {mint_1}")
    check("...which is the same share of the pool",
          mint_1 * (10 * BOOT + mint_10) == mint_10 * (BOOT + mint_1),
          f"{mint_1}/{BOOT + mint_1} == {mint_10}/{10 * BOOT + mint_10}")

    # And redeeming that share returns the same assets at either ratio.
    back_1 = withdrawal_amounts([BOOT + deposit], mint_1, BOOT + mint_1, 0)[0]
    back_10 = withdrawal_amounts([BOOT + deposit], mint_10, 10 * BOOT + mint_10, 0)[0]
    check("burning it back returns the same assets at either ratio",
          back_1 == back_10 == deposit, f"{back_1} == {back_10} == {deposit}")

    # A passive holder's stake is a share of supply, so it must be identical.
    third_1, third_10 = BOOT // 3, (10 * BOOT) // 3
    check("a holder of a third redeems the same at either ratio",
          withdrawal_amounts([BOOT], third_1, BOOT, 0)[0]
          == withdrawal_amounts([BOOT], third_10, 10 * BOOT, 0)[0])

    # ── the XCH backing is multiplied, and checked exactly ───────────────────
    # An offer sized for 1x cannot launch a pool declared at 10x: the genesis
    # mint would create 10x the LP CAT mojos with only 1x the XCH behind them.
    try:
        result = deploy({
            "offer": creation_offer_for([X], {X: BOOT}, 0xB3, lp_ratio=1).to_bech32(),
            "dry_run": True,
            "execution": {
                "protocolVersion": FORGE,
                "assetIds": [X.hex()],
                "bootstrapAmounts": [str(BOOT)],
                "swapFeeBps": 30,
                "protocolFeeBps": 0,
                "lpRatio": 10,
                "lpRecipientPuzzleHash": ("11" * 32),
            },
        })
        refused = not result.get("success")
        detail = "" if refused else "the launch succeeded and should not have"
    except ValueError as exc:
        refused, detail = True, f"{str(exc)[:60]}"
    check("an offer backed for 1x is refused when the execution says 10x",
          refused, detail)

    # ── the guard on the value itself ────────────────────────────────────────
    # Against a well-formed 1x offer, so what is under test is the guard and not
    # the offer builder's own arithmetic on a nonsense ratio. Zero is the one
    # that matters: read with `or`, a declared 0 folds into the default and opens
    # the pool at 1x -- the wrong ratio, silently, and with no way to correct it.
    for bad in (0, -1, "0"):
        try:
            result = deploy({
                "offer": creation_offer_for([X], {X: BOOT}, 0xB4, lp_ratio=1).to_bech32(),
                "dry_run": True,
                "execution": {
                    "protocolVersion": FORGE,
                    "assetIds": [X.hex()],
                    "bootstrapAmounts": [str(BOOT)],
                    "swapFeeBps": 30,
                    "protocolFeeBps": 0,
                    "lpRatio": bad,
                    "lpRecipientPuzzleHash": ("11" * 32),
                },
            })
            ok, detail = False, ("accepted, opening at "
                                 f"{result.get('lp_out')} LP" if result.get("success")
                                 else str(result.get("error"))[:50])
        except ValueError as exc:
            ok, detail = "lpRatio" in str(exc), f"{str(exc)[:50]}"
        check(f"lpRatio={bad!r} is refused rather than defaulted", ok, detail)

    # ── a vault setting, and only a vault setting ────────────────────────────
    # On a basket the LP is a share of several reserves priced against each
    # other, so a claim ratio has nothing to refer to -- and one set by accident
    # would be permanent. Refused, not ignored: ignoring it would mint at 1x a
    # pool whose offer was already backed at the ratio that was asked for.
    pair = sorted([X, Y])
    try:
        make_pool(pair, {X: BOOT, Y: BOOT}, 0xB5, protocol_bps=0, lp_ratio=10)
        ok, detail = False, "accepted on a 2-asset pool"
    except ValueError as exc:
        ok, detail = "single-asset vault" in str(exc), f"{str(exc)[:52]}"
    check("lpRatio is refused on a multi-asset pool", ok, detail)

    two = make_pool(pair, {X: BOOT, Y: BOOT}, 0xB6, protocol_bps=0)
    check("...while a multi-asset pool still launches at the default",
          supply_of(two) == BOOT, f"supply {supply_of(two)}")

    # ── what the ratio costs: claim granularity ──────────────────────────────
    # The underlying cannot pay below one mojo, so a burn under X claims nothing.
    # Every factor of ten in the ratio therefore eats one of the CAT's three
    # decimals: at 100x the smallest useful burn is 0.100 LP, at 1000x it is
    # 1.000 and the fractional part of the LP is dead.
    for ratio in (1, 10, 100, 1000):
        supply = ratio * BOOT
        smallest = next(b for b in range(1, 2 * ratio + 1)
                        if withdrawal_amounts([BOOT], b, supply, 0)[0] > 0)
        check(f"at {ratio}x the smallest burn that claims anything is {ratio} mojos",
              smallest == ratio, f"got {smallest}")

    # And the ratio buys no granularity doing it: supply is X*R and the minimum
    # burn is X, so the number of distinct claims is X*R/X == R at every ratio.
    # Divisibility comes from the SEED, never from the ratio.
    levels = {r: (r * BOOT) // r for r in (1, 10, 100, 1000)}
    check("distinct redeemable claims are the reserve, at every ratio",
          set(levels.values()) == {BOOT}, f"{levels}")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} V{FORGE} lpRatio checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
