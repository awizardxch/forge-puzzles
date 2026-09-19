#!/usr/bin/env python3
"""Every solution-supplied Bytes32 in V14, fed values that are not 32 bytes.

rue emits no runtime `strlen`, so a `Bytes32` in a leaf's signature is a compile-time tag
and nothing more: a 0-, 4-, 31- or 64-byte atom arrives, is accepted, and produces a
different downstream hash. The CHIP-0062 audit (L-2) confirmed this against the compiler
source and asked for the one thing the suites lacked -- a test positioned to catch it.

What keeps the design safe is not a length check but where each value goes. Every one of
these fields feeds a sha256 preimage whose result is compared against something consensus
committed: a coin id the bundle must actually spend, or an announcement a coin must
actually make. A wrong width therefore derives an identifier that exists nowhere, and the
spend fails on message pairing or on the announcement, never on the width itself.

That property is what this file pins. It does NOT assert a length; it asserts the refusal,
and it will start failing the day a derived hash is compared against something consensus
did not commit -- which is the refactor the audit warned about, and the one thing a suite
built on canonical inputs could never notice.

Four fields across three puzzles, each with its honest control beside it:

  remove   lp_parent_id            -> lp_action_coin_id, paired by mode-23 message
  swap     settlement parent       -> settlement_assert, an announcement a real coin makes
  LP TAIL  pool_inner_puzzle_hash  -> the sender the TAIL rebuilds for its ReceiveMessage
  LP TAIL  next_state_root         -> inside lp_message, the message the pool sends

plus `burn` encoded non-canonically (0x0003e8 for 0x03e8): the same integer, a different
atom, and `(amount as Bytes)` keeps the atom -- so the derived id differs and the spend is
refused, exactly as the audit demonstrated on the V11 remove leaf.

Exit 0 all pass, 1 a failure, 2 nothing exercised (build outputs absent).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH
from chia_rs import Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import _v14_testkit as kit
import forge_math

FAILED = 0
CAT = bytes32(b"\xd0" * 32)
H = 6_999_990

WIDTHS = {
    "empty": b"",
    "1 byte": b"\x11",
    "4 bytes": b"\x11\x22\x33\x44",
    "31 bytes": b"\x11" * 31,
    "33 bytes": b"\x11" * 33,
    "64 bytes": b"\x11" * 64,
    # Width-correct and degenerate: the value the leaves' `!= zero_bytes32()` asserts exist
    # for. Offline this assert is the ONLY refusal -- the validator cannot know a zero-parent
    # coin is impossible, and a mutant with the line deleted ACCEPTS a bundle that fabricates
    # one (audit run 2026-09-19, T-5). So the case lives here, where the mutation run sees
    # it, and the assert reads KILLED rather than argued.
    "32 zero bytes": bytes(32),
}


def check(label, ok, detail=""):
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILED += 1


def outcome(thunk):
    """('accepted', value) or ('refused', message). A crash in the probe is 'broken',
    and fails whichever check reads it, so a harness bug cannot pass as a refusal."""
    try:
        return "accepted", thunk()
    except Exception as exc:  # noqa: BLE001
        reason = kit.refusal_reason(exc)
        if reason is not None:
            return "refused", reason
        # QA-2: a TypeError or AttributeError is not a refusal, it is the harness failing
        # to reach the puzzle. It used to count as refused; a fault-injected probe passed.
        return "broken", f"the probe itself broke: {type(exc).__name__}: {str(exc)[:70]}"


def pool_at(salt: int):
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=salt, last_height=H - 40)
    return kit.replace(pool, birth=H - 39)


def sweep(label, build, honest_first=True):
    """Run `build(value)` for the honest 32-byte value and for every width; require the
    honest one to be ACCEPTED and every other one REFUSED."""
    verdict, msg = outcome(lambda: build(None))
    check(f"{label}: the honest 32-byte value is accepted (control)", verdict == "accepted", msg)
    refused = 0
    for name, value in WIDTHS.items():
        verdict, msg = outcome(lambda v=value: build(v))
        ok = verdict == "refused"
        refused += ok
        check(f"  {name}: refused", ok, f"{verdict}: {msg}")
    check(f"  every one of the {len(WIDTHS)} widths is refused", refused == len(WIDTHS),
          f"{refused}/{len(WIDTHS)}")


# ---- remove: lp_parent_id, and a non-canonical burn ----------------------------------

def remove_lane():
    print("remove leaf, lp_parent_id:")
    pool = pool_at(0x64)
    burn = 1_000_000
    vf = forge_math.vault_fee_bps(2, 10, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)

    def build(parent_override, burn_atom=None):
        probe_state, _, _, _ = kit.run_leaf(pool, "forge_action_remove",
                                            [H, burn, bytes32(b"\x01" * 32), payouts])
        lp_parent, lp_spends = kit.lp_melt_spend(pool, burn, pool.state[1] - burn,
                                                 probe_state.get_tree_hash(), salt=0x91)
        named_parent = lp_parent if parent_override is None else parent_override
        named_burn = burn if burn_atom is None else burn_atom
        b, _ = kit.spend_action(pool, "forge_action_remove",
                                [H, named_burn, named_parent, payouts], extra_spends=lp_spends)
        return kit.validate(b)

    sweep("lp_parent_id", build)
    verdict, msg = outcome(lambda: build(None, burn_atom=b"\x00" + burn.to_bytes(3, "big")))
    check("  a non-canonically encoded burn (same integer, a leading zero byte) is refused",
          verdict == "refused", f"{verdict}: {msg}")


# ---- swap: the settlement's parent ------------------------------------------------------

def swap_lane():
    print()
    print("swap leaf, settlement parent:")
    pool = pool_at(0x65)
    r, w = pool.state[0], pool.weights
    gross = 250_000
    out = forge_math.swap_output(r[0], r[1], gross, pool.fee_bps, w[0], w[1])

    def build(override):
        coin, spend = kit.offer_settlement_xch(gross, salt=0xD4)
        ref = list(kit.settlement_ref(coin))
        if override is not None:
            ref[0] = override
        b, _ = kit.spend_action(pool, "forge_action_swap", [H, 0, 1, gross, out, *ref],
                                extra_spends=[spend])
        return kit.validate(b)

    sweep("settlement parent", build)


# ---- the LP TAIL: pool_inner_puzzle_hash and next_state_root --------------------------

def tail_lane():
    print()
    print("LP TAIL, pool_inner_puzzle_hash and next_state_root:")
    pool = pool_at(0x66)
    burn = 1_000_000
    vf = forge_math.vault_fee_bps(2, 10, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)
    new_total = pool.state[1] - burn

    def build_with(index, override):
        probe_state, _, _, _ = kit.run_leaf(pool, "forge_action_remove",
                                            [H, burn, bytes32(b"\x01" * 32), payouts])
        root = probe_state.get_tree_hash()
        # forge_v14_driver.lp_melt_spend's default action, with one field substituted:
        # [expected_delta, new_total_lp, next_state_root, pool_inner_puzzle_hash, genesis]
        action = [-burn, new_total, root, pool.inner_hash, kit.ZERO_32]
        if override is not None:
            action[index] = override
        lp_parent, lp_spends = kit.lp_melt_spend(pool, burn, new_total, root, salt=0x92,
                                                 lp_action=action)
        b, _ = kit.spend_action(pool, "forge_action_remove",
                                [H, burn, lp_parent, payouts], extra_spends=lp_spends)
        return kit.validate(b)

    sweep("pool_inner_puzzle_hash", lambda v: build_with(3, v))
    print()
    sweep("next_state_root", lambda v: build_with(2, v))


# ---- the registry: launcher parent and reserve grandparent -----------------------------

def registry_lane():
    """`register` derives the launcher id from a solution-supplied parent and each reserve
    parent from a solution-supplied grandparent. A zero for either derives a coin that cannot
    exist on chain -- and offline, nothing but the assert says so."""
    print()
    print("register leaf, launcher_parent_id and reserve grandparent:")
    import _test_v14_registry as regsuite
    reg0 = kit.make_registry(salt=0x21)
    kit.validate(kit.registry_spend(reg0, "forge_registry_init", [])[0])
    reg1 = reg0.advance([1, 0])
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    left = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY)
    right = (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)

    def register(launcher_parent=None, grandparents=None):
        kw = {} if launcher_parent is None else {"launcher_parent": launcher_parent}
        pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                             leaves="forge", salt=0x50, **kw)
        if grandparents is None:
            return kit.validate(regsuite.registration(reg1, pool, left, right, slots)[0])
        solution = kit.register_solution(pool, left, right, grandparents=grandparents)
        return kit.validate(kit.registry_spend(reg1, "forge_registry_register", solution)[0])

    verdict, msg = outcome(lambda: register())
    check("the honest registration is accepted (control)", verdict == "accepted", msg)
    verdict, msg = outcome(lambda: register(launcher_parent=kit.ZERO_32))
    check("  a launcher whose parent is the zero hash is refused", verdict == "refused", f"{verdict}: {msg}")
    verdict, msg = outcome(lambda: register(grandparents=[kit.ZERO_32, kit.ZERO_32]))
    check("  a reserve grandparent of the zero hash is refused", verdict == "refused", f"{verdict}: {msg}")


# ---- zero parents, made CONSISTENT ----------------------------------------------------
#
# The sweeps above vary the solution against an honest coin, which is the right probe for
# width: a wrong value names a coin that is not there. It is the WRONG probe for the four
# `!= zero_bytes32()` asserts, because a zero in the solution beside a real coin is refused
# by message pairing or an announcement whether the assert exists or not -- and the
# mutation run then reports the line UNREACHED (audit run 2026-09-19, T-5, first attempt).
# What reaches those lines is a bundle in which EVERYTHING agrees on the zero parent: the
# coin is fabricated with parent 0x00..00 and the solution names it. Offline, nothing knows
# such a coin cannot exist, so a mutant with the assert deleted ACCEPTS the bundle and the
# assert is shown to be the only offline refusal. On chain the coin store refuses it; the
# assert and the coin store are two lanes, not two layers.

def zero_parent_lane():
    print()
    print("zero parents, with the coin fabricated to match the solution:")
    import _test_v14_registry as regsuite
    reg0 = kit.make_registry(salt=0x21)
    kit.validate(kit.registry_spend(reg0, "forge_registry_init", [])[0])
    reg1 = reg0.advance([1, 0])
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    left = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY)
    right = (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)

    # register: the XCH reserve's launcher parented by the zero hash. The template pool
    # supplies the reserve puzzle hashes the rebuilt one must sit at.
    template = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                             leaves="forge", salt=0x50)
    r0, r1 = template.reserves
    parent0 = kit.coin_id(kit.ZERO_32, kit.RESERVE_LAUNCHER_HASH, int(r0.coin.amount))
    zero_reserve = (Coin(parent0, r0.full_hash, r0.coin.amount), None, kit.ZERO_32)
    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x50,
                         reserve_coins=[zero_reserve, (r1.coin, r1.lineage, r1.grandparent, r1.launcher_lineage)])
    verdict, msg = outcome(lambda: kit.validate(regsuite.registration(reg1, pool, left, right, slots)[0]))
    check("register: a reserve launched from a zero grandparent, launcher and solution agreeing, is refused",
          verdict == "refused", f"{verdict}: {msg}")

    # swap: a settlement coin whose parent IS the zero hash, spent in the bundle.
    spool = pool_at(0x65)
    r, w = spool.state[0], spool.weights
    gross = 250_000
    out = forge_math.swap_output(r[0], r[1], gross, spool.fee_bps, w[0], w[1])
    coin = Coin(kit.ZERO_32, bytes32(OFFER_MOD_HASH), uint64(gross))
    spend = make_spend(coin, OFFER_MOD, Program.to([[coin.name()]]))
    ref = kit.settlement_ref(coin)
    check("  (the settlement reference the driver derives really names the zero parent)",
          bytes(ref[0]) == bytes(kit.ZERO_32), f"{bytes(ref[0]).hex()[:16]}")
    verdict, msg = outcome(lambda: kit.validate(kit.spend_action(
        spool, "forge_action_swap", [H, 0, 1, gross, out, *ref], extra_spends=[spend])[0]))
    check("swap: a settlement coin fabricated with a zero parent, spent in the bundle, is refused",
          verdict == "refused", f"{verdict}: {msg}")

    # remove: the one that CANNOT be made consistent. A melt coin with a zero parent has no
    # CAT lineage (its parent id would need a sha256 preimage), so the only route is a melt
    # with no CAT parent -- which the TAIL's `parent_is_cat || expected_delta > 0` refuses on
    # its own. That, not coin existence, is what covers lp_parent_id != zero offline.
    mpool = pool_at(0x66)
    burn = 1_000_000
    vf = forge_math.vault_fee_bps(2, 10, mpool.fee_bps)
    payouts = forge_math.withdrawal_amounts(mpool.state[0], burn, mpool.state[1], vf)
    probe_state, _, _, _ = kit.run_leaf(mpool, "forge_action_remove", [H, burn, bytes32(b"" * 32), payouts])
    root = probe_state.get_tree_hash()
    melt_ph = construct_cat_puzzle(CAT_MOD, mpool.lp_asset_id, kit.LP_MELT_INNER).get_tree_hash()
    melt = Coin(kit.ZERO_32, melt_ph, uint64(burn))
    action = [-burn, mpool.state[1] - burn, root, mpool.inner_hash, kit.ZERO_32]
    spendable = SpendableCAT(melt, mpool.lp_asset_id, kit.LP_MELT_INNER, Program.to([mpool.lp_tail, action]),
                             lineage_proof=LineageProof(), extra_delta=-burn,
                             limitations_program_reveal=mpool.lp_tail, limitations_solution=Program.to(action))

    def melt_route():
        ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends
        b, _ = kit.spend_action(mpool, "forge_action_remove", [H, burn, kit.ZERO_32, payouts], extra_spends=ring)
        return kit.validate(b)

    verdict, msg = outcome(melt_route)
    check("remove: a melt coin fabricated with a zero parent has no CAT lineage and the TAIL refuses the melt",
          verdict == "refused", f"{verdict}: {msg}")


def helper_lane():
    """QA-2's fault injection, kept as a regression: a probe that never reaches a puzzle
    must not read as a refusal."""
    print()
    print("the helpers themselves, fault-injected:")

    def raise_(exc):
        raise exc

    verdict, _ = outcome(lambda: raise_(AttributeError("synthetic harness defect; no puzzle executed")))
    check("an AttributeError from the probe is BROKEN, not refused", verdict == "broken", verdict)
    verdict, _ = outcome(lambda: raise_(TypeError("synthetic: wrong argument to a driver call")))
    check("a TypeError from the probe is BROKEN, not refused", verdict == "broken", verdict)
    verdict, _ = outcome(lambda: raise_(ValueError("synthetic Python ValueError, not a CLVM one")))
    check("a Python ValueError that is not a CLVM failure is BROKEN, not refused", verdict == "broken", verdict)
    verdict, _ = outcome(lambda: raise_(kit.Rejected("TypeError: 12")))
    check("a consensus rejection is refused", verdict == "refused", verdict)
    verdict, _ = outcome(lambda: raise_(ValueError(("clvm raise", "80"))))
    check("a CLVM raise is refused", verdict == "refused", verdict)


def main() -> int:
    if not kit.v14_available():
        print("  [skip] V14 build outputs are absent; run scripts/build-v14.py")
        return 2
    remove_lane()
    swap_lane()
    tail_lane()
    registry_lane()
    zero_parent_lane()
    helper_lane()
    print()
    print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- V14 solution-width checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
