"""Profile and manifest publishes pay a fee sized from their own spend.

A profile record or a lock's manifest is one small wallet spend creating 1-mojo
record coins. It used to pay the fee the tier sizes for a pool swap (0.0015275
XCH at Standard). With `fee_rate` the builder measures the spend it built and
sizes the fee from that (multisig_profile.sized, the same settle loop as lock
spends). This suite pins: the reported cost is the bundle's, the fee clears the
rate within the margin, the wallet pays exactly that fee, and a fixed fee is
left alone.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk
from chia_rs import AugSchemeMPL
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import multisig_profile as profile
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


KEY = AugSchemeMPL.key_gen(bytes([81] * 32)).get_g1()
WALLET = puzzle_for_pk(KEY)
COIN = Coin(bytes32([0x09] * 32), WALLET.get_tree_hash(), uint64(10_000_000_000))
COINS = [{"coin": {"parent_coin_info": COIN.parent_coin_info.hex(), "puzzle_hash": COIN.puzzle_hash.hex(), "amount": int(COIN.amount)},
          "puzzle": bytes(WALLET).hex()}]
OWNER = AugSchemeMPL.key_gen(bytes([82] * 32)).get_g1()
POLICY = {"name": "aWizard", "m": 1, "owners": [{"label": "Me", "pubkey": bytes(OWNER).hex()}]}


def paid(result: dict) -> int:
    """XCH the wallet coin gives up beyond the coins it creates: the real fee."""
    spends = [vault.spend_from_json(s) for s in result["coin_spends"]]
    out = 0
    for spend in spends:
        for cond in tool.conditions_dict_for_solution(spend.puzzle_reveal, spend.solution, vault.MAX_CLVM_COST).get(ConditionOpcode.CREATE_COIN, []):
            out += int.from_bytes(cond.vars[1], "big")
    return sum(int(s.coin.amount) for s in spends) - out


for label, command, payload in (
    ("profile publish", "build-publish", {"pubkey": bytes(KEY).hex(), "network": "mainnet", "coins": COINS, "safes": []}),
    ("profile publish with a manifest", "build-publish", {"pubkey": bytes(KEY).hex(), "network": "mainnet", "coins": COINS, "safes": [], "manifests": [POLICY]}),
    ("manifest publish", "manifest-build", {"network": "mainnet", "coins": COINS, "safe": POLICY}),
):
    sized = profile.run(command, {**payload, "fee_rate": 5})
    cost = vault.bundle_cost([vault.spend_from_json(s) for s in sized["coin_spends"]])
    check(f"{label}: the reported cost is the bundle's", sized["cost"], cost)
    check(f"{label}: the fee clears 5 mojos per cost", sized["fee"] >= cost * 5, True)
    check(f"{label}: within the margin", sized["fee"] <= vault.sized_fee(cost, 5), True)
    check(f"{label}: the wallet pays exactly that fee", paid(sized), sized["fee"])
    check(f"{label}: far below the swap-sized 0.0015275 XCH", sized["fee"] * 5 < 1_527_500_000, True)
    print(f"  {label}: cost {cost:,}, fee {sized['fee']:,} mojos")

    fixed = profile.run(command, {**payload, "fee": 1_000})
    check(f"{label}: a fixed fee is kept", (fixed["fee"], paid(fixed)), (1_000, 1_000))

print(f"profile fee rate: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
