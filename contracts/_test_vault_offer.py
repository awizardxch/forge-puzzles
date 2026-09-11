"""A lock that writes its own offer.

An offer is the one thing a lock does that is not finished when the owners have
signed it: half the transaction is missing on purpose, and the half that exists
has to be exactly right or the file is worthless -- takeable for the wrong
amount, replayable against other coins, or simply unparseable.

So this suite does not check hashes against expectations of my own. It builds the
offer, hands it to `chia.wallet.trading.offer.Offer` -- the same class Sage,
Dexie and the router use to read one -- and asks that class what it sees. If what
it reports is not what the lock meant to say, the offer is wrong however good the
intermediate values looked.

The two properties that carry the weight:

* the nonce is the tree hash of exactly the coins being given up, so the file
  cannot be pointed at a different set;
* the announcements are asserted by the spend that gives up the asset, so
  spending that asset elsewhere takes the offer down with it.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.util.bech32m import encode_puzzle_hash
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, SpendableCAT, construct_cat_puzzle, unsigned_spend_bundle_for_spendable_cats
from chia.wallet.conditions import CreateCoin
from chia.wallet.lineage_proof import LineageProof as SingletonLineageProof
from chia.wallet.trading.offer import OFFER_MOD_HASH, Offer
from chia_rs import AugSchemeMPL, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import multisig_tool as tool
import vault_tool as vault
import vault_offer as offers

PASSED = 0
FAILED: list[str] = []


def check(name: str, got: object, want: object) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}\n     got  {got}\n     want {want}")


def refused(build) -> str:
    try:
        build()
    except tool.MultisigError as exc:
        return f"refused: {exc}"
    return "accepted"


# ─── a lock with coins, and no chain ─────────────────────────────────────────

LAUNCHER = bytes32([0x5A] * 32)
CAT_TAIL = bytes32([0xC1] * 32)
OWNER_A = AugSchemeMPL.key_gen(bytes([21] * 32)).get_g1()
OWNER_B = AugSchemeMPL.key_gen(bytes([22] * 32)).get_g1()
POLICY = vault.Policy("test", 1, (("a", OWNER_A), ("b", OWNER_B)), vault.FORMAT_MIPS)

DEPOSIT = vault.deposit_puzzle(LAUNCHER)
DEPOSIT_PH = DEPOSIT.get_tree_hash()
CAT_OUTER_PH = construct_cat_puzzle(CAT_MOD, CAT_TAIL, DEPOSIT).get_tree_hash()

TIP = Coin(bytes32([0x01] * 32), vault.vault_puzzle(LAUNCHER, POLICY).get_tree_hash(), uint64(1))
STATE = vault.VaultState(
    LAUNCHER, POLICY, TIP,
    SingletonLineageProof(bytes32([0x02] * 32), POLICY.inner_puzzle_hash(), uint64(1)),
    height=100, spends=1,
)

XCH_COINS = [
    Coin(bytes32([0x11] * 32), DEPOSIT_PH, uint64(10_000_000_000)),
    Coin(bytes32([0x12] * 32), DEPOSIT_PH, uint64(1)),
]
CAT_PARENT = Coin(bytes32([0x20] * 32), CAT_OUTER_PH, uint64(5_000))
CAT_COIN = Coin(CAT_PARENT.name(), CAT_OUTER_PH, uint64(5_000))


def record(coin: Coin, spent: bool = False) -> dict:
    return {
        "coin": {
            "parent_coin_info": "0x" + coin.parent_coin_info.hex(),
            "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
            "amount": int(coin.amount),
        },
        "spent": spent,
        "spent_block_index": 90 if spent else 0,
        "confirmed_block_index": 80,
    }


class FakeNode(tool.Node):
    """Answers the three questions a plan asks, from a dict."""

    def __init__(self) -> None:
        super().__init__("http://fake")

    def rpc(self, route: str, payload: dict) -> dict:
        if route == "get_coin_records_by_puzzle_hashes":
            wanted = {tool.strip0x(ph) for ph in payload["puzzle_hashes"]}
            records = []
            if DEPOSIT_PH.hex() in wanted:
                records += [record(coin) for coin in XCH_COINS]
            if CAT_OUTER_PH.hex() in wanted:
                records.append(record(CAT_COIN))
            return {"success": True, "coin_records": records}
        if route == "get_coin_record_by_name":
            # The CAT's parent, for its lineage proof.
            return {"success": True, "coin_record": record(CAT_PARENT, spent=True)}
        if route == "get_puzzle_and_solution":
            # A CAT parent whose inner puzzle is the lock's deposit puzzle, which
            # is what makes the lineage proof check out.
            puzzle = construct_cat_puzzle(CAT_MOD, CAT_TAIL, DEPOSIT)
            return {"success": True, "coin_solution": {"puzzle_reveal": bytes(puzzle).hex(), "solution": "80"}}
        raise AssertionError(f"unexpected route {route}")


NODE = FakeNode()
ASSERT_PUZZLE_ANNOUNCEMENT = ConditionOpcode.ASSERT_PUZZLE_ANNOUNCEMENT
CREATE_COIN = ConditionOpcode.CREATE_COIN


def signed(plan: vault.VaultPlan) -> tuple[list, G2Element]:
    """The plan as a real signed bundle, one owner being enough here."""
    secret = AugSchemeMPL.key_gen(bytes([21] * 32))
    spends = plan.materialize([OWNER_A])
    return spends, AugSchemeMPL.sign(secret, plan.message)


# ─── XCH out, CAT in ─────────────────────────────────────────────────────────

plan = offers.build_offer_plan(NODE, "testnet11", STATE, [(None, 1_000_000_000)], [(CAT_TAIL, 250, None)])
spends, signature = signed(plan)
offer = offers.assemble_offer(plan, spends, signature)

check("the offer gives up the XCH it said", offer.get_offered_amounts(), {None: 1_000_000_000})
check("and asks for the CAT it said", {k: sum(p.amount for p in v) for k, v in offer.get_requested_payments().items()}, {CAT_TAIL: 250})
requested = offer.get_requested_payments()[CAT_TAIL][0]
check("payment goes back to the lock", requested.puzzle_hash, DEPOSIT_PH)
check("and is hinted so the lock can find it", [bytes(m) for m in requested.memos], [bytes(DEPOSIT_PH)])

# The nonce is the tree hash of the coins being given up -- the thing that stops
# a signed offer being pointed at a different set of coins. Which coins those are
# is the selector's business (largest first, only as many as the amount needs),
# so the check is that the two agree: the coins the plan spends are the coins the
# nonce names.
def nonce_over(coins: list[Coin]) -> bytes32:
    ordered = sorted(coins, key=Coin.name)
    return Program.to([[c.parent_coin_info, c.puzzle_hash, c.amount] for c in ordered]).get_tree_hash()


chosen = tool.select_coins(XCH_COINS, 1_000_000_000, "XCH")
spent_xch = [f.coin for f in plan.funds if f.kind == "xch"]
check("one coin covers this offer", len(chosen), 1)
check("and it is the coin the plan spends", sorted(c.name() for c in spent_xch), sorted(c.name() for c in chosen))
check("the nonce names exactly those coins", requested.nonce, nonce_over(chosen))

# Two coins when one will not do, and the nonce grows to match: an offer big
# enough to need the whole balance must not be notarized against half of it.
whole = offers.build_offer_plan(NODE, "testnet11", STATE, [(None, 10_000_000_001)], [(CAT_TAIL, 9, None)])
whole_payment = list(offers.payments_from_summary(whole.summary).values())[0][0]
check("a bigger offer takes both coins", len([f for f in whole.funds if f.kind == "xch"]), 2)
check("and the nonce covers both", whole_payment.nonce, nonce_over(XCH_COINS))
check("the two nonces differ", whole_payment.nonce != requested.nonce, True)

# It round-trips as a file: this is what gets handed to a router or a wallet.
text = offer.to_bech32()
check("it encodes as an offer file", text.startswith("offer1"), True)
reread = Offer.from_bech32(text)
check("and reads back with the same offer", reread.get_offered_amounts(), {None: 1_000_000_000})
check("and the same request", {k: sum(p.amount for p in v) for k, v in reread.get_requested_payments().items()}, {CAT_TAIL: 250})
check("and the same name", reread.name(), offer.name())

# The change comes home. An offer that quietly burned the rest of the coin would
# still parse, so this is worth asking of the plan itself.
change = [entry for entry in plan.summary["change"] if entry["asset_id"] is None]
check("the rest of the XCH returns to the lock", change, [{"asset_id": None, "amount": 10_000_000_000 - 1_000_000_000}])


# ─── the announcements sit on the spend that gives up the asset ──────────────

by_coin = {spend.coin.name(): spend for spend in spends}
announced = offers.Offer.calculate_announcements(offers.payments_from_summary(plan.summary), offers.driver_dict([[(None, 1)], [(CAT_TAIL, 1)]]))
wanted_msgs = {bytes(a.msg_calc) for a in announced}

carrier = by_coin[max(XCH_COINS, key=lambda c: int(c.amount)).name()]
conditions = conditions_dict_for_solution(carrier.puzzle_reveal, carrier.solution, vault.MAX_CLVM_COST)
asserted = {bytes(c.vars[0]) for c in conditions.get(ASSERT_PUZZLE_ANNOUNCEMENT, [])}
check("the offered coin asserts what is wanted back", wanted_msgs <= asserted, True)
check("there is exactly one announcement to assert", len(wanted_msgs), 1)

# Every other spend stays clean, so no part of the demand can survive without
# the coin that is being given up.
others = [s for name, s in by_coin.items() if name != carrier.coin.name()]
loose = [
    bytes(c.vars[0])
    for s in others
    for c in conditions_dict_for_solution(s.puzzle_reveal, s.solution, vault.MAX_CLVM_COST).get(ASSERT_PUZZLE_ANNOUNCEMENT, [])
    if bytes(c.vars[0]) in wanted_msgs
]
check("no other spend carries the demand", loose, [])

# The settlement coin is created by the offered spend, unhinted.
creates = conditions_dict_for_solution(carrier.puzzle_reveal, carrier.solution, vault.MAX_CLVM_COST).get(CREATE_COIN, [])
settlement = [c for c in creates if bytes(c.vars[0]) == bytes(OFFER_MOD_HASH)]
check("one settlement coin is created", len(settlement), 1)
check("for the offered amount", int.from_bytes(settlement[0].vars[1], "big"), 1_000_000_000)
check("with no hint on it", len(settlement[0].vars), 2)


# ─── CAT out, XCH in ─────────────────────────────────────────────────────────

cat_plan = offers.build_offer_plan(NODE, "testnet11", STATE, [(CAT_TAIL, 1_000)], [(None, 700_000_000, None)])
cat_spends, cat_signature = signed(cat_plan)
cat_offer = offers.assemble_offer(cat_plan, cat_spends, cat_signature)

check("the CAT side is recognised as offered", cat_offer.get_offered_amounts(), {CAT_TAIL: 1_000})
check("and XCH is what comes back", {k: sum(p.amount for p in v) for k, v in cat_offer.get_requested_payments().items()}, {None: 700_000_000})
xch_payment = cat_offer.get_requested_payments()[None][0]
check("XCH comes back to the lock", xch_payment.puzzle_hash, DEPOSIT_PH)
check("and carries no memo", [bytes(m) for m in (xch_payment.memos or [])], [])
check("the CAT offer is a file too", cat_offer.to_bech32().startswith("offer1"), True)
check("it reads back the same", Offer.from_bech32(cat_offer.to_bech32()).get_offered_amounts(), {CAT_TAIL: 1_000})

# Offering a CAT must not drag an XCH coin in for no reason: the announcements
# belong on the CAT spend, and the lock's XCH is left alone.
kinds = sorted({f.kind for f in cat_plan.funds})
check("only the CAT is spent", kinds, ["cat"])
check("the CAT change returns to the lock", [e for e in cat_plan.summary["change"] if e["asset_id"]], [{"asset_id": CAT_TAIL.hex(), "amount": 4_000}])


# ─── what the plan refuses ───────────────────────────────────────────────────

check(
    "an offer of an asset it also wants back is refused",
    refused(lambda: offers.build_offer_plan(NODE, "testnet11", STATE, [(None, 1)], [(None, 2, None)])).startswith("refused"),
    True,
)
check(
    "offering more than the lock holds is refused",
    refused(lambda: offers.build_offer_plan(NODE, "testnet11", STATE, [(None, 99_000_000_000)], [(CAT_TAIL, 1, None)])).startswith("refused"),
    True,
)
check(
    "offering a CAT the lock does not hold is refused",
    refused(lambda: offers.build_offer_plan(NODE, "testnet11", STATE, [(bytes32([0xEE] * 32), 5)], [(None, 1, None)])).startswith("refused"),
    True,
)

check("a side with no assets is refused", refused(lambda: offers.parse_side([], "offered")).startswith("refused"), True)
check("a zero amount is refused", refused(lambda: offers.parse_side([{"amount": 0}], "offered")).startswith("refused"), True)
check("the same asset twice is refused", refused(lambda: offers.parse_side([{"amount": 1}, {"amount": 2}], "offered")).startswith("refused"), True)
check("XCH parses as a null asset", offers.parse_side([{"amount": 5}], "offered"), [(None, 5)])
check("a CAT parses by id", offers.parse_side([{"asset_id": CAT_TAIL.hex(), "amount": 5}], "offered"), [(CAT_TAIL, 5)])


# ─── the vote is unchanged ───────────────────────────────────────────────────
#
# The whole design rests on an offer being an ordinary vault plan: same singleton
# spend, same owner signature, same coin-bound message. If any of that drifted,
# an offer would need its own signing path and would not be safe to treat like
# every other proposal.

check("one signature is asked for, as with any proposal", vault.validate_vault_bundle(plan, spends, signature)["agg_sig_count"], 1)
check("the message is bound to this lock's tip", plan.message.endswith(vault.AGG_SIG_ME_DATA["testnet11"]), True)
check("the tip is in the bundle", TIP.name() in {s.coin.name() for s in spends}, True)
check("the plan is marked as an offer", plan.summary["kind"], "offer")
check("no fee is taken from the lock", plan.summary["fee"], 0)

# An offer must never be pushed: its announcements are unsatisfied by design.
# Nothing here pushes, and the assembler returns an Offer rather than a bundle,
# which is the shape that keeps it that way.
check("assembling gives an offer, not a transaction", isinstance(offer, Offer), True)


# ─── a route's fee is a leg of the same offer ────────────────────────────────
#
# Forge's router takes its cut inside the offer: one more requested payment, of
# the same asset, to somewhere that is not the lock. An offer that quietly sent
# that leg home would be correct-looking and untakeable, so both legs are pinned.

DEV = bytes32([0xDE] * 32)
fee_plan = offers.build_offer_plan(
    NODE, "testnet11", STATE,
    [(None, 2_000_000_000)],
    [(CAT_TAIL, 500, None), (CAT_TAIL, 5, DEV)],
)
fee_spends, fee_signature = signed(fee_plan)
fee_offer = offers.assemble_offer(fee_plan, fee_spends, fee_signature)
legs = fee_offer.get_requested_payments()[CAT_TAIL]
check("both legs are requested", len(legs), 2)
check("the lock's leg comes home", sorted(p.amount for p in legs if p.puzzle_hash == DEPOSIT_PH), [500])
check("the fee leg goes where it was told", sorted(p.amount for p in legs if p.puzzle_hash == DEV), [5])
check("each leg is hinted to its own destination", sorted(bytes(p.memos[0]).hex() for p in legs), sorted([DEPOSIT_PH.hex(), DEV.hex()]))
check("both share one nonce", len({p.nonce for p in legs}), 1)
check("the file carries both", len(Offer.from_bech32(fee_offer.to_bech32()).get_requested_payments()[CAT_TAIL]), 2)
check(
    "the same asset to the same address twice is refused",
    refused(lambda: offers.parse_requested([{"asset_id": CAT_TAIL.hex(), "amount": 1}, {"asset_id": CAT_TAIL.hex(), "amount": 2}])).startswith("refused"),
    True,
)
check(
    "but to two addresses it is two payments",
    len(offers.parse_requested([
        {"asset_id": CAT_TAIL.hex(), "amount": 1},
        {"asset_id": CAT_TAIL.hex(), "amount": 2, "puzzle_hash": DEV.hex()},
    ])),
    2,
)

# ─── through the command surface ─────────────────────────────────────────────
#
# The API talks to this tool in JSON, so the commands are what actually ships.
# `read_vault` walks the chain, which the fake node here cannot do, so the state
# is handed in directly and only the branch above it is exercised.

vault.state_from_payload = lambda _node, _payload: STATE


def run(command: str, payload: dict) -> dict:
    return vault.run(command, {"network": "testnet11", "launcher_id": LAUNCHER.hex(), **payload}, lambda _url: NODE)


proposed = run("propose", {"offer": {
    "offered": [{"amount": 1_000_000_000}],
    "requested": [{"asset_id": CAT_TAIL.hex(), "amount": 250}],
}})
check("propose builds an offer plan", proposed["plan"]["summary"]["kind"], "offer")
check("and reports one message to sign", len(proposed["messages"]), 1)
check("and names the coins it is pinned to", len(proposed["coin_ids"]) >= 2, True)

rebuilt = vault.VaultPlan.from_json(proposed["plan"])
share_sig = AugSchemeMPL.sign(AugSchemeMPL.key_gen(bytes([21] * 32)), rebuilt.message)
shares = [{"keys": [vault.pubkey_hex(OWNER_A)], "signature": bytes(share_sig).hex()}]

assembled = run("assemble", {"plan": proposed["plan"], "shares": shares})
check("assemble returns an offer file", assembled["offer"]["text"].startswith("offer1"), True)
check("naming what it gives up", assembled["offer"]["offered"], {"xch": 1_000_000_000})
check("and what it wants back", assembled["offer"]["requested"], {CAT_TAIL.hex(): 250})
check("the same file reads back", Offer.from_bech32(assembled["offer"]["text"]).name().hex(), assembled["offer"]["id"])

check(
    "an offer cannot be pushed",
    refused(lambda: run("assemble", {"plan": proposed["plan"], "shares": shares, "push": True})).startswith("refused"),
    True,
)
check(
    "an offer cannot carry a fee",
    refused(lambda: run("propose", {"fee": 1000, "offer": {"offered": [{"amount": 5}], "requested": [{"asset_id": CAT_TAIL.hex(), "amount": 1}]}})).startswith("refused"),
    True,
)
check(
    "an offer cannot also be a payment",
    refused(lambda: run("propose", {
        "outputs": [{"puzzle_hash": DEPOSIT_PH.hex(), "amount": 5}],
        "offer": {"offered": [{"amount": 5}], "requested": [{"asset_id": CAT_TAIL.hex(), "amount": 1}]},
    })).startswith("refused"),
    True,
)


print(f"vault offer: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
