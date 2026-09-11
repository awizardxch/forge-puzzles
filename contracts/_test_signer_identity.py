"""Which of a wallet's keys is the owner, and when it is safe to say so.

A wallet holds many keys. Forge asked for one — the first derivation — and
compared it against a lock's owners, so an owner whose key came from any other
derivation was told they owned nothing. The live testnet lock's second owner is
that wallet's derivation 19; connecting that exact wallet showed an empty list.

Looking further down the derivations fixes that, but it introduces a rule that
has to hold: a key may only be substituted for the one a session volunteered
when that volunteered key is itself in the same wallet. Otherwise a
WalletConnect session could be relabelled with the local wallet's identity, and
one person would be shown another person's locks. That rule is what this pins.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sage_signer_key import choose_owner_derivation

PASSED = 0
FAILED: list[str] = []


def check(name: str, got: object, want: object) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}\n     got  {got}\n     want {want}")


def key(byte: int) -> str:
    return f"{byte:02x}" * 48


def derivation(index: int, byte: int) -> dict[str, object]:
    return {"index": index, "public_key": key(byte), "address": f"txch1{byte:02x}"}


# A wallet whose first key is 0xa1 and whose key 19 is 0xac.
WALLET = [derivation(i, 0xA0 + i) for i in range(20)]
FIRST = key(0xA0)
OWNER = key(0xA0 + 19)
STRANGER = key(0xEE)


def chosen(volunteered: str, wanted: set[str]) -> str | None:
    entry = choose_owner_derivation(WALLET, volunteered, wanted)
    return None if entry is None else str(entry["public_key"])


# ─── the fix ─────────────────────────────────────────────────────────────────

check("an owner deeper in the wallet is found", chosen(FIRST, {OWNER}), OWNER)
check("and its derivation index comes with it", choose_owner_derivation(WALLET, FIRST, {OWNER})["index"], 19)
check(
    "the first key wins when it is itself an owner",
    chosen(FIRST, {FIRST, OWNER}),
    FIRST,
)
check("no owner in this wallet means no substitution", chosen(FIRST, {STRANGER}), None)
check("no candidates means no substitution", chosen(FIRST, set()), None)


# ─── the rule that keeps it honest ───────────────────────────────────────────

check(
    "a key from another wallet is never substituted, even when this wallet holds an owner",
    chosen(STRANGER, {OWNER}),
    None,
)
check(
    "and that holds however many owners this wallet has",
    chosen(STRANGER, {OWNER, key(0xA5), key(0xA9)}),
    None,
)
check("an empty wallet substitutes nothing", choose_owner_derivation([], FIRST, {OWNER}), None)
check(
    "a wallet that does not include the volunteered key substitutes nothing",
    choose_owner_derivation([derivation(0, 0xB0)], FIRST, {key(0xB0)}),
    None,
)

# Case and 0x prefixes come from three different sources; none of them may
# change the answer, because a missed match reads as "you own nothing".
check("an uppercase volunteered key still matches", chosen(FIRST.upper(), {OWNER}), OWNER)
check("a 0x-prefixed volunteered key still matches", chosen(f"0x{FIRST}", {OWNER}), OWNER)


print(f"signer identity: {PASSED} checks passed" + (f", {len(FAILED)} FAILED" if FAILED else ""))
for failure in FAILED:
    print("  x " + failure)
sys.exit(1 if FAILED else 0)
