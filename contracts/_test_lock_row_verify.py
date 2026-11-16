"""The lock index, checked row by row against the chain (``vault_tool.py verify-rows``).

The router's lock index is a cache, and it has held rows no lock stands behind
(PRs 56-57, 75). A row names a configuration; the configuration makes puzzle hashes;
so a row is judged by recomputing them and finding a live coin, never by trusting it.
This suite pins, on a fake node:

* a real lock's row is `live`: an unspent coin sits at the singleton puzzle hash its
  launcher and policy make, and that coin is the lock's own singleton;
* a coin someone SENT to that puzzle hash is not the singleton and proves nothing;
* a row whose launcher does not exist, whose address is not its launcher's deposit
  address, or whose launcher is not a singleton launcher is `phantom`;
* a row whose policy moved on (a missed re-key) is `stale`, not phantom;
* a node that does not answer gives `unknown`, never `phantom`;
* a legacy safe row is `phantom` exactly when its owners and threshold do not make its
  address (the PR 56 row: a vault lock's policy filed as a legacy safe).
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


KEYS = [AugSchemeMPL.key_gen(bytes([81 + i] * 32)).get_g1() for i in range(3)]
A, B, C = KEYS
POLICY = vault.Policy("Real Lock", 1, (("a", A), ("b", B)), vault.FORMAT_MIPS)
LAUNCHER_PARENT = bytes32([0x09] * 32)
LAUNCHER_COIN = Coin(LAUNCHER_PARENT, vault.SINGLETON_LAUNCHER_HASH, uint64(1))
LAUNCHER = LAUNCHER_COIN.name()
VAULT_PH = vault.vault_puzzle(LAUNCHER, POLICY).get_tree_hash()
EVE = Coin(LAUNCHER, VAULT_PH, uint64(1))
TIP = Coin(EVE.name(), VAULT_PH, uint64(1))
# Someone sends an odd coin to the singleton's puzzle hash from an ordinary wallet coin.
IMPOSTER_PARENT = Coin(bytes32([0x0A] * 32), bytes32([0x0B] * 32), uint64(5))
IMPOSTER = Coin(IMPOSTER_PARENT.name(), VAULT_PH, uint64(3))


def record(coin: Coin, spent: bool, confirmed: int = 10, spent_at: int = 0) -> dict:
    return {"coin": {"parent_coin_info": "0x" + coin.parent_coin_info.hex(), "puzzle_hash": "0x" + coin.puzzle_hash.hex(),
                     "amount": int(coin.amount)},
            "confirmed_block_index": confirmed, "spent": spent, "spent_block_index": spent_at, "coinbase": False, "timestamp": 0}


class FakeNode(tool.Node):
    """A chain with one lock: launcher -> eve (spent) -> tip (unspent)."""

    def __init__(self, at_vault_ph: list[Coin], down: bool = False, eve_unspent: bool = False) -> None:
        super().__init__("http://fake")
        self.at_vault_ph = at_vault_ph
        self.down = down
        self.coins = {LAUNCHER: record(LAUNCHER_COIN, True, 9, 10), EVE.name(): record(EVE, not eve_unspent, 10, 0 if eve_unspent else 11),
                      TIP.name(): record(TIP, False, 11), IMPOSTER_PARENT.name(): record(IMPOSTER_PARENT, True, 9, 12),
                      IMPOSTER.name(): record(IMPOSTER, False, 12)}

    def rpc(self, route: str, payload: dict) -> dict:
        if self.down:
            raise tool.MultisigError("node get_coin_record_by_name unreachable: timed out")
        if route == "get_coin_record_by_name":
            found = self.coins.get(bytes32.fromhex(tool.strip0x(payload["name"])))
            if found is None:
                return {"success": False, "error": f"Coin record 0x{payload['name']} not found"}
            return {"success": True, "coin_record": found}
        if route == "get_coin_records_by_puzzle_hash":
            ph = bytes32.fromhex(tool.strip0x(payload["puzzle_hash"]))
            coins = self.at_vault_ph if ph == VAULT_PH else []
            return {"success": True, "coin_records": [self.coins[c.name()] for c in coins]}
        if route == "get_puzzle_and_solution":
            coin_id = bytes32.fromhex(tool.strip0x(payload["coin_id"]))
            if coin_id == EVE.name():
                puzzle = vault.vault_puzzle(LAUNCHER, POLICY)
            elif coin_id == IMPOSTER_PARENT.name():
                puzzle = vault.Program.to(1)  # an ordinary wallet's spend, not the singleton
            else:
                raise AssertionError(f"no spend for {coin_id.hex()}")
            return {"success": True, "coin_solution": {"puzzle_reveal": bytes(puzzle).hex(), "solution": "80"}}
        raise AssertionError(f"unexpected route {route}")


def row(**extra) -> dict:
    base = {"id": "real", "puzzle": "vault", "name": POLICY.name, "threshold": POLICY.m, "policyFormat": "mips/1",
            "launcherId": LAUNCHER.hex(), "puzzleHash": vault.deposit_puzzle_hash(LAUNCHER).hex(),
            "owners": [{"label": label, "pubkey": bytes(key).hex()} for label, key in POLICY.owners]}
    return {**base, **extra}


def verdict(node: FakeNode, entry: dict) -> str:
    return vault.verify_index_rows(node, [entry])[0]["verdict"]


# ─── a real lock ─────────────────────────────────────────────────────────────

chain = FakeNode([TIP])
result = vault.verify_index_rows(chain, [row()])[0]
check("a real lock's row is live", result["verdict"], "live")
check("at the singleton coin", result.get("coin_id"), TIP.name().hex())
check("the eve itself (parent = launcher) is the singleton too", verdict(FakeNode([EVE], eve_unspent=True), row()), "live")

# A coin sent to the singleton's puzzle hash is not the singleton. With nothing else
# there, the lock is read from its launcher instead.
vault.read_vault = lambda _node, _launcher: vault.VaultState(  # type: ignore[assignment]
    LAUNCHER, vault.Policy("Real Lock", 2, (("a", A), ("b", B), ("c", C)), vault.FORMAT_MIPS), TIP,
    SingletonLineageProof(EVE.parent_coin_info, POLICY.inner_puzzle_hash(), uint64(1)), height=11, spends=1)
check("a coin sent to the singleton puzzle hash proves nothing", verdict(FakeNode([IMPOSTER]), row()), "stale")

# ─── rows the chain contradicts ──────────────────────────────────────────────

ghost = bytes32([0x77] * 32)
check("a row whose launcher does not exist is phantom",
      verdict(chain, row(launcherId=ghost.hex(), puzzleHash=vault.deposit_puzzle_hash(ghost).hex())), "phantom")
check("a row whose address is not its launcher's deposit address is phantom",
      verdict(chain, row(puzzleHash="12" * 32)), "phantom")
check("a row whose launcher id is a coin that is not a launcher is phantom",
      verdict(chain, row(launcherId=TIP.name().hex(), puzzleHash=vault.deposit_puzzle_hash(TIP.name()).hex())), "phantom")
check("a vault row with no launcher is phantom", verdict(chain, row(launcherId="")), "phantom")

# ─── real, but behind ────────────────────────────────────────────────────────

stale = vault.verify_index_rows(FakeNode([TIP]), [row(threshold=1, owners=[{"label": "a", "pubkey": bytes(A).hex()}])])[0]
check("a row whose policy moved on is stale, not phantom", stale["verdict"], "stale")
check("and the chain's policy comes back with it", stale["policy"]["m"], 2)

# ─── the node did not answer ─────────────────────────────────────────────────

check("an unreachable node is unknown, never phantom", verdict(FakeNode([TIP], down=True), row()), "unknown")

# ─── legacy safes ────────────────────────────────────────────────────────────

safe = tool.Safe(1, (A, B))
legacy = {"id": "legacy", "puzzle": "p2_m_of_n_delegate_direct", "threshold": 1, "puzzleHash": safe.puzzle_hash().hex(),
          "owners": [{"pubkey": bytes(A).hex()}, {"pubkey": bytes(B).hex()}]}
check("a legacy safe whose owners make its address is live", verdict(chain, legacy), "live")
check("the PR 56 phantom: a vault lock's policy filed as a legacy safe at the lock's address",
      verdict(chain, {**legacy, "puzzleHash": vault.deposit_puzzle_hash(LAUNCHER).hex()}), "phantom")

print(f"lock row verify: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  ✗ " + failure)
sys.exit(1 if FAILED else 0)
