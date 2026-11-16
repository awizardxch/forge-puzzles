"""Owner membership coins: a lock can be found from any of its owners' addresses.

Owner, 2026-10-21: "the connected address should detect a lock/multisig its associated with".
A lock launch (and a re-key that seats new owners) leaves a few mojos at every owner's address
with memos ``[owner puzzle hash, "forge-vault-owner/1", launcher_id]``. The suite pins:

* every owner gets exactly one, naming this launcher, and a key seated twice gets one;
* no two outputs of the paying spend are equal (DUPLICATE_OUTPUT): the creator's profile coin
  sits at the creator's address with amount 1, so the creator's membership coin moves up;
* the spend still balances, and opting out (`hint_owners: false`) leaves the old shape;
* a re-key's sponsor pays membership coins for NEW owners only, and none for a rename;
* reading: a membership coin is only a pointer -- the owner is `current` only if the lock's
  policy, read from its launcher, seats that address now.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.wallet.lineage_proof import LineageProof as SingletonLineageProof
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk, puzzle_hash_for_synthetic_public_key
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


KEYS = [AugSchemeMPL.key_gen(bytes([41 + i] * 32)).get_g1() for i in range(3)]
A, B, C = KEYS
PH = {k: puzzle_hash_for_synthetic_public_key(k) for k in KEYS}


def owners_json(*keys) -> list[dict]:
    return [{"label": f"o{i}", "pubkey": bytes(k).hex()} for i, k in enumerate(keys)]


def wallet_entry(key, amount: int = 10_000_000) -> tuple[dict, Coin, Program]:
    puzzle = puzzle_for_pk(key)
    coin = Coin(bytes32([0x5A] * 32), puzzle.get_tree_hash(), uint64(amount))
    entry = {"coin": {"parent_coin_info": coin.parent_coin_info.hex(), "puzzle_hash": coin.puzzle_hash.hex(), "amount": amount},
             "puzzle": bytes(puzzle).hex()}
    return entry, coin, puzzle


def creates(spend_json: dict) -> list[tuple[bytes32, int, list[bytes]]]:
    puzzle = Program.fromhex(tool.strip0x(spend_json["puzzle_reveal"]))
    solution = Program.fromhex(tool.strip0x(spend_json["solution"]))
    out = []
    for cond in puzzle.run(solution).as_iter():
        items = list(cond.as_iter())
        if items[0].as_int() != 51:
            continue
        memos = [m.as_atom() for m in items[3].as_iter()] if len(items) > 3 else []
        out.append((bytes32(items[1].as_atom()), items[2].as_int(), memos))
    return out


def tagged(outputs, launcher: bytes) -> dict[bytes32, int]:
    return {ph: amount for ph, amount, memos in outputs
            if len(memos) >= 3 and memos[1] == vault.OWNER_TAG and memos[2] == launcher and memos[0] == ph}


# ─── launch ──────────────────────────────────────────────────────────────────

entry, wallet_coin, _ = wallet_entry(A)
payload = {"network": "testnet11", "coins": [entry], "fee": 1000,
           "policy": {"name": "hinted", "m": 2, "owners": owners_json(A, B, C)},
           "profile": {"pubkey": bytes(A).hex(), "include_self": True}}
launched = vault.cmd_launch(payload, lambda _n: None)
launcher = bytes.fromhex(launched["launcher_id"])
outputs = creates(launched["coin_spends"][0])
hints = tagged(outputs, launcher)
check("every owner address gets one membership coin", sorted(hints), sorted(PH.values()))
check("the creator's moves up a mojo: the profile coin already holds (address, 1)", hints[PH[A]], 2)
check("the others are one mojo", (hints[PH[B]], hints[PH[C]]), (1, 1))
pairs = [(ph, amount) for ph, amount, _m in outputs]
check("no two outputs are equal (DUPLICATE_OUTPUT)", len(pairs), len(set(pairs)))
check("the spend balances: outputs plus fee are the coin", sum(a for _p, a, _m in outputs) + 1000, int(wallet_coin.amount))
check("the result lists the membership coins", len(launched["owner_hints"]), 3)

no_profile = vault.cmd_launch({**payload, "profile": None}, lambda _n: None)
check("without a profile coin every owner's is one mojo",
      sorted(tagged(creates(no_profile["coin_spends"][0]), bytes.fromhex(no_profile["launcher_id"])).values()), [1, 1, 1])

opted_out = vault.cmd_launch({**payload, "hint_owners": False}, lambda _n: None)
out_outputs = creates(opted_out["coin_spends"][0])
check("hint_owners: false leaves no membership coin", tagged(out_outputs, bytes.fromhex(opted_out["launcher_id"])), {})
change = lambda outs, coin: max(a for ph, a, _m in outs if ph == coin.puzzle_hash)  # noqa: E731
check("and keeps the four mojos as change", change(out_outputs, wallet_coin) - change(outputs, wallet_coin), 4)

twice = vault.cmd_launch({**payload, "profile": None,
                          "policy": {"name": "twice", "m": 1, "owners": [{"label": "x", "pubkey": bytes(B).hex()}, {"label": "y", "pubkey": bytes(B).hex()}]}},
                         lambda _n: None)
check("a key seated twice gets one membership coin", list(tagged(creates(twice["coin_spends"][0]), bytes.fromhex(twice["launcher_id"]))), [PH[B]])

# ─── re-key ──────────────────────────────────────────────────────────────────

LAUNCHER = bytes32([0x6C] * 32)
POLICY = vault.Policy("lock", 2, (("a", A), ("b", B)), vault.FORMAT_MIPS)
TIP = Coin(bytes32([0x01] * 32), vault.vault_puzzle(LAUNCHER, POLICY).get_tree_hash(), uint64(1))
STATE = vault.VaultState(LAUNCHER, POLICY, TIP, SingletonLineageProof(bytes32([0x02] * 32), POLICY.inner_puzzle_hash(), uint64(1)),
                         height=10, spends=1)
_e, sponsor_coin, sponsor_puzzle = wallet_entry(A, 5_000)
NODE = tool.Node("http://fake")


def sponsor_outputs(plan) -> list[tuple[bytes32, int, list[bytes]]]:
    return creates({"puzzle_reveal": bytes(plan.sponsor.puzzle_reveal).hex(), "solution": bytes(plan.sponsor.solution()).hex()})


grown = vault.Policy("lock", 2, (("a", A), ("b", B), ("c", C)), vault.FORMAT_MIPS)
plan = vault.build_vault_plan(NODE, "testnet11", STATE, [], 1000, (sponsor_coin, sponsor_puzzle), grown)
s_out = sponsor_outputs(plan)
check("a re-key seating C gives C, and only C, a membership coin", tagged(s_out, LAUNCHER), {PH[C]: 1})
check("the sponsor pays it: change is the coin less fee and hint", change(s_out, sponsor_coin), 5_000 - 1000 - 1)
check("the plan says so", plan.summary["owner_hints"][0]["amount"], 1)

renamed = vault.Policy("renamed", 2, (("a", A), ("b", B)), vault.FORMAT_MIPS)
check("a rename seats nobody new", tagged(sponsor_outputs(vault.build_vault_plan(NODE, "testnet11", STATE, [], 1000, (sponsor_coin, sponsor_puzzle), renamed)), LAUNCHER), {})
check("opting out of a re-key's hints", tagged(sponsor_outputs(vault.build_vault_plan(
    NODE, "testnet11", STATE, [], 1000, (sponsor_coin, sponsor_puzzle), grown, hint_owners=False)), LAUNCHER), {})
small = Coin(bytes32([0x5B] * 32), sponsor_puzzle.get_tree_hash(), uint64(1000))
try:
    vault.build_vault_plan(NODE, "testnet11", STATE, [], 1000, (small, sponsor_puzzle), grown)
    check("a sponsor covering the fee but not the hint is refused", "accepted", "refused")
except tool.MultisigError as exc:
    check("a sponsor covering the fee but not the hint is refused", "membership" in str(exc), True)

# ─── reading ─────────────────────────────────────────────────────────────────

LAUNCH_SPEND = launched["coin_spends"][0]
LAUNCH_PARENT = Coin(bytes.fromhex(LAUNCH_SPEND["coin"]["parent_coin_info"]), bytes.fromhex(LAUNCH_SPEND["coin"]["puzzle_hash"]),
                     uint64(int(LAUNCH_SPEND["coin"]["amount"])))


class HintNode(tool.Node):
    def __init__(self, extra: list[Coin] = ()) -> None:
        super().__init__("http://fake")
        self.extra = list(extra)

    def rpc(self, route: str, payload: dict) -> dict:
        if route == "get_coin_records_by_puzzle_hash":
            ph = bytes32.fromhex(tool.strip0x(payload["puzzle_hash"]))
            coins = [Coin(LAUNCH_PARENT.name(), p, uint64(a)) for p, a, _m in outputs if p == ph] + [c for c in self.extra if c.puzzle_hash == ph]
            return {"success": True, "coin_records": [{"coin": {"parent_coin_info": c.parent_coin_info.hex(), "puzzle_hash": c.puzzle_hash.hex(),
                                                                "amount": int(c.amount)}, "spent": False, "confirmed_block_index": 7} for c in coins]}
        if route == "get_puzzle_and_solution":
            if bytes32.fromhex(tool.strip0x(payload["coin_id"])) == LAUNCH_PARENT.name():
                return {"success": True, "coin_solution": {"puzzle_reveal": LAUNCH_SPEND["puzzle_reveal"], "solution": LAUNCH_SPEND["solution"]}}
            return {"success": True, "coin_solution": {"puzzle_reveal": "01", "solution": "80"}}
        raise AssertionError(f"unexpected route {route}")


def with_policy(policy: vault.Policy):
    def read(_node, launcher_id):
        return vault.VaultState(launcher_id, policy, TIP, SingletonLineageProof(bytes32([2] * 32), None, uint64(1)), height=9, spends=0)
    return read


real_read = vault.read_vault
try:
    vault.read_vault = with_policy(vault.Policy("hinted", 2, (("a", A), ("b", B), ("c", C)), vault.FORMAT_MIPS))
    found = vault.locks_for_owner(HintNode(), PH[C])
    check("an owner's address lists the lock, verified and current", [(f["launcher_id"], f["verified"], f["current"]) for f in found],
          [(launcher.hex(), True, True)])
    vault.read_vault = with_policy(vault.Policy("hinted", 1, (("a", A), ("b", B)), vault.FORMAT_MIPS))
    check("after C is re-keyed out, C's coin reads as a former seat", [f["current"] for f in vault.locks_for_owner(HintNode(), PH[C])], [False])
    stray = Coin(bytes32([0x77] * 32), PH[B], uint64(3))
    check("a small coin with no membership memo is ignored", len(vault.locks_for_owner(HintNode([stray]), PH[B])), 1)
finally:
    vault.read_vault = real_read

print(f"vault owner hints: {PASSED} checks passed")
if FAILED:
    print("\n".join(FAILED))
    raise SystemExit(1)
