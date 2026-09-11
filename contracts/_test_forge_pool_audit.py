#!/usr/bin/env python3
"""Adversarial probes against the shipping pool singleton.

Complements _test_forge_audit (the LP TAIL) and _test_forge_reserve_binding (the
reserve). Everything here drives the real compiled pool inner with a solution an
attacker would write, and asserts it is refused -- or, for the honest cases, that
it still goes through, so a probe cannot pass merely because the puzzle rejects
everything.

Grouped by the property each case is trying to break:
  * the curve      -- taking more out than the invariant allows
  * the fee        -- skipping, redirecting, or inflating the protocol fee
  * the config     -- minting a pool whose parameters the puzzle should refuse
  * the accounting -- reserves or LP supply moving in ways nothing backs
"""
import hashlib
import sys

sys.path.insert(0, ".")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD
from chia_rs.sized_bytes import bytes32

import forge_puzzles
from forge_offer import ZERO_32, compiled_program

MODE_SWAP, MODE_ADD, MODE_REMOVE = 0, 1, 2
WEIGHT_SCALE = 10_000
FEE_BPS, PROTOCOL_BPS = 30, 25

LP_ASSET = bytes32(bytes.fromhex("aa" * 32))
RESERVE_INNER = bytes32(bytes.fromhex("bb" * 32))
POOL_MOD = bytes32(bytes.fromhex("cc" * 32))
CAT_B = bytes32(bytes.fromhex("dd" * 32))
TREASURY = bytes32(bytes.fromhex("7e" * 32))
SINGLETON = [bytes32(b"\x0a" * 32), bytes32(b"\x0b" * 32), bytes32(b"\x0c" * 32)]
POOL_COIN = bytes32(b"\x01" * 32)

NATIVE_START, CAT_START, TOTAL_LP = 10_000_000_000_000, 5_000_000, 100_000


def amt(n: int) -> bytes:
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 8) // 8, "big")


def coin_id(parent: bytes32, ph: bytes32, a: int) -> bytes32:
    return bytes32(hashlib.sha256(bytes(parent) + bytes(ph) + amt(a)).digest())


def cat_ph(asset_id: bytes32, inner: bytes32) -> bytes32:
    return construct_cat_puzzle(CAT_MOD, asset_id, Program.to(inner)).get_tree_hash_precalc(inner)


def reserve_ph(asset_id: bytes32) -> bytes32:
    return RESERVE_INNER if asset_id == ZERO_32 else cat_ph(asset_id, RESERVE_INNER)


def settle_ph(asset_id: bytes32) -> bytes32:
    return bytes32(OFFER_MOD.get_tree_hash()) if asset_id == ZERO_32 else \
        construct_cat_puzzle(CAT_MOD, asset_id, OFFER_MOD).get_tree_hash()


def config(fee_bps=FEE_BPS, protocol_bps=PROTOCOL_BPS, weights=None,
           assets=None, treasury=TREASURY, version=None):
    return [version or forge_puzzles.FORGE_VERSION, POOL_MOD,
            assets or [ZERO_32, CAT_B], weights or [1, 1],
            fee_bps, protocol_bps, treasury, LP_ASSET, RESERVE_INNER]


STATE = [[[ZERO_32, bytes32(b"\x21" * 32), NATIVE_START],
          [CAT_B, bytes32(b"\x22" * 32), CAT_START]], TOTAL_LP]


def plan_for(asset_id, cur_coin, current, successor, protocol_fee=0, recipient=None):
    """One reserve's plan.

    A shrinking reserve names the settlement coin it creates, and the amount is
    what the TRADER gets -- the protocol fee leaves as its own coin. A GROWING
    reserve is fed by the Offer's settlement instead, so the pool only requires
    that id to be non-zero; a zero there is what "untouched" means.
    """
    released = current - successor
    if released > 0:
        settle = coin_id(cur_coin, settle_ph(asset_id), released - protocol_fee)
    elif released < 0:
        settle = coin_id(cur_coin, settle_ph(asset_id), -released)
    else:
        settle = ZERO_32
    return [asset_id, cur_coin, current, settle,
            coin_id(cur_coin, reserve_ph(asset_id), successor), successor,
            protocol_fee, (recipient if recipient is not None else (TREASURY if protocol_fee else ZERO_32))]


def run(cfg, state, action):
    inner = compiled_program("pool_singleton_FORGE").curry(SINGLETON, cfg, state)
    return inner.run(Program.to([action]))


def swap_action(amount_in, amount_out, *, protocol_fee=None, recipient=None, cfg=None):
    """Native in, CAT out, sized on the curve unless told otherwise."""
    cfg = cfg or config()
    fee_bps = int(cfg[4])
    effective = amount_in * (WEIGHT_SCALE - fee_bps) // WEIGHT_SCALE
    new_native = NATIVE_START + amount_in
    new_cat = CAT_START - amount_out
    released = CAT_START - new_cat
    if protocol_fee is None:
        protocol_fee = released * int(cfg[5]) // WEIGHT_SCALE
    plans = [
        plan_for(ZERO_32, bytes32(b"\x21" * 32), NATIVE_START, new_native),
        plan_for(CAT_B, bytes32(b"\x22" * 32), CAT_START, new_cat, protocol_fee, recipient),
    ]
    return [MODE_SWAP, POOL_COIN, plans, ZERO_32, 0, ZERO_32], effective


def curve_output(amount_in, fee_bps=FEE_BPS):
    """Largest out the bracket accepts: charged_in*out >= in*out0, one more breaks it."""
    effective = amount_in * (WEIGHT_SCALE - fee_bps) // WEIGHT_SCALE
    charged, floor = NATIVE_START + effective, NATIVE_START * CAT_START
    new_out = floor // charged + 1
    while charged * new_out < floor:
        new_out += 1
    while charged * (new_out - 1) >= floor:
        new_out -= 1
    return CAT_START - new_out


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    return ok


def refuses(label, cfg, state, action):
    try:
        run(cfg, state, action)
        return check(label, False, "the pool ACCEPTED it")
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
    amount_in = 1_000_000_000
    fair_out = curve_output(amount_in)

    print("the curve:")
    action, _ = swap_action(amount_in, fair_out)
    results.append(accepts("a correctly sized swap is accepted", config(), STATE, action))
    for extra, label in ((1, "one mojo"), (fair_out // 10, "10%")):
        greedy, _ = swap_action(amount_in, fair_out + extra)
        results.append(refuses(f"taking {label} more than the curve allows is refused",
                               config(), STATE, greedy))
    stingy, _ = swap_action(amount_in, fair_out - 1)
    results.append(refuses("an under-sized output is refused (the bracket is exact)",
                           config(), STATE, stingy))
    free, _ = swap_action(0, fair_out)
    results.append(refuses("taking output for no input is refused", config(), STATE, free))

    print()
    print("the protocol fee:")
    results.append(refuses("skipping the fee entirely is refused",
                           config(), STATE, swap_action(amount_in, fair_out, protocol_fee=0)[0]))
    owed = (CAT_START - (CAT_START - fair_out)) * PROTOCOL_BPS // WEIGHT_SCALE
    results.append(refuses("underpaying the fee is refused",
                           config(), STATE, swap_action(amount_in, fair_out, protocol_fee=owed - 1)[0]))
    results.append(refuses("overpaying the fee is refused",
                           config(), STATE, swap_action(amount_in, fair_out, protocol_fee=owed + 1)[0]))
    results.append(refuses("redirecting the fee to another recipient is refused",
                           config(), STATE,
                           swap_action(amount_in, fair_out, recipient=bytes32(b"\xEE" * 32))[0]))

    print()
    print("the config (a pool that should never mint):")
    for label, cfg in (
        ("a liquidity fee above MAX_FEE_BPS", config(fee_bps=201)),
        ("a negative liquidity fee", config(fee_bps=-1)),
        ("a protocol fee above MAX_PROTOCOL_FEE_BPS", config(protocol_bps=101)),
        ("a protocol fee with no recipient", config(treasury=ZERO_32)),
        ("a zero weight", config(weights=[0, 1])),
        ("assets out of canonical order", config(assets=[CAT_B, ZERO_32])),
        ("duplicate assets", config(assets=[CAT_B, CAT_B])),
        ("a mismatched protocol version", config(version=forge_puzzles.FORGE_VERSION - 1)),
    ):
        act, _ = swap_action(amount_in, curve_output(amount_in, int(cfg[4]) if int(cfg[4]) >= 0 else 30), cfg=cfg)
        results.append(refuses(label, cfg, STATE, act))

    print()
    print("the accounting:")
    # A swap must move exactly two reserves, and must not touch LP supply.
    minted = list(swap_action(amount_in, fair_out)[0])
    minted[3], minted[4] = bytes32(b"\x55" * 32), 1_000
    results.append(refuses("minting LP during a swap is refused", config(), STATE, minted))

    frozen = list(swap_action(amount_in, fair_out)[0])
    frozen[2] = [plan_for(ZERO_32, bytes32(b"\x21" * 32), NATIVE_START, NATIVE_START),
                 plan_for(CAT_B, bytes32(b"\x22" * 32), CAT_START, CAT_START)]
    results.append(refuses("a swap that moves nothing is refused", config(), STATE, frozen))

    both_up = list(swap_action(amount_in, fair_out)[0])
    both_up[2] = [plan_for(ZERO_32, bytes32(b"\x21" * 32), NATIVE_START, NATIVE_START + amount_in),
                  plan_for(CAT_B, bytes32(b"\x22" * 32), CAT_START, CAT_START + 1)]
    results.append(refuses("a swap with both reserves rising is refused", config(), STATE, both_up))

    # A remove may not burn the entire supply: the singleton must outlive it.
    whole = [MODE_REMOVE, POOL_COIN,
             [plan_for(ZERO_32, bytes32(b"\x21" * 32), NATIVE_START, 1),
              plan_for(CAT_B, bytes32(b"\x22" * 32), CAT_START, 1)],
             bytes32(b"\x33" * 32), -TOTAL_LP, bytes32(b"\x44" * 32)]
    results.append(refuses("burning the entire LP supply is refused", config(), STATE, whole))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} pool-singleton probes passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
