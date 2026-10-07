"""The key behind an address, and telling "never spent" from "could not read".

Adding a co-owner by address reads the key from one of the address's spends. On
mainnet (2026-10-05) an address with 1,848 spends was reported as "never spent":
the lookup skipped every spend whose read the node refused and then answered
`found: false`, which the page could only show one way. This suite pins:

* a refused read is retried once, and a key behind it is still found;
* when the reads keep failing, the answer says the address HAS spent and how
  many reads failed, so the page can say "try again" instead of "never spent";
* an address with no spends at all still reads as never spent.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk
from chia_rs import AugSchemeMPL
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import multisig_profile as profile
import multisig_tool as tool

PASSED = 0
FAILED: list[str] = []


def check(name: str, got: object, want: object) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}\n     got  {got}\n     want {want}")


KEY = AugSchemeMPL.key_gen(bytes([71] * 32)).get_g1()
PUZZLE = puzzle_for_pk(KEY)
PH = PUZZLE.get_tree_hash()
COINS = [Coin(bytes32([i + 1] * 32), PH, uint64(1_000 + i)) for i in range(3)]


class FlakyNode(tool.Node):
    """Answers coin records; refuses the first `refusals` spend reads."""

    def __init__(self, coins: list[Coin], refusals: int) -> None:
        super().__init__("http://fake")
        self.coins = coins
        self.refusals = refusals
        self.reads = 0

    def rpc(self, route: str, payload: dict) -> dict:
        if route == "get_coin_records_by_puzzle_hash":
            return {"success": True, "coin_records": [
                {"coin": {"parent_coin_info": "0x" + c.parent_coin_info.hex(), "puzzle_hash": "0x" + c.puzzle_hash.hex(), "amount": int(c.amount)},
                 "spent": True, "spent_block_index": 100 + i, "confirmed_block_index": 50}
                for i, c in enumerate(self.coins)]}
        if route == "get_puzzle_and_solution":
            self.reads += 1
            if self.reads <= self.refusals:
                raise tool.MultisigError("node get_puzzle_and_solution HTTP 429: rate limited")
            return {"success": True, "coin_solution": {"puzzle_reveal": bytes(PUZZLE).hex(), "solution": "80"}}
        raise AssertionError(f"unexpected route {route}")


flaky = profile.key_for_address(FlakyNode(COINS, refusals=1), "mainnet", PH)
check("one refused read is retried and the key is found", flaky.get("found"), True)
# The synthetic key the standard puzzle curries, which is what an owner signs with.
SYNTHETIC = bytes(PUZZLE.uncurry()[1].first().as_atom()).hex()
check("it is the address's own (synthetic) key", flaky.get("public_key"), SYNTHETIC)

# One spend only, refused once: nothing else to fall back on, so only the retry finds it.
single = profile.key_for_address(FlakyNode(COINS[:1], refusals=1), "mainnet", PH)
check("an address with one spend, refused once, is still found by the retry", single.get("found"), True)

down = profile.key_for_address(FlakyNode(COINS, refusals=10_000), "mainnet", PH)
check("a node that refuses every read finds nothing", down.get("found"), False)
check("but says the address has spent", down.get("spent_coins"), len(COINS))
check("and that every spend looked at went unread", down.get("unread"), len(COINS))

never = profile.key_for_address(FlakyNode([], refusals=0), "mainnet", PH)
check("an address with no spends is never spent", (never.get("found"), never.get("spent_coins"), never.get("unread")), (False, 0, 0))

print(f"key for address: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
