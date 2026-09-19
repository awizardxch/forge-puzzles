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

print("\nbad input is refused, not guessed at:")
short = run({
    "action": "build",
    "change_puzzle_hash": "0x" + CHANGE_PH.hex(),
    "offered": [{"asset_id": None, "amount": 2_000_000, "coins": [xch_coin(1_000_000, 0x15)]}],
    "requested": [{"asset_id": None, "amount": 1}],
})
check("coins that cannot cover the offer are refused", short.get("success") is False, str(short)[:120])

print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    raise SystemExit(1)
print("ALL PASSED -- the app-side offer builder")
