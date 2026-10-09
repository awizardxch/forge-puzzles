#!/usr/bin/env python3
"""Keyless pool creation (phase 8.1) in the simulator.

A creator's coins are `(1)` puzzles nobody signs: an XCH coin and one CAT coin per
CAT asset. `forge_v16_create.plan` splits the creation bundle into the creator's
spends (which bind the genesis LP payment to the creator's address) and the
router's spends (launcher, eve mint to OFFER_MOD, LP payment, fee settlement,
slots, registry). The bundle must validate through consensus, register the pool
between the right slots, mint exactly total_lp to the creator, pay the treasury,
and refuse: a router that pays the genesis LP to itself, an under-funded XCH coin,
a duplicate configuration, a wrong bracket. Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import hashlib
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key
from chia_rs import AugSchemeMPL, Coin, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import forge_v16_create as create
import forge_v16_driver as drv
import forge_v16_offer as v16
from forge_offer import ZERO_32

IDENTITY = Program.to(1)
# the creator's coins sit behind the standard p2 puzzle (any key: signatures are not checked here),
# because the library builds the delegated-puzzle solution form a real wallet signs
P2 = puzzle_for_synthetic_public_key(AugSchemeMPL.key_gen(b"" * 32).get_g1())   # a real point: the infinity key is an invalid condition
CREATOR_PH = bytes32(b"\x77" * 32)
ROUTER_PH = bytes32(b"\x66" * 32)
T_A = bytes32(b"\xa1" * 32)
T_B = bytes32(b"\xb2" * 32)
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def run_spend(cs) -> list:
    """A spend's conditions, as it runs on chain; only the opcode-led ones."""
    out = Program.from_bytes(bytes(cs.puzzle_reveal)).run(Program.from_bytes(bytes(cs.solution)))
    return [c for c in out.as_iter() if c.first().atom is not None]


def creator_xch(amount: int, salt: int) -> create.CreatorXch:
    return create.CreatorXch(Coin(bytes32(bytes([salt]) * 32), P2.get_tree_hash(), uint64(amount)), P2)


def creator_cat(asset: bytes32, amount: int, salt: int) -> create.CreatorCat:
    outer = construct_cat_puzzle(CAT_MOD, asset, P2).get_tree_hash()
    grand = bytes32(bytes([salt]) * 32)
    coin = Coin(drv.coin_id(grand, outer, amount), outer, uint64(amount))
    return create.CreatorCat(asset, coin, P2, LineageProof(grand, P2.get_tree_hash(), uint64(amount)))


def fresh_registry():
    """An initialized registry with its two sentinel slots, as the registry suite builds it."""
    reg0 = drv.make_registry(salt=0x21, creation_fee=1_000_000, treasury_ph=bytes32(b"\x55" * 32))
    _bundle, new_state = drv.registry_spend(reg0, "forge_registry_init", [])
    reg1 = reg0.advance([x.as_int() for x in new_state.as_iter()])
    parent = {"parent_coin_info": reg0.coin.parent_coin_info.hex(), "puzzle_hash": reg0.coin.puzzle_hash.hex(), "amount": 1}
    slots = {
        drv.MIN_KEY.hex(): {"key": drv.MIN_KEY.hex(), "launcher_id": ZERO_32.hex(), "left": drv.MIN_KEY.hex(), "right": drv.MAX_KEY.hex(),
                            "parent": parent, "parent_inner_hash": reg0.inner_hash.hex()},
        drv.MAX_KEY.hex(): {"key": drv.MAX_KEY.hex(), "launcher_id": ZERO_32.hex(), "left": drv.MIN_KEY.hex(), "right": drv.MAX_KEY.hex(),
                            "parent": parent, "parent_inner_hash": reg0.inner_hash.hex()},
    }
    return reg1, slots


def paid_to(bundle, ph: bytes32, cat_asset=None) -> int:
    target = ph if cat_asset is None else construct_cat_puzzle(CAT_MOD, cat_asset, Program.to(ph)).get_tree_hash_precalc(ph)
    total = 0
    for cs in bundle.coin_spends:
        puzzle = Program.from_bytes(bytes(cs.puzzle_reveal))
        for c in puzzle.run(Program.from_bytes(bytes(cs.solution))).as_iter():
            if c.first().atom is not None and c.first().as_int() == 51 and bytes32(c.rest().first().as_atom()) == target:
                total += c.rest().rest().first().as_int()
    return total


def main() -> int:
    if not (drv.v16_available() and drv.registry_available()):
        print("  [skip] V16 build outputs are absent"); return 2
    reg, slots = fresh_registry()
    fee = 5_000_000

    print("a TXCH/A pool from a creator's coins")
    cfg = create.CreationConfig([None, T_A], [100_000_000, 50_000], [1, 1], 30, 5, bytes32(b"\x55" * 32), 50_000, "TXCH 🍕", "TXCH/A")
    xch = creator_xch(2 + 100_000_000 + 1_000_000 + 49_999 + fee + 777, 0x31)
    cat = creator_cat(T_A, 60_000, 0x32)
    p = create.plan(reg, slots, cfg, xch, {T_A: cat}, CREATOR_PH, fee)
    check("  the launcher's parent is the creator's XCH coin", p.pool.launcher_parent == xch.coin.name())
    check("  the plan knows the LP asset id before anything is signed", len(p.details["lp_asset_id"]) == 64)
    bundle = create.finalize(p, G2Element())
    conds, additions = drv.validate(bundle)
    check("  the bundle validates through consensus", True, f"cost {conds.cost:,}")
    check("  the pool's eve singleton is created at the registry's recomputed hash", (p.pool.coin.puzzle_hash, 1) in additions)
    # V16: the settlement burns the locked floor and pays the creator the rest
    check("  the genesis LP lands at the creator, exactly total_lp - LOCKED_BURN", paid_to(bundle, CREATOR_PH, p.pool.lp_asset_id) == 50_000 - drv.LOCKED_BURN)
    check("  and LOCKED_BURN is burned to Chia's burn address 0x...dead",
          paid_to(bundle, drv.BURN_PUZZLE_HASH, p.pool.lp_asset_id) == drv.LOCKED_BURN)
    check("  and nothing of it reaches the all-zero hash", paid_to(bundle, drv.ZERO_32, p.pool.lp_asset_id) == 0)
    check("  the plan reports both", p.details["burned_lp"] == drv.LOCKED_BURN and p.details["creator_lp"] == 50_000 - drv.LOCKED_BURN)
    check("  the treasury receives the creation fee", (reg.treasury_ph, 1_000_000) in additions)
    check("  the creator's change comes back", paid_to(bundle, P2.get_tree_hash()) == 777)
    check("  the CAT change comes back to the creator", paid_to(bundle, P2.get_tree_hash(), T_A) == 10_000)
    check("  the new slot sits between the sentinels",
          any(reg.slot_puzzle(drv.slot_value(p.key, p.pool.launcher_id, drv.MIN_KEY, drv.MAX_KEY)).get_tree_hash() == ph for ph, a in additions))
    check("  the launcher creation carries name and symbol memos",
          any(c.first().as_int() == 51 and len(list(c.as_iter())) > 3 and b"TXCH/A" in bytes(c.rest().rest().rest().first().rest().first().as_atom())
              for cs in bundle.coin_spends if cs.coin == xch.coin
              for c in Program.from_bytes(bytes(cs.puzzle_reveal)).run(Program.from_bytes(bytes(cs.solution))).as_iter()
              if c.first().atom is not None))
    snap = v16.pool_to_snapshot(p.pool)
    check("  the successor pool snapshot rebuilds", v16.snapshot_to_pool(snap).coin == p.pool.coin)
    check("  plan_json carries the spends to sign and the pool", len(create.plan_json(p)["creator_spends"]) == 2)

    print("the creator's bind")
    # a hostile router rewrites the LP payment to itself: the creator's assertion fails
    hostile = []
    for cs in p.router_spends:
        sol = bytes(cs.solution)
        if bytes(CREATOR_PH) in sol and cs.coin.puzzle_hash == construct_cat_puzzle(CAT_MOD, p.pool.lp_asset_id, v16.OFFER_MOD).get_tree_hash():
            hostile.append(drv.make_spend(cs.coin, Program.from_bytes(bytes(cs.puzzle_reveal)),
                                          Program.from_bytes(sol.replace(bytes(CREATOR_PH), bytes(ROUTER_PH)))))
        else:
            hostile.append(cs)
    try:
        drv.validate(drv.SpendBundle([*p.creator_spends, *hostile], G2Element()))
        check("  a router paying the genesis LP to itself is refused", False)
    except drv.Rejected as exc:
        check("  a router paying the genesis LP to itself is refused", True, str(exc)[:60])

    # F1 (2026-10-07 review, confirmed on the simulator): the registry's `register` and slot
    # spends hold no key, and until now the creator's spends asserted only the LP payment. A
    # farmer could drop those three spends, point each reserve launcher and the fee settlement
    # at itself, and reuse the creator's signed spends byte for byte: the creator paid for a
    # pool whose reserves were never created. The creator's XCH spend now asserts an
    # announcement of every binding router spend, so the bundle is whole or refused.
    print("the creator's spends require the whole bundle (F1)")
    H = lambda b: hashlib.sha256(b).digest()  # noqa: E731
    xch_spend = next(cs for cs in p.creator_spends if cs.coin == xch.coin)
    asserted = {bytes(c.rest().first().as_atom()) for c in run_spend(xch_spend) if c.first().as_int() in (61, 63)}
    reg_spend = next(cs for cs in p.router_spends if cs.coin == reg.coin)
    registered = [H(bytes(reg.coin.puzzle_hash) + bytes(c.rest().first().as_atom())) for c in run_spend(reg_spend) if c.first().as_int() == 62]
    check("  the creator asserts the registry's forge-registered-v16 announcement", len(registered) == 1 and registered[0] in asserted)
    check("  and the singleton launcher's", H(bytes(p.pool.launcher_id) + bytes(
        Program.to([p.pool.coin.puzzle_hash, 1, [50_000, p.pool.extra["eve_coin_id"]]]).get_tree_hash())) in asserted)
    check("  and every reserve launcher's", len(p.pool.reserves) == 2 and all(
        H(bytes(Coin(r.grandparent, drv.reserve_launcher_full_hash(r.asset_id), uint64(int(r.coin.amount))).name())
          + drv.reserve_launcher_message(r.inner_hash, int(r.coin.amount), p.pool.launcher_id)) in asserted
        for r in p.pool.reserves))
    check("  and the fee settlement's", H(bytes(create.OFFER_PH) + bytes(
        Program.to([p.pool.launcher_id, [reg.treasury_ph, 1_000_000, [reg.treasury_ph]]]).get_tree_hash())) in asserted)

    def farmer(drop_registry: bool, redirect: bool) -> list:
        """What a farmer can do to the router's spends without touching the creator's."""
        slot_parent = Coin(bytes32.fromhex(slots[drv.MIN_KEY.hex()]["parent"]["parent_coin_info"]),
                           bytes32.fromhex(slots[drv.MIN_KEY.hex()]["parent"]["puzzle_hash"]), uint64(1)).name()
        launcher_hashes = {drv.reserve_launcher_full_hash(a) for a in cfg.asset_ids}
        out = []
        for cs in p.router_spends:
            if drop_registry and (cs.coin == reg.coin or cs.coin.parent_coin_info == slot_parent):
                continue
            sol = bytes(cs.solution)
            if redirect and cs.coin.puzzle_hash in launcher_hashes:
                for r in p.pool.reserves:
                    sol = sol.replace(bytes(r.inner_hash), bytes(ROUTER_PH))
            elif redirect and cs.coin.puzzle_hash == create.OFFER_PH and cs.coin.parent_coin_info == xch.coin.name():
                sol = sol.replace(bytes(reg.treasury_ph), bytes(ROUTER_PH))
            out.append(drv.make_spend(cs.coin, Program.from_bytes(bytes(cs.puzzle_reveal)), Program.from_bytes(sol)))
        return out

    for label, spends in (("dropping the registry spends and redirecting the reserves and the fee", farmer(True, True)),
                          ("dropping only the registry spends", farmer(True, False)),
                          ("redirecting the reserves with the registry spends kept", farmer(False, True))):
        try:
            drv.validate(drv.SpendBundle([*p.creator_spends, *spends], G2Element()))
            check(f"  {label} is refused", False)
        except drv.Rejected as exc:
            check(f"  {label} is refused", True, str(exc)[:60])

    print("refusals")
    for label, fn in (
        ("an under-funded XCH coin", lambda: create.plan(reg, slots, cfg, creator_xch(1_000, 0x33), {T_A: cat}, CREATOR_PH, fee)),
        ("a missing CAT coin", lambda: create.plan(reg, slots, cfg, xch, {}, CREATOR_PH, fee)),
        ("a CAT coin smaller than the reserve", lambda: create.plan(reg, slots, cfg, xch, {T_A: creator_cat(T_A, 10, 0x34)}, CREATOR_PH, fee)),
    ):
        try:
            fn(); check(f"  {label} is refused", False)
        except v16.OfferRejected as exc:
            check(f"  {label} is refused", True, str(exc)[:70])

    print("a second pool, and a duplicate")
    slots2 = dict(slots)
    parent2 = {"parent_coin_info": reg.coin.parent_coin_info.hex(), "puzzle_hash": reg.coin.puzzle_hash.hex(), "amount": 1}
    slots2[p.key.hex()] = {"key": p.key.hex(), "launcher_id": p.pool.launcher_id.hex(), "left": drv.MIN_KEY.hex(), "right": drv.MAX_KEY.hex(),
                           "parent": parent2, "parent_inner_hash": reg.inner_hash.hex()}
    slots2[drv.MIN_KEY.hex()] = {**slots2[drv.MIN_KEY.hex()], "right": p.key.hex(), "parent": parent2, "parent_inner_hash": reg.inner_hash.hex()}
    slots2[drv.MAX_KEY.hex()] = {**slots2[drv.MAX_KEY.hex()], "left": p.key.hex(), "parent": parent2, "parent_inner_hash": reg.inner_hash.hex()}
    reg2 = p.registry_after
    cfg2 = create.CreationConfig([T_A, T_B], [30_000, 40_000], [1, 1], 30, 5, bytes32(b"\x55" * 32), 30_000, "🍕🍀", "A/B")
    p2 = create.plan(reg2, slots2, cfg2, creator_xch(2 + 1_000_000 + 29_999 + fee, 0x35),
                     {T_A: creator_cat(T_A, 30_000, 0x36), T_B: creator_cat(T_B, 40_000, 0x37)}, CREATOR_PH, fee)
    bundle2 = create.finalize(p2, G2Element())
    check("  an all-CAT pool registers next to the first", True)
    check("  its genesis LP lands at the creator, less the burned floor", paid_to(bundle2, CREATOR_PH, p2.pool.lp_asset_id) == 30_000 - drv.LOCKED_BURN)
    try:
        create.plan(reg2, slots2, cfg, creator_xch(2 + 100_000_000 + 1_000_000 + 49_999 + fee, 0x38), {T_A: creator_cat(T_A, 50_000, 0x39)}, CREATOR_PH, fee)
        check("  the same configuration again is refused", False)
    except v16.OfferRejected as exc:
        check("  the same configuration again is refused", "already registered" in str(exc))

    print("a one-asset pool at a 10:1 LP ratio")
    cfg3 = create.CreationConfig([T_B], [10_000], [1], 30, 5, bytes32(b"\x55" * 32), 100_000, "🍀", "B")
    p3 = create.plan(reg2, slots2, cfg3, creator_xch(2 + 1_000_000 + 99_999 + fee, 0x3A), {T_B: creator_cat(T_B, 10_000, 0x3B)}, CREATOR_PH, fee)
    bundle3 = create.finalize(p3, G2Element())
    check("  mints 100,000 LP against 10,000 of the asset, 99,000 to the creator", paid_to(bundle3, CREATOR_PH, p3.pool.lp_asset_id) == 100_000 - drv.LOCKED_BURN)

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} creation checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
