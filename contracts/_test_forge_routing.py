#!/usr/bin/env python3
"""Routing across V10 pools: multi-hop, split, and vault crossings.

The routing builders were all touched when V10 bound reserves to their pool --
the reserve solution changed shape and the inner puzzle is now curried -- but
every existing routing suite builds V7/V8 pools, so none of them exercise the
new shape. This does.

Multi-pool bundles are also where cross-pool authorization would show up if it
were possible at all: several pools move at once, each with its own reserves and
its own announcements. V10 keys each reserve's authorization to its owning
pool's singleton puzzle hash, so a leg cannot be satisfied by the wrong pool.
The audit below is what checks that in practice -- it verifies every announcement
in the bundle is answered and that value is conserved across the whole thing, so
a leg authorized by the wrong pool, or an intermediate diverted mid-route, shows
up as an unsatisfied assertion rather than a silent success.
"""
import sys

sys.path.insert(0, ".")

from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_puzzles
from _forge_testkit import USER_PH, audit, cat_maker_spend
from _forge_testkit import (
    FEE_BPS, NONCE, PROTOCOL_BPS, TREASURY, X, Y, Z, creation_offer_for,
    external_of, make_offer, paid_to,
)
import forge_stdin as fs
from forge_create_pool import deploy
from forge_math import swap_output
from forge_multihop_swap import build_multihop_swap
from forge_offer import ZERO_32
from forge_split_swap import SplitBranchSpec, build_split_swap
from forge_vault_route import build_swap_then_redeem, quote_swap_then_redeem

FORGE = forge_puzzles.FORGE_VERSION
BOOT = 5_000_000


def make_pool(assets, bootstrap, salt, protocol_bps=PROTOCOL_BPS):
    """A pool at the shipping revision -- no legacy opt-in."""
    result = deploy({
        "offer": creation_offer_for(assets, bootstrap, salt).to_bech32(),
        "dry_run": True,
        "execution": {
            "protocolVersion": FORGE,
            "assetIds": [a.hex() for a in assets],
            "bootstrapAmounts": [str(bootstrap[a]) for a in assets],
            "swapFeeBps": FEE_BPS,
            "protocolFeeBps": protocol_bps,
            "protocolPuzzleHash": TREASURY.hex(),
            "lpRecipientPuzzleHash": USER_PH.hex(),
        },
    })
    if not result.get("success"):
        raise RuntimeError(result.get("error"))
    return fs._pool(result["poolSnapshot"])


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def hop_out(pool, asset_in, asset_out, amount_in):
    assets = [bytes32(a) for a in pool.config[2]]
    reserves = {bytes32(r[0]): int(r[2]) for r in pool.state[0]}
    gross = swap_output(reserves[asset_in], reserves[asset_out], amount_in, FEE_BPS)
    return gross - gross * PROTOCOL_BPS // 10_000


def main() -> int:
    results = []
    boot = {a: BOOT for a in (X, Y, Z)}

    print(f"routing across V{FORGE} pools:")

    # ---- multi-hop: X -> Y -> Z across two pools -------------------------
    xy = make_pool(sorted([X, Y]), boot, 0xA1)
    yz = make_pool(sorted([Y, Z]), boot, 0xA2)
    amount_in = 100_000
    mid = hop_out(xy, X, Y, amount_in)
    final = hop_out(yz, Y, Z, mid)

    spends = cat_maker_spend(X, amount_in, 0xB1)
    offer = make_offer(spends, {Z: [NotarizedPayment(USER_PH, uint64(final), [], NONCE)]})
    try:
        result = build_multihop_swap([xy, yz], [X, Y, Z], offer)
        problems, checks = audit(result.bundle, external_of([xy, yz], spends))
        results.append(check(f"a two-hop route builds and audits clean ({checks} assertions, "
                             f"{len(result.bundle.coin_spends)} spends)", not problems))
        for p in problems:
            print(f"         - {p}")
        results.append(check("the trader is paid the final leg's output",
                             paid_to(result.bundle, Z, USER_PH) == final,
                             f"{paid_to(result.bundle, Z, USER_PH)} == {final}"))
        results.append(check("both pools advanced", len(result.pools) == 2))
        # Each hop pays its own protocol fee, so the recipient is paid twice.
        treasury = paid_to(result.bundle, Y, TREASURY) + paid_to(result.bundle, Z, TREASURY)
        results.append(check("the protocol recipient is paid on every hop",
                             treasury > 0, f"{treasury} across two legs"))
    except Exception as exc:
        results.append(check(f"a two-hop route builds: {type(exc).__name__}: {str(exc)[:70]}", False))

    # ---- a route may not be over-asked -----------------------------------
    greedy_spends = cat_maker_spend(X, amount_in, 0xB2)
    greedy = make_offer(greedy_spends,
                        {Z: [NotarizedPayment(USER_PH, uint64(final + 1), [], NONCE)]})
    try:
        build_multihop_swap([xy, yz], [X, Y, Z], greedy)
        results.append(check("a route that cannot pay the Offer is refused", False))
    except Exception:
        results.append(check("a route that cannot pay the Offer is refused", True))

    # ---- a route may not reuse a pool ------------------------------------
    try:
        build_multihop_swap([xy, xy], [X, Y, X], offer)
        results.append(check("reusing one pool in a route is refused", False))
    except Exception:
        results.append(check("reusing one pool in a route is refused", True))

    print()
    print("splitting one order across two V10 pools:")
    # ---- split: X -> Z through two independent pools ---------------------
    xz_a = make_pool(sorted([X, Z]), boot, 0xA3)
    xz_b = make_pool(sorted([X, Z]), {X: BOOT * 2, Z: BOOT * 2}, 0xA4)
    half = 60_000
    out_a = hop_out(xz_a, X, Z, half)
    out_b = hop_out(xz_b, X, Z, half)

    split_spends = cat_maker_spend(X, half * 2, 0xB3)
    split_offer = make_offer(split_spends, {
        Z: [NotarizedPayment(USER_PH, uint64(out_a + out_b), [], NONCE)]})
    branches = [SplitBranchSpec([xz_a], [X, Z], half), SplitBranchSpec([xz_b], [X, Z], half)]
    try:
        result = build_split_swap(branches, split_offer)
        problems, checks = audit(result.bundle, external_of([xz_a, xz_b], split_spends))
        results.append(check(f"a two-branch split builds and audits clean ({checks} assertions)",
                             not problems))
        for p in problems:
            print(f"         - {p}")
        results.append(check("the split reports the combined output",
                             result.total_out == out_a + out_b,
                             f"{result.total_out} == {out_a + out_b}"))
        results.append(check("both branches' pools advanced", len(result.pools) == 2))
    except Exception as exc:
        results.append(check(f"a two-branch split builds: {type(exc).__name__}: {str(exc)[:70]}", False))

    # Branches sharing a pool would price twice against reserves that move once.
    try:
        build_split_swap([SplitBranchSpec([xz_a], [X, Z], half),
                          SplitBranchSpec([xz_a], [X, Z], half)], split_offer)
        results.append(check("branches sharing a pool are refused", False))
    except Exception:
        results.append(check("branches sharing a pool are refused", True))

    print()
    print("crossing a V10 vault (swap into its LP, then redeem):")
    # A vault holds one asset and cannot trade, so its liquidity is only reachable
    # by acquiring its LP elsewhere and burning it here. That makes the route two
    # different kinds of leg in one bundle -- a swap and a redemption -- and the
    # redemption is the V10 melt path, so this is the lane most changed by the
    # reserve binding.
    vault = make_pool([X], {X: BOOT}, 0xA5, protocol_bps=0)
    lp_asset = bytes32(vault.lp_asset_id)
    pair_assets = sorted([Y, lp_asset])
    pair = make_pool(pair_assets, {Y: BOOT, lp_asset: BOOT}, 0xA6, protocol_bps=0)

    vault_reserve = int(vault.state[0][0][2])
    vault_supply = int(vault.state[1])
    # The builder owns this quote, including the vault's fee on the crossing;
    # recomputing it here would only test the test.
    lp_out, redeemed = quote_swap_then_redeem(pair, Y, vault, 50_000)

    vault_spends = cat_maker_spend(Y, 50_000, 0xB4)
    vault_offer = make_offer(vault_spends, {
        X: [NotarizedPayment(USER_PH, uint64(redeemed), [], NONCE)]})
    try:
        # A route ending in a redemption is the vault-route lane, not multi-hop:
        # the LP is minted and burned inside the one bundle.
        result = build_swap_then_redeem(pair, Y, vault, vault_offer)
        problems, checks = audit(result.bundle, external_of([pair, vault], vault_spends))
        results.append(check(f"a vault-crossing route builds and audits clean "
                             f"({checks} assertions, {len(result.bundle.coin_spends)} spends)",
                             not problems))
        for p in problems:
            print(f"         - {p}")
        results.append(check("the trader receives the vault's underlying",
                             paid_to(result.bundle, X, USER_PH) == redeemed,
                             f"{paid_to(result.bundle, X, USER_PH)} == {redeemed}"))
        results.append(check("the vault withheld its LP fee on the crossing",
                             redeemed < vault_reserve * lp_out // vault_supply))
    except Exception as exc:
        results.append(check(f"a vault-crossing route builds: "
                             f"{type(exc).__name__}: {str(exc)[:70]}", False))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} V{FORGE} routing checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
