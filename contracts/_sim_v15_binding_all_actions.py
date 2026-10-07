#!/usr/bin/env python3
"""The settlement amount binding, run against EVERY action shape before it is built.

Adopted design (spec 1.8), corrected for what the router actually does:

    id = sha256(settlement_parent + settlement_puzzle_hash(asset_id) + settlement_amount)
    ASSERT_CONCURRENT_SPEND(id)
    assert settlement_amount >= gross_input          # for swap; >= deposit for add

`settlement_amount` is the coin's REAL amount, taken from the solution and then pinned by
the derivation -- not `gross_input`. That distinction is the whole reason this file
exists: the router's fee is carved out of the SAME settlement coin, so

    settlement.amount == gross_input + router_fee

and a binding on `gross_input` would refuse every swap that pays a router fee. The coin
is bound by derivation; the inequality is what ties it to the action.

Each probe states what it expects. A PASS means the binding behaved correctly -- which
for an attack means refused, and for an honest shape means accepted.

Exit 0 if every action shape behaves, 1 otherwise.
"""
import hashlib
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.coin_spend import make_spend
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD, OFFER_MOD_HASH
from chia_rs import Coin, G2Element, SpendBundle
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import _v13_testkit as kit
from forge_v13_driver import Program, coin_id

results = []
ASSERT_CONCURRENT_SPEND = 64
CAT_A = bytes32(b"\xd0" * 32)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def settle_ph(asset):
    return bytes32(OFFER_MOD_HASH) if asset is None else \
        bytes32(construct_cat_puzzle(CAT_MOD, asset, OFFER_MOD).get_tree_hash())


def derive(parent, asset, amount):
    """What the leaf would compute from the solution."""
    return bytes32(coin_id(parent, settle_ph(asset), amount))


def guard(ids):
    """A stand-in for the leaf: asserts each derived settlement is spent."""
    return Program.to((1, [[ASSERT_CONCURRENT_SPEND, i] for i in ids]))


def cat_settlement(asset, amount, salt):
    """A valid CAT settlement, built as forge_v13_driver builds one.

    A CAT's parent must itself be a CAT coin at the same outer puzzle hash, so the parent
    id is DERIVED from a grandparent rather than invented. The ring is balanced by paying
    the value onward, standing in for the reserve that would absorb it -- the coin is
    still SPENT, which is all ASSERT_CONCURRENT_SPEND asks.
    """
    outer = settle_ph(asset)
    grandparent = bytes32(bytes([salt]) * 32)
    parent = bytes32(coin_id(grandparent, outer, amount))
    coin = Coin(parent, outer, uint64(amount))
    onward = bytes32(b"\x7f" * 32)
    spendable = kit.SpendableCAT(
        coin, asset, OFFER_MOD, Program.to([[coin.name(), [onward, amount]]]),
        lineage_proof=kit.LineageProof(grandparent, bytes32(OFFER_MOD_HASH), uint64(amount)))
    return coin, spendable


def run(settlement_coins, derived_ids, extra_spends=()):
    """Spend the settlements, and a coin asserting the derived ids.

    Entries are (coin, None) for XCH or (coin, asset, spendable) for a CAT.
    """
    g = guard(derived_ids)
    gc = Coin(bytes32(b"\x5a" * 32), bytes32(g.get_tree_hash()), uint64(1))
    spends = [make_spend(gc, g, Program.to([]))]
    cats = []
    for entry in settlement_coins:
        coin, asset = entry[0], entry[1]
        if asset is None:
            # A bare settlement spent with no payments releases its mojos into the bundle,
            # which is exactly how a reserve is funded. Nothing to balance here.
            spends.append(make_spend(coin, OFFER_MOD, Program.to([[coin.name()]])))
        else:
            cats.append(entry[2])
    if cats:
        spends.extend(kit.unsigned_spend_bundle_for_spendable_cats(CAT_MOD, cats).coin_spends)
    spends.extend(extra_spends)
    try:
        kit.validate(SpendBundle(spends, G2Element()))
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:42]}"


P = bytes32(b"\x61" * 32)

print("=" * 94)
print("SWAP -- the shape the router actually builds")
print("=" * 94)
print("""
   The router carves its fee out of the settlement, so the coin holds gross + router_fee.
   The binding must be on the coin's REAL amount, with gross checked by inequality.
""")
GROSS, ROUTER_FEE = 250_000, 7_500
OFFERED = GROSS + ROUTER_FEE
real = Coin(P, settle_ph(None), uint64(OFFERED))

ok, why = run([(real, None)], [derive(P, None, OFFERED)])
check("a swap paying a router fee: binding on the coin's real amount is accepted", ok, why)

ok, why = run([(real, None)], [derive(P, None, GROSS)])
check("  binding on gross_input instead would REFUSE that same honest swap", not ok,
      "which is why the solution carries the settlement's own amount")

zero_fee = Coin(P, settle_ph(None), uint64(GROSS))
ok, why = run([(zero_fee, None)], [derive(P, None, GROSS)])
check("a swap with no router fee, where amount == gross, is accepted", ok, why)

print("\n   the inequality is what ties the bound coin to the action:")
for offered, gross, expect in ((OFFERED, GROSS, True), (GROSS, GROSS, True), (1, GROSS, False)):
    holds = offered >= gross
    check(f"   settlement {offered:>7,} against gross {gross:>7,}: "
          f"{'accepted' if holds else 'refused'}", holds == expect)

print("\n" + "=" * 94)
print("ATTACKS on the swap binding")
print("=" * 94 + "\n")
liar = Coin(P, settle_ph(None), uint64(1))
ok, why = run([(liar, None)], [derive(P, None, OFFERED)])
check("a 1-mojo settlement cannot satisfy a binding derived for 257,500", not ok, why)

ok, why = run([(real, None)], [derive(bytes32(b"\x62" * 32), None, OFFERED)])
check("a settlement with a different parent cannot satisfy it", not ok, why)

cat_coin = Coin(P, settle_ph(CAT_A), uint64(OFFERED))
ok, why = run([(real, None)], [derive(P, CAT_A, OFFERED)])
check("an XCH settlement cannot satisfy a binding derived for a CAT", not ok, why)

print("\n" + "=" * 94)
print("ADD -- several settlements, and zero deposits that have none")
print("=" * 94 + "\n")
xch_c = Coin(P, settle_ph(None), uint64(100_000))
cat_c, cat_spendable = cat_settlement(CAT_A, 200_000, 0x63)
coins = [(xch_c, None), (cat_c, CAT_A, cat_spendable)]
ids = [derive(P, None, 100_000), derive(cat_c.parent_coin_info, CAT_A, 200_000)]
ok, why = run(coins, ids)
check("a two-asset add binds one settlement per deposited asset", ok, why)

# A zero deposit needs no settlement, and must not be asserted.
ok, why = run(coins, ids + [derive(P, CAT_A, 0)])
check("asserting a settlement for a ZERO deposit is refused -- so the leaf must not emit one",
      not ok, why)
print("""     That is a build note rather than a finding: `deposit_asserts` already skips a
     zero deposit, and the amount binding has to skip it in the same branch.""")

print("\n" + "=" * 94)
print("REMOVE, OBSERVE, COLLECT, DAO_FEE -- actions with no settlement at all")
print("=" * 94)
print("""
   `remove` pays OUT through payouts rather than in through settlements, and the operator
   leaves move no trader value. The binding must not be emitted for any of them: there is
   no settlement to derive from, and asserting one would make these actions unspendable.
""")
ok, why = run([], [])
check("an action asserting no settlements validates", ok, why)
ok, why = run([], [derive(P, None, 1)])
check("  ...and asserting one that does not exist is refused", not ok, why)

print("\n" + "=" * 94)
print("ROUTES -- a payout coin becomes the next pool's settlement")
print("=" * 94)
print("""
   In a multi-hop the second hop's settlement is the first hop's payout, whose parent is
   the RESERVE coin that released it. The derivation still works, because the payout's
   parent and amount are both known to the builder.
""")
hop_amount = 4_520
hop, hop_spendable = cat_settlement(CAT_A, hop_amount, 0x71)
ok, why = run([(hop, CAT_A, hop_spendable)], [derive(hop.parent_coin_info, CAT_A, hop_amount)])
check("a hop settlement binds correctly, parent derived like any other CAT", ok, why)

print("\n" + "=" * 94)
print("WHAT THIS CHANGES IN THE SPEC")
print("=" * 94)
print("""
   The binding is on the settlement's OWN amount, not on the action's input:

       assert settlement_amount >= gross_input          (swap)
       assert settlement_amount >= deposit              (add, per positive deposit)
       ASSERT_CONCURRENT_SPEND(sha256(parent + settle_ph(asset) + settlement_amount))

   Binding gross_input directly would have refused every swap that pays a router fee --
   which is every swap on the public lane. Caught here rather than on testnet.

   The leaf's solution gains the settlement PARENT and AMOUNT and loses the coin id, which
   it now derives. Net: one field wider per settlement, one condition per settlement.
""")


def main() -> int:
    passed = sum(1 for r in results if r)
    print(f"{passed}/{len(results)} action shapes behaved as required")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
