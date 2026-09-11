"""A lock's policy in both shapes: the legacy one and the one Chia ships.

Forge locks minted before MIPS are ``forge/1``: ``delegated_puzzle_feeder`` over
``m_of_n`` over bare ``bls_member`` leaves. Locks minted now are ``mips/1``,
which is what ``chia-wallet-sdk`` builds and what the Chia Cloud Wallet and the
custody tool read. The two hash differently, so this suite exists to prove three
things at once: that an existing lock is untouched, that a new lock is a real
Chia vault, and that both actually spend.

Every custody puzzle here is *run*, so a passing line means the owners' spend
produces the conditions the delegated puzzle asked for and the signature
requests the wallet will be asked to satisfy.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from chia.types.blockchain_format.program import Program
from chia.types.condition_opcodes import ConditionOpcode
from chia_rs.sized_bytes import bytes32

import mips
import multisig_profile as profile
import vault_tool as vault

PASSED = 0
FAILED: list[str] = []


def check(name: str, got: object, want: object) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}\n     got  {got}\n     want {want}")


def refused(build) -> str:
    """"refused" when ``build`` raises the tool's own error, "accepted" otherwise."""
    try:
        build()
    except Exception:
        return "refused"
    return "accepted"


KEYS = [
    "a945c8a3f67d005ed4ec8209b3603fbef16dc73b01e3a8875847fdb893c543e27f6175d4c9db43ef77a6fb5c95a3d21b",
    "acfdd91431bb5fc15c0a4d287fc7065b52d0b7012587e50f72940b2c14c99fbc37e6a3dd2d65d9f71a0acb95eac7af94",
    "891815ef6ecea1da72d345d3108d6b1832d73299723120e123a70c4ec75aeabe8da0f57309039fda89752344557bf5c4",
]
LAUNCHER = bytes32.fromhex("a1732b95bb89a99af816921a7418836bd18972dc84f3975bf419e523d2b2ee60")
AGG_SIG_ME = int.from_bytes(ConditionOpcode.AGG_SIG_ME, "big")
DELEGATED = Program.to((1, [[51, bytes32([9] * 32), 1], [52, 1000]]))


def policy(m: int, count: int, fmt: str) -> vault.Policy:
    owners = [{"label": f"owner{i}", "pubkey": KEYS[i]} for i in range(count)]
    return vault.Policy.from_json({"name": "lock", "m": m, "owners": owners, "format": fmt})


# ─── an existing lock is not disturbed ───────────────────────────────────────

# This is the live 1-of-2 testnet lock's inner puzzle hash, recorded before MIPS
# was adopted. If this line ever changes, an existing lock has been orphaned.
LIVE_LEGACY_INNER = "3d7b597881cec8b489f481ff5f0eba5e85ff44c046d3deedd7cf747ef59db761"
check(
    "the live legacy lock still hashes to what the chain holds",
    policy(1, 2, vault.FORMAT_LEGACY).inner_puzzle_hash().hex(),
    LIVE_LEGACY_INNER,
)
check(
    "a record with no format is read as legacy, not as MIPS",
    vault.Policy.from_json(
        {"name": "lock", "m": 1, "owners": [{"label": "a", "pubkey": KEYS[0]}, {"label": "b", "pubkey": KEYS[1]}]}
    ).fmt,
    vault.FORMAT_LEGACY,
)
check(
    "a legacy memo has three fields and reads back as legacy",
    (len(policy(1, 2, vault.FORMAT_LEGACY).encode().decode().split("|")), vault.Policy.decode(policy(1, 2, vault.FORMAT_LEGACY).encode()).fmt),
    (3, vault.FORMAT_LEGACY),
)


# ─── a new lock is a Chia vault ──────────────────────────────────────────────

# Produced by chia-wallet-sdk: mOfNHash(topLevel, 1, [blsMemberHash(nonce 0),
# blsMemberHash(nonce 1)]) for the same two keys. Our lock must equal it.
SDK_CUSTODY_1_OF_2 = "1510e46f4e323b983f49ae23a04866cc61ad791eefc6ecab744ea1149920988f"
check(
    "a new 1-of-2 lock equals the SDK's custody hash",
    policy(1, 2, vault.FORMAT_MIPS).inner_puzzle_hash().hex(),
    SDK_CUSTODY_1_OF_2,
)
check(
    "the custody hash is what VaultInfo would carry",
    policy(1, 2, vault.FORMAT_MIPS).custody().custody_hash().hex(),
    SDK_CUSTODY_1_OF_2,
)
check(
    "the two formats are different locks",
    policy(1, 2, vault.FORMAT_LEGACY).inner_puzzle_hash() != policy(1, 2, vault.FORMAT_MIPS).inner_puzzle_hash(),
    True,
)
check(
    "a MIPS memo names its format and reads back",
    vault.Policy.decode(policy(1, 2, vault.FORMAT_MIPS).encode()).fmt,
    vault.FORMAT_MIPS,
)
check("an unknown format is refused", refused(
    lambda: vault.Policy.from_json(
        {"name": "x", "m": 1, "owners": [{"label": "a", "pubkey": KEYS[0]}], "format": "safe/2"}
    )
), "refused")


# ─── the deposit address does not depend on the policy ───────────────────────

# The deposit puzzle is curried with the singleton's identity, never with the
# policy, so adopting MIPS moves no funds and changes no published address.
check(
    "the deposit address does not depend on the policy",
    vault.deposit_puzzle(LAUNCHER).get_tree_hash().hex(),
    "04e134e99460fc5a9a4a6716947c7cb7e5cee20f36f862e1415bc48fe044892c",
)


# ─── both shapes actually spend ──────────────────────────────────────────────


def run_policy(m: int, count: int, fmt: str, signer: int) -> list[list[object]]:
    p = policy(m, count, fmt)
    signers = p.signing_selection(p.keys[signer])
    solution = p.inner_solution(DELEGATED, signers)
    conditions = p.inner_puzzle().run(solution)
    check(
        f"{fmt} {m}-of-{count}: the puzzle run matches the hash proposed",
        p.inner_puzzle().get_tree_hash().hex(),
        p.inner_puzzle_hash().hex(),
    )
    return [c.as_python() for c in conditions.as_iter()]


for fmt in (vault.FORMAT_LEGACY, vault.FORMAT_MIPS):
    for m, count in ((1, 1), (1, 2), (2, 2), (1, 3), (2, 3), (3, 3)):
        for signer in range(count):
            conditions = run_policy(m, count, fmt, signer)
            signatures = [c for c in conditions if int.from_bytes(c[0], "big") == AGG_SIG_ME]
            check(f"{fmt} {m}-of-{count} signer {signer}: {m} signatures asked for", len(signatures), m)
            check(
                f"{fmt} {m}-of-{count} signer {signer}: every signature is over the delegated puzzle hash",
                {bytes(c[2]) for c in signatures},
                {bytes(DELEGATED.get_tree_hash())},
            )
            check(
                f"{fmt} {m}-of-{count} signer {signer}: the signer is among them",
                bytes.fromhex(KEYS[signer]) in {bytes(c[1]) for c in signatures},
                True,
            )
            check(
                f"{fmt} {m}-of-{count} signer {signer}: the delegated puzzle ran",
                len([c for c in conditions if int.from_bytes(c[0], "big") == 51]),
                1,
            )


# ─── the message a wallet signs is the same in both shapes ───────────────────

# This is the whole point of the migration being safe for signers: MIPS changes
# the puzzle around the member, not what the member asks the wallet to sign.
for m, count in ((1, 2), (2, 3)):
    legacy_conditions = run_policy(m, count, vault.FORMAT_LEGACY, 0)
    mips_conditions = run_policy(m, count, vault.FORMAT_MIPS, 0)
    check(
        f"{m}-of-{count}: both shapes ask for the same signatures",
        sorted(bytes(c[1]).hex() for c in legacy_conditions if int.from_bytes(c[0], "big") == AGG_SIG_ME),
        sorted(bytes(c[1]).hex() for c in mips_conditions if int.from_bytes(c[0], "big") == AGG_SIG_ME),
    )


# ─── a re-key does not change the composition ────────────────────────────────

check(
    "a MIPS lock re-keys to a MIPS lock",
    vault.Policy.from_json({"name": "next", "m": 2, "owners": [{"label": "a", "pubkey": KEYS[0]}, {"label": "b", "pubkey": KEYS[1]}], "format": vault.FORMAT_MIPS}).fmt,
    vault.FORMAT_MIPS,
)
check(
    "the profile encoder round-trips a MIPS safe",
    profile.decode_safe(profile.encode_safe({"name": "n", "m": 1, "format": profile.MIPS_FORMAT, "owners": [{"label": "a", "pubkey": KEYS[0]}]}))["format"],
    profile.MIPS_FORMAT,
)
check(
    "the profile encoder leaves a legacy safe three-field",
    profile.decode_safe(profile.encode_safe({"name": "n", "m": 1, "owners": [{"label": "a", "pubkey": KEYS[0]}]}))["format"],
    profile.LEGACY_FORMAT,
)


# ─── a launch carries the composition on chain ───────────────────────────────

# The launcher's key/value list is the only record of a lock's policy, so if the
# format did not survive that memo the lock would be read back as legacy and its
# puzzle hash recomputed wrong. Building a real launch proves the round trip:
# build_launch refuses to return unless the memo it wrote rebuilds the same
# inner puzzle hash.
from chia.types.blockchain_format.coin import Coin
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk
from chia_rs import G1Element

wallet_puzzle = puzzle_for_pk(G1Element.from_bytes(bytes.fromhex(KEYS[0])))
wallet_coin = Coin(bytes32([1] * 32), wallet_puzzle.get_tree_hash(), 1_000_000)
launch_payload = {
    "network": "testnet11",
    "coins": [
        {
            "coin": {
                "parent_coin_info": wallet_coin.parent_coin_info.hex(),
                "puzzle_hash": wallet_coin.puzzle_hash.hex(),
                "amount": int(wallet_coin.amount),
            },
            "puzzle": bytes(wallet_puzzle).hex(),
        }
    ],
    "fee": 0,
}
owners_json = [{"label": "a", "pubkey": KEYS[0]}, {"label": "b", "pubkey": KEYS[1]}]

launched = vault.cmd_launch({**launch_payload, "policy": {"name": "lock", "m": 1, "owners": owners_json}}, lambda _n: None)
check("a lock minted today is a MIPS vault", launched["format"], vault.FORMAT_MIPS)
check("its custody hash is the SDK's", launched["custody_hash"], SDK_CUSTODY_1_OF_2)
check("the launcher memo carries the format", launched["policy"]["format"], vault.FORMAT_MIPS)
check("the launch is two spends: the wallet coin and the launcher", len(launched["coin_spends"]), 2)

launched_legacy = vault.cmd_launch(
    {**launch_payload, "policy": {"name": "lock", "m": 1, "owners": owners_json, "format": vault.FORMAT_LEGACY}},
    lambda _n: None,
)
check("a legacy launch is still possible and reports no custody hash", launched_legacy["custody_hash"], None)
check("and it hashes to the pre-MIPS shape", launched_legacy["inner_puzzle_hash"], LIVE_LEGACY_INNER)
# Both launches spend the same wallet coin, so both derive the same launcher and
# therefore the same deposit address, while their custody puzzles differ. That is
# the property that makes adopting MIPS safe: an address is a singleton identity,
# never a policy hash.
check(
    "the address follows the launcher, not the policy",
    launched["deposit_address"],
    launched_legacy["deposit_address"],
)
check(
    "but the custody puzzles differ",
    launched["inner_puzzle_hash"] != launched_legacy["inner_puzzle_hash"],
    True,
)


print(f"vault policy: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
