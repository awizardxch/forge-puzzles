"""The vault lock against a fake chain that actually runs the puzzles.

The fake node applies a pushed bundle the way a full node would: it runs
every spend, marks the inputs spent, and creates the CREATE_COIN children at
the puzzle hashes the conditions name. So a green run proves the launch, a
payment, and an owner change all compose into bundles whose conditions are
consistent with each other — the singleton recreates itself, the funds coins
are announced, and the lineage reads back with the policies in order. What it
does not prove: announcement matching and mempool acceptance, which only a
node judges.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

CONTRACTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTRACTS_DIR))

from chia.types.blockchain_format.coin import Coin  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.condition_opcodes import ConditionOpcode  # noqa: E402
from chia.consensus.condition_tools import conditions_dict_for_solution  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key  # noqa: E402
from chia_rs import AugSchemeMPL, G2Element  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import multisig_tool as tool  # noqa: E402
import vault_tool as vault  # noqa: E402


class FakeChain(tool.Node):
    def __init__(self) -> None:
        super().__init__("http://fake")
        self.records: dict[str, dict[str, Any]] = {}
        self.spends: dict[tuple[str, int], dict[str, str]] = {}
        self.height = 100

    def add(self, coin: Coin) -> Coin:
        self.records[coin.name().hex()] = {"coin": tool.coin_to_json(coin), "spent": False, "spent_block_index": 0, "confirmed_block_index": self.height, "timestamp": 0}
        return coin

    def rpc(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        if route == "get_coin_records_by_puzzle_hashes":
            wanted = {tool.strip0x(p) for p in payload["puzzle_hashes"]}
            return {"success": True, "coin_records": [r for r in self.records.values() if r["coin"]["puzzle_hash"] in wanted and (payload.get("include_spent_coins") or not r["spent"])]}
        if route == "get_coin_records_by_puzzle_hash":
            ph = tool.strip0x(payload["puzzle_hash"])
            return {"success": True, "coin_records": [r for r in self.records.values() if r["coin"]["puzzle_hash"] == ph and (payload.get("include_spent_coins") or not r["spent"])]}
        if route == "get_coin_records_by_parent_ids":
            parents = {tool.strip0x(p) for p in payload["parent_ids"]}
            return {"success": True, "coin_records": [r for r in self.records.values() if r["coin"]["parent_coin_info"] in parents]}
        if route == "get_coin_record_by_name":
            return {"success": True, "coin_record": self.records.get(tool.strip0x(payload["name"]))}
        if route == "get_puzzle_and_solution":
            key = (tool.strip0x(payload["coin_id"]), int(payload["height"]))
            if key not in self.spends:
                raise tool.MultisigError("no such spend")
            return {"success": True, "coin_solution": self.spends[key]}
        if route == "push_tx":
            self.apply(payload["spend_bundle"]["coin_spends"])
            return {"success": True, "status": "SUCCESS"}
        raise AssertionError(route)

    def apply(self, coin_spends: list[dict[str, Any]]) -> None:
        """Include a bundle: spend the inputs, create the outputs."""
        self.height += 1
        for spend in coin_spends:
            coin = tool.coin_from_json(spend["coin"])
            record = self.records.get(coin.name().hex())
            assert record is not None and not record["spent"], f"unknown or spent coin {coin.name().hex()}"
            record["spent"] = True
            record["spent_block_index"] = self.height
            self.spends[(coin.name().hex(), self.height)] = {"puzzle_reveal": tool.strip0x(spend["puzzle_reveal"]), "solution": tool.strip0x(spend["solution"])}
            puzzle = Program.from_bytes(bytes.fromhex(tool.strip0x(spend["puzzle_reveal"])))
            solution = Program.from_bytes(bytes.fromhex(tool.strip0x(spend["solution"])))
            for cond in conditions_dict_for_solution(puzzle, solution, tool.MAX_CLVM_COST).get(ConditionOpcode.CREATE_COIN, []):
                child = Coin(coin.name(), bytes32(cond.vars[0]), uint64(int.from_bytes(cond.vars[1], "big")))
                self.add(child)


def sage_like(secret_keys, spends: list[dict[str, Any]]) -> str:
    sigs = []
    for s in spends:
        puzzle = Program.from_bytes(bytes.fromhex(s["puzzle_reveal"]))
        solution = Program.from_bytes(bytes.fromhex(s["solution"]))
        coin = tool.coin_from_json(s["coin"])
        conds = conditions_dict_for_solution(puzzle, solution, tool.MAX_CLVM_COST)
        for sk in secret_keys:
            for c in conds.get(ConditionOpcode.AGG_SIG_ME, []):
                if c.vars[0] == bytes(sk.get_g1()):
                    sigs.append(AugSchemeMPL.sign(sk, c.vars[1] + bytes(coin.name()) + tool.AGG_SIG_ME_DATA["testnet11"]))
    return bytes(AugSchemeMPL.aggregate(sigs)).hex() if sigs else bytes(G2Element()).hex()


class VaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chain = FakeChain()
        self.factory = lambda _url: self.chain
        self.sks = [AugSchemeMPL.key_gen(bytes([i + 1]) * 32) for i in range(4)]
        self.pks = [sk.get_g1() for sk in self.sks]
        self.labels = ["Alice", "Bob", "Carol", "Dave"]
        self.policy = {"name": "Treasury", "m": 2, "owners": [{"label": l, "pubkey": bytes(pk).hex()} for l, pk in zip(self.labels[:3], self.pks[:3])]}
        # Alice's wallet: a standard coin she pays the launch and fees from.
        self.wallet_sk = AugSchemeMPL.key_gen(b"\x77" * 32)
        self.wallet_puzzle = puzzle_for_synthetic_public_key(self.wallet_sk.get_g1())
        self.wallet_coin = self.chain.add(Coin(bytes32(b"\x01" * 32), self.wallet_puzzle.get_tree_hash(), uint64(10_000_000)))

    def wallet_coins(self) -> list[dict[str, Any]]:
        return [{"coin": r["coin"], "puzzle": bytes(self.wallet_puzzle).hex()} for r in self.chain.records.values() if r["coin"]["puzzle_hash"] == self.wallet_puzzle.get_tree_hash().hex() and not r["spent"]]

    def launch(self) -> dict[str, Any]:
        built = vault.run("launch", {"network": "testnet11", "policy": self.policy, "coins": self.wallet_coins(), "fee": 1000}, self.factory)
        # The wallet spend needs Alice's wallet signature; the launcher none.
        self.chain.apply(built["coin_spends"])
        return built

    def share(self, plan: dict[str, Any], index: int, extra_sks=()) -> dict[str, Any]:
        request = vault.run("sign-request", {"plan": plan, "signer": bytes(self.pks[index]).hex()}, self.factory)
        signature = sage_like([self.sks[index], *extra_sks], request["coin_spends"])
        return vault.run("verify-share", {"plan": plan, "signature": signature, "candidates": request["selected_pubkeys"]}, self.factory)

    def test_launch_read_pay_and_rekey_keep_the_deposit_address(self) -> None:
        built = self.launch()
        launcher_id = built["launcher_id"]
        deposit = built["deposit_address"]

        # Read back from chain: the launcher's key/value list carries the policy.
        state = vault.run("read", {"launcher_id": launcher_id}, self.factory)
        self.assertTrue(state["found"])
        self.assertEqual(state["policy"]["m"], 2)
        self.assertEqual([o["label"] for o in state["policy"]["owners"]], ["Alice", "Bob", "Carol"])
        self.assertEqual(state["deposit_address"], deposit)
        self.assertEqual(state["spends"], 0)
        # …and from the deposit address alone, through the pointer coin.
        by_address = vault.run("read", {"address": deposit}, self.factory)
        self.assertEqual(by_address["launcher_id"], launcher_id)

        # Fund the lock at its deposit address, like any sender would.
        deposit_ph = bytes32.fromhex(state["deposit_puzzle_hash"])
        self.chain.add(Coin(bytes32(b"\x02" * 32), deposit_ph, uint64(5_000)))
        balance = vault.run("balance", {"launcher_id": launcher_id}, self.factory)
        self.assertEqual(balance["xch"]["balance"], 5_001, "the 5000 plus the 1-mojo pointer")

        # Pay 3000 to a recipient: the singleton spend (the vote) plus the funds spend.
        recipient = bytes32(b"\x09" * 32)
        proposal = vault.run("propose", {"launcher_id": launcher_id, "outputs": [{"puzzle_hash": recipient.hex(), "amount": 3_000}], "fee": 100}, self.factory)
        plan = proposal["plan"]
        self.assertEqual(plan["summary"]["kind"], "send")
        self.assertEqual(plan["summary"]["change"], [{"asset_id": None, "amount": 1_900}])
        share_b = self.share(plan, 1)
        share_c = self.share(plan, 2)
        assembled = vault.run("assemble", {"plan": plan, "shares": [share_b, share_c], "push": True}, self.factory)
        self.assertTrue(assembled["success"])
        self.assertEqual(assembled["agg_sig_count"], 2)
        # Recipient paid, change back at the same deposit address, singleton advanced.
        self.assertEqual(vault.run("balance", {"launcher_id": launcher_id}, self.factory)["xch"]["balance"], 1_901)
        after = vault.run("read", {"launcher_id": launcher_id}, self.factory)
        self.assertEqual(after["spends"], 1)
        self.assertEqual(after["tip"]["coin_id"], assembled["next"]["tip"]["coin_id"])
        self.assertEqual(after["policy"]["m"], 2)

        # Add Dave and raise the threshold to 3 of 4: a vote, sponsored by Alice's wallet.
        successor = {"name": "Treasury", "m": 3, "owners": self.policy["owners"] + [{"label": "Dave", "pubkey": bytes(self.pks[3]).hex()}]}
        rekey = vault.run("propose", {"launcher_id": launcher_id, "successor": successor, "fee": 500, "sponsor": {"coins": self.wallet_coins()}}, self.factory)
        rplan = rekey["plan"]
        self.assertEqual(rplan["summary"]["kind"], "rekey")
        self.assertTrue(rplan["summary"]["successor"]["same_address"])
        share_a = self.share(rplan, 0, extra_sks=[self.wallet_sk])
        self.assertTrue(share_a["sponsor_signed"])
        share_c2 = self.share(rplan, 2)
        done = vault.run("assemble", {"plan": rplan, "shares": [share_a, share_c2], "push": True}, self.factory)
        self.assertTrue(done["success"])
        self.assertEqual(done["agg_sig_count"], 3)

        final = vault.run("read", {"launcher_id": launcher_id}, self.factory)
        self.assertEqual(final["policy"]["m"], 3)
        self.assertEqual([o["label"] for o in final["policy"]["owners"]], ["Alice", "Bob", "Carol", "Dave"])
        self.assertEqual(final["deposit_address"], deposit, "the deposit address never changes")
        self.assertEqual(final["spends"], 2)
        self.assertEqual([h["event"] for h in final["history"]], ["launch", "spend", "rekey"])
        # The funds did not move for the re-key.
        self.assertEqual(vault.run("balance", {"launcher_id": launcher_id}, self.factory)["xch"]["balance"], 1_901)

        # The new policy governs: two owners are no longer enough.
        later = vault.run("propose", {"launcher_id": launcher_id, "outputs": [{"puzzle_hash": recipient.hex(), "amount": 1}], "fee": 0}, self.factory)
        lplan = later["plan"]
        with self.assertRaises(tool.MultisigError):
            vault.run("assemble", {"plan": lplan, "shares": [self.share(lplan, 0), self.share(lplan, 1)]}, self.factory)
        ok = vault.run("assemble", {"plan": lplan, "shares": [self.share(lplan, 0), self.share(lplan, 1), self.share(lplan, 3)], "push": True}, self.factory)
        self.assertTrue(ok["success"])

    def test_launch_can_carry_the_creators_profile(self) -> None:
        import multisig_profile as profile
        creator_pk = self.wallet_sk.get_g1()
        built = vault.run("launch", {
            "network": "testnet11", "policy": self.policy, "coins": self.wallet_coins(), "fee": 0,
            "profile": {"pubkey": bytes(creator_pk).hex(), "safes": [], "include_self": True},
        }, self.factory)
        self.chain.apply(built["coin_spends"])
        read = profile.run("read", {"pubkey": bytes(creator_pk).hex()}, self.factory)
        self.assertTrue(read["found"])
        entry = read["safes"][0]
        self.assertEqual(entry["puzzle_hash"], built["deposit_puzzle_hash"])
        self.assertTrue(entry["resolved"], "resolved through the vault pointer and lineage")
        self.assertEqual(entry.get("puzzle"), "vault")
        self.assertEqual(entry["launcher_id"], built["launcher_id"])
        self.assertEqual(entry["m"], 2)

    def test_owner_whose_wallet_key_pays_the_fee_signs_both_roles_at_once(self) -> None:
        """The Sage case: the owner key is the wallet key of the coin sponsoring the fee."""
        # A 1-of-1 vault owned by the wallet key itself.
        policy = {"name": "Solo", "m": 1, "owners": [{"label": "Me", "pubkey": bytes(self.wallet_sk.get_g1()).hex()}]}
        built = vault.run("launch", {"network": "testnet11", "policy": policy, "coins": self.wallet_coins(), "fee": 0}, self.factory)
        self.chain.apply(built["coin_spends"])
        launcher_id = built["launcher_id"]
        newcomer = {"label": "Bob", "pubkey": bytes(self.pks[1]).hex()}
        rekey = vault.run("propose", {"launcher_id": launcher_id, "successor": {"name": "Solo", "m": 1, "owners": policy["owners"] + [newcomer]}, "fee": 400, "sponsor": {"coins": self.wallet_coins()}}, self.factory)
        plan = rekey["plan"]
        request = vault.run("sign-request", {"plan": plan, "signer": bytes(self.wallet_sk.get_g1()).hex()}, self.factory)
        # One wallet, one key, two messages: the vote and the fee coin.
        share = vault.run("verify-share", {"plan": plan, "signature": sage_like([self.wallet_sk], request["coin_spends"]), "candidates": request["selected_pubkeys"]}, self.factory)
        self.assertEqual(share["owner_keys"], [bytes(self.wallet_sk.get_g1()).hex()])
        self.assertTrue(share["sponsor_signed"])
        done = vault.run("assemble", {"plan": plan, "shares": [share], "push": True}, self.factory)
        self.assertTrue(done["success"])
        final = vault.run("read", {"launcher_id": launcher_id}, self.factory)
        self.assertEqual([o["label"] for o in final["policy"]["owners"]], ["Me", "Bob"])

    def test_refusals(self) -> None:
        built = self.launch()
        launcher_id = built["launcher_id"]
        with self.assertRaises(tool.MultisigError):
            vault.run("propose", {"launcher_id": launcher_id, "successor": self.policy, "fee": 0}, self.factory)  # identical policy
        with self.assertRaises(tool.MultisigError):
            vault.run("propose", {"launcher_id": launcher_id, "successor": {**self.policy, "m": 1}, "fee": 5}, self.factory)  # fee with no sponsor
        with self.assertRaises(tool.MultisigError):
            vault.run("propose", {"launcher_id": launcher_id, "outputs": [{"puzzle_hash": "aa" * 32, "amount": 2}], "fee": 0}, self.factory)  # only the 1-mojo pointer is there
        # A stranger's signature is worthless.
        deposit_ph = bytes32.fromhex(built["deposit_puzzle_hash"])
        self.chain.add(Coin(bytes32(b"\x03" * 32), deposit_ph, uint64(500)))
        plan = vault.run("propose", {"launcher_id": launcher_id, "outputs": [{"puzzle_hash": "aa" * 32, "amount": 1}], "fee": 0}, self.factory)["plan"]
        request = vault.run("sign-request", {"plan": plan, "signer": bytes(self.pks[0]).hex()}, self.factory)
        forged = sage_like([AugSchemeMPL.key_gen(b"\x99" * 32)], request["coin_spends"])
        with self.assertRaises(tool.MultisigError):
            vault.run("verify-share", {"plan": plan, "signature": forged}, self.factory)


if __name__ == "__main__":
    unittest.main()
