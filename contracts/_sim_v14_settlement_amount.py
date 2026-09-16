#!/usr/bin/env python3
"""Can the AMOUNT binding live in our puzzle too, not only in the CAT ring?

Today a leaf asserts a puzzle announcement keyed by
`settlement_puzzle_hash(asset_id)` with message `tree_hash((coin_id, nil))`. That binds
the asset and the coin id and says nothing about the amount; conservation catches the
rest. The question is whether the puzzle can bind the amount itself, so the two
mechanisms are independent.

The lever is the same one V14 already uses for reserves: **a coin id commits to its
parent, its puzzle hash and its amount.** So a leaf given the settlement's PARENT and
amount can DERIVE the id, and anything that then proves that exact coin was spent proves
the amount too.

Three candidate mechanisms, tested against the validator:

  1. keep the puzzle announcement, encode the amount in the nonce -- does the nonce bind?
  2. ASSERT_COIN_ANNOUNCEMENT from the derived id -- does OFFER_MOD make one?
  3. ASSERT_CONCURRENT_SPEND of the derived id -- binds the coin without any announcement

Exit 0 if the probes behave as reported.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.coin_spend import make_spend
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import _v13_testkit as kit
from forge_v13_driver import Program, coin_id

results = []
ASSERT_COIN_ANN, ASSERT_CONCURRENT_SPEND = 61, 64


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def quoted(conditions):
    return Program.to((1, conditions))


def validates(spends):
    try:
        kit.validate(SpendBundle(spends, G2Element()))
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:44]}"


PARENT = bytes32(b"\x41" * 32)
HONEST_AMOUNT = 250_000
CLAIMED_AMOUNT = 250_000

print("=" * 92)
print("1. Encoding the amount in the announcement's NONCE does not bind it")
print("=" * 92)
print("""
   A notarized group is (nonce . payments) and OFFER_MOD announces its tree hash. The
   nonce is chosen by whoever solves the settlement, so putting the amount there proves
   only that they typed it.
""")
liar = Coin(PARENT, bytes32(OFFER_MOD_HASH), uint64(1))          # holds ONE mojo
nonce = Program.to((liar.name(), CLAIMED_AMOUNT))                 # claims 250,000
msg = Program.to((nonce, None)).get_tree_hash()
asserter = quoted([[62, msg]] if False else [[63, bytes32(__import__("hashlib").sha256(
    bytes(OFFER_MOD_HASH) + bytes(msg)).digest())]])
ok, why = validates([
    make_spend(liar, OFFER_MOD, Program.to([[nonce]])),
    make_spend(Coin(bytes32(b"\x42" * 32), bytes32(asserter.get_tree_hash()), uint64(1)),
               asserter, Program.to([])),
])
check("a 1-mojo settlement can announce any amount it likes", ok,
      "the nonce is free, so this proves nothing about value" if ok else why)

print("\n" + "=" * 92)
print("2. Does OFFER_MOD make a COIN announcement? (a coin announcement WOULD bind)")
print("=" * 92)
print("""
   ASSERT_COIN_ANNOUNCEMENT is keyed by sha256(coin_id + message), and a coin id commits
   to the amount -- so if the settlement made one, deriving the id would bind the value.
""")
honest = Coin(PARENT, bytes32(OFFER_MOD_HASH), uint64(HONEST_AMOUNT))
derived = bytes32(coin_id(PARENT, bytes32(OFFER_MOD_HASH), HONEST_AMOUNT))
check("the derived id matches the real coin, so the derivation is sound",
      bytes(derived) == bytes(honest.name()), derived.hex()[:16])
ann = bytes32(__import__("hashlib").sha256(bytes(derived) + b"forge").digest())
asserter2 = quoted([[ASSERT_COIN_ANN, ann]])
ok, why = validates([
    make_spend(honest, OFFER_MOD, Program.to([[honest.name()]])),
    make_spend(Coin(bytes32(b"\x43" * 32), bytes32(asserter2.get_tree_hash()), uint64(1)),
               asserter2, Program.to([])),
])
check("OFFER_MOD does NOT make a coin announcement, so this route is unavailable",
      not ok, why)

print("\n" + "=" * 92)
print("3. ASSERT_CONCURRENT_SPEND of the DERIVED id -- binds without any announcement")
print("=" * 92)
print("""
   The condition asserts that a coin with a given id is spent in the same bundle. Derive
   the id from (settlement parent, settlement puzzle hash for the asset, gross_input) and
   the assertion binds the asset, the amount and the coin's existence at once -- inside
   the puzzle, with no help from the ring.
""")
asserter3 = quoted([[ASSERT_CONCURRENT_SPEND, derived]])
guard = Coin(bytes32(b"\x44" * 32), bytes32(asserter3.get_tree_hash()), uint64(1))
ok, why = validates([
    make_spend(honest, OFFER_MOD, Program.to([[honest.name()]])),
    make_spend(guard, asserter3, Program.to([])),
])
check("the honest settlement satisfies it", ok, why)

# The attacker spends a 1-mojo settlement and hopes the assertion passes.
short = Coin(PARENT, bytes32(OFFER_MOD_HASH), uint64(1))
ok, why = validates([
    make_spend(short, OFFER_MOD, Program.to([[short.name()]])),
    make_spend(guard, asserter3, Program.to([])),
])
check("a 1-mojo settlement does NOT satisfy it", not ok, why)

# ... and a settlement of the right amount at the WRONG puzzle (a different asset).
wrong_ph = bytes32(b"\x99" * 32)
wrong = Coin(PARENT, wrong_ph, uint64(HONEST_AMOUNT))
ok, why = validates([
    make_spend(wrong, quoted([]), Program.to([])),
    make_spend(guard, asserter3, Program.to([])),
])
check("the right amount at the wrong puzzle does NOT satisfy it", not ok, why)

print("""
   So the amount CAN be bound in-puzzle, and the mechanism is one condition:

       ASSERT_CONCURRENT_SPEND( sha256(parent + settlement_puzzle_hash(asset) + amount) )

   The leaf would take the settlement's PARENT instead of its coin id -- one field of the
   same width -- and derive the rest. Nothing else changes.
""")

print("=" * 92)
print("WHAT IT WOULD AND WOULD NOT BUY")
print("=" * 92)
print("""
   BUYS   a second, independent binding. Today the asset comes from the announcement and
          the amount from conservation; with this the puzzle asserts both, so a future
          change to how value is accounted could not silently remove half the guarantee.
          It also makes the leaf's intent legible on chain: the condition names the exact
          coin the action is paid by.

   COSTS  one more condition per settlement per action -- cost, and a bigger solution for
          multi-settlement adds. And it duplicates a guarantee conservation already gives,
          which is the definition of defence in depth and also the definition of something
          that can rot untested.

   WORTH NOTING it does NOT replace conservation. A settlement can be spent and its value
   sent somewhere other than the reserve; only the ring notices that. The two bind
   different halves and both are still needed.
""")


def main() -> int:
    passed = sum(1 for r in results if r)
    print(f"{passed}/{len(results)} probes behaved as reported")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
