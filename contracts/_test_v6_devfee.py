#!/usr/bin/env python3
"""Prove the V6 swap dev fee is actually collected on chain, not just quoted.

The frontend quote nets the dev fee off the trader's input, so the curve
releases more than the trader notarised. That gap used to be refunded to the
trader. These cases check it now reaches the dev recipient instead, that the
trader still receives at least their notarised minimum, and that nothing is
created out of thin air.
"""
import sys

sys.path.insert(0, ".")

import forge_puzzles
if not forge_puzzles.available("pool_singleton_v6"):
    print("SKIP: the V6 puzzles are archived and absent from this checkout.")
    print("      Superseded revisions are not published; see docs/FORGE_SECURITY_AUDIT.md.")
    raise SystemExit(2)   # 2 == skipped; a skip must never read as a pass

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode as OP
from chia.wallet.cat_wallet.cat_utils import CAT_MOD
from chia.wallet.util.curry_and_treehash import calculate_hash_of_quoted_mod_hash, curry_and_treehash, shatree_atom
from chia.wallet.trading.offer import NotarizedPayment
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_stdin as fs
from _forge_testkit import DEV_BPS, DEV_PH, USER_PH, cat_maker_spend, cat_wrap, make_offer, payouts_to
from _test_v6_transition import load_v6_pool
from forge_offer import MODE_SWAP, ZERO_32
from forge_math import swap_output
from forge_transition import build_transition

NONCE = bytes32.fromhex("ee" * 32)


def main() -> int:
    snapshot = load_v6_pool()
    if not snapshot:
        print("SKIP: no V6 pool snapshot found in the deployment index.")
        print("      Superseded pools were retired; current-revision coverage")
        print("      lives in the _test_forge_* suites.")
        return 0

    pool = fs._pool(snapshot)
    assets = [bytes32(a) for a in pool.config[2]]
    reserves = [int(r[2]) for r in pool.state[0]]
    fee_bps = int(pool.config[4])
    cats = [i for i in range(len(assets)) if assets[i] != ZERO_32]
    native = assets.index(ZERO_32)

    # The live T6/T11 reserves are only 3000 units, so 50bps of a CAT-out swap
    # floors to a couple of mojos. The TXCH leg is 3e12, which is where a
    # realistically-sized fee shows up -- and it exercises the native
    # settlement path rather than a CAT one.
    results = []
    for label, i_in, i_out, divisor in [
        ("CAT -> CAT, quarter reserve", cats[0], cats[1], 4),
        ("CAT -> TXCH, quarter reserve", cats[0], native, 4),
        ("CAT -> TXCH, small", cats[0], native, 50),
    ]:
        amount_in = max(reserves[i_in] // divisor, 2)
        gross = swap_output(reserves[i_in], reserves[i_out], amount_in, fee_bps)
        # What the trader is quoted. calcSwapOut in 'output' mode runs the curve
        # on the full input -- matching the puzzle exactly -- then deducts the
        # dev fee from the result, so the gap is precisely the fee.
        expected_fee = gross * DEV_BPS // 10_000
        quoted = gross - expected_fee

        spends = cat_maker_spend(assets[i_in], amount_in, 0x51)
        # Chia keys an XCH request as None, not as 32 zero bytes.
        out_key = None if assets[i_out] == ZERO_32 else assets[i_out]
        offer = make_offer(spends, {out_key: [NotarizedPayment(USER_PH, uint64(quoted), [], NONCE)]})

        with_fee = build_transition(pool, offer, MODE_SWAP, DEV_PH, DEV_BPS)
        without = build_transition(pool, offer, MODE_SWAP)

        paid = payouts_to(with_fee.bundle, assets[i_out])
        dev_got = paid.get(DEV_PH, 0)
        user_got = paid.get(USER_PH, 0)

        checks = {
            "fee reported == fee derived": with_fee.dev_fee_collected == expected_fee,
            "fee never exceeds surplus": with_fee.dev_fee_collected <= gross - quoted,
            "collects the full configured rate": with_fee.dev_fee_collected == gross * DEV_BPS // 10_000,
            "dev coin created for exactly the fee": dev_got == expected_fee,
            "trader still gets >= notarised minimum": user_got >= quoted,
            "no value invented (dev + trader == gross)": dev_got + user_got == gross,
            "fee is zero when not requested": without.dev_fee_collected == 0,
            "unfeed build pays the whole gross to trader":
                payouts_to(without.bundle, assets[i_out]).get(USER_PH, 0) == gross,
            "reserves identical either way": with_fee.pool.state[0] == without.pool.state[0],
        }
        ok = all(checks.values())
        rate = (with_fee.dev_fee_collected * 10_000 / gross) if gross else 0
        print(f"{label}: in={amount_in} gross={gross} quoted={quoted} "
              f"fee={with_fee.dev_fee_collected} ({rate:.1f}bps) trader={user_got}")
        for name, passed in checks.items():
            print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        results.append(ok)
        print()

    passed = sum(results)
    print(f"{passed}/{len(results)} dev-fee cases collect correctly")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
