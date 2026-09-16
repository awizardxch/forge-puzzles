#!/usr/bin/env python3
"""Each V14 change, shown against BOTH builds: what V13 did, what V14 does.

A "fixed" claim without the before half is a claim about the reviewer's reading, not
about the code. Every row here runs the same construction against the V13 and the V14
puzzles and records both verdicts:

  R-1  a registration whose reserve parents name nothing   V13 ACCEPTED   V14 refused
  1.8  a one-mojo settlement named as a 250,000 input, the  V13 ACCEPTED (the leaf never bound the amount;
       value supplied by another coin in the bundle           conservation alone guarded it)
                                                            V14 refused in the puzzle (ASSERT_CONCURRENT_SPEND)
  3    the floor: the deepest remove and the smallest genesis, at 1000 on V13 and 1 on V14
  R-2  the I-1 vector                                       refused on both; the line was always there

Needs both builds: contracts/v13/compiled and contracts/v14/compiled.
Exit 0 all pass, 1 a failure, 2 nothing exercised.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs.sized_bytes import bytes32

import _v13_testkit as k13
import _v14_testkit as k14
import forge_math

results = []
CAT = bytes32(b"\xd0" * 32)
RECIPIENT = bytes32(b"\x55" * 32)
H0 = 6_999_990


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def verdict(kit, thunk) -> str:
    """'ACCEPTED', or the refusal's code."""
    try:
        thunk()
        return "ACCEPTED"
    except kit.Rejected as exc:
        return str(exc).split(": ")[-1].split(" ")[0]
    except Exception as exc:
        return f"local:{type(exc).__name__}"


def registry_and_slots(kit):
    reg0 = kit.make_registry(salt=0x21)
    bundle, _ = kit.registry_spend(reg0, "forge_registry_init", [])
    kit.validate(bundle)
    return reg0.advance([1, 0]), {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}


def unfunded_registration(kit, pool, reg, slots):
    """The V13 construction: the registry spend, slots, launcher, genesis mint, fee -- and
    nothing that creates or spends a reserve."""
    left, right = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
    extra = []
    for value, key in ((kit.slot_value(left[0], left[1], left[2], right[0]), left[0]),
                       (kit.slot_value(right[0], right[1], left[0], right[2]), right[0])):
        parent, parent_inner = slots[key]
        extra.append(kit.slot_spend(reg, value, parent, parent_inner)[1])
    extra.append(kit.launcher_spend(pool)[1])
    extra.extend(kit.lp_genesis_mint_spends(pool, RECIPIENT))
    extra.append(kit.fee_settlement(reg, pool.launcher_id)[1])
    return kit.registry_spend(reg, "forge_registry_register", kit.register_solution(pool, left, right), extra_spends=extra)[0]


def swap_with(kit, pool, settlement_coin, settlement_spend, parent, amount):
    """A swap naming `settlement_coin` (V13: by id; V14: by parent and amount) with the bundle
    value-balanced by a plain coin, so conservation is satisfied and only a puzzle can refuse."""
    r, w = pool.state[0], pool.weights
    honest = forge_math.swap_output(r[0], r[1], 250_000, pool.fee_bps, w[0], w[1])
    ref = [settlement_coin.name()] if kit is k13 else [parent, amount]
    filler = kit.xch_settlement(250_000 - int(settlement_coin.amount), salt=0x71)
    return kit.spend_action(pool, "forge_action_swap", [H0, 0, 1, 250_000, honest, *ref], extra_spends=[settlement_spend, filler])[0]


def remove_burning(kit, pool, burn, salt):
    vf = forge_math.vault_fee_bps(len(pool.state[0]), 10, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)
    probe, _, _, _ = kit.run_leaf(pool, "forge_action_remove", [H0, burn, bytes32(b"\x01" * 32), payouts])
    lp_parent, lp_spends = kit.lp_melt_spend(pool, burn, pool.state[1] - burn, probe.get_tree_hash(), salt=salt)
    return kit.spend_action(pool, "forge_action_remove", [H0, burn, lp_parent, payouts], extra_spends=lp_spends)[0]


def main() -> int:
    if not (k13.v13_available() and k13.registry_available() and k14.v14_available() and k14.registry_available()):
        print("  [skip] both builds are needed: scripts/build-v13.py and scripts/build-v14.py")
        return 2
    floor13, floor14 = k13.MIN_LOCKED_LP, k14.LOCKED_BURN
    check(f"the floors: V13 MIN_LOCKED_LP = {floor13}, V14 LOCKED_BURN = {floor14}", floor13 == 1000 and floor14 == 1)

    print("R-1  a registration whose reserve parents name coins nobody creates:")
    before, after = [], []
    for kit, tag, sink in ((k13, "V13", before), (k14, "V14", after)):
        reg, slots = registry_and_slots(kit)
        pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50)
        sink.append(verdict(kit, lambda: kit.validate(unfunded_registration(kit, pool, reg, slots))))
    check(f"V13: {before[0]} -- the slot is taken by a pool no one can ever spend", before[0] == "ACCEPTED")
    check(f"V14: {after[0]} -- register asserts each launcher's announcement from the derived parent", after[0] == "12")

    print("1.8  a one-mojo settlement named as the 250,000 input of a swap, the value supplied elsewhere in the bundle:")
    for kit, tag, want in ((k13, "V13", "ACCEPTED"), (k14, "V14", "132")):
        pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x40)
        parent = bytes32(b"\xc5" * 32)
        tiny = kit.Coin(parent, bytes32(kit.OFFER_MOD_HASH), kit.uint64(1))
        tiny_spend = kit.make_spend(tiny, kit.OFFER_MOD, kit.Program.to([[tiny.name()]]))
        v = verdict(kit, lambda: kit.validate(swap_with(kit, pool, tiny, tiny_spend, parent, 250_000)))
        check(f"{tag}: {v} -- " + ("the leaf bound the asset and the coin id, never the amount; only conservation guarded value"
                                    if tag == "V13" else "the leaf's own derived-id assert (132 = ASSERT_CONCURRENT_SPEND_FAILED)"),
              v == want)

    print("3    the floor, from both sides:")
    for kit, tag, floor in ((k13, "V13", floor13), (k14, "V14", floor14)):
        pool = kit.make_pool([None, CAT, bytes32(b"\xd1" * 32)], [3_000_000, 4_000_000, 5_000_000], total_lp=3_000_000, leaves="forge", salt=0x34)
        at = verdict(kit, lambda: kit.validate(remove_burning(kit, pool, 3_000_000 - floor, 0x95)))
        past = verdict(kit, lambda: kit.validate(remove_burning(kit, pool, 3_000_000 - floor + 1, 0x96)))
        check(f"{tag}: burning down to total_lp - {floor} is {at}; one unit further is {past}",
              at == "ACCEPTED" and past != "ACCEPTED")
        reg, slots = registry_and_slots(kit)
        for supply, expect in ((floor, False), (floor + 1, True)):
            g = kit.make_pool([None, CAT], [1_000_000, 1_000_000], total_lp=supply, leaves="forge", salt=0x56)
            extra = [] if kit is k13 else kit.reserve_launcher_spends(g)
            left, right = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
            spends = []
            for value, key in ((kit.slot_value(left[0], left[1], left[2], right[0]), left[0]),
                               (kit.slot_value(right[0], right[1], left[0], right[2]), right[0])):
                parent, parent_inner = slots[key]
                spends.append(kit.slot_spend(reg, value, parent, parent_inner)[1])
            spends += [kit.launcher_spend(g)[1], *kit.lp_genesis_mint_spends(g, RECIPIENT), kit.fee_settlement(reg, g.launcher_id)[1], *extra]
            v = verdict(kit, lambda: kit.validate(kit.registry_spend(reg, "forge_registry_register",
                                                                       kit.register_solution(g, left, right), extra_spends=spends)[0]))
            check(f"  {tag}: a genesis supply of {supply} is {v}", (v == "ACCEPTED") == expect)

    print("R-2  the I-1 vector [-100000, +500000] at lp_delta 36,611:")
    for kit, tag in ((k13, "V13"), (k14, "V14")):
        pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50)
        tail = [[bytes32(b"\x02" * 32)] * 2] if kit is k13 else [[bytes32(b"\x02" * 32)] * 2, [500_000, 500_000]]
        v = verdict(kit, lambda: kit.run_leaf(pool, "forge_action_add", [H0, [-100_000, 500_000], 36_611, bytes32(b"\x01" * 32), *tail]))
        check(f"{tag}: {v} -- `assert deposit >= 0` was always present and is load-bearing", v != "ACCEPTED")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} before/after checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
