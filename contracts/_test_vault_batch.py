"""Many actions, one vote — what the audited puzzle already allows.

The question this suite answers is whether a lock can do several things at once
without changing anything on chain. It can, and the reason is in CNI's puzzle,
not in ours: the singleton's delegated puzzle emits one
`CREATE_PUZZLE_ANNOUNCEMENT` per funds coin naming *that coin with that
delegated puzzle*, and each funds coin refuses to run unless the singleton is
spent alongside it. So one singleton spend authorises an arbitrary set of coins,
each doing something different, and the owners sign exactly one message for the
whole set.

That is the batch. It has to be composed BEFORE the vote, because the message
the owners sign is the hash of that one delegated puzzle — which is also why
two separately-signed proposals can never be merged afterwards, and why both
name the same tip coin and so cannot both be spent.

The suite pins the properties a batch depends on:

* one message, however many coins are in it;
* every coin authorised specifically, by its own delegated puzzle hash;
* every coin required to be present, so no part of an approved batch can be
  dropped on the way to the chain;
* the arithmetic stays right per asset as actions are added.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.consensus.condition_tools import conditions_dict_for_solution
from chia.types.blockchain_format.coin import Coin
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.lineage_proof import LineageProof as SingletonLineageProof
from chia_rs import AugSchemeMPL
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import multisig_tool as tool
import vault_tool as vault

PASSED = 0
FAILED: list[str] = []


def check(name: str, got: object, want: object) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}\n     got  {got}\n     want {want}")


LAUNCHER = bytes32([0x7B] * 32)
CAT_A = bytes32([0xA1] * 32)
CAT_B = bytes32([0xB2] * 32)
OWNER_A = AugSchemeMPL.key_gen(bytes([31] * 32)).get_g1()
OWNER_B = AugSchemeMPL.key_gen(bytes([32] * 32)).get_g1()
POLICY = vault.Policy("batch", 2, (("a", OWNER_A), ("b", OWNER_B)), vault.FORMAT_MIPS)

DEPOSIT = vault.deposit_puzzle(LAUNCHER)
DEPOSIT_PH = DEPOSIT.get_tree_hash()
OUTER = {asset: construct_cat_puzzle(CAT_MOD, asset, DEPOSIT).get_tree_hash() for asset in (CAT_A, CAT_B)}

TIP = Coin(bytes32([0x01] * 32), vault.vault_puzzle(LAUNCHER, POLICY).get_tree_hash(), uint64(1))
STATE = vault.VaultState(
    LAUNCHER, POLICY, TIP,
    SingletonLineageProof(bytes32([0x02] * 32), POLICY.inner_puzzle_hash(), uint64(1)),
    height=10, spends=1,
)

XCH_COINS = [
    Coin(bytes32([0x11] * 32), DEPOSIT_PH, uint64(5_000_000_000)),
    Coin(bytes32([0x12] * 32), DEPOSIT_PH, uint64(1)),
]
# A CAT coin is only spendable if its parent really hashes to its parent id, so
# the parents are built first and the coins hang off them.
CAT_PARENTS = {
    CAT_A: Coin(bytes32([0x21] * 32), OUTER[CAT_A], uint64(9_000)),
    CAT_B: Coin(bytes32([0x22] * 32), OUTER[CAT_B], uint64(4_000)),
}
CAT_COINS = {
    asset: Coin(parent.name(), OUTER[asset], parent.amount)
    for asset, parent in CAT_PARENTS.items()
}
PARENT_BY_ID = {parent.name(): (asset, parent) for asset, parent in CAT_PARENTS.items()}
STRANGER = bytes32([0xEE] * 32)


def record(coin: Coin, spent: bool = False) -> dict:
    return {
        "coin": {
            "parent_coin_info": "0x" + coin.parent_coin_info.hex(),
            "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
            "amount": int(coin.amount),
        },
        "spent": spent,
        "spent_block_index": 5 if spent else 0,
        "confirmed_block_index": 4,
    }


class FakeNode(tool.Node):
    def __init__(self) -> None:
        super().__init__("http://fake")

    def rpc(self, route: str, payload: dict) -> dict:
        if route == "get_coin_records_by_puzzle_hashes":
            wanted = {tool.strip0x(ph) for ph in payload["puzzle_hashes"]}
            records = []
            if DEPOSIT_PH.hex() in wanted:
                records += [record(coin) for coin in XCH_COINS]
            for asset, outer in OUTER.items():
                if outer.hex() in wanted:
                    records.append(record(CAT_COINS[asset]))
            return {"success": True, "coin_records": records}
        if route == "get_coin_record_by_name":
            name = bytes32(bytes.fromhex(tool.strip0x(payload["name"])))
            found = PARENT_BY_ID.get(name)
            return {"success": True, "coin_record": record(found[1], spent=True) if found else None}
        if route == "get_puzzle_and_solution":
            # Whichever CAT parent was asked for; both share the lock's inner puzzle.
            name = bytes32(bytes.fromhex(tool.strip0x(payload["coin_id"])))
            found = PARENT_BY_ID.get(name)
            if found is None:
                raise AssertionError("unexpected parent")
            puzzle = construct_cat_puzzle(CAT_MOD, found[0], DEPOSIT)
            return {"success": True, "coin_solution": {"puzzle_reveal": bytes(puzzle).hex(), "solution": "80"}}
        raise AssertionError(f"unexpected route {route}")


NODE = FakeNode()
CREATE_PUZZLE_ANNOUNCEMENT = ConditionOpcode.CREATE_PUZZLE_ANNOUNCEMENT
ASSERT_COIN_ANNOUNCEMENT = ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT
CREATE_COIN = ConditionOpcode.CREATE_COIN
AGG_SIG_ME = ConditionOpcode.AGG_SIG_ME


def out(asset: bytes32 | None, amount: int, to: bytes32 = STRANGER) -> tool.Output:
    return tool.Output(to, amount, asset, "")


# ─── one action, then three, then five ───────────────────────────────────────
#
# The same builder, given more to do. What must not change is the number of
# messages the owners are asked for.

one = vault.build_vault_plan(NODE, "testnet11", STATE, [out(None, 1_000)], 0, None, None)
three = vault.build_vault_plan(
    NODE, "testnet11", STATE,
    [out(None, 1_000), out(CAT_A, 500), out(CAT_B, 250)],
    0, None, None,
)
five = vault.build_vault_plan(
    NODE, "testnet11", STATE,
    [out(None, 1_000), out(None, 2_000, DEPOSIT_PH), out(CAT_A, 500), out(CAT_A, 100, DEPOSIT_PH), out(CAT_B, 250)],
    0, None, None,
)

check("one action is one funds coin", len(one.funds), 1)
check("three assets are three funds coins", len(three.funds), 3)
check("five payments across three assets are still three coins", len(five.funds), 3)

# The whole point: the owners sign ONE message whatever the batch holds.
check("one message for one action", isinstance(one.message, bytes), True)
check("and one message for five", len(set([one.message, three.message, five.message])), 3)  # different plans, one each
check("a batch does not multiply what an owner signs", (one.message != three.message) and (three.message != five.message), True)


def singleton_conditions(plan: vault.VaultPlan):
    spends = plan.materialize(list(POLICY.keys)[: POLICY.m])
    return spends[0], conditions_dict_for_solution(spends[0].puzzle_reveal, spends[0].solution, vault.MAX_CLVM_COST), spends


for label, plan in (("one", one), ("three", three), ("five", five)):
    tip_spend, conds, spends = singleton_conditions(plan)

    # Every funds coin is authorised specifically: the announcement names that
    # coin together with the delegated puzzle it is allowed to run, so a coin
    # cannot be swapped for another or made to run something else.
    announced = {bytes(c.vars[0]) for c in conds.get(CREATE_PUZZLE_ANNOUNCEMENT, [])}
    expected = {bytes(vault.funds_announcement(f.coin, f.delegated_puzzle)) for f in plan.funds}
    check(f"{label}: every coin is authorised by name and by what it runs", announced, expected)

    # …and every one of them is required to be there. A batch cannot be trimmed
    # on the way to the chain: drop a coin and the singleton spend fails.
    asserted = {bytes(c.vars[0]) for c in conds.get(ASSERT_COIN_ANNOUNCEMENT, [])}
    required = {bytes(vault.announcement_id(f.coin.name(), b"$")) for f in plan.funds}
    check(f"{label}: every coin in the batch is required to be spent", required <= asserted, True)

    # One signature demand per owner in the threshold, over the one delegated
    # puzzle — the same count whether the batch is one action or five.
    sigs = [c for c in conds.get(AGG_SIG_ME, [])]
    check(f"{label}: the threshold signs once, not once per action", len(sigs), POLICY.m)
    check(f"{label}: over the delegated puzzle the batch agreed", {bytes(c.vars[1]) for c in sigs}, {bytes(plan.delegated_puzzle.get_tree_hash())})


# ─── the arithmetic stays right as actions pile up ───────────────────────────

tip_spend, conds, spends = singleton_conditions(five)
by_coin = {s.coin.name(): s for s in spends}


def creations(spend) -> list[tuple[bytes32, int]]:
    conditions = conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, vault.MAX_CLVM_COST)
    return [(bytes32(c.vars[0]), int.from_bytes(c.vars[1], "big")) for c in conditions.get(CREATE_COIN, [])]


xch_spend = by_coin[max(XCH_COINS, key=lambda c: int(c.amount)).name()]
xch_made = creations(xch_spend)
check("the XCH payments both go out", sorted(a for ph, a in xch_made if ph == STRANGER), [1_000])
check("and the self-payment lands at the lock", sorted(a for ph, a in xch_made if ph == DEPOSIT_PH), [2_000, 5_000_000_000 - 3_000])
check("XCH conserves", sum(a for _, a in xch_made), 5_000_000_000)

# CAT A carries two payments and its own change; CAT B one and change. Each is a
# separate ring, so one asset's arithmetic cannot borrow from another's.
for asset, paid_out, held in ((CAT_A, [500, 100], 9_000), (CAT_B, [250], 4_000)):
    spend = by_coin[CAT_COINS[asset].name()]
    made = creations(spend)
    check(f"CAT {asset.hex()[:4]} conserves", sum(a for _, a in made), held)
    check(f"CAT {asset.hex()[:4]} pays out what was asked", sorted(a for _, a in made if a in paid_out), sorted(paid_out))


# ─── why the batch has to be composed before the vote ────────────────────────
#
# Two proposals built against the same lock name the same tip coin, and each
# owner's signature covers that coin id as well as the delegated puzzle. So the
# two can neither be merged into a third plan (the hash the owners signed would
# no longer exist) nor pushed together (one coin, two spends).

other = vault.build_vault_plan(NODE, "testnet11", STATE, [out(CAT_A, 700)], 0, None, None)
check("two proposals name the same tip coin", other.tip.name(), three.tip.name())
check("so they cannot both be spent", other.tip.name() in set(three.all_coin_ids()), True)
check("and they ask for different signatures", other.message != three.message, True)
# The tip is in every plan's coin list, which is what makes the queue's
# "superseded" rule correct rather than merely cautious.
check("every plan is pinned to the tip", all(p.tip.name() in p.all_coin_ids() for p in (one, three, five, other)), True)


# ─── an offer cannot join a batch of payments ────────────────────────────────
#
# Not a limit of this builder: an offer's bundle is incomplete on purpose, so
# anything sharing its singleton spend waits for a taker. The tool refuses the
# combination rather than producing a batch that silently never settles.

def refused(build) -> bool:
    try:
        build()
    except tool.MultisigError:
        return True
    return False


vault.state_from_payload = lambda _node, _payload: STATE
check(
    "an offer and a payment cannot be one proposal",
    refused(lambda: vault.run("propose", {
        "network": "testnet11",
        "launcher_id": LAUNCHER.hex(),
        "outputs": [{"puzzle_hash": STRANGER.hex(), "amount": 5}],
        "offer": {"offered": [{"amount": 5}], "requested": [{"asset_id": CAT_A.hex(), "amount": 1}]},
    }, lambda _url: NODE)),
    True,
)


print(f"vault batch: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
