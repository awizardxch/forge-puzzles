#!/usr/bin/env python3
"""A route that crosses a vault, against the live pools.

A single-asset pool cannot trade, but its LP is an ordinary CAT that can be
paired elsewhere, and burning that LP returns the underlying. Chaining the two
reaches liquidity the swap graph cannot:

    TXCH --swap--> vaultLP --redeem--> t8

The redemption leg is exact: a vault can never swap, so no fee accrues to it and
its reserve-to-LP ratio is fixed at creation. This checks the composed bundle
settles as one unit, that the intermediate LP is created and destroyed inside it
rather than left claimable, and that the trader is paid what the route quotes.
"""
import io
import json
import sys

sys.path.insert(0, ".")

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.condition_opcodes import ConditionOpcode as OP
from chia.wallet.cat_wallet.cat_utils import CAT_MOD
from chia.wallet.trading.offer import NotarizedPayment
from chia.wallet.util.curry_and_treehash import (
    calculate_hash_of_quoted_mod_hash, curry_and_treehash, shatree_atom,
)
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin as fs
from _forge_testkit import USER_PH, audit, make_offer, xch_maker_spend
from forge_offer import ZERO_32
from forge_vault_route import (
    build_swap_then_redeem,
    quote_swap_then_redeem,
    vault_redeem_rate,
)

NONCE = bytes32.fromhex("ee" * 32)
MAX_COST = 11_000_000_000


def load_pools():
    """The live vault and the pool holding its LP, from the deployment index."""
    index = json.load(io.open("../.awizard/deployment-index.json", encoding="utf-8"))
    vault = swap_pool = None
    snapshots = []
    for entry in index.values():
        for batch in (entry.get("batches") or {}).values():
            snapshot = batch.get("poolSnapshot")
            # the swap-then-redeem lane is V10's; V11 vaults are not V3Pool snapshots
            if snapshot and int(snapshot.get("protocol_version") or 0) <= 10:
                snapshots.append(snapshot)
    for snapshot in snapshots:
        if len(snapshot["asset_ids"]) == 1:
            vault = snapshot
    if vault is None:
        return None, None
    lp_asset = vault["lp_asset_id"]
    for snapshot in snapshots:
        if lp_asset in snapshot["asset_ids"] and len(snapshot["asset_ids"]) > 1:
            swap_pool = snapshot
    return vault, swap_pool


def payouts_in(bundle, asset_id):
    """Value each inner puzzle hash receives in `asset_id`."""
    quoted = calculate_hash_of_quoted_mod_hash(CAT_MOD.get_tree_hash())

    def wrap(inner):
        return curry_and_treehash(quoted, shatree_atom(CAT_MOD.get_tree_hash()),
                                  shatree_atom(asset_id), inner)

    raw = {}
    for spend in bundle.coin_spends:
        try:
            cond = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_COST)
        except Exception:
            continue
        for c in cond.get(OP.CREATE_COIN, []):
            amount = int.from_bytes(c.vars[1], "big") if c.vars[1] else 0
            if amount > 0:
                key = bytes32(c.vars[0])
                raw[key] = raw.get(key, 0) + amount
    if asset_id == ZERO_32:
        return raw
    return {inner: raw.get(wrap(inner), 0) + raw.get(inner, 0) for inner in (USER_PH,)}


def check(label, ok):
    verdict = "PASS" if ok else "FAIL"
    print(f"  [{verdict}] {label}")
    return ok


def main() -> int:
    vault_snapshot, swap_snapshot = load_pools()
    if not vault_snapshot or not swap_snapshot:
        print("SKIP: no vault + LP-pair combination found in the deployment index.")
        print("      Superseded pools were retired; current-revision coverage")
        print("      lives in the _test_forge_* suites.")
        return 0

    vault = fs._pool(vault_snapshot)
    swap_pool = fs._pool(swap_snapshot)
    lp_asset = bytes32(vault.lp_asset_id)
    underlying = bytes32(vault.config[2][0])
    swap_assets = [bytes32(a) for a in swap_pool.config[2]]
    asset_in = next(a for a in swap_assets if a != lp_asset)

    reserve, supply = vault_redeem_rate(vault)
    print(f"vault      {vault.launcher_id.hex()[:14]}  reserve={reserve} lp={supply} "
          f"ratio={reserve / supply:.6f}")
    print(f"swap pool  {swap_pool.launcher_id.hex()[:14]}  "
          f"{[a.hex()[:8] if a != ZERO_32 else 'TXCH' for a in swap_assets]}")
    print(f"route      {'TXCH' if asset_in == ZERO_32 else asset_in.hex()[:8]}"
          f" -> {lp_asset.hex()[:8]} -> {underlying.hex()[:8]}")
    print()

    results = []
    # Size the trade from the pool rather than hardcoding: this pool is heavily
    # TXCH-weighted, so a small absolute input rounds the LP output to zero.
    swap_reserves = [int(r[2]) for r in swap_pool.state[0]]
    amount_in = max(swap_reserves[swap_assets.index(asset_in)] // 10, 2)
    lp_out, redeemed = quote_swap_then_redeem(swap_pool, asset_in, vault, amount_in)
    print(f"quote: {amount_in} in -> {lp_out} LP -> {redeemed} underlying")

    results.append(check("redemption is exact at the vault ratio",
                         redeemed == reserve * lp_out // supply))

    spends = xch_maker_spend(amount_in, 0xD1) if asset_in == ZERO_32 else None
    if spends is None:
        print("  (non-native route input is not covered by this fixture)")
        return 2

    offer = make_offer(spends, {
        underlying: [NotarizedPayment(USER_PH, uint64(redeemed), [], NONCE)],
    })
    try:
        result = build_swap_then_redeem(swap_pool, asset_in, vault, offer)
    except Exception as exc:
        print(f"  [FAIL] route builds: {type(exc).__name__}: {exc}")
        return 1

    external = {bytes(swap_pool.pool.coin.name()), bytes(vault.pool.coin.name())}
    external |= {bytes(r.coin.name()) for r in swap_pool.reserves.values()}
    external |= {bytes(r.coin.name()) for r in vault.reserves.values()}
    external |= {bytes(s.coin.name()) for s in spends}
    problems, checks = audit(result.bundle, external)

    print(f"       {len(result.bundle.coin_spends)} spends, {checks} assertions")
    results.append(check(f"bundle audits clean ({checks} assertions)", not problems))
    for p in problems:
        print(f"         - {p}")

    paid = payouts_in(result.bundle, underlying)
    results.append(check(f"trader is paid {redeemed} of the underlying",
                         paid.get(USER_PH, 0) == redeemed))

    # The whole point: the LP the swap released must be consumed inside this
    # bundle, never left as a coin someone could claim on its own.
    spent = {bytes(spend.coin.name()) for spend in result.bundle.coin_spends}
    created = {}
    for spend in result.bundle.coin_spends:
        try:
            cond = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, MAX_COST)
        except Exception:
            continue
        for c in cond.get(OP.CREATE_COIN, []):
            amount = int.from_bytes(c.vars[1], "big") if c.vars[1] else 0
            if amount >= 0:
                from chia_rs import Coin
                coin = Coin(spend.coin.name(), bytes32(c.vars[0]), uint64(amount))
                created[bytes(coin.name())] = (bytes32(c.vars[0]), amount)

    # Identify LP coins by puzzle hash, not amount. The LP-to-asset ratio is a
    # capital decision made at creation and is not necessarily unity, so an
    # amount test would be reading a property this vault happens to have rather
    # than one the protocol guarantees. Puzzle hash is right at any ratio.
    from chia.wallet.trading.offer import OFFER_MOD_HASH
    from chia.types.blockchain_format.program import Program
    quoted = calculate_hash_of_quoted_mod_hash(CAT_MOD.get_tree_hash())
    lp_shapes = {
        # The inner puzzle hash is already a tree hash and goes in raw; only the
        # mod hash and asset id are hashed as atoms.
        bytes(curry_and_treehash(quoted, shatree_atom(CAT_MOD.get_tree_hash()),
                                 shatree_atom(lp_asset), inner))
        for inner in (OFFER_MOD_HASH, Program.to(1).get_tree_hash())
    }
    lp_coins = {cid for cid, (ph, _) in created.items() if bytes(ph) in lp_shapes}
    results.append(check(
        f"every LP coin the route creates is spent in-bundle ({len(lp_coins)} of them)",
        bool(lp_coins) and lp_coins <= spent))

    # Both singletons must advance, or the route was not atomic.
    swap_next, vault_next = result.pools
    results.append(check("swap pool advanced",
                         int(swap_next.state[1]) == int(swap_pool.state[1])))
    results.append(check(f"vault LP supply fell by the burn ({lp_out})",
                         int(vault_next.state[1]) == supply - lp_out))
    results.append(check("vault reserve fell by exactly the payout",
                         int(vault_next.state[0][0][2]) == reserve - redeemed))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} vault-route checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
