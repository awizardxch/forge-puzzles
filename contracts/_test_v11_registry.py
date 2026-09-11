#!/usr/bin/env python3
"""The registry singleton: init once, register a real pool, refuse everything else.

A registration bundle is the registry spend (action layer with the default
finalizer, `register` leaf), the two adjacent slot coins spent by message, the
pool's launcher creating the eve singleton, and an OFFER_MOD coin paying the
creation fee to the treasury under the launcher id. The pool is built by the
same harness that drives the action suites, so the puzzle hash the launcher
announces is a real V11 pool's -- and `register` must recompute exactly that
hash from the launcher id and the config, or the announcement it asserts is
never made. That equality is the whole revision gate.

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs.sized_bytes import bytes32

import _v11_testkit as kit

results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def refuses(label, thunk):
    try:
        thunk()
    except kit.Rejected as exc:
        return check(label, True, str(exc)[:60])
    except Exception as exc:
        return check(label, True, f"{type(exc).__name__} (local run)")
    return check(label, False, "accepted")


def accepts(label, thunk):
    try:
        out = thunk()
    except kit.Rejected as exc:
        check(label, False, str(exc)[:90])
        return None
    check(label, True)
    return out


CATS = [bytes32(bytes([0xD0 + i]) * 32) for i in range(10)]


RECIPIENT = bytes32(b"\x55" * 32)


def registration(reg, pool, left, right, slots, fee=None, kv=None, launcher_pool=None, with_fee=True,
                 with_launcher=True, genesis_mint=None, genesis_cat_parent=False):
    """Assemble a full creation-and-registration bundle: registry spend, the two
    neighbour slots, the launcher, the genesis LP mint, the fee settlement.
    `slots` maps slot key -> (parent coin, parent inner hash)."""
    extra = []
    left_value = kit.slot_value(left[0], left[1], left[2], right[0])
    right_value = kit.slot_value(right[0], right[1], left[0], right[2])
    for value, key in ((left_value, left[0]), (right_value, right[0])):
        parent, parent_inner = slots[key]
        _, spend = kit.slot_spend(reg, value, parent, parent_inner)
        extra.append(spend)
    if with_launcher:
        _, spend = kit.launcher_spend(launcher_pool or pool, kv)
        extra.append(spend)
        extra.extend(kit.lp_genesis_mint_spends(launcher_pool or pool, RECIPIENT, mint=genesis_mint,
                                                with_cat_parent=genesis_cat_parent))
    if with_fee:
        _, spend = kit.fee_settlement(reg, pool.launcher_id, amount=fee)
        extra.append(spend)
    return kit.registry_spend(reg, "forge_registry_register", kit.register_solution(pool, left, right),
                              extra_spends=extra)


def main() -> int:
    if not (kit.v11_available() and kit.registry_available()):
        print("  [skip] V11 build outputs are absent; run scripts/build-v11.py")
        return 2

    print("init:")
    reg0 = kit.make_registry(salt=0x21)
    refuses("register before init is refused",
            lambda: kit.registry_spend(reg0, "forge_registry_register",
                                       kit.register_solution(kit.make_pool([None, CATS[0]], [1, 1], leaves="forge", salt=0x50),
                                                             (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY),
                                                             (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY))))
    bundle, new_state = kit.registry_spend(reg0, "forge_registry_init", [])
    out = accepts("init creates the two sentinel slots", lambda: kit.validate(bundle))
    sentinel_min = kit.slot_value(kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY, kit.MAX_KEY)
    sentinel_max = kit.slot_value(kit.MAX_KEY, kit.ZERO_32, kit.MIN_KEY, kit.MAX_KEY)
    if out:
        conds, additions = out
        hinted = kit.additions_with_hints(conds)
        for name, value in (("min", sentinel_min), ("max", sentinel_max)):
            ph = reg0.slot_puzzle(value).get_tree_hash()
            check(f"  sentinel {name} slot at its puzzle hash, amount 0, hinted with the registry launcher",
                  any(p == ph and a == 0 and h == bytes(reg0.launcher_id) for p, a, h in hinted))
        check("  state becomes (initialized 1, pool_count 0)", [x.as_int() for x in new_state.as_iter()] == [1, 0])
        check("  successor registry singleton at the new state", (reg0.successor_puzzle_hash([1, 0]), 1) in additions)
    reg1 = reg0.advance([1, 0])
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    refuses("init a second time is refused", lambda: kit.validate(kit.registry_spend(reg1, "forge_registry_init", [])[0]))

    print("register:")
    pool_a = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50)
    key_a = kit.pool_key(pool_a.config())
    left, right = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
    bundle, new_state = registration(reg1, pool_a, left, right, slots)
    out = accepts("a real two-asset pool registers between the sentinels", lambda: kit.validate(bundle))
    if out:
        conds, additions = out
        hinted = kit.additions_with_hints(conds)
        check("  the launcher created the pool at the hash the registry recomputed", (pool_a.coin.puzzle_hash, 1) in additions)
        check("  treasury received the creation fee", (reg1.treasury_ph, reg1.creation_fee) in additions)
        for label, value in (("new", kit.slot_value(key_a, pool_a.launcher_id, kit.MIN_KEY, kit.MAX_KEY)),
                             ("left", kit.slot_value(kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY, key_a)),
                             ("right", kit.slot_value(kit.MAX_KEY, kit.ZERO_32, key_a, kit.MAX_KEY))):
            ph = reg1.slot_puzzle(value).get_tree_hash()
            check(f"  {label} slot re-created around the key, hinted", any(p == ph and a == 0 and h == bytes(reg1.launcher_id) for p, a, h in hinted))
        check("  pool_count is 1", [x.as_int() for x in new_state.as_iter()] == [1, 1])
        lp_ph = kit.construct_cat_puzzle(kit.CAT_MOD, pool_a.lp_asset_id, kit.Program.to(RECIPIENT)).get_tree_hash_precalc(RECIPIENT)
        check("  the genesis LP mint of exactly total_lp landed at the creator, hinted, authorized by the launcher",
              any(p == lp_ph and a == pool_a.state[1] and h == bytes(RECIPIENT) for p, a, h in hinted))
        print(f"          cost: {conds.cost:,}")
    reg2 = reg1.advance([1, 1])
    slots = {kit.MIN_KEY: (reg1.coin, reg1.inner_hash), key_a: (reg1.coin, reg1.inner_hash), kit.MAX_KEY: (reg1.coin, reg1.inner_hash)}

    pool_b = kit.make_pool([None, CATS[1], CATS[2]], [3_000_000, 4_000_000, 5_000_000], total_lp=3_000_000,
                           leaves="forge", weights=[4, 1, 1], salt=0x51)
    key_b = kit.pool_key(pool_b.config())
    if key_b < key_a:
        left_b, right_b = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (key_a, pool_a.launcher_id, kit.MAX_KEY)
    else:
        left_b, right_b = (key_a, pool_a.launcher_id, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
    bundle, new_state = registration(reg2, pool_b, left_b, right_b, slots)
    out = accepts("a weighted three-asset pool registers next to the first", lambda: kit.validate(bundle))
    if out:
        check("  pool_count is 2", [x.as_int() for x in new_state.as_iter()] == [1, 2])
    ten = kit.make_pool([None, *CATS[:9]], [1_000_000 + i for i in range(10)], total_lp=1_000_000, leaves="forge", salt=0x52)
    key_ten = kit.pool_key(ten.config())
    # place it against the sentinels it actually sits between (using the post-A list; B is not applied here)
    l, r = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (key_a, pool_a.launcher_id, kit.MAX_KEY)
    if key_ten > key_a:
        l, r = (key_a, pool_a.launcher_id, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
    bundle, _ = registration(reg2, ten, l, r, slots)
    out = accepts("a ten-asset pool registers (the registry rebuilds ten reserve hashes)", lambda: kit.validate(bundle))
    if out:
        print(f"          cost: {out[0].cost:,}")

    print("adversarial:")
    dup = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x53)
    refuses("the same economic config under a new launcher is refused on the left of its twin",
            lambda: kit.validate(registration(reg2, dup, (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (key_a, pool_a.launcher_id, kit.MAX_KEY), slots)[0]))
    refuses("...and on the right of it",
            lambda: kit.validate(registration(reg2, dup, (key_a, pool_a.launcher_id, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY), slots)[0]))
    other = pool_b if key_b > key_a else pool_b
    wrong_left, wrong_right = ((key_a, pool_a.launcher_id, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)) if key_b < key_a \
        else ((kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (key_a, pool_a.launcher_id, kit.MAX_KEY))
    refuses("neighbours that do not bracket the key are refused",
            lambda: kit.validate(registration(reg2, pool_b, wrong_left, wrong_right, slots)[0]))
    refuses("a fee one mojo short is refused",
            lambda: kit.validate(registration(reg2, pool_b, left_b, right_b, slots, fee=reg2.creation_fee - 1)[0]))
    refuses("no fee coin at all is refused",
            lambda: kit.validate(registration(reg2, pool_b, left_b, right_b, slots, with_fee=False)[0]))
    refuses("no launcher spend in the bundle is refused",
            lambda: kit.validate(registration(reg2, pool_b, left_b, right_b, slots, with_launcher=False)[0]))
    impostor = kit.make_pool([None, CATS[1], CATS[2]], [3_000_000, 4_000_000, 5_000_000], total_lp=3_000_000,
                             leaves="forge", weights=[4, 1, 1], fee_bps=100, salt=0x51)
    refuses("a launcher that minted a pool with a different config than registered is refused",
            lambda: kit.validate(registration(reg2, pool_b, left_b, right_b, slots, launcher_pool=impostor)[0]))
    bad_tail = kit.make_pool([None, CATS[3]], [1_000_000, 1_000_000], leaves="forge", salt=0x54)
    bad_tail.lp_tail = pool_a.lp_tail  # claims another pool's LP asset id
    refuses("a config whose LP asset id is not this launcher's TAIL is refused",
            lambda: kit.validate(registration(reg2, bad_tail, left_b, right_b, slots)[0]))
    empty = kit.make_pool([None, CATS[3]], [1_000_000, 1_000_000], leaves="forge", salt=0x55)
    empty.state = kit.forge_state([1_000_000, 0], 1_000_000)
    refuses("a genesis with an unfunded reserve is refused",
            lambda: kit.validate(registration(reg2, empty, left_b, right_b, slots)[0]))
    refuses("a genesis mint of one LP more than the launcher named is refused",
            lambda: kit.validate(registration(reg2, pool_b, left_b, right_b, slots, genesis_mint=pool_b.state[1] + 1)[0]))
    refuses("a launcher naming a genesis supply other than the eve state's total_lp cannot register",
            lambda: kit.validate(registration(reg2, pool_b, left_b, right_b, slots, kv=[pool_b.state[1] + 1],
                                              genesis_mint=pool_b.state[1] + 1)[0]))
    refuses("a genesis eve with a CAT parent is refused by the TAIL",
            lambda: kit.validate(registration(reg2, pool_b, left_b, right_b, slots, genesis_cat_parent=True)[0]))
    refuses("a slot spent by a coin that is not the registry is refused",
            lambda: kit.validate(kit.SpendBundle([
                *registration(reg2, pool_b, left_b, right_b, slots)[0].coin_spends[:1],
                kit.slot_spend(reg2, kit.slot_value(left_b[0], left_b[1], left_b[2], right_b[0]), reg1.coin, reg1.inner_hash,
                               spender_inner_hash=bytes32(b"\x42" * 32))[1],
                kit.slot_spend(reg2, kit.slot_value(right_b[0], right_b[1], left_b[0], right_b[2]), reg1.coin, reg1.inner_hash)[1],
                kit.launcher_spend(pool_b)[1], kit.fee_settlement(reg2, pool_b.launcher_id)[1],
            ], kit.G2Element())))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} registry checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
