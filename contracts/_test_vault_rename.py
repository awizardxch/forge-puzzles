"""Renaming a lock, or relabelling its owners, is a re-key that keeps the keys.

The owner asked (2026-10-05) to change the lock's name and their own label in a
re-key. The builder used to refuse any successor with the same keys and
threshold ("identical ... nothing to change"), and the chain reader adopted a
memo only when the keys changed, so a rename could neither be proposed nor be
read back.

What makes a rename safe to accept is that it cannot reach who signs: the memo
rides in the singleton spend the owners sign, and the reader adopts it as a
rename only when its inner puzzle hash -- keys, threshold, composition -- is the
one the lock already has. This suite pins:

* a rename-only and a label-only successor build, and the recreated singleton
  keeps the same inner puzzle while its memo carries the new policy;
* policy_change reads that memo as a rename, new keys as a re-key, and the
  same policy as nothing;
* a policy identical in every field is still refused.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.coin import Coin
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


LAUNCHER = bytes32([0x7D] * 32)
ME = AugSchemeMPL.key_gen(bytes([61] * 32)).get_g1()
FRIEND = AugSchemeMPL.key_gen(bytes([62] * 32)).get_g1()
POLICY = vault.Policy("aWizard", 1, (("Me", ME),), vault.FORMAT_MIPS)
DEPOSIT_PH = vault.deposit_puzzle(LAUNCHER).get_tree_hash()
VAULT_PH = vault.vault_puzzle(LAUNCHER, POLICY).get_tree_hash()
LINEAGE_PARENT = bytes32([0x02] * 32)
TIP = Coin(Coin(LINEAGE_PARENT, VAULT_PH, uint64(1)).name(), VAULT_PH, uint64(1))
STATE = vault.VaultState(LAUNCHER, POLICY, TIP, SingletonLineageProof(LINEAGE_PARENT, POLICY.inner_puzzle_hash(), uint64(1)), height=9_386_700, spends=1)


class FakeNode(tool.Node):
    def __init__(self) -> None:
        super().__init__("http://fake")

    def rpc(self, route: str, payload: dict) -> dict:
        if route == "get_coin_records_by_puzzle_hashes":
            return {"success": True, "coin_records": []}
        raise AssertionError(f"unexpected route {route}")


NODE = FakeNode()
vault.state_from_payload = lambda _node, _payload: STATE


def propose(successor: dict) -> dict:
    return vault.run("propose", {"network": "mainnet", "launcher_id": LAUNCHER.hex(), "successor": successor}, lambda _url: NODE)


def successor_memo_policy(result: dict) -> tuple[vault.Policy | None, bytes32]:
    """The policy the recreated singleton's memo carries, and the full puzzle hash of the coin it creates."""
    plan = vault.VaultPlan.from_json(result["plan"])
    singleton = plan.materialize(list(POLICY.keys)[: POLICY.m])[0]
    # Exactly as the reader gets it from the node: bytes, parsed back.
    written = vault.policy_from_singleton_spend(vault.Program.from_bytes(bytes(singleton.puzzle_reveal)), vault.Program.from_bytes(bytes(singleton.solution)))
    created = [c for c in tool.conditions_dict_for_solution(singleton.puzzle_reveal, singleton.solution, vault.MAX_CLVM_COST).get(vault.ConditionOpcode.CREATE_COIN, []) if int.from_bytes(c.vars[1], "big") % 2 == 1]
    return written, bytes32(created[0].vars[0])


me_hex = bytes(ME).hex()

# ─── rename only ─────────────────────────────────────────────────────────────
renamed = propose({"name": "Treasury", "m": 1, "owners": [{"label": "Me", "pubkey": me_hex}]})
written, child = successor_memo_policy(renamed)
check("a rename-only re-key builds", renamed["success"], True)
check("the memo carries the new name", written.name if written else None, "Treasury")
check("the singleton is recreated at the same puzzle hash (keys untouched)", child, VAULT_PH)
check("and with the same full puzzle, so the lock keeps its coin shape", vault.vault_puzzle(LAUNCHER, written).get_tree_hash(), VAULT_PH)
check("the reader takes it as a rename", vault.policy_change(POLICY, written), "rename")

# ─── relabel only ────────────────────────────────────────────────────────────
relabelled = propose({"name": "aWizard", "m": 1, "owners": [{"label": "Speechless", "pubkey": me_hex}]})
written_label, child_label = successor_memo_policy(relabelled)
check("a label-only re-key builds", relabelled["success"], True)
check("the memo carries the new label", [label for label, _ in written_label.owners] if written_label else None, ["Speechless"])
check("the keys are unchanged: same puzzle hash", child_label, VAULT_PH)
check("the reader takes it as a rename", vault.policy_change(POLICY, written_label), "rename")

# ─── a real re-key, and nothing at all ───────────────────────────────────────
rekeyed = propose({"name": "aWizard", "m": 1, "owners": [{"label": "Me", "pubkey": me_hex}, {"label": "Friend", "pubkey": bytes(FRIEND).hex()}]})
written_rekey, child_rekey = successor_memo_policy(rekeyed)
check("new keys still re-key, to a new puzzle hash", child_rekey != VAULT_PH, True)
check("and the reader says re-key", vault.policy_change(POLICY, written_rekey), "rekey")
check("the same policy is no change", vault.policy_change(POLICY, POLICY), None)
check("no memo is no change", vault.policy_change(POLICY, None), None)


def refused(successor: dict) -> bool:
    try:
        propose(successor)
    except tool.MultisigError:
        return True
    return False


check("a policy identical in every field is still refused", refused({"name": "aWizard", "m": 1, "owners": [{"label": "Me", "pubkey": me_hex}]}), True)

print(f"vault rename: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
