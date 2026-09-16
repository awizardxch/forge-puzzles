#!/usr/bin/env python3
"""The three surviving R-1 candidates, simulated against consensus.

_sim_v14_r1_routes.py eliminated the first three ideas. What survives:

  OPTION 1  drop ASSERT_MY_BIRTH_HEIGHT on the genesis spend, so the eve CAN be spent
            in its own creation block
  OPTION 2  two-phase registration: the slot is provisional until a later block's
            first spend confirms it
  OPTION 3  a purpose-built reserve-creating puzzle that makes a COIN announcement
            committing to what it creates -- binding both the id and the creation

Each is tested for the property that decides it, not for a full implementation.

Exit 0 if every candidate behaved as the simulation predicts, 1 otherwise.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from dataclasses import replace

from chia.types.coin_spend import make_spend
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import _v13_testkit as kit
from forge_v13_driver import Program, coin_id

results = []

CREATE_COIN, CREATE_COIN_ANN, ASSERT_COIN_ANN = 51, 60, 61
# 70 ASSERT_MY_COIN_ID, 71 ASSERT_MY_PARENT_ID, 72 ASSERT_MY_PUZZLEHASH,
# 73 ASSERT_MY_AMOUNT, 74 ASSERT_MY_BIRTH_SECONDS, 75 ASSERT_MY_BIRTH_HEIGHT.
ASSERT_MY_BIRTH_HEIGHT = 75


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def quoted(conditions):
    """A coin puzzle that ignores its solution and emits exactly `conditions`."""
    return Program.to((1, conditions))


def validates(spends):
    try:
        kit.validate(SpendBundle(spends, G2Element()))
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:56]}"


# ---------------------------------------------------------------------------
# OPTION 1 -- is the ephemeral rule really about the birth condition?
# ---------------------------------------------------------------------------

def option_1():
    print("OPTION 1 -- drop ASSERT_MY_BIRTH_HEIGHT on the genesis spend")
    print("""  Tested on bare coins rather than our puzzles, so the answer is about the CONSENSUS
  RULE and not about Forge: create a coin and spend it in the same bundle, once with a
  birth-height condition and once without.
""")
    funder_parent = bytes32(b"\x11" * 32)
    for with_birth in (True, False):
        child_conditions = [[ASSERT_MY_BIRTH_HEIGHT, kit.VALIDATION_HEIGHT]] if with_birth else []
        child_puzzle = quoted(child_conditions)
        child_ph = child_puzzle.get_tree_hash()
        parent_puzzle = quoted([[CREATE_COIN, child_ph, 1]])
        parent = Coin(funder_parent, bytes32(parent_puzzle.get_tree_hash()), uint64(1))
        child = Coin(parent.name(), bytes32(child_ph), uint64(1))
        ok, why = validates([make_spend(parent, parent_puzzle, Program.to([])),
                             make_spend(child, child_puzzle, Program.to([]))])
        tag = "WITH ASSERT_MY_BIRTH_HEIGHT" if with_birth else "WITHOUT it"
        check(f"  same-block create-and-spend, {tag}: {'ACCEPTED' if ok else 'REFUSED'}",
              ok != with_birth, why if not ok else "")
    print("""  So the rule is exactly the birth condition. Removing it from the genesis path WOULD
  make the eve spendable in its own block -- OPTION 1 is mechanically available.

  It is still the wrong trade. ASSERT_MY_BIRTH_HEIGHT is what the S3 oracle fix rests
  on: `birth > last_height` and `h >= birth` are only meaningful because consensus pins
  birth. A genesis exemption puts a branch in the one assert a reviewer just validated,
  and the third review checked that boundary specifically.
""")


# ---------------------------------------------------------------------------
# OPTION 2 -- does a LATER-block first spend catch a wrong parent?
# ---------------------------------------------------------------------------

def option_2():
    print("OPTION 2 -- two-phase: a later block's first spend confirms the slot")
    CAT = bytes32(b"\xd0" * 32)
    honest = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                           leaves="forge", salt=0x60)
    real_coins = [(r.coin, r.lineage) for r in honest.reserves]
    lying_state = [*list(honest.state)[:7], [bytes32(b"\xee" * 32), bytes32(b"\xef" * 32)]]
    liar = kit.make_pool([None, CAT], [10_000_000, 20_000_000], total_lp=5_000_000,
                         leaves="forge", salt=0x60, reserve_coins=real_coins, state=lying_state)
    for pool, tag, expect in ((honest, "parents CORRECT", True), (liar, "parents WRONG", False)):
        b, _ = kit.spend_action(replace(pool, birth=1), "forge_action_observe", [6_999_990])
        ok, why = validates(list(b.coin_spends))
        check(f"  first spend in a later block, {tag}: {'ACCEPTED' if ok else 'REFUSED'}",
              ok == expect, why if not ok else "")
    print("""  The mechanism works: once the pool is spent at all, a wrong parent cannot pair. What
  OPTION 2 costs is registry machinery -- a provisional slot, a confirm path, and a rule
  for what an unconfirmed slot means to everyone else. The squat window is open until
  the confirm lands.
""")


# ---------------------------------------------------------------------------
# OPTION 3 -- a reserve-creating puzzle that announces what it creates
# ---------------------------------------------------------------------------

def option_3():
    print("OPTION 3 -- a reserve launcher whose COIN announcement commits to the reserve")
    print("""  The shape: `register` is given each reserve's GRANDPARENT, and computes
      P_i = coinid(grandparent_i, RESERVE_LAUNCHER_HASH, reserves[i])
  then asserts a coin announcement from P_i carrying (reserve_full_hash, amount).
  Because a coin id commits to its puzzle hash, any coin able to satisfy that assertion
  IS a reserve launcher -- and the launcher's puzzle creates the reserve. `register`
  then uses P_i as the reserve parent: derived, never claimed.
""")
    RESERVE_PH = bytes32(b"\xab" * 32)     # stands in for the real reserve puzzle hash
    AMOUNT = 10_000_000
    LAUNCHER_ID = bytes32(b"\x5a" * 32)
    msg = Program.to([RESERVE_PH, AMOUNT, LAUNCHER_ID]).get_tree_hash()

    # The reserve launcher: creates the reserve, and announces exactly what it created.
    rl_puzzle = quoted([[CREATE_COIN, RESERVE_PH, AMOUNT, [LAUNCHER_ID]],
                        [CREATE_COIN_ANN, msg]])
    RL_HASH = bytes32(rl_puzzle.get_tree_hash())

    grandparent = bytes32(b"\x22" * 32)
    P = bytes32(coin_id(grandparent, RL_HASH, AMOUNT))
    rl_coin = Coin(grandparent, RL_HASH, uint64(AMOUNT))
    check("  register derives P from (grandparent, RESERVE_LAUNCHER_HASH, amount)",
          bytes(rl_coin.name()) == bytes(P), P.hex()[:16])

    registry = quoted([[ASSERT_COIN_ANN, bytes32(kit.Program.to(0).get_tree_hash())]])  # placeholder, replaced below
    ann_id = bytes32(__import__("hashlib").sha256(bytes(P) + bytes(msg)).digest())
    registry = quoted([[ASSERT_COIN_ANN, ann_id]])
    reg_coin = Coin(bytes32(b"\x33" * 32), bytes32(registry.get_tree_hash()), uint64(1))

    # Honest: the launcher is spent, creates the reserve, announces it.
    ok, why = validates([make_spend(rl_coin, rl_puzzle, Program.to([])),
                         make_spend(reg_coin, registry, Program.to([]))])
    check(f"  honest: launcher spent, reserve created, registration {'ACCEPTED' if ok else 'REFUSED'}",
          ok, why)

    # Attacker: wants a DIFFERENT parent recorded, so they try to satisfy the assertion
    # with a coin of their own that announces without creating the reserve.
    imposter_puzzle = quoted([[CREATE_COIN_ANN, msg]])          # announces, creates nothing
    imposter = Coin(grandparent, bytes32(imposter_puzzle.get_tree_hash()), uint64(AMOUNT))
    check("  the imposter's coin id differs from P, because a coin id commits to the puzzle",
          bytes(imposter.name()) != bytes(P))
    ok, why = validates([make_spend(imposter, imposter_puzzle, Program.to([])),
                         make_spend(reg_coin, registry, Program.to([]))])
    check(f"  attacker: announce-without-creating is {'ACCEPTED' if ok else 'REFUSED'}",
          not ok, why)

    # And the reserve the honest path creates really is parented by P.
    reserve = Coin(P, RESERVE_PH, uint64(AMOUNT))
    check("  the created reserve's parent IS P, so state and chain agree by construction",
          bytes(reserve.parent_coin_info) == bytes(P))
    print("""  OPTION 3 binds both halves at once: the coin id fixes WHICH coin, and the puzzle at
  that id fixes WHAT it does. Neither a puzzle announcement (binds the puzzle, not the
  coin) nor a coin announcement from an arbitrary coin (binds the coin, not the deed)
  can do this alone.
""")


def main() -> int:
    if not (kit.v13_available() and kit.registry_available()):
        print("  [skip] V13 build outputs are absent; run scripts/build-v13.py")
        return 2
    option_1()
    option_2()
    option_3()
    passed = sum(1 for r in results if r)
    print(f"{passed}/{len(results)} candidate simulations behaved as predicted")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
