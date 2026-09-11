"""The on-chain profile and safe manifest: encode, build the publishing spend,
read it back, resolve by address, and refuse what must be refused."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

CONTRACTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTRACTS_DIR))

from chia.types.blockchain_format.coin import Coin  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key  # noqa: E402
from chia_rs import AugSchemeMPL  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

import multisig_profile as profile  # noqa: E402
import multisig_tool as tool  # noqa: E402


class FakeNode(tool.Node):
    def __init__(self) -> None:
        super().__init__("http://fake")
        self.records: list[dict[str, Any]] = []
        self.spends: dict[tuple[str, int], dict[str, str]] = {}

    def rpc(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        if route == "get_coin_records_by_puzzle_hash":
            ph = tool.strip0x(payload["puzzle_hash"])
            return {"success": True, "coin_records": [r for r in self.records if r["coin"]["puzzle_hash"] == ph]}
        if route == "get_puzzle_and_solution":
            key = (tool.strip0x(payload["coin_id"]), int(payload["height"]))
            if key not in self.spends:
                raise tool.MultisigError("no such spend")
            return {"success": True, "coin_solution": self.spends[key]}
        raise AssertionError(route)

    def confirm(self, spend_json: dict[str, Any], height: int) -> None:
        """Pretend a built spend was included: record its outputs and the spend."""
        coin = tool.coin_from_json(spend_json["coin"])
        self.spends[(coin.name().hex(), height)] = {"puzzle_reveal": spend_json["puzzle_reveal"], "solution": spend_json["solution"]}
        puzzle = Program.from_bytes(bytes.fromhex(spend_json["puzzle_reveal"]))
        solution = Program.from_bytes(bytes.fromhex(spend_json["solution"]))
        from chia.consensus.condition_tools import conditions_dict_for_solution
        for cond in conditions_dict_for_solution(puzzle, solution, tool.MAX_CLVM_COST).get(profile.CREATE_COIN, []):
            child = Coin(coin.name(), bytes32(cond.vars[0]), uint64(int.from_bytes(cond.vars[1], "big")))
            self.records.append({"coin": tool.coin_to_json(child), "spent": False, "confirmed_block_index": height, "timestamp": 1_700_000_000 + height})


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sk = AugSchemeMPL.key_gen(b"\x05" * 32)
        self.pk = self.sk.get_g1()
        self.pk_hex = bytes(self.pk).hex()
        self.puzzle = puzzle_for_synthetic_public_key(self.pk)
        self.ph = self.puzzle.get_tree_hash()
        self.node = FakeNode()
        self.factory = lambda _url: self.node
        others = [bytes(AugSchemeMPL.key_gen(bytes([i]) * 32).get_g1()).hex() for i in (11, 12)]
        self.safes = [
            {"name": "Treasury", "m": 2, "owners": [{"label": "Me", "pubkey": self.pk_hex}, {"label": "Bob", "pubkey": others[0]}, {"label": "Carol", "pubkey": others[1]}]},
            {"name": "Watched | weird; name=1", "m": 1, "owners": [{"label": "", "pubkey": others[1]}]},
        ]

    def wallet_coins(self, amounts: list[int]) -> list[dict[str, Any]]:
        coins = []
        for i, amount in enumerate(amounts):
            coin = Coin(bytes32(bytes([i + 1]) * 32), self.ph, uint64(amount))
            coins.append({"coin": tool.coin_to_json(coin), "puzzle": bytes(self.puzzle).hex()})
        return coins

    def test_round_trip_through_a_fake_chain(self) -> None:
        self.assertFalse(profile.run("read", {"pubkey": self.pk_hex}, self.factory)["found"])

        built = profile.run("build-publish", {"pubkey": self.pk_hex, "coins": self.wallet_coins([5_000, 200]), "safes": self.safes, "fee": 100}, self.factory)
        self.assertEqual(built["coin_count"], 1)  # 5000 covers 101
        self.node.confirm(built["coin_spends"][0], height=500)

        read = profile.run("read", {"pubkey": self.pk_hex}, self.factory)
        self.assertTrue(read["found"])
        self.assertEqual(read["height"], 500)
        self.assertEqual(len(read["safes"]), 2)
        self.assertEqual(read["safes"][0]["name"], "Treasury")
        self.assertEqual(read["safes"][0]["m"], 2)
        self.assertEqual([o["label"] for o in read["safes"][0]["owners"]], ["Me", "Bob", "Carol"])
        # Separator characters in names are neutralised, not allowed to corrupt the record.
        self.assertEqual(read["safes"][1]["name"], "Watched   weird  name 1")

        # A newer record replaces the old one; an unrelated 1-mojo coin is ignored.
        newer = profile.run("build-publish", {"pubkey": self.pk_hex, "coins": self.wallet_coins([5_000]), "safes": self.safes[:1], "fee": 0}, self.factory)
        self.node.confirm(newer["coin_spends"][0], height=600)
        self.node.records.append({"coin": tool.coin_to_json(Coin(bytes32(b"\x77" * 32), self.ph, uint64(1))), "spent": False, "confirmed_block_index": 700, "timestamp": 0})
        read = profile.run("read", {"pubkey": self.pk_hex}, self.factory)
        self.assertEqual(read["height"], 600)
        self.assertEqual(len(read["safes"]), 1)

    def test_multi_coin_publish_links_the_spends(self) -> None:
        built = profile.run("build-publish", {"pubkey": self.pk_hex, "coins": self.wallet_coins([60, 50, 40]), "safes": self.safes, "fee": 100}, self.factory)
        self.assertEqual(built["coin_count"], 2)
        for spend in built["coin_spends"]:
            self.node.confirm(spend, height=10)
        read = profile.run("read", {"pubkey": self.pk_hex}, self.factory)
        self.assertTrue(read["found"])

    def test_manifest_lets_a_safe_be_observed_by_address_and_profile_stores_the_address(self) -> None:
        treasury = self.safes[0]
        # Nothing at the safe address yet.
        target_ph = profile.policy_puzzle_hash(profile.normalize_safes([treasury])[0])
        self.assertFalse(profile.run("manifest-read", {"puzzle_hash": target_ph.hex()}, self.factory)["found"])

        # Any wallet publishes the manifest; it lands at the safe's own address.
        built = profile.run("manifest-build", {"coins": self.wallet_coins([9_000]), "safe": treasury, "fee": 10}, self.factory)
        self.assertEqual(built["puzzle_hash"], target_ph.hex())
        self.node.confirm(built["coin_spends"][0], height=300)
        found = profile.run("manifest-read", {"address": built["address"]}, self.factory)
        self.assertTrue(found["found"])
        self.assertEqual(found["safe"]["name"], "Treasury")
        self.assertEqual(found["safe"]["m"], 2)
        self.assertEqual([o["label"] for o in found["safe"]["owners"]], ["Me", "Bob", "Carol"])

        # A forged manifest — right tag, wrong policy for the address — is ignored.
        forged = dict(treasury, m=1)
        forged_ph = profile.policy_puzzle_hash(profile.normalize_safes([forged])[0])
        self.assertNotEqual(forged_ph, target_ph)
        memos = [bytes(target_ph), profile.MANIFEST_TAG, profile.encode_safe(profile.normalize_safes([forged])[0])]
        fake = profile.build_record(target_ph, memos, self.wallet_coins([9_000]), 0, tag=profile.MANIFEST_TAG)
        self.node.confirm({"coin": fake["coin_spends"][0]["coin"], **fake["coin_spends"][0]}, height=400)
        still = profile.run("manifest-read", {"puzzle_hash": target_ph.hex()}, self.factory)
        self.assertEqual(still["height"], 300, "the forged, newer record must be skipped")

        # The profile can now name the safe by address with a display name of its own.
        published = profile.run("build-publish", {"pubkey": self.pk_hex, "coins": self.wallet_coins([9_000]), "safes": [{"name": "Our treasury", "puzzle_hash": target_ph.hex()}, {"name": "", "puzzle_hash": "ab" * 32}], "fee": 0}, self.factory)
        self.assertLess(published["memo_bytes"], 200)
        self.node.confirm(published["coin_spends"][0], height=500)
        read = profile.run("read", {"pubkey": self.pk_hex}, self.factory)
        self.assertTrue(read["found"])
        first, second = read["safes"]
        self.assertTrue(first["resolved"])
        self.assertEqual(first["name"], "Our treasury")
        self.assertEqual(first["manifest_name"], "Treasury")
        self.assertEqual(first["m"], 2)
        self.assertEqual(len(first["owners"]), 3)
        self.assertFalse(second["resolved"])

    def test_creation_posts_manifest_and_profile_in_one_spend(self) -> None:
        treasury = self.safes[0]
        target_ph = profile.policy_puzzle_hash(profile.normalize_safes([treasury])[0])
        built = profile.run("build-publish", {
            "pubkey": self.pk_hex,
            "coins": self.wallet_coins([9_000]),
            "safes": [{"name": "", "puzzle_hash": target_ph.hex()}],
            "manifest": treasury,
            "fee": 5,
        }, self.factory)
        self.assertEqual(built["coin_count"], 1)
        self.assertEqual(built["manifest"]["puzzle_hash"], target_ph.hex())
        self.node.confirm(built["coin_spends"][0], height=800)
        # Both records are readable, and the by-address profile entry resolves
        # through the manifest that the very same spend created.
        manifest = profile.run("manifest-read", {"puzzle_hash": target_ph.hex()}, self.factory)
        self.assertTrue(manifest["found"])
        read = profile.run("read", {"pubkey": self.pk_hex}, self.factory)
        self.assertTrue(read["found"])
        self.assertTrue(read["safes"][0]["resolved"])
        self.assertEqual(read["safes"][0]["name"], "Treasury")
        self.assertEqual(read["safes"][0]["m"], 2)
        # Several manifests at once: one spend, one mojo each, all readable.
        both = profile.run("build-publish", {"pubkey": self.pk_hex, "coins": self.wallet_coins([9_000]), "safes": [], "manifests": self.safes, "fee": 0}, self.factory)
        self.assertEqual(len(both["manifests"]), 2)
        self.node.confirm(both["coin_spends"][0], height=900)
        for entry in both["manifests"]:
            self.assertTrue(profile.run("manifest-read", {"puzzle_hash": entry["puzzle_hash"]}, self.factory)["found"])
        # Two records need two mojos: 6 mojos of coins cannot cover 2 + fee 5.
        with self.assertRaises(tool.MultisigError):
            profile.run("build-publish", {"pubkey": self.pk_hex, "coins": self.wallet_coins([6]), "safes": [], "manifest": treasury, "fee": 5}, self.factory)

    def test_key_is_found_from_an_address_once_it_has_spent(self) -> None:
        address = tool.encode_puzzle_hash(self.ph, "txch")
        # Never spent: nothing to learn from the address.
        self.assertFalse(profile.run("key-for-address", {"address": address}, self.factory)["found"])
        # A profile publish is a spend from the address; its reveal carries the key.
        built = profile.run("build-publish", {"pubkey": self.pk_hex, "coins": self.wallet_coins([5_000]), "safes": [], "fee": 0}, self.factory)
        spend = built["coin_spends"][0]
        coin = tool.coin_from_json(spend["coin"])
        self.node.records.append({"coin": spend["coin"], "spent": True, "spent_block_index": 42, "confirmed_block_index": 1})
        self.node.spends[(coin.name().hex(), 42)] = {"puzzle_reveal": spend["puzzle_reveal"], "solution": spend["solution"]}
        found = profile.run("key-for-address", {"address": address}, self.factory)
        self.assertTrue(found["found"])
        self.assertEqual(found["public_key"], self.pk_hex)
        self.assertEqual(found["spent_height"], 42)

    def test_emoji_in_names_and_labels_round_trip(self) -> None:
        name = "aWizard \U0001F510 treasury"
        label = "Me \U0001F9D9"
        safe = {"name": name, "m": 1, "owners": [{"label": label, "pubkey": self.pk_hex}]}
        built = profile.run("build-publish", {"pubkey": self.pk_hex, "coins": self.wallet_coins([9_000]), "safes": [safe], "manifests": [safe], "fee": 0}, self.factory)
        self.node.confirm(built["coin_spends"][0], height=950)
        read = profile.run("read", {"pubkey": self.pk_hex}, self.factory)
        self.assertEqual(read["safes"][0]["name"], name)
        self.assertEqual(read["safes"][0]["owners"][0]["label"], label)
        manifest = profile.run("manifest-read", {"puzzle_hash": built["manifests"][0]["puzzle_hash"]}, self.factory)
        self.assertEqual(manifest["safe"]["name"], name)
        # A lone surrogate (what a mis-decoded byte looks like) is dropped, not fatal.
        self.assertEqual(profile.clean_text("bad " + chr(0xDC90) + " byte", 40), "bad  byte")

    def test_refusals(self) -> None:
        with self.assertRaises(tool.MultisigError):
            profile.run("build-publish", {"pubkey": self.pk_hex, "coins": self.wallet_coins([50]), "safes": self.safes, "fee": 100}, self.factory)
        bad = self.wallet_coins([5_000])
        bad[0]["puzzle"] = bytes(Program.to((1, []))).hex()
        with self.assertRaises(tool.MultisigError):
            profile.run("build-publish", {"pubkey": self.pk_hex, "coins": bad, "safes": self.safes, "fee": 0}, self.factory)
        with self.assertRaises(tool.MultisigError):
            profile.run("build-publish", {"pubkey": self.pk_hex, "coins": self.wallet_coins([5_000]), "safes": [{"name": "x", "m": 3, "owners": [{"pubkey": self.pk_hex}]}], "fee": 0}, self.factory)


if __name__ == "__main__":
    unittest.main()
