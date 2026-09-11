# The lock, on Chia's own custody standard

Forge's locks used to be Forge's own shape. They are now the shape Chia Network
ships, so a Forge lock is a Chia vault: the same custody hash the wallet SDK
computes, the same member and restriction puzzles the custody tool uses, the
same primitive the Chia Cloud Wallet is built on.

This note records what the standard is, where Forge's old shape differed, what
that means for the two locks already on chain, and what the adoption buys.

Read alongside `FORGE_MULTISIG.md` (the puzzles) and `FORGE_LOCK_SAFE_MODEL.md`
(the Safe mapping).

---

## 1. What MIPS is

MIPS is the member/restriction composition in `chia_puzzles`, driven by
`chia-sdk-driver`'s `mips_puzzle_hash`. A custody puzzle is built by wrapping:

    INDEX_WRAPPER(nonce,
      [DELEGATED_PUZZLE_FEEDER]                     # only at the top level
        ( [RESTRICTIONS(member_validators, dpuz_validators)]
            ( inner ) ) )

`INDEX_WRAPPER` is seven bytes, `(a 5 7)`: run the curried inner puzzle on the
solution, unchanged. It carries no logic. It exists so the same member at a
different seat hashes differently, which is what lets two owners share a key.

The threshold layer **dispatches on the threshold**, and this is the part most
easily got wrong:

| threshold | puzzle | proof carried in the solution |
|---|---|---|
| 1 of N | `one_of_n` | a compact merkle proof: path plus siblings |
| M of N, `1 < M < N` | `m_of_n` | a partially revealed merkle tree |
| N of N | `n_of_n` | none; every member puzzle is curried in |

Members shipped: `bls_member` and its puzzle-assert (fast-forward) variant,
`secp256k1_member`, `secp256r1_member`, `passkey_member`, `fixed_puzzle_member`,
`singleton_member` and `singleton_member_with_mode`. Restrictions shipped:
`timelock`, `prevent_condition_opcode`, `prevent_multiple_create_coins`,
`enforce_dpuz_wrappers`, `force_1_of_2_w_restricted_variable`, `covenant_layer`,
`credential_restriction`.

---

## 2. Where Forge differed

Forge's original lock was:

    DELEGATED_PUZZLE_FEEDER( M_OF_N(m, merkle_root([BLS_MEMBER(pk) …])) )

Three differences, each enough on its own to change the hash:

1. **No index wrapper**, at the leaves or at the top.
2. **`m_of_n` at every threshold**, including 1-of-N, where MIPS uses `one_of_n`.
3. **Wrapping order**: Forge put the feeder outermost; MIPS puts the index
   wrapper outermost, over the feeder.

Measured, for the live 1-of-2 testnet policy:

| | inner puzzle hash |
|---|---|
| Forge's shape | `3d7b5978…f59db761` |
| MIPS, same owners | `1510e46f…9920988f` |

The second value is what `chia-wallet-sdk`'s `mOfNHash` returns for the same two
keys at nonces 0 and 1. `contracts/_test_vault_policy.py` pins both.

**So the shapes are not byte-compatible, and no migration path makes them so.**

---

## 3. What that means for the locks already on chain

Nothing moves, and nothing has to.

A lock's policy is recorded in one place: the launcher's key/value list, and
then each re-key's memos. Forge's memo was `name|m|label=key;…`. A MIPS lock
writes a fourth field, `name|m|label=key;…|mips/1`. An existing memo has three
fields, so **the absent field is the answer**: a lock minted before the adoption
reads back as `forge/1` and is rebuilt with exactly the puzzle the chain holds.

The defaults are set so that the unsafe direction cannot happen by accident:

- `Policy.from_json` defaults to `forge/1`, because a stored record written
  before the adoption carries no format, and reading it as MIPS would compute a
  puzzle hash the chain does not hold.
- Only `cmd_create` and `cmd_launch` opt in to `mips/1`, and only when the
  caller did not name a format.
- A re-key inherits the lock's own format from chain, so a MIPS lock stays MIPS
  and a legacy lock stays legacy. A re-key is a change of owners, never a change
  of composition.

**The address is unaffected either way.** Funds sit at
`p2_singleton_via_delegated_puzzle` curried with the *singleton struct* — the
launcher id — never with the policy. Two launches from the same wallet coin
produce the same deposit address whichever composition they use; the suite
checks exactly that.

An existing lock can move onto the standard the way anything else moves: mint a
new lock and pay to it. There is no in-place upgrade, and pretending otherwise
would be the dangerous option.

---

## 4. What signers see

Nothing changes. In both shapes the member that a signer's wallet answers to is
`bls_member`, and `bls_member` emits `AGG_SIG_ME(pk, delegated_puzzle_hash)`.
The wrapping sits *around* the member; it does not touch the message.

`contracts/_test_vault_policy.py` runs both compositions at every threshold from
1-of-1 to 3-of-3 and checks that a given signer is asked for the same signature
over the same delegated puzzle hash in both. That is what makes the adoption
invisible to Sage and to any other wallet an owner uses.

---

## 5. What it buys

The point was never the hash. It is that the following stop being Forge's work:

- **Passkeys and secp keys as owners** — `passkey_member`, `secp256k1_member`,
  `secp256r1_member`. A phone or a hardware key becomes a seat on a lock.
- **A singleton as an owner** — `singleton_member`. Authority held by whoever
  can spend an NFT or a DID, so a treasure chest's owner changes by sending the
  NFT, with no re-key and no new address. This is the member the chest design
  needs, and it ships.
- **Guards** — `timelock`, `prevent_condition_opcode`,
  `prevent_multiple_create_coins`, held together by `enforce_dpuz_wrappers` so a
  wrapper cannot be quietly dropped. These are the Safe-guard analogue the model
  note listed as unbuilt.
- **Other people's tools** — the custody tool, the Cloud Wallet, and any wallet
  that learns to read a vault, all address the lock by the same custody hash.

Forge still writes the proposal, the queue, the sponsor pairing and the fee
inversion. What it stops writing is the custody layer.

---

## 6. How it is verified

`contracts/mips.py` reproduces the composition in Python.
`contracts/_test_mips.py` checks it against `contracts/_mips_vectors.json`,
which is generated by `node scripts/gen-mips-vectors.mjs` from
`chia-wallet-sdk` itself. So the suite compares our Python against Chia's Rust,
not against itself. Regenerate after an SDK upgrade; a diff there is a change in
the standard, and the response is to follow it, not to relax the test.

| suite | checks | what it proves |
|---|---|---|
| `_test_mips.py` | 146 | every member, restriction and threshold hash equals the SDK's, and every custody puzzle runs |
| `_test_vault_policy.py` | 166 | the live legacy lock is untouched, a new lock equals the SDK's custody hash, both spend, signers see the same message, and a launch round-trips its format through the launcher memo |

The restriction layer took two corrections worth recording, because both are
easy to get wrong and neither shows up as anything but a wrong hash:

- `restrictions.clsp` curries the validators as **programs**, so a validator
  list's tree hash is built from the validators' own puzzle hashes, not from
  atoms holding those hashes.
- `enforce_dpuz_wrappers` is curried with **quoted programs** — `(q . mod)` —
  so the wrapper hash goes in directly rather than as an atom.

---

## Where these details come from

`chia_puzzles` (the `.clsp` sources for `restrictions`, `enforce_dpuz_wrappers`
and the members), `chia-wallet-sdk`'s `crates/chia-sdk-driver/src/primitives/mips`
and `crates/chia-sdk-types/src/puzzles/mips` for the composition and the
threshold dispatch, and this repository's `contracts/mips.py` and its two suites
for every claim about Forge.
