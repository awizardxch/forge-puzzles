"""End-to-end checks for the M-of-N safe builder against a fake node.

What a green run proves: a proposal built by the tool runs under CLVM, emits
exactly the AGG_SIG conditions the plan promises, binds every spend to its own
coin id, and assembles into a bundle whose aggregate signature verifies when
signed by any M owners — each signing independently, the way Sage does. What it
does not prove: mempool acceptance and CAT ring validity against a real parent,
which only a node can judge.
"""

from __future__ import annotations

import hashlib
import os
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
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle  # noqa: E402
from chia.wallet.puzzles.p2_m_of_n_delegate_direct import puzzle_for_m_of_public_key_list  # noqa: E402
from chia_rs import AugSchemeMPL, G2Element  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import multisig_tool as tool  # noqa: E402


def sha(*parts: bytes) -> bytes32:
    return bytes32(hashlib.sha256(b"".join(parts)).digest())


class FakeNode(tool.Node):
    """Serves coin records from memory and remembers what was pushed."""

    def __init__(self) -> None:
        super().__init__("http://fake")
        self.records: dict[bytes32, dict[str, Any]] = {}
        self.reveals: dict[bytes32, Program] = {}
        self.pushed: list[dict[str, Any]] = []

    def add_coin(self, coin: Coin, *, spent: bool = False, height: int = 100) -> Coin:
        self.records[coin.name()] = {
            "coin": tool.coin_to_json(coin),
            "spent": spent,
            "spent_block_index": height + 1 if spent else 0,
            "confirmed_block_index": height,
            "timestamp": 1_700_000_000,
        }
        return coin

    def rpc(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        if route == "get_coin_records_by_puzzle_hashes":
            wanted = {tool.strip0x(ph) for ph in payload["puzzle_hashes"]}
            return {
                "success": True,
                "coin_records": [
                    r for r in self.records.values()
                    if r["coin"]["puzzle_hash"] in wanted and (payload.get("include_spent_coins") or not r["spent"])
                ],
            }
        if route == "get_coin_record_by_name":
            return {"success": True, "coin_record": self.records.get(bytes32.fromhex(payload["name"]))}
        if route == "get_puzzle_and_solution":
            reveal = self.reveals[bytes32.fromhex(payload["coin_id"])]
            return {"success": True, "coin_solution": {"puzzle_reveal": bytes(reveal).hex(), "solution": "80"}}
        if route == "push_tx":
            self.pushed.append(payload)
            return {"success": True, "status": "SUCCESS"}
        raise AssertionError(f"unexpected route {route}")


def sage_like_partial_sign(secret_key, coin_spends_json: list[dict[str, Any]]) -> str:
    """What Sage does with partial=true: sign every AGG_SIG for keys it holds."""
    signatures = []
    for spend in coin_spends_json:
        puzzle = Program.from_bytes(bytes.fromhex(spend["puzzle_reveal"]))
        solution = Program.from_bytes(bytes.fromhex(spend["solution"]))
        conditions = conditions_dict_for_solution(puzzle, solution, tool.MAX_CLVM_COST)
        for condition in conditions.get(ConditionOpcode.AGG_SIG_UNSAFE, []):
            if condition.vars[0] == bytes(secret_key.get_g1()):
                signatures.append(AugSchemeMPL.sign(secret_key, condition.vars[1]))
        coin = tool.coin_from_json(spend["coin"])
        for condition in conditions.get(ConditionOpcode.AGG_SIG_ME, []):
            if condition.vars[0] == bytes(secret_key.get_g1()):
                signatures.append(AugSchemeMPL.sign(secret_key, condition.vars[1] + bytes(coin.name()) + tool.AGG_SIG_ME_DATA["testnet11"]))
    return bytes(AugSchemeMPL.aggregate(signatures)).hex() if signatures else bytes(G2Element()).hex()


class MultisigToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sks = [AugSchemeMPL.key_gen(bytes([i + 1]) * 32) for i in range(3)]
        self.pks = [sk.get_g1() for sk in self.sks]
        self.safe_json = {"m": 2, "pubkeys": [bytes(pk).hex() for pk in self.pks], "network": "testnet11"}
        self.safe = tool.Safe.from_json(self.safe_json)
        self.safe_ph = self.safe.puzzle_hash()
        self.node = FakeNode()
        self.factory = lambda _url: self.node
        self.recipient = bytes32(b"\x11" * 32)
        self.recipient_addr = tool.encode_puzzle_hash(self.recipient, "txch")

    # ── helpers ──────────────────────────────────────────────────────────

    def fund_xch(self, amounts: list[int]) -> list[Coin]:
        return [
            self.node.add_coin(Coin(sha(b"xch-parent", bytes([i])), self.safe_ph, uint64(amount)))
            for i, amount in enumerate(amounts)
        ]

    def fund_cat(self, asset_id: bytes32, amounts: list[int]) -> list[Coin]:
        """A CAT coin at the safe whose parent was a CAT held by some sender."""
        sender_inner = Program.to((1, []))  # any inner puzzle; only its hash matters
        parent_puzzle = construct_cat_puzzle(CAT_MOD, asset_id, sender_inner)
        outer_ph = tool.cat_outer_puzzle_hash(asset_id, self.safe.puzzle())
        coins = []
        for i, amount in enumerate(amounts):
            parent = Coin(sha(b"cat-grandparent", bytes([i])), parent_puzzle.get_tree_hash(), uint64(amount + 7))
            self.node.add_coin(parent, spent=True, height=90)
            self.node.reveals[parent.name()] = parent_puzzle
            coins.append(self.node.add_coin(Coin(parent.name(), outer_ph, uint64(amount)), height=91))
        return coins

    def propose(self, outputs: list[dict[str, Any]], fee: int = 0) -> dict[str, Any]:
        return tool.run("propose", {"safe": self.safe_json, "network": "testnet11", "outputs": outputs, "fee": fee}, self.factory)

    def share_for(self, plan: dict[str, Any], index: int) -> dict[str, Any]:
        request = tool.run("sign-request", {"plan": plan, "signer": bytes(self.pks[index]).hex()}, self.factory)
        self.assertIn(bytes(self.pks[index]).hex(), request["selected_pubkeys"])
        signature = sage_like_partial_sign(self.sks[index], request["coin_spends"])
        return tool.run("verify-share", {"plan": plan, "signature": signature, "candidates": request["selected_pubkeys"]}, self.factory)

    # ── tests ────────────────────────────────────────────────────────────

    def test_derive_matches_cni_reference_puzzle(self) -> None:
        derived = tool.run("derive", self.safe_json, self.factory)
        reference = puzzle_for_m_of_public_key_list(2, [bytes(pk) for pk in self.pks])
        self.assertEqual(derived["puzzle_hash"], reference.get_tree_hash().hex())
        self.assertTrue(derived["address"].startswith("txch1"))
        self.assertEqual(tool.decode_puzzle_hash(derived["address"]), reference.get_tree_hash())

    def test_derive_reports_each_owners_wallet_address(self) -> None:
        from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_hash_for_synthetic_public_key
        derived = tool.run("derive", self.safe_json, self.factory)
        self.assertEqual([o["pubkey"] for o in derived["owners"]], self.safe_json["pubkeys"])
        for owner, pk in zip(derived["owners"], self.pks):
            self.assertEqual(tool.decode_puzzle_hash(owner["address"]), puzzle_hash_for_synthetic_public_key(pk))
            self.assertTrue(owner["address"].startswith("txch1"))

    def test_derive_rejects_bad_shapes(self) -> None:
        with self.assertRaises(tool.MultisigError):
            tool.run("derive", {**self.safe_json, "m": 4}, self.factory)
        with self.assertRaises(tool.MultisigError):
            tool.run("derive", {**self.safe_json, "pubkeys": self.safe_json["pubkeys"][:1] * 2}, self.factory)
        with self.assertRaises(tool.MultisigError):
            tool.run("derive", {**self.safe_json, "pubkeys": ["00" * 48] + self.safe_json["pubkeys"][1:]}, self.factory)

    def test_balance_reads_xch_and_cats(self) -> None:
        self.fund_xch([5_000, 3_000])
        asset = bytes32(b"\xaa" * 32)
        self.fund_cat(asset, [400, 600])
        result = tool.run("balance", {**self.safe_json, "asset_ids": [asset.hex(), "bb" * 32]}, self.factory)
        self.assertEqual(result["xch"]["balance"], 8_000)
        self.assertEqual(len(result["cats"]), 1)
        self.assertEqual(result["cats"][0]["balance"], 1_000)

    def test_xch_proposal_signs_and_assembles_with_any_two_owners(self) -> None:
        self.fund_xch([5_000, 3_000, 1_000])
        proposal = self.propose([{"address": self.recipient_addr, "amount": 6_000}], fee=500)
        plan = proposal["plan"]
        # Largest-first: 5000 + 3000 covers 6500; the 1000 coin stays put.
        self.assertEqual(plan["summary"]["coin_count"], 2)
        self.assertEqual(plan["summary"]["change"], [{"asset_id": None, "amount": 1_500}])

        share_b = self.share_for(plan, 1)
        share_c = self.share_for(plan, 2)
        self.assertEqual(share_b["keys"], [bytes(self.pks[1]).hex()])
        self.assertEqual(share_c["keys"], [bytes(self.pks[2]).hex()])

        assembled = tool.run("assemble", {"plan": plan, "shares": [share_b, share_c], "push": True}, self.factory)
        self.assertTrue(assembled["success"])
        self.assertEqual(assembled["selectors"], [0, 1, 1])
        self.assertEqual(assembled["agg_sig_count"], 4)
        self.assertEqual(len(self.node.pushed), 1)

        # The bundle spends what it says: outputs, change, fee, and the link.
        bundle = assembled["spend_bundle"]
        primary = bundle["coin_spends"][0]
        conditions = conditions_dict_for_solution(
            Program.from_bytes(bytes.fromhex(primary["puzzle_reveal"])),
            Program.from_bytes(bytes.fromhex(primary["solution"])),
            tool.MAX_CLVM_COST,
        )
        creates = {(bytes(c.vars[0]), int.from_bytes(c.vars[1], "big")) for c in conditions[ConditionOpcode.CREATE_COIN]}
        self.assertEqual(creates, {(self.recipient, 6_000), (self.safe_ph, 1_500)})
        self.assertEqual(int.from_bytes(conditions[ConditionOpcode.RESERVE_FEE][0].vars[0], "big"), 500)
        self.assertIn(ConditionOpcode.CREATE_COIN_ANNOUNCEMENT, conditions)
        secondary = bundle["coin_spends"][1]
        conditions2 = conditions_dict_for_solution(
            Program.from_bytes(bytes.fromhex(secondary["puzzle_reveal"])),
            Program.from_bytes(bytes.fromhex(secondary["solution"])),
            tool.MAX_CLVM_COST,
        )
        self.assertIn(ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT, conditions2)
        self.assertNotIn(ConditionOpcode.CREATE_COIN, conditions2)

        # A and C is just as good as B and C.
        share_a = self.share_for(plan, 0)
        again = tool.run("assemble", {"plan": plan, "shares": [share_a, share_c]}, self.factory)
        self.assertEqual(again["selectors"], [1, 0, 1])
        self.assertNotEqual(again["spend_bundle"]["aggregated_signature"], bundle["aggregated_signature"])

    def test_rekey_proposal_moves_funds_and_creates_the_successor_manifest(self) -> None:
        import multisig_profile as profile
        self.fund_xch([10_000])
        successor = {"name": "Treasury v2", "m": 2, "owners": [{"label": "A", "pubkey": bytes(self.pks[0]).hex()}, {"label": "B", "pubkey": bytes(self.pks[1]).hex()}]}
        target_ph = profile.policy_puzzle_hash(profile.normalize_safes([successor])[0])
        # Move everything: balance minus the fee minus the manifest mojo.
        proposal = tool.run("propose", {
            "safe": self.safe_json, "network": "testnet11", "fee": 100,
            "successor": successor,
            "outputs": [{"puzzle_hash": target_ph.hex(), "amount": 10_000 - 100 - 1}],
        }, self.factory)
        plan = proposal["plan"]
        self.assertEqual(plan["summary"]["kind"], "rekey")
        self.assertEqual(plan["summary"]["successor"]["puzzle_hash"], target_ph.hex())
        self.assertEqual(plan["summary"]["change"], [])

        # The primary spend creates the 1-mojo manifest coin at the successor,
        # with memos a reader accepts, and the funds coin beside it.
        share_a = self.share_for(plan, 0)
        share_c = self.share_for(plan, 2)
        assembled = tool.run("assemble", {"plan": plan, "shares": [share_a, share_c]}, self.factory)
        primary = assembled["spend_bundle"]["coin_spends"][0]
        puzzle = Program.from_bytes(bytes.fromhex(primary["puzzle_reveal"]))
        solution = Program.from_bytes(bytes.fromhex(primary["solution"]))
        memos = profile.memos_creating(puzzle, solution, Coin(bytes32.fromhex(primary["coin"]["parent_coin_info"]) and tool.coin_from_json(primary["coin"]).name(), target_ph, uint64(1)))
        self.assertIsNotNone(memos)
        parsed = profile.parse_memos(memos, profile.MANIFEST_TAG)
        self.assertEqual(parsed[0]["name"], "Treasury v2")
        self.assertEqual(profile.policy_puzzle_hash(parsed[0]), target_ph)
        creates = conditions_dict_for_solution(puzzle, solution, tool.MAX_CLVM_COST)[ConditionOpcode.CREATE_COIN]
        amounts = sorted(int.from_bytes(c.vars[1], "big") for c in creates)
        self.assertEqual(amounts, [1, 9_899])

        # A successor identical to the current policy is refused.
        same = {"name": "x", "m": 2, "owners": [{"label": "", "pubkey": k} for k in self.safe_json["pubkeys"]]}
        with self.assertRaises(tool.MultisigError):
            tool.run("propose", {"safe": self.safe_json, "network": "testnet11", "fee": 0, "successor": same, "outputs": []}, self.factory)

    def test_sponsored_rekey_is_paid_by_the_proposer_wallet(self) -> None:
        """The lock holds only its manifest mojo; the proposer's wallet pays."""
        import multisig_profile as profile
        from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key
        self.fund_xch([1])
        # Alice's wallet coin: a standard puzzle for a wallet key of hers.
        wallet_sk = AugSchemeMPL.key_gen(b"\x21" * 32)
        wallet_puzzle = puzzle_for_synthetic_public_key(wallet_sk.get_g1())
        wallet_coin = Coin(sha(b"alice-wallet"), wallet_puzzle.get_tree_hash(), uint64(50_000))
        successor = {"name": "Treasury", "m": 2, "owners": [{"label": "A", "pubkey": bytes(self.pks[0]).hex()}, {"label": "B", "pubkey": bytes(self.pks[1]).hex()}]}
        target_ph = profile.policy_puzzle_hash(profile.normalize_safes([successor])[0])

        proposal = tool.run("propose", {
            "safe": self.safe_json, "network": "testnet11", "fee": 700,
            "successor": successor,
            "outputs": [{"address": "successor", "amount": 1}],
            "sponsor": {"coins": [{"coin": tool.coin_to_json(wallet_coin), "puzzle": bytes(wallet_puzzle).hex()}]},
        }, self.factory)
        plan = proposal["plan"]
        self.assertEqual(plan["summary"]["fee_paid_by"], "sponsor")
        self.assertEqual(plan["summary"]["sponsor"]["outputs"], [1])
        self.assertEqual(len(proposal["coin_ids"]), 2, "lock coin plus the sponsor coin")

        # Alice signs from the wallet that holds both her owner key and the
        # sponsor key: one aggregate covering both, recognized as such.
        request = tool.run("sign-request", {"plan": plan, "signer": bytes(self.pks[0]).hex()}, self.factory)
        self.assertEqual(len(request["coin_spends"]), 2)
        owner_sig = G2Element.from_bytes(bytes.fromhex(sage_like_partial_sign(self.sks[0], request["coin_spends"])))
        wallet_sig = G2Element.from_bytes(bytes.fromhex(sage_like_partial_sign(wallet_sk, request["coin_spends"])))
        both = bytes(AugSchemeMPL.aggregate([owner_sig, wallet_sig])).hex()
        share_a = tool.run("verify-share", {"plan": plan, "signature": both, "candidates": request["selected_pubkeys"]}, self.factory)
        self.assertTrue(share_a["sponsor_signed"])
        self.assertEqual(share_a["owner_keys"], [bytes(self.pks[0]).hex()])
        self.assertEqual(share_a["keys"], [bytes(self.pks[0]).hex()], "the sponsor key is a role, not an owner")

        # Alice plus Carol is 2 of 3; one lock coin, so 2 owner signatures plus the sponsor's.
        share_c = self.share_for(plan, 2)
        assembled = tool.run("assemble", {"plan": plan, "shares": [share_a, share_c], "push": True}, self.factory)
        self.assertTrue(assembled["success"])
        self.assertEqual(assembled["agg_sig_count"], 1 * 2 + 1)
        self.assertEqual(assembled["selectors"], [1, 0, 1])
        bundle = assembled["spend_bundle"]
        self.assertEqual(len(bundle["coin_spends"]), 2)
        # The sponsor spend pays the fee, creates the manifest at the successor, and returns change.
        sponsor_spend = bundle["coin_spends"][1]
        conditions = conditions_dict_for_solution(Program.from_bytes(bytes.fromhex(sponsor_spend["puzzle_reveal"])), Program.from_bytes(bytes.fromhex(sponsor_spend["solution"])), tool.MAX_CLVM_COST)
        self.assertEqual(int.from_bytes(conditions[ConditionOpcode.RESERVE_FEE][0].vars[0], "big"), 700)
        creates = {(bytes(c.vars[0]), int.from_bytes(c.vars[1], "big")) for c in conditions[ConditionOpcode.CREATE_COIN]}
        self.assertIn((bytes(target_ph), 1), creates)
        self.assertIn((bytes(wallet_puzzle.get_tree_hash()), 50_000 - 700 - 1), creates)

        # Without the sponsor signature, two owner signatures cannot assemble.
        share_a_owner_only = tool.run("verify-share", {"plan": plan, "signature": bytes(owner_sig).hex(), "candidates": request["selected_pubkeys"]}, self.factory)
        self.assertFalse(share_a_owner_only["sponsor_signed"])
        with self.assertRaises(tool.MultisigError):
            tool.run("assemble", {"plan": plan, "shares": [share_a_owner_only, share_c]}, self.factory)

    def test_one_share_is_not_enough(self) -> None:
        self.fund_xch([10_000])
        plan = self.propose([{"address": self.recipient_addr, "amount": 1}])["plan"]
        share_a = self.share_for(plan, 0)
        with self.assertRaises(tool.MultisigError):
            tool.run("assemble", {"plan": plan, "shares": [share_a]}, self.factory)
        with self.assertRaises(tool.MultisigError):
            tool.run("assemble", {"plan": plan, "shares": [share_a, share_a]}, self.factory)

    def test_wrong_signature_is_refused(self) -> None:
        self.fund_xch([10_000])
        plan = self.propose([{"address": self.recipient_addr, "amount": 1}])["plan"]
        stranger = AugSchemeMPL.key_gen(b"\x99" * 32)
        request = tool.run("sign-request", {"plan": plan, "signer": bytes(self.pks[0]).hex()}, self.factory)
        forged = sage_like_partial_sign(stranger, request["coin_spends"])
        with self.assertRaises(tool.MultisigError):
            tool.run("verify-share", {"plan": plan, "signature": forged}, self.factory)
        # A signature over a different proposal's messages is just as dead.
        other_plan = self.propose([{"address": self.recipient_addr, "amount": 2}])["plan"]
        other_request = tool.run("sign-request", {"plan": other_plan, "signer": bytes(self.pks[0]).hex()}, self.factory)
        crossed = sage_like_partial_sign(self.sks[0], other_request["coin_spends"])
        with self.assertRaises(tool.MultisigError):
            tool.run("verify-share", {"plan": plan, "signature": crossed}, self.factory)

    def test_wallet_holding_two_owner_keys_returns_a_two_key_share(self) -> None:
        self.fund_xch([10_000])
        plan = self.propose([{"address": self.recipient_addr, "amount": 1}])["plan"]
        request = tool.run("sign-request", {"plan": plan, "signer": bytes(self.pks[0]).hex()}, self.factory)
        # Selected keys are A and B; a wallet holding both signs for both.
        sig_a = sage_like_partial_sign(self.sks[0], request["coin_spends"])
        sig_b = sage_like_partial_sign(self.sks[1], request["coin_spends"])
        both = bytes(AugSchemeMPL.aggregate([G2Element.from_bytes(bytes.fromhex(sig_a)), G2Element.from_bytes(bytes.fromhex(sig_b))])).hex()
        share = tool.run("verify-share", {"plan": plan, "signature": both, "candidates": request["selected_pubkeys"]}, self.factory)
        self.assertEqual(sorted(share["keys"]), sorted([bytes(self.pks[0]).hex(), bytes(self.pks[1]).hex()]))
        assembled = tool.run("assemble", {"plan": plan, "shares": [share]}, self.factory)
        self.assertEqual(assembled["selectors"], [1, 1, 0])

    def test_cat_proposal_with_fee_from_xch(self) -> None:
        asset = bytes32(b"\xaa" * 32)
        self.fund_xch([2_000])
        self.fund_cat(asset, [400, 600])
        proposal = self.propose([{"address": self.recipient_addr, "amount": 700, "asset_id": asset.hex()}], fee=100)
        plan = proposal["plan"]
        kinds = [s["kind"] for s in plan["spends"]]
        self.assertEqual(kinds, ["xch", "cat", "cat"])
        self.assertEqual(plan["summary"]["change"], [{"asset_id": None, "amount": 1_900}, {"asset_id": asset.hex(), "amount": 300}])

        share_a = self.share_for(plan, 0)
        share_b = self.share_for(plan, 1)
        assembled = tool.run("assemble", {"plan": plan, "shares": [share_a, share_b]}, self.factory)
        self.assertTrue(assembled["success"])
        self.assertEqual(assembled["agg_sig_count"], 6)

        # The CAT outputs are inner-puzzle CREATE_COINs with a hint memo, and
        # the CAT ring's own announcements come out of the outer puzzle.
        cat_primary = assembled["spend_bundle"]["coin_spends"][1]
        conditions = conditions_dict_for_solution(
            Program.from_bytes(bytes.fromhex(cat_primary["puzzle_reveal"])),
            Program.from_bytes(bytes.fromhex(cat_primary["solution"])),
            tool.MAX_CLVM_COST,
        )
        creates = [c for c in conditions[ConditionOpcode.CREATE_COIN] if int.from_bytes(c.vars[1], "big") > 0]
        amounts = sorted(int.from_bytes(c.vars[1], "big") for c in creates)
        self.assertEqual(amounts, [300, 700])

    def test_cat_without_xch_cannot_pay_a_fee(self) -> None:
        asset = bytes32(b"\xaa" * 32)
        self.fund_cat(asset, [400])
        with self.assertRaises(tool.MultisigError):
            self.propose([{"address": self.recipient_addr, "amount": 100, "asset_id": asset.hex()}], fee=1)
        # …but a zero-fee CAT send is fine.
        plan = self.propose([{"address": self.recipient_addr, "amount": 100, "asset_id": asset.hex()}])["plan"]
        self.assertEqual([s["kind"] for s in plan["spends"]], ["cat"])

    def test_insufficient_funds_and_bad_outputs(self) -> None:
        self.fund_xch([100])
        with self.assertRaises(tool.MultisigError):
            self.propose([{"address": self.recipient_addr, "amount": 101}])
        with self.assertRaises(tool.MultisigError):
            self.propose([{"address": self.recipient_addr.replace("txch1", "xch1"), "amount": 1}])
        with self.assertRaises(tool.MultisigError):
            self.propose([{"address": self.recipient_addr, "amount": 0}])

    def test_plan_without_my_coin_id_is_refused(self) -> None:
        """The replay guard: a delegated puzzle that does not pin its coin."""
        self.fund_xch([10_000])
        plan = self.propose([{"address": self.recipient_addr, "amount": 1}])["plan"]
        loose = Program.to((1, [[ConditionOpcode.CREATE_COIN, self.recipient, 1]]))
        plan["spends"][0]["delegated_puzzle"] = bytes(loose).hex()
        with self.assertRaises(tool.MultisigError):
            tool.run("sign-request", {"plan": plan, "signer": bytes(self.pks[0]).hex()}, self.factory)

    def test_tampered_plan_hash_is_refused(self) -> None:
        self.fund_xch([10_000])
        plan = self.propose([{"address": self.recipient_addr, "amount": 1}])["plan"]
        plan["safe"]["m"] = 1
        with self.assertRaises(tool.MultisigError):
            tool.run("sign-request", {"plan": plan, "signer": bytes(self.pks[0]).hex()}, self.factory)

    def test_status_reports_spent_coins(self) -> None:
        coins = self.fund_xch([10_000, 20])
        self.node.records[coins[1].name()]["spent"] = True
        result = tool.run("status", {"coin_ids": [c.name().hex() for c in coins]}, self.factory)
        self.assertTrue(result["any_spent"])
        self.assertFalse(result["all_spent"])


if __name__ == "__main__":
    unittest.main()
