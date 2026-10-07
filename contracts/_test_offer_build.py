#!/usr/bin/env python3
"""The app-side offer builder, run against the real puzzles.

What is being proved: coins a wallet reports, with the puzzle reveals and lineage
proofs the Sage bridge hands out, become maker spends that conserve value, pay
the settlement puzzle exactly what was offered, and round-trip through `Offer`
into an `offer1...` string whose requested side is what was asked for.

Signing is the wallet's job and is not tested here: the bundle is finalized with
an empty signature, which `Offer` accepts and consensus would not. That boundary
is the point of the design.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.trading.offer import OFFER_MOD_HASH, Offer
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk
from chia_rs import Coin, G1Element, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

BUILDER = Path(__file__).resolve().parent / "forge_offer_build.py"
# The real standard puzzle, so the spends this builds are the shape a wallet
# would actually be asked to sign. The key is unusable, which is fine: nothing
# here signs, and an Offer's structure does not depend on whose key it is.
IDENTITY = puzzle_for_pk(G1Element())
IDENTITY_HASH = IDENTITY.get_tree_hash()
CHANGE_PH = bytes32.fromhex("ab" * 32)
CAT_ASSET = bytes32.fromhex("c1" * 32)
FAILED = 0


def run(payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(BUILDER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        raise AssertionError(f"builder produced nothing: {result.stderr[:400]}")
    return json.loads(result.stdout)


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        FAILED += 1


def xch_coin(amount: int, seed: int) -> dict:
    parent = bytes32.fromhex(f"{seed:02x}" * 32)
    coin = Coin(parent, IDENTITY_HASH, uint64(amount))
    return {
        "coin": {
            "parent_coin_info": "0x" + coin.parent_coin_info.hex(),
            "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
            "amount": amount,
        },
        "puzzle": bytes(IDENTITY).hex(),
    }


def cat_coin(amount: int, seed: int) -> dict:
    outer = construct_cat_puzzle(CAT_MOD, CAT_ASSET, IDENTITY).get_tree_hash()
    grandparent = bytes32.fromhex(f"{seed:02x}" * 32)
    parent = Coin(grandparent, outer, uint64(amount))
    coin = Coin(parent.name(), outer, uint64(amount))
    return {
        "coin": {
            "parent_coin_info": "0x" + coin.parent_coin_info.hex(),
            "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
            "amount": amount,
        },
        "inner_puzzle": bytes(IDENTITY).hex(),
        "lineage_proof": {
            "parent_name": "0x" + grandparent.hex(),
            "inner_puzzle_hash": "0x" + IDENTITY_HASH.hex(),
            "amount": amount,
        },
    }


def conditions_of(spend: dict) -> list:
    """The conditions a standard coin's spend produces.

    A p2_delegated_puzzle_or_hidden_puzzle coin is solved with
    `(() delegated_puzzle solution)` and its conditions are whatever the
    delegated puzzle returns, so that is what is read here rather than the
    outer puzzle's own output.
    """
    solution = Program.fromhex(spend["solution"])
    delegated = solution.rest().first()
    return list(delegated.run(Program.to([])).as_iter())


print("XCH offered, change returned:")
built = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "fee": 0,
    "offered": [{"asset_id": None, "amount": 250_000, "coins": [xch_coin(1_000_000, 0x11)]}],
    "requested": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 7_500}],
})
check("the builder answered", built.get("success") is True, str(built.get("error")))
spends = built.get("coin_spends") or []
check("one coin spent", len(spends) == 1, f"{len(spends)} spends")

created = [c for c in conditions_of(spends[0]) if int(c.first().as_int()) == 51]
settlement = [c for c in created if bytes(c.rest().first().as_atom()) == bytes(OFFER_MOD_HASH)]
change = [c for c in created if bytes(c.rest().first().as_atom()) == bytes(CHANGE_PH)]
check("it pays the settlement puzzle", len(settlement) == 1)
check("  exactly what was offered", bool(settlement) and int(settlement[0].rest().rest().first().as_int()) == 250_000)
check("change returns to the trader", len(change) == 1)
check("  and value is conserved", bool(change) and int(change[0].rest().rest().first().as_int()) == 750_000)

print("\na fee is value not recreated:")
with_fee = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "fee": 1_000,
    "offered": [{"asset_id": None, "amount": 250_000, "coins": [xch_coin(1_000_000, 0x12)]}],
    "requested": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 7_500}],
})
fee_created = [c for c in conditions_of(with_fee["coin_spends"][0]) if int(c.first().as_int()) == 51]
fee_change = [c for c in fee_created if bytes(c.rest().first().as_atom()) == bytes(CHANGE_PH)]
check("the change is short by the fee", bool(fee_change) and int(fee_change[0].rest().rest().first().as_int()) == 749_000)
# Stated as well as paid: Sage's approval sums RESERVE_FEE (52) and showed "FEE 0"
# for an implicit fee (2026-10-03).
reserved = [c for c in conditions_of(with_fee["coin_spends"][0]) if int(c.first().as_int()) == 52]
check("the fee is stated as one RESERVE_FEE of exactly that amount",
      len(reserved) == 1 and int(reserved[0].rest().first().as_int()) == 1_000, str(len(reserved)))
no_fee_reserved = [c for c in conditions_of(spends[0]) if int(c.first().as_int()) == 52]
check("  and no RESERVE_FEE when there is no fee", len(no_fee_reserved) == 0)

print("\nmore coins than one leg needs:")
many = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{
        "asset_id": None,
        "amount": 1_500_000,
        "coins": [xch_coin(1_000_000, 0x13), xch_coin(800_000, 0x14)],
    }],
    "requested": [{"asset_id": None, "amount": 1}],
})
check("every coin is spent", len(many["coin_spends"]) == 2, str(len(many["coin_spends"])))
second = [c for c in conditions_of(many["coin_spends"][1]) if int(c.first().as_int()) == 51]
check("  and only the first creates anything", len(second) == 0, f"{len(second)} creates")

print("\na CAT leg rides its ring:")
cat_built = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 400, "coins": [cat_coin(1_000, 0x21)]}],
    "requested": [{"asset_id": None, "amount": 50_000}],
})
check("the builder answered", cat_built.get("success") is True, str(cat_built.get("error")))
cat_spends = cat_built.get("coin_spends") or []
check("the CAT is spent", len(cat_spends) == 1, f"{len(cat_spends)} spends")
check(
    "  through the CAT layer, not bare",
    bool(cat_spends) and bytes(CAT_MOD).hex()[:32] in cat_spends[0]["puzzle_reveal"],
)

print("\nthe offer that comes out:")
finalized = run({
    "action": "finalize",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "coin_spends": built["coin_spends"],
    "requested": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 7_500}],
    "signature": "0x" + bytes(G2Element()).hex(),
})
check("it finalizes", finalized.get("success") is True, str(finalized.get("error")))
offer_text = finalized.get("offer", "")
check("it is an offer string", offer_text.startswith("offer1"), offer_text[:24])

parsed = Offer.from_bech32(offer_text)
requested = {
    (asset.hex() if asset else "xch"): sum(int(p.amount) for p in payments)
    for asset, payments in parsed.get_requested_payments().items()
}
check("it asks for what the trader wanted", requested == {CAT_ASSET.hex(): 7_500}, str(requested))
offered = {
    (asset.hex() if asset else "xch"): amount for asset, amount in parsed.get_offered_amounts().items()
}
check("it gives up what the trader offered", offered == {"xch": 250_000}, str(offered))

# 2026-09-26: found while remediating the external review. A maker spend with
# only CREATE_COINs is a gift, not an offer: whoever settles the settlement coin
# may pay anyone. The maker's spend must assert the announcement the settlement
# puzzle makes when it pays the notarized requested payments.
asserted = {bytes(c.rest().first().as_atom()) for spend in built["coin_spends"]
            for c in conditions_of(spend) if c.first().as_int() == 63}
expected = {ann.msg_calc for ann in Offer.calculate_announcements(parsed.requested_payments, parsed.driver_dict)}
check("the maker's spend asserts the settlement announcement of its requested payments", bool(expected) and expected <= asserted,
      f"asserted {[a.hex()[:10] for a in asserted]} expected {[e.hex()[:10] for e in expected]}")

print("\nbad input is refused, not guessed at:")
short = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": None, "amount": 2_000_000, "coins": [xch_coin(1_000_000, 0x15)]}],
    "requested": [{"asset_id": None, "amount": 1}],
})
check("coins that cannot cover the offer are refused", short.get("success") is False, str(short)[:120])

# 2026-09-26 external review, finding 15: the builder executed every caller-supplied
# CAT inner puzzle before anything checked the coin could exist. Now a coin whose
# puzzle hash is not the hash of its reveal is refused first, by a tree hash.
wrong_xch = xch_coin(1_000_000, 0x16)
wrong_xch["coin"]["puzzle_hash"] = "0x" + ("de" * 32)
mismatch = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": None, "amount": 500_000, "coins": [wrong_xch]}],
    "requested": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 1}],
})
check("an XCH coin whose hash is not its reveal's is refused", mismatch.get("success") is False, str(mismatch)[:160])
check("...and the refusal names the mismatch", "cannot be spent with this reveal" in str(mismatch.get("error")), str(mismatch.get("error"))[:160])
wrong_cat = cat_coin(5_000, 0x17)
wrong_cat["inner_puzzle"] = bytes(Program.to((1, [[51, CHANGE_PH, 1]]))).hex()   # a different inner: the CAT hash no longer matches
mismatch_cat = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 1_000, "coins": [wrong_cat]}],
    "requested": [{"asset_id": None, "amount": 1}],
})
check("a CAT coin whose inner does not produce its hash is refused before it runs", mismatch_cat.get("success") is False, str(mismatch_cat)[:160])

# 2026-10-01: Sage's getAssetCoins reports a CAT coin's FULL puzzle (the CAT layer
# already around the p2), and the Sage app passed it on as the inner one, so every
# CAT-paid swap from the app was refused by the check above. The full puzzle is
# taken only when it is this coin's own puzzle over this asset.
print("\na CAT coin reported with its full puzzle, as Sage reports it:")
full = cat_coin(1_000, 0x31)
full["inner_puzzle"] = bytes(construct_cat_puzzle(CAT_MOD, CAT_ASSET, IDENTITY)).hex()
full_built = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 400, "coins": [full]}],
    "requested": [{"asset_id": None, "amount": 50_000}],
})
check("it builds", full_built.get("success") is True, str(full_built.get("error"))[:160])
full_spends = full_built.get("coin_spends") or []
check("  wrapped in the CAT layer once, not twice: the reveal is the coin's own puzzle",
      bool(full_spends) and Program.fromhex(full_spends[0]["puzzle_reveal"].removeprefix("0x")).get_tree_hash().hex()
      == full["coin"]["puzzle_hash"].removeprefix("0x"))
same_coin_inner = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 400, "coins": [cat_coin(1_000, 0x31)]}],
    "requested": [{"asset_id": None, "amount": 50_000}],
})
check("  and the spend is identical to the one built from the inner puzzle",
      bool(full_spends) and full_spends == same_coin_inner.get("coin_spends"))

OTHER_ASSET = bytes32(b"\x0a" * 32)
other_full = cat_coin(1_000, 0x32)
other_puzzle = construct_cat_puzzle(CAT_MOD, OTHER_ASSET, IDENTITY)
other_full["inner_puzzle"] = bytes(other_puzzle).hex()
other_full["coin"]["puzzle_hash"] = "0x" + other_puzzle.get_tree_hash().hex()   # a real coin, of ANOTHER CAT
wrong_asset = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 400, "coins": [other_full]}],
    "requested": [{"asset_id": None, "amount": 1}],
})
check("a full puzzle of another CAT is refused, not spent as this one",
      wrong_asset.get("success") is False and "cannot be spent with this reveal" in str(wrong_asset.get("error")),
      str(wrong_asset.get("error"))[:160])

not_this_coin = cat_coin(1_000, 0x33)
not_this_coin["inner_puzzle"] = bytes(construct_cat_puzzle(CAT_MOD, CAT_ASSET, Program.to((1, [[51, CHANGE_PH, 1]])))).hex()
stranger = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 400, "coins": [not_this_coin]}],
    "requested": [{"asset_id": None, "amount": 1}],
})
check("a full CAT puzzle that is not this coin's is refused", stranger.get("success") is False, str(stranger.get("error"))[:160])

# 2026-10-02: the fee rode only on the XCH leg, so an offer that gives up no XCH (a
# CAT-paid swap) went out with fee 0 whatever the page showed -- a T6-paid 7-pool
# split sat in the mempool for minutes. It now pays from `fee_coins`.
print("\na CAT-paid offer pays its network fee from fee coins:")
FEE = 10_693
fee_coin = xch_coin(1_000_000, 0x41)
cat_fee = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "fee": FEE,
    "offered": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 400, "coins": [cat_coin(1_000, 0x42)]}],
    "fee_coins": [fee_coin],
    "requested": [{"asset_id": None, "amount": 50_000}],
})
check("it builds", cat_fee.get("success") is True, str(cat_fee.get("error"))[:160])
fee_spends = [s for s in (cat_fee.get("coin_spends") or []) if s["coin"]["puzzle_hash"].removeprefix("0x") == fee_coin["coin"]["puzzle_hash"].removeprefix("0x")]
check("  the fee coin is spent", len(fee_spends) == 1, f"{len(fee_spends)}")
if fee_spends:
    created = [c for c in conditions_of(fee_spends[0]) if c.first().as_int() == 51]
    paid_out = sum(int(c.rest().rest().first().as_int()) for c in created)
    check("  it pays exactly the fee: everything else comes back as change", 1_000_000 - paid_out == FEE,
          f"in 1000000, out {paid_out}")
    stated = [c for c in conditions_of(fee_spends[0]) if c.first().as_int() == 52]
    check("  and states it as a RESERVE_FEE, so the wallet shows it",
          len(stated) == 1 and int(stated[0].rest().first().as_int()) == FEE)
    check("  and nothing of it goes to the settlement",
          all(bytes(c.rest().first().as_atom()) != bytes(OFFER_MOD_HASH) for c in created))
    check("  it asserts the offer's announcements, so it cannot be spent apart from the offer",
          any(c.first().as_int() == 63 for c in conditions_of(fee_spends[0])))
fee_offer = run({
    "action": "finalize",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "coin_spends": cat_fee.get("coin_spends") or [],
    "requested": [{"asset_id": None, "amount": 50_000}],
    "signature": "0x" + bytes(G2Element()).hex(),
})
check("  it finalizes", fee_offer.get("success") is True, str(fee_offer.get("error"))[:160])
if fee_offer.get("success"):
    parsed_fee = Offer.from_bech32(fee_offer["offer"])
    # The fee coin's own spend (conditions_of reads standard XCH spends only).
    asserted_fee = {bytes(c.rest().first().as_atom()) for spend in fee_spends
                    for c in conditions_of(spend) if c.first().as_int() == 63}
    expected_fee = {ann.msg_calc for ann in Offer.calculate_announcements(parsed_fee.requested_payments, parsed_fee.driver_dict)}
    check("  build and finalize agree on the nonce: the fee coin asserts every announcement the offer needs",
          bool(expected_fee) and expected_fee <= asserted_fee)

no_fee_coins = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "fee": FEE,
    "offered": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 400, "coins": [cat_coin(1_000, 0x43)]}],
    "requested": [{"asset_id": None, "amount": 50_000}],
})
check("a fee with no XCH to pay it is refused, not dropped",
      no_fee_coins.get("success") is False and "fee_coins" in str(no_fee_coins.get("error")), str(no_fee_coins.get("error"))[:160])

zero_fee = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "fee": 0,
    "offered": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 400, "coins": [cat_coin(1_000, 0x44)]}],
    "fee_coins": [xch_coin(1_000_000, 0x45)],
    "requested": [{"asset_id": None, "amount": 50_000}],
})
check("with no fee, a fee coin sent anyway is not spent", zero_fee.get("success") is True and len(zero_fee.get("coin_spends") or []) == 1,
      f"{len(zero_fee.get('coin_spends') or [])} spends")

xch_paid = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "fee": FEE,
    "offered": [{"asset_id": None, "amount": 250_000, "coins": [xch_coin(1_000_000, 0x46)]}],
    "fee_coins": [xch_coin(1_000_000, 0x47)],
    "requested": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 7_500}],
})
check("an offer that gives up XCH pays the fee from that XCH, and spends no fee coin",
      xch_paid.get("success") is True and len(xch_paid.get("coin_spends") or []) == 1,
      f"{len(xch_paid.get('coin_spends') or [])} spends")

print("\na payment rides the trader's own spend (the router fee on a Dexie-only swap, 2026-10-05):")
FEE_PH = bytes32.fromhex("fe" * 32)


def finalized_offer(built: dict, requested: list) -> Offer:
    done = run({
        "action": "finalize",
        "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
        "coin_spends": built["coin_spends"],
        "requested": requested,
        "signature": "0x" + bytes(G2Element()).hex(),
    })
    return Offer.from_bech32(done["offer"])


xch_requested = [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 7_500}]
paid_xch = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "fee": 1_000,
    "offered": [{"asset_id": None, "amount": 970_000, "coins": [xch_coin(1_100_000, 0x51)],
                 "payments": [{"puzzle_hash": "0x" + FEE_PH.hex(), "amount": 30_000}]}],
    "requested": xch_requested,
})
check("an XCH leg with a payment builds", paid_xch.get("success") is True, str(paid_xch.get("error"))[:160])
if paid_xch.get("success"):
    made = [c for c in conditions_of(paid_xch["coin_spends"][0]) if int(c.first().as_int()) == 51]
    to_fee = [c for c in made if bytes(c.rest().first().as_atom()) == bytes(FEE_PH)]
    check("  the same spend pays the fee address exactly the payment", len(to_fee) == 1
          and int(to_fee[0].rest().rest().first().as_int()) == 30_000)
    check("  hinted to the fee address, so its wallet sees it", len(to_fee) == 1
          and [bytes(m.as_atom()) for m in to_fee[0].rest().rest().rest().first().as_iter()] == [bytes(FEE_PH)])
    to_change = [c for c in made if bytes(c.rest().first().as_atom()) == bytes(CHANGE_PH)]
    check("  change is what the coins leave after offer, payment and network fee",
          len(to_change) == 1 and int(to_change[0].rest().rest().first().as_int()) == 1_100_000 - 970_000 - 30_000 - 1_000)
    offer = finalized_offer(paid_xch, xch_requested)
    offered = {(a.hex() if a else "xch"): n for a, n in offer.get_offered_amounts().items()}
    check("  the offer still reads as the trade: it gives up the net amount, not the fee", offered == {"xch": 970_000}, str(offered))
    asserted = {bytes(c.rest().first().as_atom()) for c in conditions_of(paid_xch["coin_spends"][0]) if c.first().as_int() == 63}
    expected = {ann.msg_calc for ann in Offer.calculate_announcements(offer.requested_payments, offer.driver_dict)}
    check("  and the spend that pays the fee asserts the settlement: no swap, no fee", bool(expected) and expected <= asserted)

cat_requested = [{"asset_id": None, "amount": 50_000}]
paid_cat = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": "0x" + CAT_ASSET.hex(), "amount": 388, "coins": [cat_coin(1_000, 0x52)],
                 "payments": [{"puzzle_hash": "0x" + FEE_PH.hex(), "amount": 12}]}],
    "requested": cat_requested,
})
check("a CAT leg with a payment builds", paid_cat.get("success") is True, str(paid_cat.get("error"))[:160])
if paid_cat.get("success"):
    offer = finalized_offer(paid_cat, cat_requested)
    fee_cat_ph = CAT_MOD.curry(CAT_MOD.get_tree_hash(), CAT_ASSET, FEE_PH).get_tree_hash_precalc(FEE_PH)
    additions = offer._bundle.additions()
    fee_coins = [c for c in additions if c.puzzle_hash == fee_cat_ph]
    check("  the fee is paid in the CAT, to the fee address under the CAT layer",
          len(fee_coins) == 1 and int(fee_coins[0].amount) == 12, f"{[(c.puzzle_hash.hex()[:8], c.amount) for c in additions]}")
    check("  value is conserved: offered + payment + change = the coin",
          sum(int(c.amount) for c in additions) == 1_000, f"{sum(int(c.amount) for c in additions)}")
    offered = {(a.hex() if a else "xch"): n for a, n in offer.get_offered_amounts().items()}
    check("  the offer gives up the net CAT amount", offered == {CAT_ASSET.hex(): 388}, str(offered))

over = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": None, "amount": 970_000, "coins": [xch_coin(990_000, 0x53)],
                 "payments": [{"puzzle_hash": "0x" + FEE_PH.hex(), "amount": 30_000}]}],
    "requested": xch_requested,
})
check("coins that cover the offer but not the payment are refused", over.get("success") is False, str(over)[:120])
zero = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": None, "amount": 970_000, "coins": [xch_coin(1_100_000, 0x54)],
                 "payments": [{"puzzle_hash": "0x" + FEE_PH.hex(), "amount": 0}]}],
    "requested": xch_requested,
})
check("a zero payment is refused", zero.get("success") is False, str(zero)[:120])

print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    raise SystemExit(1)
print("ALL PASSED -- the app-side offer builder")
