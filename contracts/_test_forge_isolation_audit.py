#!/usr/bin/env python3
"""Cross-pool isolation and the single-asset (vault) path.

Two surfaces not covered by the other audits:

**Isolation.** A pool singleton is curried with its own state, and nothing stops
someone launching a pool whose state *claims* another pool's reserve coins. Under
V9 that was a live theft: the reserve authorized on an announcement the attacker
could produce, so a hostile pool could name a victim's reserves and drain them.
V10 curries the launcher into the reserve, so the victim's reserve coin runs a
puzzle whose hash commits to the victim's launcher -- a hostile pool derives
different hashes and its plan cannot match. These probes check that end of it.

**The vault.** A single-asset pool cannot swap, so crossing between its LP and
its one reserve is its only trade, and from V8 the LP fee applies to that
crossing in both directions. That path has its own branches in the puzzle
(`vault_fee_bps`, the `total_weight == weights.first` case in `effective_product`)
which the two-asset probes never reach. Honest cases are checked alongside the
adversarial ones so a probe cannot pass just because the branch rejects
everything.
"""
import hashlib
import sys

sys.path.insert(0, ".")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD
from chia_rs.sized_bytes import bytes32

import forge_puzzles
from forge_offer import ZERO_32, compiled_program, reserve_inner_puzzle
from forge_math import invariant_lp_mint, vault_fee_bps, withdrawal_amounts

MODE_SWAP, MODE_ADD, MODE_REMOVE = 0, 1, 2
FORGE = forge_puzzles.FORGE_VERSION
POOL_MOD = bytes32(bytes.fromhex("cc" * 32))
TREASURY = bytes32(bytes.fromhex("7e" * 32))
SINGLETON = [bytes32(b"\x0a" * 32), bytes32(b"\x0b" * 32), bytes32(b"\x0c" * 32)]

VICTIM_LAUNCHER = bytes32(b"\x10" * 32)
HOSTILE_LAUNCHER = bytes32(b"\xF0" * 32)
LP_ASSET = bytes32(bytes.fromhex("aa" * 32))
VAULT_COIN = bytes32(b"\x31" * 32)


def amt(n: int) -> bytes:
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 8) // 8, "big")


def coin_id(parent: bytes32, ph: bytes32, a: int) -> bytes32:
    return bytes32(hashlib.sha256(bytes(parent) + bytes(ph) + amt(a)).digest())


def cat_ph(asset_id: bytes32, inner: bytes32) -> bytes32:
    return construct_cat_puzzle(CAT_MOD, asset_id, Program.to(inner)).get_tree_hash_precalc(inner)


def reserve_hash_for(launcher: bytes32) -> bytes32:
    return reserve_inner_puzzle(FORGE, launcher).get_tree_hash()


def settle_ph(asset_id: bytes32) -> bytes32:
    return bytes32(OFFER_MOD.get_tree_hash()) if asset_id == ZERO_32 else \
        construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash()


def melt_coin_id(parent: bytes32, burn: int) -> bytes32:
    melt = compiled_program("forge_lp_melt_inner_FORGE").get_tree_hash()
    return coin_id(parent, cat_ph(LP_ASSET, melt), burn)


def mint_eve_id(parent: bytes32) -> bytes32:
    mint = compiled_program("forge_lp_mint_inner_FORGE").get_tree_hash()
    return coin_id(parent, cat_ph(LP_ASSET, mint), 1)


def vault_config(launcher: bytes32, fee_bps=30):
    """A single-asset native-XCH pool: one reserve, all the weight on it."""
    return [FORGE, POOL_MOD, [ZERO_32], [1], fee_bps, 0, ZERO_32,
            LP_ASSET, reserve_hash_for(launcher)]


def vault_plan(launcher, current, successor, protocol_fee=0):
    released = current - successor
    if released > 0:
        settle = coin_id(VAULT_COIN, settle_ph(ZERO_32), released - protocol_fee)
    elif released < 0:
        settle = coin_id(VAULT_COIN, settle_ph(ZERO_32), -released)
    else:
        settle = ZERO_32
    return [ZERO_32, VAULT_COIN, current, settle,
            coin_id(VAULT_COIN, reserve_hash_for(launcher), successor), successor,
            protocol_fee, ZERO_32]


def run(cfg, state, action):
    inner = compiled_program("pool_singleton_FORGE").curry(SINGLETON, cfg, state)
    return inner.run(Program.to([action]))


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def refuses(label, cfg, state, action):
    try:
        run(cfg, state, action)
        return check(label, False, "ACCEPTED")
    except Exception:
        return check(label, True)


def accepts(label, cfg, state, action):
    try:
        run(cfg, state, action)
        return check(label, True)
    except Exception as exc:
        return check(label, False, f"{type(exc).__name__}: {str(exc)[:60]}")


def main() -> int:
    results = []

    # ---- isolation -------------------------------------------------------
    print("cross-pool isolation:")
    victim_hash = reserve_hash_for(VICTIM_LAUNCHER)
    hostile_hash = reserve_hash_for(HOSTILE_LAUNCHER)
    results.append(check("a reserve puzzle hash is launcher-specific",
                         victim_hash != hostile_hash))

    # A hostile pool whose state claims the victim's reserve coin. Its config
    # carries ITS OWN reserve hash (it cannot claim the victim's without also
    # giving up control of the successor), so the successor id it must name
    # differs from the one the victim's coin can actually produce.
    reserve_amount, total_lp = 10_000_000_000_000, 100_000
    victim_state = [[[ZERO_32, VAULT_COIN, reserve_amount]], total_lp]
    burn = total_lp // 4
    fee = vault_fee_bps(1, FORGE, 30)
    payout = withdrawal_amounts([reserve_amount], burn, total_lp, fee)[0]

    honest = [MODE_REMOVE, bytes32(b"\x01" * 32),
              [vault_plan(VICTIM_LAUNCHER, reserve_amount, reserve_amount - payout)],
              melt_coin_id(bytes32(b"\x41" * 32), burn), -burn, bytes32(b"\x41" * 32)]
    results.append(accepts("the victim's own pool can move its reserve",
                           vault_config(VICTIM_LAUNCHER), victim_state, honest))

    # The same withdrawal, authorized by a pool on a different launcher: the
    # plan's successor id is derived from the hostile launcher's reserve hash,
    # which is not what the victim's coin would create.
    hostile = [MODE_REMOVE, bytes32(b"\x01" * 32),
               [vault_plan(HOSTILE_LAUNCHER, reserve_amount, reserve_amount - payout)],
               melt_coin_id(bytes32(b"\x41" * 32), burn), -burn, bytes32(b"\x41" * 32)]
    results.append(refuses("a hostile pool cannot name the victim's reserve",
                           vault_config(VICTIM_LAUNCHER), victim_state, hostile))

    # And a hostile pool that keeps its own config cannot describe the victim's
    # coin either -- the state it claims must match what the reserve really is.
    results.append(refuses("a hostile pool claiming the victim's state is refused",
                           vault_config(HOSTILE_LAUNCHER), victim_state, honest))

    # ---- the vault -------------------------------------------------------
    print()
    print("the vault (single-asset) path:")
    cfg = vault_config(VICTIM_LAUNCHER)

    results.append(refuses("a vault cannot swap (needs one reserve up and one down)",
                           cfg, victim_state,
                           [MODE_SWAP, bytes32(b"\x01" * 32),
                            [vault_plan(VICTIM_LAUNCHER, reserve_amount, reserve_amount - 1_000)],
                            ZERO_32, 0, ZERO_32]))

    # Redeem: the LP fee is withheld and stays with the remaining holders.
    free_payout = withdrawal_amounts([reserve_amount], burn, total_lp, 0)[0]
    results.append(check("the vault withholds its LP fee on redeem",
                         payout < free_payout, f"{payout} < {free_payout}"))
    results.append(refuses("redeeming without paying the vault fee is refused",
                           cfg, victim_state,
                           [MODE_REMOVE, bytes32(b"\x01" * 32),
                            [vault_plan(VICTIM_LAUNCHER, reserve_amount, reserve_amount - free_payout)],
                            melt_coin_id(bytes32(b"\x41" * 32), burn), -burn, bytes32(b"\x41" * 32)]))
    results.append(refuses("taking more than the vault owes is refused",
                           cfg, victim_state,
                           [MODE_REMOVE, bytes32(b"\x01" * 32),
                            [vault_plan(VICTIM_LAUNCHER, reserve_amount, reserve_amount - payout - 1)],
                            melt_coin_id(bytes32(b"\x41" * 32), burn), -burn, bytes32(b"\x41" * 32)]))

    # Wrap: depositing mints LP against the one reserve, net of the same fee.
    deposit = 1_000_000_000
    mint = invariant_lp_mint([reserve_amount], [deposit], total_lp, 30, [1])
    eve_parent = bytes32(b"\x51" * 32)
    wrap = [MODE_ADD, bytes32(b"\x01" * 32),
            [vault_plan(VICTIM_LAUNCHER, reserve_amount, reserve_amount + deposit)],
            mint_eve_id(eve_parent), mint, eve_parent]
    results.append(accepts(f"an honest vault deposit mints {mint} LP", cfg, victim_state, wrap))

    greedy_wrap = list(wrap)
    greedy_wrap[4] = mint + 1
    results.append(refuses("minting one LP more than the deposit earns is refused",
                           cfg, victim_state, greedy_wrap))

    # A deposit of nothing must not mint anything.
    results.append(refuses("minting LP for no deposit is refused", cfg, victim_state,
                           [MODE_ADD, bytes32(b"\x01" * 32),
                            [vault_plan(VICTIM_LAUNCHER, reserve_amount, reserve_amount)],
                            mint_eve_id(eve_parent), 1, eve_parent]))

    # The LP action coin still has to be the pinned melt/mint puzzle here too.
    results.append(refuses("a vault redeem with a fabricated LP coin is refused",
                           cfg, victim_state,
                           [MODE_REMOVE, bytes32(b"\x01" * 32),
                            [vault_plan(VICTIM_LAUNCHER, reserve_amount, reserve_amount - payout)],
                            coin_id(bytes32(b"\xAA" * 32), bytes32(b"\xBB" * 32), 1),
                            -burn, bytes32(b"\xAA" * 32)]))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} isolation and vault probes passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
