#!/usr/bin/env python3
"""Every message V12 sends or receives, and what binds each end of it.

A message is the weakest joint in a puzzle set: it is the one place where a coin
acts on something another coin said. Mode 23 (`SENDER_PUZZLE | RECEIVER_COIN`)
commits both ends, but only to values the *puzzle computes* -- so the question
for each site is where those values come from. Curried constants and CAT or
action-layer truths are facts. Solution data is an attacker's choice, and is only
safe when something else in the bundle forces it to be the honest value.

This file walks the joints the other suites do not: who may SEND the pool's LP
message, what the LP asset id commits to, and what a settlement announcement
does and does not prove. The receiver end of the LP message is
`_test_v12_lp_receive_forgery.py`; the reserve messages are
`_test_v12_finalizer.py`; the DAO message is `_test_v12_dao_fee.py`; the registry
and genesis announcements are `_test_v12_registry.py` and `_test_v12_genesis.py`.

The honest result, stated up front because one case here is subtle: the TAIL
derives its expected sender as `singleton(launcher_id, claimed_inner_hash)` with
`launcher_id` CURRIED into the TAIL (so it is part of the LP asset id) and the
inner hash taken from the solution. Naming the real pool's inner makes an
impostor's own puzzle hash mismatch, so the message is not received. Naming its
OWN inner makes the derivation point at the impostor -- and then the only thing
left standing between that and a mint from nothing is the singleton puzzle's
lineage assert, which requires the impostor's parent to be the launcher or
another coin in the same singleton line. That is upstream's guarantee, curried
with our launcher id, and it is checked here.

Exit codes: 0 all checks pass, 1 a check failed, 2 a build is absent.
"""
import sys

sys.path.insert(0, ".")

from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.util.errors import Err
from chia.wallet.cat_wallet.cat_utils import (CAT_MOD, SpendableCAT, construct_cat_puzzle,
                                              unsigned_spend_bundle_for_spendable_cats)
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.singleton_top_layer_v1_1 import puzzle_for_singleton, solution_for_singleton
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import _v12_testkit as kit
import forge_math

FAILED = 0
SEND_MESSAGE, CREATE_COIN, MODE_23 = 66, 51, 0b010111
CAT = bytes32(bytes([0xD0]) * 32)
H = 6_999_990
BURN = 100_000
ROOT = bytes32(b"\x11" * 32)      # the TAIL never inspects the root; only the message must match


def check(label, ok, detail=""):
    global FAILED
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILED += 1


def code_of(exc) -> str:
    text = str(exc).strip()
    digits = text.rsplit(": ", 1)[-1]
    try:
        return Err(int(digits)).name
    except ValueError:
        return text[:60]


def refuses(label, thunk, expect: str = ""):
    """Only a consensus refusal counts. A Python error in the test is a FAILURE:
    a broken probe that raises AttributeError must never read as a closed hole."""
    try:
        thunk()
    except (kit.Rejected, ValueError, TypeError) as exc:
        name = code_of(exc)
        ok = (not expect) or name == expect
        check(f"{label}: {name}", ok, f"expected {expect}")
        return
    except Exception as exc:
        check(label, False, f"the probe itself broke: {type(exc).__name__}: {exc}")
        return
    check(label, False, "ACCEPTED")


def lp_message(delta, new_total, root):
    return Program.to(["forge-lp-v12", delta, new_total, root]).get_tree_hash()


def melt_ring(pool, claimed_inner_hash, salt):
    """A real LP melt at the pinned melt inner, whose TAIL is told which pool inner to expect."""
    new_total = pool.state[1] - BURN
    action = [0 - BURN, new_total, ROOT, claimed_inner_hash, bytes32(b"\x00" * 32)]
    melt_ph = construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, kit.LP_MELT_INNER).get_tree_hash()
    gp = bytes32(bytes([salt]) * 32)
    parent = kit.coin_id(gp, construct_cat_puzzle(CAT_MOD, pool.lp_asset_id, kit.IDENTITY).get_tree_hash(), BURN)
    melt = Coin(parent, melt_ph, uint64(BURN))
    spendable = SpendableCAT(melt, pool.lp_asset_id, kit.LP_MELT_INNER, Program.to([pool.lp_tail, action]),
                             lineage_proof=LineageProof(gp, kit.IDENTITY.get_tree_hash(), uint64(BURN)),
                             extra_delta=0 - BURN, limitations_program_reveal=pool.lp_tail,
                             limitations_solution=Program.to(action))
    ring = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, [spendable]).coin_spends
    return melt, ring, lp_message(0 - BURN, new_total, ROOT)


def impostor_spend(pool, inner, parent_id, lineage, receiver, message):
    """A coin at singleton(pool's launcher_id, `inner`), recreating its line and sending."""
    full = puzzle_for_singleton(pool.launcher_id, inner)
    conditions = Program.to([[CREATE_COIN, full.get_tree_hash(), 1],
                             [SEND_MESSAGE, MODE_23, message, receiver]])
    coin = Coin(parent_id, full.get_tree_hash(), uint64(1))
    return coin, make_spend(coin, full, solution_for_singleton(lineage, uint64(1), conditions))


def main() -> int:
    if not kit.v12_available():
        print("  [skip] the V12 build is absent; run scripts/build-v12.py")
        return 2

    pool = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x90, last_height=H - 40)
    pool = kit.replace(pool, birth=H - 40)
    real_inner = pool.inner.get_tree_hash()
    attacker_inner = kit.IDENTITY
    attacker_inner_hash = attacker_inner.get_tree_hash()

    print("who may SEND the pool's LP message:")
    melt, ring, message = melt_ring(pool, real_inner, salt=0x91)

    plain = Coin(bytes32(b"\xaa" * 32), attacker_inner_hash, uint64(1))
    plain_spend = make_spend(plain, attacker_inner,
                             Program.to([[SEND_MESSAGE, MODE_23, message, melt.name()]]))
    refuses("a coin that is not a singleton at all cannot send it",
            lambda: kit.validate(SpendBundle([plain_spend, *ring], G2Element())),
            Err.MESSAGE_NOT_SENT_OR_RECEIVED.name)

    _, impostor = impostor_spend(pool, attacker_inner, pool.launcher_id,
                                 LineageProof(pool.launcher_parent, None, uint64(1)),
                                 melt.name(), message)
    refuses("an impostor singleton that names the REAL pool inner is not the sender it derived",
            lambda: kit.validate(SpendBundle([impostor, *ring], G2Element())),
            Err.MESSAGE_NOT_SENT_OR_RECEIVED.name)

    # Now the sharp case: the impostor names its OWN inner, so the TAIL's derivation
    # points at the impostor. Everything now rests on the singleton's lineage assert.
    melt2, ring2, message2 = melt_ring(pool, attacker_inner_hash, salt=0x92)
    _, stray = impostor_spend(pool, attacker_inner, bytes32(b"\xbb" * 32),
                              LineageProof(bytes32(b"\xcc" * 32), attacker_inner_hash, uint64(1)),
                              melt2.name(), message2)
    refuses("an impostor naming its own inner, parented by an unrelated coin, is refused "
            "by the singleton's lineage assert",
            lambda: kit.validate(SpendBundle([stray, *ring2], G2Element())),
            Err.ASSERT_MY_PARENT_ID_FAILED.name)

    print("  what the lineage assert leaves, and why the chain closes it:")
    # With a lineage proof consistent with its actual parent, the impostor validates
    # OFFLINE -- the validator does not ask whether that parent ever created such a coin.
    # There are exactly two parents it can claim, and neither can exist.
    accepted_offline = []
    for label, parent_id, lineage in (
            ("the launcher coin", pool.launcher_id, LineageProof(pool.launcher_parent, None, uint64(1))),
            ("the real pool coin", pool.coin.name(),
             LineageProof(pool.coin.parent_coin_info, real_inner, uint64(1)))):
        _, spend = impostor_spend(pool, attacker_inner, parent_id, lineage, melt2.name(), message2)
        try:
            kit.validate(SpendBundle([spend, *ring2], G2Element()))
            accepted_offline.append(label)
        except Exception:
            pass
    check("offline, the only impostors that validate are those claiming the launcher or the pool coin "
          "as parent", len(accepted_offline) == 2, f"{accepted_offline}")

    # The launcher creates exactly one coin, and it is the real eve.
    _, launcher = kit.launcher_spend(pool)
    conds, launcher_adds = kit.validate(SpendBundle([launcher], G2Element()))
    check("  the launcher creates exactly one coin", len(launcher_adds) == 1, f"{launcher_adds}")
    check("  ...and it is the real pool eve, not any impostor puzzle hash",
          launcher_adds[0][0] == pool.coin.puzzle_hash,
          f"{launcher_adds[0][0].hex()[:16]} vs {pool.coin.puzzle_hash.hex()[:16]}")

    # A pool spend creates exactly one odd coin, and it is the successor the finalizer derived.
    honest, new_state = kit.spend_action(pool, "forge_action_observe", [H])
    _, additions = kit.validate(honest)
    odd = [(ph, amount) for ph, amount in additions if amount % 2 == 1]
    successor_ph = pool.successor_puzzle_hash(kit.state_to_list(new_state))
    check("  a pool spend creates exactly one odd-amount coin", len(odd) == 1, f"{odd}")
    check("  ...and it sits at the successor puzzle hash the finalizer derived",
          bool(odd) and odd[0][0] == successor_ph,
          f"{odd[0][0].hex()[:16] if odd else None} vs {successor_ph.hex()[:16]}")
    check("  ...so no coin at an impostor inner is ever born into this singleton line",
          bool(odd) and odd[0][0] != puzzle_for_singleton(pool.launcher_id, attacker_inner).get_tree_hash())

    print("what the LP asset id commits to:")
    other_launcher = kit.LP_TAIL_MOD.curry(bytes32(b"\x77" * 32), kit.PROTOCOL_VERSION).get_tree_hash()
    other_version = kit.LP_TAIL_MOD.curry(pool.launcher_id, kit.PROTOCOL_VERSION - 1).get_tree_hash()
    check("the launcher id is curried into the TAIL, so another launcher is another asset",
          other_launcher != pool.lp_asset_id)
    check("the protocol version is curried too, so protocol 12 LP is a different asset entirely",
          other_version != pool.lp_asset_id)
    check("  and the pool's config names that exact asset id, committed in its puzzle hash",
          pool.config()[5] == pool.lp_asset_id, f"{pool.config()[5]}")

    print("what a settlement announcement proves:")
    # The swap leaf asserts sha256(settlement_puzzle_hash(asset) + tree_hash((claimed_id, nil))).
    # The nonce in a notarized payment list is the offer's own solution data, so this binds
    # "some settlement coin of this asset was spent here", not "this exact coin was".
    # What forces the trader's mojos in is the reserve's recreated amount plus bundle
    # conservation, and that is what the missing-settlement case below actually catches.
    reserves, weights = pool.state[0], pool.weights
    honest_out = forge_math.swap_output(reserves[0], reserves[1], 250_000, pool.fee_bps,
                                        weights[0], weights[1])
    check("the curve gives a single honest output for this input", honest_out > 0, f"{honest_out}")

    # Two failures, two different mechanisms, and the pair is the whole point. Drop the
    # settlement coin and the bundle has no mojos to grow the reserve with: conservation
    # catches it before any announcement is even considered. Keep the coin but lie about
    # its id and the announcement assert is what fires. So the announcement binds WHICH
    # coin was spent; the reserve's recreated amount plus conservation bind HOW MUCH.
    refuses("a swap with no settlement coin at all: caught by conservation, not the announcement",
            lambda: kit.validate(kit.spend_action(
                pool, "forge_action_swap",
                [H, 0, 1, 250_000, honest_out, bytes32(b"\x33" * 32)])[0]),
            "MINTING_COIN")

    coin, settle_spend = kit.offer_settlement_xch(250_000, salt=0x95)
    refuses("a swap that funds the reserve but names the wrong settlement id: the announcement fires",
            lambda: kit.validate(kit.spend_action(
                pool, "forge_action_swap",
                [H, 0, 1, 250_000, honest_out, bytes32(b"\x33" * 32)],
                extra_spends=[settle_spend])[0]),
            Err.ASSERT_ANNOUNCE_CONSUMED_FAILED.name)
    check("  the honest id is the settlement coin's own name", coin.name() != bytes32(b"\x33" * 32))

    print()
    print(f"{'ALL PASSED' if not FAILED else f'{FAILED} FAILED'} -- V12 message-binding checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
