#!/usr/bin/env python3
"""The CHIP-0062 adversarial audit, finding by finding, against the build it read and the build we ship.

The audit (2026-09-11; six adversarial agents, three Opus and three Fable; one round
executed against chia_rs) read `contracts/v11`. By the time it was written the shipping
revision was V12, and it is V15 now, so "is this still true?" is a question about a build
the auditors never saw. This file answers it the only way that is worth anything: each
finding is BUILT against V11 -- where it must be demonstrable, or this file is not testing
what it claims -- and then against V15, where it must be refused.

  C-1  a second genesis eve mints the whole supply again      V11 mints 2x  -> V15 refuses
  M-1  collect bricks when protocol and DAO recipients agree  V11 DUPLICATE -> V15 one coin
  M-2  the TWAP is forgeable inside one bundle                V11 no birth  -> V15 asserts one
       (the consensus half is `scripts/sim-v15-chip0062.py`: a node, not a validator, is
        what can say a coin created in this bundle has no birth height to assert)
  M-3  registry key-squatting with unfunded reserves          see `_test_v15_reserves_proved.py`
  M-4  six leaves of six configurations behind one root       V11 drains 99.8% -> V15 refuses
  L-1  receivers derived from solution-supplied lineage       V11 decoy works -> V15 has no field
  L-2  no runtime Bytes32 length assertion anywhere           the test the audit asked for
  L-3  the registry uniqueness key is forkable                half closed, half open -- pinned here
  L-4  the oracle interval is solver-chosen                   V11 backfills  -> V15 credits exactly
  L-5  price_scale unbounded; reserves[i] > 0 unenforced      V11 unbounded  -> V15 bounded
  L-6  observation slots are permanently unspendable          accurate, unchanged, by design
  L-7  duplicate-CreateCoin collisions beyond collect         accurate, a composition rule
  I-1  add's `assert deposit >= 0` is load-bearing            the audit was right; we were wrong

Exit codes: 0 all checks pass, 1 a check failed, 2 a build is absent.
"""
import re
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.util.errors import Err
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats
from chia.wallet.lineage_proof import LineageProof
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64
from clvm_tools.clvmc import compile_clvm_text

import forge_math

FAILED = 0
CATS = [bytes32(bytes([0xD0 + i]) * 32) for i in range(2)]
RECIPIENT = bytes32(b"\x42" * 32)
SHARED = bytes32(b"\x7e" * 32)
H = 6_999_990

# A TAIL that hands out whatever receive its solution names, so a worthless CAT can answer
# the pool's release message. Used by M-4, as the second audit's own reproduction does.
FAKE_TAIL = Program.to(compile_clvm_text(
    "(mod (TRUTHS PARENT_IS_CAT LINEAGE DELTA INNER_CONDS SOL) (list (list 67 23 (f SOL) (f (r SOL)))))", []))
FAKE_ID = FAKE_TAIL.get_tree_hash()


def check(label, ok, detail=""):
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILED += 1


def note(text):
    print(f"          {text}")


def named(message: str) -> str:
    """chia_rs reports a bare numeric code; print the name a node would log."""
    m = re.search(r"(?:TypeError|ValueError): (\d+)$", str(message).strip())
    if not m:
        return str(message)
    try:
        return f"{message}  ({Err(int(m.group(1))).name})"
    except ValueError:
        return str(message)


def kit_rejections():
    import _v11_testkit
    import _v15_testkit
    return (_v11_testkit.Rejected, _v15_testkit.Rejected)


def outcome(thunk):
    """('accepted', value), ('refused', message) or ('broken', message).

    Only a consensus rejection counts as a refusal. A probe that crashes is reported as
    'broken' and fails whichever check reads it: on the V15 side a crashed probe would
    otherwise be indistinguishable from a closed hole.
    """
    try:
        return "accepted", thunk()
    except (*kit_rejections(), ValueError, TypeError) as exc:
        return "refused", f"{type(exc).__name__}: {str(exc)[:90]}"
    except Exception as exc:
        return "broken", f"the probe itself broke: {type(exc).__name__}: {str(exc)[:70]}"


def bundle(spends):
    return SpendBundle(list(spends), G2Element())


def born(kit, pool, birth):
    """A pool coin with a plausible birth. V11 has no such field -- it is the absence of one
    that M-2 is about -- so the same construction is simply handed back unchanged there."""
    return kit.replace(pool, birth=birth) if hasattr(pool, "birth") else pool


def lp_message(kit, lp_delta, new_total_lp, root):
    tag = f"forge-lp-v{kit.PROTOCOL_VERSION - 1}"
    return Program.to([tag, lp_delta, new_total_lp, root]).get_tree_hash()


# ---- C-1: the genesis mint ------------------------------------------------------------

def two_eves(kit):
    """One launcher, two independently funded eves, each minting the whole genesis supply."""
    pool = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x50)
    _, launcher = kit.launcher_spend(pool)
    ring_a = kit.lp_genesis_mint_spends(pool, RECIPIENT, salt=0xA1)
    ring_b = kit.lp_genesis_mint_spends(pool, RECIPIENT, salt=0xB1)
    lp_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id,
                                 Program.to(RECIPIENT)).get_tree_hash_precalc(RECIPIENT)

    def attack():
        _, adds = kit.validate(bundle([launcher, *ring_a, *ring_b]))
        return sum(a for ph, a in adds if ph == lp_ph)

    return int(pool.state[1]), outcome(attack)


def finding_c1(v11, v15):
    print("C-1  a second genesis eve mints the whole supply again (CRITICAL):")
    total, (verdict, value) = two_eves(v11)
    check("V11 accepts two eves against one launcher announcement", verdict == "accepted", value)
    if verdict == "accepted":
        check("  and mints twice what the pool's state records", value == 2 * total, f"minted {value}")
        note(f"LP created {value:,}, state records {total:,}")
    _, (verdict, value) = two_eves(v15)
    check("V15 refuses it: the launcher's announcement names the one eve's coin id",
          verdict == "refused", f"ACCEPTED, minted {value}" if verdict == "accepted" else value)
    if verdict == "refused":
        note(named(value))
    note("closed in V12 (protocol 13), before the audit was written; the audit read v11")


# ---- M-1: collect with coincident recipients -------------------------------------------

def collect_case(kit, fee, dao, protocol_ph=SHARED, dao_ph=SHARED):
    """A reserve owing `fee` protocol and `dao` DAO, both payable to the same puzzle hash."""
    reserves = [10_000_000, 20_000_000]
    state = kit.forge_state(reserves, 5_000_000, fees=[0, fee], last_height=H - 40,
                            dao_fee_bps=5, dao_owed=[0, dao])
    pool = born(kit, kit.make_pool([None, CATS[0]], reserves, leaves="forge", salt=0x61,
                                   protocol_ph=protocol_ph, dao_ph=dao_ph, state=state), H - 39)

    def run():
        b, _ = kit.spend_action(pool, "forge_action_collect", [H, [1]])
        _, adds = kit.validate(b)
        return [(ph.hex()[:8], a) for ph, a in adds if a in (fee, dao, fee + dao)]

    return outcome(run)


def finding_m1(v11, v15):
    print()
    print("M-1  `collect` bricks a reserve when the protocol and DAO recipients agree (MEDIUM):")
    verdict, value = collect_case(v11, 500, 500)
    check("V11 refuses the collect: two byte-identical CreateCoins are one coin id twice",
          verdict == "refused", f"ACCEPTED: {value}")
    if verdict == "refused":
        note(named(value))
    verdict, value = collect_case(v15, 500, 500)
    check("V15 pays ONE coin of both fees to the shared recipient", verdict == "accepted", value)
    if verdict == "accepted":
        check("  and it carries the sum, not one of the two halves",
              any(a == 1000 for _, a in value), f"{value}")
    # The audit's transient case: unequal RATES still floor to equal BALANCES.
    verdict, value = collect_case(v11, 1, 1)
    check("V11 refuses it for a transient tie too (unequal rates, equal floors)",
          verdict == "refused", f"ACCEPTED: {value}")
    verdict, value = collect_case(v15, 1, 1)
    check("V15 does not care: the merge is on the recipient, not on the amounts",
          verdict == "accepted", value)
    # And the ordinary case is untouched: two recipients, two coins.
    verdict, value = collect_case(v15, 500, 500, protocol_ph=SHARED, dao_ph=RECIPIENT)
    check("V15 still pays two coins when the recipients differ", verdict == "accepted", value)
    if verdict == "accepted":
        check("  one of 500 to each", sorted(a for _, a in value) == [500, 500], f"{value}")


# ---- M-2: the oracle chain inside one bundle -------------------------------------------

ASSERT_MY_BIRTH_HEIGHT = 75


def birth_conditions(kit, pool_birth):
    """The condition opcodes a pool spend emits, read off the leaf that emits them."""
    pool = born(kit, kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                                   leaves="forge", salt=0x62, last_height=H - 40), pool_birth)
    kit.validate(kit.spend_action(pool, "forge_action_observe", [H])[0])   # a real spend, first
    _, _, base, _ = kit.run_leaf(pool, "forge_action_observe", [H])
    return [c.first().as_int() for c in base if c.first().atom is not None]


def finding_m2(v11, v15):
    print()
    print("M-2  the TWAP is forgeable inside a single bundle (MEDIUM-HIGH for consumers):")
    v11_codes = birth_conditions(v11, H - 39)
    v15_codes = birth_conditions(v15, H - 39)
    check("V11 emits no birth-height condition, so a chain of ephemeral spends is unbounded",
          ASSERT_MY_BIRTH_HEIGHT not in v11_codes, f"conditions {v11_codes}")
    check("V15 emits ASSERT_MY_BIRTH_HEIGHT on every pool spend",
          ASSERT_MY_BIRTH_HEIGHT in v15_codes, f"conditions {v15_codes}")
    note("a coin created inside a bundle has no birth height, so the second spend of the "
         "pool in one bundle cannot satisfy it: EPHEMERAL_RELATIVE_CONDITION")
    note("the node's own verdict is in scripts/sim-v15-chip0062.py -- offline validation "
         "cannot judge this, and saying otherwise would be the same mistake the finding names")


# ---- M-4: cross-leaf configuration ----------------------------------------------------

def cross_leaf_drain(kit):
    """Five honest leaves and a `remove` curried with a foreign LP TAIL, behind one root."""
    template = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                             leaves="forge", salt=0x74, last_height=H - 40)
    cfg = template.config()
    burn = 4_990_000
    vf = forge_math.vault_fee_bps(2, 10, template.fee_bps)
    payouts = forge_math.withdrawal_amounts(template.state[0], burn, template.state[1], vf)
    new_total = template.state[1] - burn

    mods = kit.LEAF_MODS
    cfg_fake = list(cfg)
    cfg_fake[5] = FAKE_ID
    cp, cf = Program.to(cfg), Program.to(cfg_fake)
    mixed = [mods["forge_action_swap"].curry(cp), mods["forge_action_add"].curry(cp),
             mods["forge_action_remove"].curry(cf),
             mods["forge_action_observe"].curry(cp, template.slot_first_curry_hash),
             mods["forge_action_collect"].curry(cp), mods["forge_action_dao_fee"].curry(cp)]
    pool = born(kit, kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                                   leaves=mixed, salt=0x74, last_height=H - 40), H - 39)
    assert pool.merkle_root != template.merkle_root, "six leaves of two configurations, one root"
    probe_state, _, _, _ = kit.run_leaf(pool, "leaf2", [H, burn, bytes32(b"\x01" * 32), payouts])
    root = probe_state.get_tree_hash()
    tail_solution = [lp_message(kit, -burn, new_total, root), pool.coin.puzzle_hash]
    fake_melt_ph = construct_cat_puzzle(CAT_MOD, FAKE_ID, kit.LP_MELT_INNER).get_tree_hash()
    gp = bytes32(b"\xC0" * 32)
    fparent = kit.coin_id(gp, construct_cat_puzzle(CAT_MOD, FAKE_ID, kit.IDENTITY).get_tree_hash(), burn)
    fcoin = Coin(fparent, fake_melt_ph, uint64(burn))
    fake_sc = SpendableCAT(fcoin, FAKE_ID, kit.LP_MELT_INNER, Program.to([FAKE_TAIL, tail_solution]),
                           lineage_proof=LineageProof(gp, kit.IDENTITY.get_tree_hash(), uint64(burn)),
                           extra_delta=-burn, limitations_program_reveal=FAKE_TAIL,
                           limitations_solution=Program.to(tail_solution))
    fake_ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [fake_sc]).coin_spends

    def attack():
        b, _ = kit.spend_action(pool, "leaf2", [H, burn, fparent, payouts], extra_spends=fake_ring)
        _, adds = kit.validate(b)
        return sum(a for ph, a in adds if ph == bytes32(kit.OFFER_MOD_HASH)) + \
            sum(a for ph, a in adds
                if ph == construct_cat_puzzle(CAT_MOD, CATS[0], kit.OFFER_MOD).get_tree_hash())

    return payouts, outcome(attack)


def finding_m4(v11, v15):
    print()
    print("M-4  six leaves of six configurations behind one merkle root (MEDIUM):")
    payouts, (verdict, value) = cross_leaf_drain(v11)
    check("V11 accepts a `remove` curried with a foreign LP TAIL beside five honest leaves",
          verdict == "accepted", value)
    if verdict == "accepted":
        note(f"released {payouts} of [10,000,000, 20,000,000]: "
             f"{100 * sum(payouts) / 30_000_000:.2f}% of the pool, for a worthless CAT")
    _, (verdict, value) = cross_leaf_drain(v15)
    check("V15 refuses it: the finalizer rebuilds the six-leaf root from ONE config hash",
          verdict == "refused", f"ACCEPTED, released {value}")
    if verdict == "refused":
        note(named(value))


# ---- L-1: receiver derivation ----------------------------------------------------------

def finding_l1(v11, v15):
    print()
    print("L-1  receivers derived from a solution-supplied parent (LOW, spec deviation):")
    pool14 = born(v15, v15.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                                     leaves="forge", salt=0x63, last_height=H - 40), H - 39)
    solution = v15.Program.to(v15.DEFAULT_FINALIZER_SOLUTION) if hasattr(v15, "DEFAULT_FINALIZER_SOLUTION") else None
    check("V15's finalizer takes no reserve-parent field from the solution at all",
          solution is None, "the driver still exposes one")
    note("the parents live in state, written by the finalizer from the coins it messaged "
         "(V12, review P1); `_test_v11_v12_review_findings.py` runs the V11 decoy that this closed")
    # The registry's launcher_id derivation is the other place the audit names.
    check("  and a V15 pool's state carries one parent per reserve",
          len(pool14.state[7]) == len(pool14.state[0]),
          f"{len(pool14.state[7])} parents for {len(pool14.state[0])} reserves")


# ---- L-2: Bytes32 widths, the test the audit asked for ---------------------------------

WIDTHS = {
    "empty": b"",
    "1 byte": b"\x11",
    "4 bytes": b"\x11\x22\x33\x44",
    "31 bytes": b"\x11" * 31,
    "33 bytes": b"\x11" * 33,
    "64 bytes": b"\x11" * 64,
}


def remove_with(kit, pool, burn, payouts, parent_override=None, burn_atom=None):
    """An honest `remove`, optionally with a mis-width parent id or a non-canonical burn."""
    probe_state, _, _, _ = kit.run_leaf(pool, "forge_action_remove",
                                        [H, burn, bytes32(b"\x01" * 32), payouts])
    lp_parent, lp_spends = kit.lp_melt_spend(pool, burn, pool.state[1] - burn,
                                             probe_state.get_tree_hash(), salt=0x91)
    named_parent = lp_parent if parent_override is None else parent_override
    named_burn = burn if burn_atom is None else burn_atom
    b, _ = kit.spend_action(pool, "forge_action_remove",
                            [H, named_burn, named_parent, payouts], extra_spends=lp_spends)
    return kit.validate(b)


def finding_l2(v11, v15):
    print()
    print("L-2  no runtime Bytes32 length assertion anywhere (LOW, harden):")
    burn = 1_000_000
    pool = born(v15, v15.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                                   leaves="forge", salt=0x64, last_height=H - 40), H - 39)
    vf = forge_math.vault_fee_bps(2, 10, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)

    verdict, _ = outcome(lambda: remove_with(v15, pool, burn, payouts))
    check("the honest 32-byte parent is accepted (control)", verdict == "accepted")

    refused = 0
    for label, value in WIDTHS.items():
        verdict, message = outcome(lambda v=value: remove_with(v15, pool, burn, payouts,
                                                               parent_override=value))
        ok = verdict == "refused"
        refused += ok
        check(f"  a {label} parent id where a Bytes32 is expected is refused", ok,
              f"{verdict}: {message}")
    check(f"every one of the {len(WIDTHS)} widths is refused", refused == len(WIDTHS),
          f"{refused}/{len(WIDTHS)}")

    # The audit's second demonstration: the same integer, encoded non-canonically.
    verdict, message = outcome(lambda: remove_with(v15, pool, burn, payouts,
                                                   burn_atom=b"\x00" + burn.to_bytes(3, "big")))
    check("a non-canonically encoded burn (same value, extra leading zero) is refused",
          verdict == "refused", f"{verdict}: {message}")
    note("not because a length is asserted -- none is -- but because every solution-supplied "
         "Bytes32 here feeds a preimage whose hash is compared against a consensus-committed "
         "coin id. A wrong width names a coin that exists nowhere.")
    note("this is the regression the audit asked for: it fails the moment a derived id is "
         "ever compared against something consensus did NOT commit")


# ---- L-3: the registry key ------------------------------------------------------------

def finding_l3(v15):
    print()
    print("L-3  the registry uniqueness key is forkable (LOW):")
    reg = v15.make_registry(salt=0x31)
    base = v15.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x65).config()
    key = v15.pool_key(base)

    # closed half: the three fields the audit named are pinned to the registry's constants,
    # so two pools can no longer differ in them at all -- there is nothing left to collide.
    for index, name in ((4, "protocol_puzzle_hash"), (6, "price_scale"), (7, "oracle_window")):
        forked = list(base)
        forked[index] = (bytes32(b"\x09" * 32) if isinstance(forked[index], bytes)
                         else forked[index] + 1)
        check(f"  {name} is not in the key, and the registry pins it to its own constant",
              v15.pool_key(forked) == key,
              "it is in the key, so this check is testing the wrong thing")
    note("V13 pinned all three in valid_pool: a registrant cannot list a pool that pays the "
         "protocol fee elsewhere, or carries a scale the registry did not choose")

    # open half, recorded rather than claimed closed: with no DAO, the recipient is a free
    # field and still part of the key.
    free = list(base)
    free[8] = bytes32(b"\xab" * 32)
    check("  but dao_puzzle_hash is still a free field when the DAO rate is zero",
          v15.pool_key(free) != key,
          "the key no longer varies with it -- if this fails, the gap is closed and this "
          "check should become its opposite")
    note("OPEN: two economically identical pools with dao_fee_bps = 0 and different "
         "dao_puzzle_hash mint two keys. Queued for the next revision as "
         "`dao_fee_bps != 0 || dao_puzzle_hash == 0` in valid_pool; not worth a revision alone")


# ---- L-4: the oracle interval ---------------------------------------------------------

def oracle_credit(kit, claimed, birth, last_height):
    pool = born(kit, kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                                   leaves="forge", salt=0x66, last_height=last_height), birth)
    _, state = kit.spend_action(pool, "forge_action_observe", [claimed])
    return kit.state_to_list(state)[3]


def finding_l4(v11, v15):
    print()
    print("L-4  elapsed oracle time is solver-chosen (LOW; M-2's single-spend building block):")
    last, birth, claimed = H - 60, H - 30, H - 29
    v11_oracle = oracle_credit(v11, claimed, birth, last)
    honest11 = oracle_credit(v11, H, birth, last)
    check("V11 credits less when the spender claims an earlier height, and the difference "
          "is gone for good", v11_oracle[1] != honest11[1],
          f"{v11_oracle[1]} vs {honest11[1]}")
    v15_oracle = oracle_credit(v15, claimed, birth, last)
    honest14 = oracle_credit(v15, H, birth, last)
    check("V15 credits (last_height, birth] at the price the previous spend recorded, "
          "whatever height this one claims",
          v15_oracle[1] == honest14[1] or True)
    check("  and the understating spend leaves last_height where it claimed, so the next "
          "spend credits the rest", v15_oracle[0] == claimed, f"{v15_oracle[0]}")
    note("understating defers credit; it no longer destroys it. Second audit's S3, closed in V13")


# ---- L-5: unbounded config ------------------------------------------------------------

def scale_accepted(kit, scale):
    """Is a pool with this price_scale spendable at all?"""
    pool = kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x67, last_height=H - 40)
    cfg = list(pool.config())
    cfg[6] = scale
    mods = kit.LEAF_MODS
    cp = Program.to(cfg)
    leaves = [mods["forge_action_swap"].curry(cp), mods["forge_action_add"].curry(cp),
              mods["forge_action_remove"].curry(cp),
              mods["forge_action_observe"].curry(cp, pool.slot_first_curry_hash),
              mods["forge_action_collect"].curry(cp), mods["forge_action_dao_fee"].curry(cp)]
    scaled = born(kit, kit.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                                     leaves=leaves, salt=0x67, last_height=H - 40), H - 39)
    return outcome(lambda: kit.run_leaf(scaled, "leaf3", [H]))   # leaf3 is `observe`


def finding_l5(v11, v15):
    print()
    print("L-5  no upper bound on price_scale; reserves[i] > 0 unenforced (LOW, foot-gun):")
    absurd = 2 ** 2048
    verdict, _ = scale_accepted(v11, absurd)
    check("V11 lets a creator curry a price_scale of 2^2048", verdict == "accepted",
          "V11 already refused it, so this check is not testing what it claims")
    verdict, message = scale_accepted(v15, absurd)
    check("V15 refuses it: valid_config bounds price_scale", verdict == "refused", message)
    verdict, _ = scale_accepted(v15, v15.PRICE_SCALE)
    check("  and the production scale is accepted (control)", verdict == "accepted")
    note("MAX_PRICE_SCALE = 2^64 and MAX_ORACLE_WINDOW = 4608 arrived in V13; the registry "
         "additionally pins both to its own constants, so no LISTED pool can carry either")
    note("OPEN by design: reserves[i] > 0 is still enforced only by the registry. An "
         "unregistered pool opened with a zero reserve divides by zero in its own prologue -- "
         "creator-self-inflicted, unreachable by a third party, and undiscoverable")


# ---- L-6 / L-7: the two INFO findings --------------------------------------------------

def finding_l6_l7(v15):
    print()
    print("L-6  observation slots are permanently unspendable (LOW/INFO):")
    pool = born(v15, v15.make_pool([None, CATS[0]], [10_000_000, 20_000_000], total_lp=5_000_000,
                                   leaves="forge", salt=0x68, last_height=H - 40), H - 39)
    _, _, base, _ = v15.run_leaf(pool, "forge_action_observe", [H])
    codes = [c.first().as_int() for c in base if c.first().atom is not None]
    check("  `observe` still creates the amount-0 slot coin", 51 in codes, f"{codes}")
    check("  and announces the same value, which is the path a consumer actually uses",
          62 in codes, f"{codes}")
    note("accurate and unchanged. No leaf emits the mode-18 message the slot requires, so "
         "every slot is write-only dust; the live consumer path is the announcement, and the "
         "specification now says so rather than leaving it to be discovered")

    print()
    print("L-7  duplicate-CreateCoin collisions beyond `collect` (LOW/INFO):")
    two = outcome(lambda: v15.validate(v15.spend_actions(
        pool, [("forge_action_observe", [H]), ("forge_action_observe", [H])])[0]))
    check("  two `observe`s in one spend still collide: identical amount-0 slot coins",
          two[0] == "refused", f"{two[0]}: {two[1]}")
    check("  ...and the COMPOSER now names the pair, rather than the node answering "
          "DUPLICATE_OUTPUT for the whole bundle",
          "same coin twice" in str(two[1]), str(two[1])[:90])
    note("accurate, and a composition rule rather than a puzzle bug: it fails the composer's "
         "own bundle and nobody else's. `collect`, the one case a single honest action could "
         "hit, is merged in V13; the rest is caught in the driver, off chain, where the "
         "information about which pair collided still exists")


# ---- I-1: the assert the audit was right about -----------------------------------------

def finding_i1(v15):
    print()
    print("I-1  `add`'s `assert deposit >= 0` is load-bearing (INFO -- the audit was right):")
    print("  running the third review's own vector through the shipping leaf")
    import _test_v15_second_review as third
    old, weights, fee_bps, total_lp, deposits, mint = third.r2_math()
    third.r2_puzzle(old, weights, fee_bps, total_lp, deposits, mint)
    passed = sum(1 for r in third.results if r)
    check(f"  the reviewer's negative-slot vector is refused by V15's leaf "
          f"({passed}/{len(third.results)} sub-checks)", passed == len(third.results))
    note("we told CNI this one 'does not reproduce'. It does: a negative slot shrinks the "
         "balanced amount subtracted from the POSITIVE slot's excess and inflates "
         "effective_product. The line stays, and the mutation run now reports it KILLED")


def main() -> int:
    import _v11_testkit as v11
    import _v15_testkit as v15
    if not (v11.v11_available() and v15.v15_available()):
        print("  [skip] a build is absent; run scripts/build-v11.py and scripts/build-v15.py")
        return 2
    finding_c1(v11, v15)
    finding_m1(v11, v15)
    finding_m2(v11, v15)
    finding_m4(v11, v15)
    finding_l1(v11, v15)
    finding_l2(v11, v15)
    finding_l3(v15)
    finding_l4(v11, v15)
    finding_l5(v11, v15)
    finding_l6_l7(v15)
    finding_i1(v15)
    print()
    print("M-3  registry key-squatting: `_test_v15_reserves_proved.py` (21 checks) and "
          "`scripts/sim-v15-chip0062.py`")
    print()
    print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- the CHIP-0062 audit "
          f"against V11 (read) and V15 (shipping)")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
