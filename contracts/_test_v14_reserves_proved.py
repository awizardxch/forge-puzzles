#!/usr/bin/env python3
"""V14 registration proves its reserves exist (spec 1.3-1.7, fourth review R-1).

`register` is given each reserve's GRANDPARENT and derives the parent itself,
P_i = coinid(grandparent_i, RESERVE_LAUNCHER_HASH_i, reserves[i]), then asserts the coin
announcement a reserve launcher at P_i makes about the coin it created. Every case in
spec 1.7, beside the honest one:

  * a grandparent no coin answers to                          refused
  * a coin announcing the right message without creating       refused (its id is not P_i)
  * a launcher creating the wrong amount / puzzle / hint        refused
  * the V13 construction, unfunded reserves                     refused
  * funded reserves recorded under the wrong parent             refused
  * the honest registration                                     accepted; P_i IS the reserve's parent
  * and the registered pool then swaps, adds and removes        accepted

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.coin_spend import make_spend
from chia_rs import G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import _v14_testkit as kit
import forge_math

results = []
CAT = bytes32(b"\xd0" * 32)
RECIPIENT = bytes32(b"\x55" * 32)
H0 = 6_999_990
CREATE_COIN_ANN = 60


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def refuses(label, thunk, expect_code=None):
    try:
        thunk()
    except kit.Rejected as exc:
        code = str(exc).split(": ")[-1]
        ok = expect_code is None or code == str(expect_code)
        return check(label, ok, f"{str(exc)[:48]}" + ("" if ok else f"  (expected {expect_code})"))
    except Exception as exc:
        # QA-2: only a CLVM failure is the puzzle refusing locally. Anything else is the
        # probe failing to run it, and must not pass as a refusal.
        reason = kit.refusal_reason(exc)
        return check(label, reason is not None,
                     "ValueError (local run)" if reason else f"probe broke: {type(exc).__name__}: {str(exc)[:60]}")
    return check(label, False, "ACCEPTED")


def accepts(label, thunk):
    try:
        out = thunk()
    except kit.Rejected as exc:
        check(label, False, str(exc)[:90])
        return None
    check(label, True)
    return out


def fresh_registry():
    reg0 = kit.make_registry(salt=0x21)
    bundle, _ = kit.registry_spend(reg0, "forge_registry_init", [])
    kit.validate(bundle)
    return reg0.advance([1, 0]), {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}


def registration(reg, pool, slots, solution=None, launcher_spends=None):
    """The registration bundle with the launcher spends supplied by the caller."""
    left, right = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
    extra = []
    for value, key in ((kit.slot_value(left[0], left[1], left[2], right[0]), left[0]),
                       (kit.slot_value(right[0], right[1], left[0], right[2]), right[0])):
        parent, parent_inner = slots[key]
        extra.append(kit.slot_spend(reg, value, parent, parent_inner)[1])
    extra.append(kit.launcher_spend(pool)[1])
    extra.extend(kit.lp_genesis_mint_spends(pool, RECIPIENT))
    extra.append(kit.fee_settlement(reg, pool.launcher_id)[1])
    extra.extend(kit.reserve_launcher_spends(pool) if launcher_spends is None else launcher_spends)
    solution = solution if solution is not None else kit.register_solution(pool, left, right)
    return kit.registry_spend(reg, "forge_registry_register", solution, extra_spends=extra)[0]


def main() -> int:
    if not (kit.v14_available() and kit.registry_available()):
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py")
        return 2
    reg, slots = fresh_registry()
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50)
    left, right = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)

    print("the derivation:")
    derived = [kit.coin_id(r.grandparent, kit.reserve_launcher_full_hash(r.asset_id), int(r.coin.amount)) for r in pool.reserves]
    check("P_i = coinid(grandparent, launcher hash, amount) is each reserve's parent in the eve state",
          derived == list(pool.state[7]))
    check("  the CAT launcher hash is cat_puzzle_hash(asset, RESERVE_LAUNCHER_HASH); XCH's is the bare hash",
          kit.reserve_launcher_full_hash(None) == kit.RESERVE_LAUNCHER_HASH
          and kit.reserve_launcher_full_hash(CAT) != kit.RESERVE_LAUNCHER_HASH)
    check("  register's solution carries grandparents, never parents",
          kit.register_solution(pool, left, right)[5] == [r.grandparent for r in pool.reserves])

    print("honest:")
    honest = registration(reg, pool, slots)
    out = accepts("a registration whose launchers create both reserves is accepted", lambda: kit.validate(honest))
    if out:
        conds, additions = out
        hinted = kit.additions_with_hints(conds)
        for r in pool.reserves:
            check(f"  reserve {r.index} created at its full puzzle hash for {int(r.coin.amount):,}, hinted with the launcher id",
                  any(ph == r.full_hash and a == int(r.coin.amount) and h == bytes(pool.launcher_id) for ph, a, h in hinted))
        check("  the pool coin was created at the hash the registry recomputed", (pool.coin.puzzle_hash, 1) in additions)
        print(f"          cost: {conds.cost:,}")

    print("adversarial, beside the honest case:")
    wrong = [bytes32(b"\xee" * 32), bytes32(b"\xef" * 32)]
    refuses("a grandparent no coin answers to: the derived P_i announces nothing",
            lambda: kit.validate(registration(reg, pool, slots, solution=kit.register_solution(pool, left, right, grandparents=wrong))), 12)
    refuses("funded reserves, honest launchers, but the solution names another grandparent: refused",
            lambda: kit.validate(registration(reg, pool, slots,
                                              solution=kit.register_solution(pool, left, right,
                                                                             grandparents=[wrong[0], pool.reserves[1].grandparent]))), 12)
    refuses("the V13 construction -- no launcher spends, parents that name nothing -- is refused",
            lambda: kit.validate(registration(reg, pool, slots, launcher_spends=[])), 12)

    # An imposter: a coin parented by the same grandparent that makes the same announcement but
    # creates nothing. Its puzzle differs, so its id differs, so its announcement is not P_i's.
    r0 = pool.reserves[0]
    msg = kit.reserve_launcher_message(r0.inner_hash, int(r0.coin.amount), pool.launcher_id)
    imposter_puzzle = kit.Program.to((1, [[CREATE_COIN_ANN, msg]]))
    imposter = kit.Coin(r0.grandparent, bytes32(imposter_puzzle.get_tree_hash()), uint64(int(r0.coin.amount)))
    check("an imposter announcing the right message has a different id from P_0", imposter.name() != r0.coin.parent_coin_info)
    refuses("  so announcing without creating the reserve is refused",
            lambda: kit.validate(registration(reg, pool, slots, launcher_spends=[
                make_spend(imposter, imposter_puzzle, kit.Program.to([])),
                *kit.reserve_launcher_spends(pool, indices=[1])])), 12)
    refuses("a launcher creating one mojo less than the registered reserve is refused",
            lambda: kit.validate(registration(reg, pool, slots, launcher_spends=kit.reserve_launcher_spends(pool, amounts={0: int(r0.coin.amount) - 1}))))
    refuses("a launcher creating the reserve at another puzzle hash is refused",
            lambda: kit.validate(registration(reg, pool, slots, launcher_spends=kit.reserve_launcher_spends(pool, created={1: bytes32(b"\x99" * 32)}))), 12)
    refuses("a launcher hinting and announcing another launcher id is refused",
            lambda: kit.validate(registration(reg, pool, slots, launcher_spends=kit.reserve_launcher_spends(pool, launcher_id=bytes32(b"\x98" * 32)))), 12)
    refuses("only one of two reserves launched is refused",
            lambda: kit.validate(registration(reg, pool, slots, launcher_spends=kit.reserve_launcher_spends(pool, indices=[0]))), 12)
    short = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=0x50)
    refuses("a solution with one grandparent for two reserves is refused",
            lambda: kit.validate(registration(reg, short, slots, solution=kit.register_solution(short, left, right, grandparents=[short.reserves[0].grandparent]))))

    print("the registered pool is a working pool (its reserves are the launchers' children):")
    r, w = pool.state[0], pool.weights
    gross = 250_000
    honest_out = forge_math.swap_output(r[0], r[1], gross, pool.fee_bps, w[0], w[1])
    s1, s1_spend = kit.offer_settlement_xch(gross, salt=0xC0)
    bundle, st = kit.spend_action(pool, "forge_action_swap", [H0, 0, 1, gross, honest_out, *kit.settlement_ref(s1)], extra_spends=[s1_spend])
    out = accepts("swap through the real leaf, the finalizer messaging the launched reserves", lambda: kit.validate(bundle))
    if out:
        after = pool.advance(kit.state_to_list(st))
        check("  the successor's reserve parents are the reserves this spend consumed",
              list(after.state[7]) == [rr.coin.name() for rr in pool.reserves])
        deposits = [1_000_000, 2_000_000]
        minted = forge_math.invariant_lp_mint(after.state[0], deposits, after.state[1], after.fee_bps, after.weights, version=10)
        sx, sx_spend = kit.offer_settlement_xch(deposits[0], salt=0xC1)
        sa, sa_cat = kit.offer_settlement_cat(CAT, deposits[1], salt=0xC2)
        probe, _, _, _ = kit.run_leaf(after, "forge_action_add", [H0 + 1, deposits, minted, bytes32(b"\x01" * 32), *kit.settlement_refs([sx, sa])])
        lp_parent, lp_spends = kit.lp_mint_spends(after, minted, after.state[1] + minted, probe.get_tree_hash(), RECIPIENT, salt=0xC3)
        b2, st2 = kit.spend_action(after, "forge_action_add", [H0 + 1, deposits, minted, lp_parent, *kit.settlement_refs([sx, sa])],
                                   extra_spends=[sx_spend, *lp_spends], extra_cats={CAT: [sa_cat]})
        accepts("  add on the successor mints LP", lambda: kit.validate(b2))
        after2 = after.advance(kit.state_to_list(st2))
        burn = 100_000
        vf = forge_math.vault_fee_bps(len(after2.state[0]), 10, after2.fee_bps)
        payouts = forge_math.withdrawal_amounts(after2.state[0], burn, after2.state[1], vf)
        probe, _, _, _ = kit.run_leaf(after2, "forge_action_remove", [H0 + 2, burn, bytes32(b"\x01" * 32), payouts])
        lp_parent, lp_spends = kit.lp_melt_spend(after2, burn, after2.state[1] - burn, probe.get_tree_hash(), salt=0xC4)
        b3, _ = kit.spend_action(after2, "forge_action_remove", [H0 + 2, burn, lp_parent, payouts], extra_spends=lp_spends)
        accepts("  remove on the successor pays every reserve pro rata", lambda: kit.validate(b3))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} reserves-proved checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
