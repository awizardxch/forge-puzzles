"""A lock describes its custody the way Chia's SDK reads it (CHIP-0043).

Until 2026-10-24 a Forge lock's policy rode only in Forge's own memo
(``forge-vault/1`` + ``name|m|label=key;...``). Its puzzle has been a Chia vault
since 2026-09-06 -- the custody hash the SDK computes for the same owners -- but no
SDK wallet could know who those owners are without reading Forge's format. Read on
mainnet: neither the DAO lock nor the dev lock carries a memo ``parseMipsMemo``
accepts, at launch or at any re-key.

Now a MIPS lock also writes the SDK's memo. This suite pins:

* a launch's key/value list carries the CHIP-0043 memo first, describing exactly the
  inner puzzle the eve singleton holds, and Forge's policy pair still reads back;
* a legacy (``forge/1``) launch writes none: no CHIP-0043 memo describes that puzzle;
* a re-key's recreated singleton carries the successor's memo after its hint and
  Forge's pair; a rename carries one too; a plain spend's memos are unchanged;
* Forge's own reader still decodes the successor from such a spend.

The SDK's side -- that ``chia-wallet-sdk`` parses these bytes and rebuilds the lock --
is api/__checks__/lockMipsMemo.check.mjs, and ``_test_mips.py`` pins the bytes
against vectors the SDK generated.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.wallet.lineage_proof import LineageProof as SingletonLineageProof
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk
from chia_rs import AugSchemeMPL
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import mips
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


KEYS = [AugSchemeMPL.key_gen(bytes([71 + i] * 32)).get_g1() for i in range(3)]
A, B, C = KEYS


def owners_json(*keys) -> list[dict]:
    return [{"label": f"o{i}", "pubkey": bytes(k).hex()} for i, k in enumerate(keys)]


def wallet_entry(key, amount: int = 10_000_000) -> dict:
    puzzle = puzzle_for_pk(key)
    coin = Coin(bytes32([0x5C] * 32), puzzle.get_tree_hash(), uint64(amount))
    return {"coin": {"parent_coin_info": coin.parent_coin_info.hex(), "puzzle_hash": coin.puzzle_hash.hex(), "amount": amount},
            "puzzle": bytes(puzzle).hex()}


def launcher_key_values(launched: dict) -> list[Program]:
    spend = next(s for s in launched["coin_spends"] if tool.strip0x(s["coin"]["puzzle_hash"]) == vault.SINGLETON_LAUNCHER_HASH.hex())
    solution = Program.fromhex(tool.strip0x(spend["solution"]))
    return list(list(solution.as_iter())[2].as_iter())


# ─── launch ──────────────────────────────────────────────────────────────────

for m, keys in ((1, (A,)), (1, (A, B)), (2, (A, B, C)), (3, (A, B, C))):
    label = f"{m}-of-{len(keys)} launch"
    launched = vault.cmd_launch({"network": "testnet11", "coins": [wallet_entry(A)], "fee": 1000,
                                 "policy": {"name": "memo", "m": m, "owners": owners_json(*keys)}}, lambda _n: None)
    items = launcher_key_values(launched)
    check(f"{label}: the key/value list holds the CHIP-0043 memo and Forge's pair", len(items), 2)
    check(f"{label}: the CHIP-0043 memo comes first", items[0].first().as_atom(), mips.MEMO_NAMESPACE)
    check(f"{label}: it describes the custody the eve singleton holds",
          mips.custody_memo_hash(items[0]).hex(), launched["custody_hash"])
    check(f"{label}: and it is the SDK-shaped memo for these owners", bytes(items[0]),
          bytes(mips.custody_memo(m, [bytes(k) for k in keys])))
    solution = Program.fromhex(tool.strip0x(next(s for s in launched["coin_spends"] if tool.strip0x(s["coin"]["puzzle_hash"]) == vault.SINGLETON_LAUNCHER_HASH.hex())["solution"]))
    check(f"{label}: Forge's own reader still reads the policy",
          vault.policy_from_launcher(solution).inner_puzzle_hash().hex(), launched["custody_hash"])
    check(f"{label}: and the launcher memo reader agrees", vault.mips_memo_from_launcher(solution).hex(), launched["custody_hash"])

legacy = vault.cmd_launch({"network": "testnet11", "coins": [wallet_entry(A)], "fee": 1000,
                           "policy": {"name": "old", "m": 1, "format": "forge/1", "owners": owners_json(A, B)}}, lambda _n: None)
legacy_items = launcher_key_values(legacy)
check("a legacy launch writes no CHIP-0043 memo: none describes its puzzle", len(legacy_items), 1)
check("and keeps Forge's pair", legacy_items[0].first().as_atom(), vault.VAULT_TAG)

# ─── re-key, rename and a plain spend ────────────────────────────────────────

LAUNCHER = bytes32([0x6D] * 32)
POLICY = vault.Policy("lock", 1, (("a", A), ("b", B)), vault.FORMAT_MIPS)
VAULT_PH = vault.vault_puzzle(LAUNCHER, POLICY).get_tree_hash()
LINEAGE_PARENT = bytes32([0x03] * 32)
TIP = Coin(Coin(LINEAGE_PARENT, VAULT_PH, uint64(1)).name(), VAULT_PH, uint64(1))
STATE = vault.VaultState(LAUNCHER, POLICY, TIP, SingletonLineageProof(LINEAGE_PARENT, POLICY.inner_puzzle_hash(), uint64(1)),
                         height=10, spends=1)


class FakeNode(tool.Node):
    def __init__(self) -> None:
        super().__init__("http://fake")

    def rpc(self, route: str, payload: dict) -> dict:
        if route == "get_coin_records_by_puzzle_hashes":
            # One XCH coin at the deposit address, for the plain payment below.
            coin = Coin(bytes32([0x04] * 32), vault.deposit_puzzle_hash(LAUNCHER), uint64(1_000_000))
            return {"success": True, "coin_records": [{"coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(),
                                                                "puzzle_hash": "0x" + coin.puzzle_hash.hex(), "amount": 1_000_000},
                                                       "confirmed_block_index": 5, "spent": False, "spent_block_index": 0,
                                                       "coinbase": False, "timestamp": 0}]}
        raise AssertionError(f"unexpected route {route}")


vault.state_from_payload = lambda _node, _payload: STATE


def singleton_child_memos(successor: dict | None) -> tuple[list[Program], vault.Policy | None]:
    payload = {"network": "testnet11", "launcher_id": LAUNCHER.hex(), "hint_owners": False}
    if successor is not None:
        payload["successor"] = successor
    else:
        payload["outputs"] = [{"puzzle_hash": "11" * 32, "amount": 1000}]  # a plain payment
    result = vault.run("propose", payload, lambda _url: FakeNode())
    plan = vault.VaultPlan.from_json(result["plan"])
    singleton = plan.materialize(list(POLICY.keys)[: POLICY.m])[0]
    puzzle = Program.from_bytes(bytes(singleton.puzzle_reveal))
    solution = Program.from_bytes(bytes(singleton.solution))
    for condition in puzzle.run(solution).as_iter():
        parts = list(condition.as_iter())
        if parts[0].as_int() == 51 and parts[2].as_int() % 2 == 1:
            return list(parts[3].as_iter()), vault.policy_from_singleton_spend(puzzle, solution)
    raise AssertionError("no singleton child")


grown = {"name": "lock", "m": 2, "format": "mips/1", "owners": [{"label": "a", "pubkey": bytes(A).hex()},
                                                                 {"label": "b", "pubkey": bytes(B).hex()},
                                                                 {"label": "c", "pubkey": bytes(C).hex()}]}
memos, read_back = singleton_child_memos(grown)
successor = vault.Policy.from_json(grown)
check("a re-key: the hint stays first", memos[0].as_atom(), LAUNCHER)
check("a re-key: Forge's pair follows", (memos[1].as_atom(), memos[2].as_atom()), (vault.VAULT_TAG, successor.encode()))
check("a re-key: the CHIP-0043 memo is last", len(memos), 4)
check("a re-key: it describes the successor's custody", mips.custody_memo_hash(memos[3]), successor.inner_puzzle_hash())
check("a re-key: Forge's reader still decodes the successor", read_back.inner_puzzle_hash() if read_back else None,
      successor.inner_puzzle_hash())

renamed = {"name": "renamed", "m": 1, "format": "mips/1", "owners": [{"label": "a", "pubkey": bytes(A).hex()},
                                                                        {"label": "b", "pubkey": bytes(B).hex()}]}
memos, read_back = singleton_child_memos(renamed)
check("a rename carries the (unchanged) custody's memo too", mips.custody_memo_hash(memos[-1]), POLICY.inner_puzzle_hash())
check("and reads back as a rename", vault.policy_change(POLICY, read_back), "rename")

memos, read_back = singleton_child_memos(None)
check("a plain spend's memos are only the hint, as before", [m.as_atom() for m in memos], [LAUNCHER])
check("and carry no policy", read_back, None)


print(f"vault sdk memo: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  ✗ " + failure)
sys.exit(1 if FAILED else 0)
