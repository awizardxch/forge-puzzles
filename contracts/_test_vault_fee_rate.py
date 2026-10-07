"""A lock's fee is sized from the spend it pays for.

The lock panel used to hand every proposal the site's swap fee: 235M cost x 1.3
margin x 5 mojos = 0.0015275 XCH. The first mainnet lock send (2026-10-05) cost
41.9M on chain, so it paid 36 mojos per cost, about seven times what it needed.

With `fee_rate` the tool builds the plan, measures the cost the mempool will
charge (the same keys revealed, a 96-byte signature), and sets the fee to that
cost times the rate plus a 10% margin. This suite pins:

* the fee clears the rate on the bundle actually built, and not by much;
* the measured cost is the bundle's real cost, and the lock pays exactly the fee;
* a CAT-only send counts the XCH coin its own fee needs;
* a fixed fee is left alone, a pure re-key with nobody to pay costs nothing,
  and an offer takes no fee.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.lineage_proof import LineageProof as SingletonLineageProof
from chia_rs import AugSchemeMPL
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64
from chia.types.blockchain_format.coin import Coin

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


LAUNCHER = bytes32([0x7C] * 32)
CAT_A = bytes32([0xA1] * 32)
OWNER = AugSchemeMPL.key_gen(bytes([51] * 32)).get_g1()
POLICY = vault.Policy("fees", 1, (("me", OWNER),), vault.FORMAT_MIPS)
DEPOSIT = vault.deposit_puzzle(LAUNCHER)
DEPOSIT_PH = DEPOSIT.get_tree_hash()
CAT_OUTER = construct_cat_puzzle(CAT_MOD, CAT_A, DEPOSIT).get_tree_hash()

# The singleton asserts its parent, so the tip descends from the lineage's parent.
VAULT_PH = vault.vault_puzzle(LAUNCHER, POLICY).get_tree_hash()
LINEAGE_PARENT = bytes32([0x02] * 32)
TIP = Coin(Coin(LINEAGE_PARENT, VAULT_PH, uint64(1)).name(), VAULT_PH, uint64(1))
STATE = vault.VaultState(LAUNCHER, POLICY, TIP, SingletonLineageProof(LINEAGE_PARENT, POLICY.inner_puzzle_hash(), uint64(1)), height=9_386_700, spends=1)

XCH = Coin(bytes32([0x11] * 32), DEPOSIT_PH, uint64(100_000_000_000))
CAT_PARENT = Coin(bytes32([0x21] * 32), CAT_OUTER, uint64(9_000))
CAT_COIN = Coin(CAT_PARENT.name(), CAT_OUTER, uint64(9_000))
STRANGER = bytes32([0xEE] * 32)


def record(coin: Coin, spent: bool = False) -> dict:
    return {"coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(), "puzzle_hash": "0x" + coin.puzzle_hash.hex(), "amount": int(coin.amount)},
            "spent": spent, "spent_block_index": 5 if spent else 0, "confirmed_block_index": 4}


class FakeNode(tool.Node):
    def __init__(self) -> None:
        super().__init__("http://fake")

    def rpc(self, route: str, payload: dict) -> dict:
        if route == "get_coin_records_by_puzzle_hashes":
            wanted = {tool.strip0x(ph) for ph in payload["puzzle_hashes"]}
            records = []
            if DEPOSIT_PH.hex() in wanted:
                records.append(record(XCH))
            if CAT_OUTER.hex() in wanted:
                records.append(record(CAT_COIN))
            return {"success": True, "coin_records": records}
        if route == "get_coin_record_by_name":
            name = tool.strip0x(payload["name"])
            return {"success": True, "coin_record": record(CAT_PARENT, spent=True) if name == CAT_PARENT.name().hex() else None}
        if route == "get_puzzle_and_solution":
            puzzle = construct_cat_puzzle(CAT_MOD, CAT_A, DEPOSIT)
            return {"success": True, "coin_solution": {"puzzle_reveal": bytes(puzzle).hex(), "solution": "80"}}
        raise AssertionError(f"unexpected route {route}")


NODE = FakeNode()
vault.state_from_payload = lambda _node, _payload: STATE


def propose(**extra) -> dict:
    return vault.run("propose", {"network": "mainnet", "launcher_id": LAUNCHER.hex(), **extra}, lambda _url: NODE)


def lock_pays(plan: vault.VaultPlan) -> int:
    """XCH in minus XCH out across the bundle: the fee the farmer actually gets."""
    spends = plan.materialize(list(POLICY.keys)[: POLICY.m])
    xch_in = sum(int(s.coin.amount) for s in spends if s.coin.puzzle_hash == DEPOSIT_PH) + int(TIP.amount)
    xch_out = 0
    for spend in spends:
        if spend.coin.puzzle_hash in (DEPOSIT_PH, VAULT_PH):
            for cond in tool.conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, vault.MAX_CLVM_COST).get(vault.ConditionOpcode.CREATE_COIN, []):
                xch_out += int.from_bytes(cond.vars[1], "big")
    return xch_in - xch_out


send = [{"puzzle_hash": STRANGER.hex(), "amount": 50_000_000_000}]

# ─── an XCH send at 5 mojos per cost ─────────────────────────────────────────
auto = propose(outputs=send, fee_rate=5)
plan = vault.VaultPlan.from_json(auto["plan"])
cost = vault.plan_cost(plan, STATE.height)
check("the reported cost is the cost of the bundle built", auto["cost"], cost)
check("a 1-of-1 XCH send costs what the chain charged (41.9M), give or take 2%", abs(cost - 41_940_879) * 50 < 41_940_879, True)
check("the fee clears 5 mojos per cost on that bundle", auto["fee"] >= cost * 5, True)
check("and does not overshoot the 10% margin", auto["fee"] <= vault.sized_fee(cost, 5), True)
check("the lock pays exactly that fee", lock_pays(plan), auto["fee"])
check("about a seventh of the old flat fee", auto["fee"] * 5 < 1_527_500_000, True)

fast = propose(outputs=send, fee_rate=10)
check("twice the rate is twice the fee", abs(fast["fee"] - 2 * auto["fee"]) <= 2 * auto["fee"] // 100, True)

# ─── a CAT-only send: the fee needs an XCH coin, and the cost counts it ──────
cat = propose(outputs=[{"puzzle_hash": STRANGER.hex(), "amount": 500, "asset_id": CAT_A.hex()}], fee_rate=5)
cat_plan = vault.VaultPlan.from_json(cat["plan"])
check("a CAT send with a fee spends an XCH coin for it", any(f.kind == "xch" for f in cat_plan.funds), True)
check("and its fee covers that coin's cost too", cat["fee"] >= vault.plan_cost(cat_plan, STATE.height) * 5, True)
check("a CAT send costs more than an XCH send", cat["cost"] > auto["cost"], True)

# ─── what fee_rate leaves alone ──────────────────────────────────────────────
fixed = propose(outputs=send, fee=1_527_500_000)
check("a fixed fee is kept as given", fixed["fee"], 1_527_500_000)
check("and still reports the cost, for showing the rate", isinstance(fixed["cost"], int) and fixed["cost"] > 0, True)
check("a fixed fee is what the lock pays", lock_pays(vault.VaultPlan.from_json(fixed["plan"])), 1_527_500_000)

other = AugSchemeMPL.key_gen(bytes([52] * 32)).get_g1()
rekey = propose(successor={"name": "fees", "m": 1, "owners": [{"label": "new", "pubkey": bytes(other).hex()}]}, fee_rate=5)
check("a pure re-key with nobody to sponsor it pays no fee", rekey["fee"], 0)


# ─── the pusher's fee: the lock's spends plus the fee spend, measured together ─
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk

WALLET_PUZZLE = puzzle_for_pk(AugSchemeMPL.key_gen(bytes([53] * 32)).get_g1())


def wallet_coin(amount: int, salt: int = 3) -> dict:
    coin = Coin(bytes32([salt] * 32), WALLET_PUZZLE.get_tree_hash(), uint64(amount))
    return {"coin": {"parent_coin_info": coin.parent_coin_info.hex(), "puzzle_hash": coin.puzzle_hash.hex(), "amount": int(coin.amount)},
            "puzzle": bytes(WALLET_PUZZLE).hex()}


unfunded = propose(outputs=send)               # fee 0: whoever pushes pays
check("a proposal without a fee carries none", unfunded["fee"], 0)
pushed = vault.run("fee-spend", {"plan": unfunded["plan"], "coins": [wallet_coin(10_000_000_000)], "fee_rate": 5}, lambda _url: NODE)
lock_spends = vault.VaultPlan.from_json(unfunded["plan"]).materialize(list(POLICY.keys)[: POLICY.m])
whole = vault.bundle_cost(lock_spends + [vault.spend_from_json(s) for s in pushed["coin_spends"]])
check("the push fee is sized on the lock's spends plus the fee spend", pushed["cost"], whole)
check("it clears 5 mojos per cost on that whole bundle", pushed["fee"] >= whole * 5, True)
check("and stays within the margin", pushed["fee"] <= vault.sized_fee(whole, 5), True)
check("the fee spend reserves exactly that fee", any(
    int.from_bytes(c.vars[0], "big") == pushed["fee"]
    for s in pushed["coin_spends"]
    for c in tool.conditions_dict_for_solution(vault.spend_from_json(s).puzzle_reveal, vault.spend_from_json(s).solution, vault.MAX_CLVM_COST).get(vault.ConditionOpcode.RESERVE_FEE, [])
), True)
fixed_push = vault.run("fee-spend", {"plan": unfunded["plan"], "coins": [wallet_coin(10_000_000_000)], "fee": 1_000}, lambda _url: NODE)
check("a fixed push fee is kept as given", fixed_push["fee"], 1_000)

# ─── a launch: the creator's wallet spend plus the launcher ──────────────────
launch = vault.run("launch", {"network": "mainnet", "policy": {"name": "fees", "m": 1, "owners": [{"label": "me", "pubkey": bytes(OWNER).hex()}]},
                              "coins": [wallet_coin(1_000_000_000, salt=4)], "fee_rate": 5}, lambda _url: NODE)
launch_cost = vault.bundle_cost([vault.spend_from_json(s) for s in launch["coin_spends"]])
check("a launch fee is sized on the launch bundle", launch["cost"], launch_cost)
check("and clears 5 mojos per cost on it", launch["fee"] >= launch_cost * 5, True)
check("a launch costs less than a pool swap's 235M", launch_cost < 235_000_000, True)


def refused(**extra) -> bool:
    try:
        propose(**extra)
    except tool.MultisigError:
        return True
    return False


check("a rate of 0 is refused", refused(outputs=send, fee_rate=0), True)
check("a rate above 1000 is refused", refused(outputs=send, fee_rate=1_001), True)

print(f"vault fee rate: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
