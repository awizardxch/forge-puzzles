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
| `_test_mips.py` | 183 | every member, restriction and threshold hash equals the SDK's, every custody puzzle runs, and a custody's CHIP-0043 memo is byte for byte the SDK's and refuses tampering |
| `_test_vault_policy.py` | 166 | the live legacy lock is untouched, a new lock equals the SDK's custody hash, both spend, signers see the same message, and a launch round-trips its format through the launcher memo |
| `_test_vault_sdk_memo.py` | 35 | a launch writes the CHIP-0043 memo for exactly its custody, a re-key and a rename for the successor's, a legacy lock and a plain spend none |
| `_test_lock_row_verify.py` | 13 | an index row is `live` only at its lock's real singleton, `phantom` only on a contradiction, `stale` or `unknown` otherwise |
| `api/__checks__/lockMipsMemo.check.mjs` | 13 | `chia-wallet-sdk` parses a real launch build's memo and rebuilds its custody and eve singleton from the memo alone |

The restriction layer took two corrections worth recording, because both are
easy to get wrong and neither shows up as anything but a wrong hash:

- `restrictions.clsp` curries the validators as **programs**, so a validator
  list's tree hash is built from the validators' own puzzle hashes, not from
  atoms holding those hashes.
- `enforce_dpuz_wrappers` is curried with **quoted programs** — `(q . mod)` —
  so the wrapper hash goes in directly rather than as an atom.

---

## 7. Finding a lock from the chain (CHIP-0043 memos)

A puzzle hash says nothing about who can spend it. CHIP-0043 memos say it: a
vault writes a description of its custody beside the coin, and any wallet can
rebuild the custody puzzle from it and check that against the hash.
`chia-wallet-sdk` 0.37.0 reads them (`Program.parseMipsMemo`, xch-dev PRs 448,
451, 476).

**What the locks on chain carry (read 2026-10-24).** Forge's builders did not
write these memos. A lock's policy rode only in Forge's own format: the pair
`("forge-vault/1" . "name|m|label=key;…|mips/1")` in the launcher's key/value
list, and `[launcher_id, "forge-vault/1", policy]` on each re-key's recreated
singleton. On mainnet, the SDK parser returns null for every entry of both live
locks:

| lock | launcher | what the SDK finds |
|---|---|---|
| aWizard Multisig (DAO, 2-of-4) | `ae65809d…e2dd` | launch: the `forge-vault/1` pair only; 3 re-keys: hint + Forge pair only; 2 plain spends: hint only |
| Speechless Dev (dev, 1-of-2) | `b6daa739…e94c` | launch: the `forge-vault/1` pair only; 1 plain spend: hint only |

Their **puzzles** are Chia vaults: the SDK's `mOfNHash` over `blsMemberHash`
(seat *i* at nonce *i*), wrapped in the singleton, rebuilds each live tip's
puzzle hash exactly (`74c8c054…2ce2` and `69355b95…b51a`). So an SDK wallet
that is *told* the owners can verify these locks. It cannot learn the owners
from the chain without reading Forge's format.

**New locks write both (2026-10-24).** A MIPS lock's launcher key/value list is
now `(<CHIP-0043 memo> ("forge-vault/1" . policy))`, and a re-key or rename's
recreated singleton carries `[launcher_id, "forge-vault/1", policy, <CHIP-0043 memo>]`.
The hint stays first and each reader skips the other's entry. The memo is the
SDK's own encoding (`contracts/mips.py` `custody_memo`): a top-level M-of-N with
no restrictions, each owner a revealed plain BLS member at nonce = position.
The builder refuses a launch whose memo does not read back to its custody.
A legacy (`forge/1`) lock writes none, because no CHIP-0043 memo describes that
puzzle. It costs about 82 bytes per owner, and the fee is measured on the bundle
as before.

**Existing locks.** A memo is written only by a spend the owners sign, so the
two live locks gain one at their next re-key or rename, and not before. There is
no way to attach it otherwise: a coin sent beside the lock is not the lock's own
statement. Until then, finding an existing lock from the chain goes through what
it already carries, each verified rather than trusted:

1. **The deposit pointer.** A 1-mojo coin at the deposit address, memos
   `[hint, "forge-vault/1", launcher_id]`, written at launch
   (`vault_tool.find_launcher_from_deposit`). Any address a pool or registry
   names (DAO recipient, protocol recipient) leads to its lock this way.
2. **The launcher's lineage.** From the launcher id, `read_vault` walks to the
   unspent tip and decodes every policy on the way. That is the authority on owners.
3. **Membership coins** (locks launched since 2026-10-21) at each owner's
   address, and **profile coins** an owner published. These are pointers only.
   Each is checked against step 2.
4. **The index check.** Every index row is now checked against the chain
   (`vault_tool.py verify-rows`, run in the background by `GET/POST
   /api/multisig-safes`). The check recomputes the row's puzzle hashes from its
   configuration and looks for a live coin there. A vault row is `live` only if an
   unspent coin sits at `singleton(launcher, custody(owners, threshold))` and that
   coin's parent is the launcher or the lock's own singleton. A coin someone sent
   to that hash does not count. A row is `phantom` only on a definite
   contradiction: its address is not its launcher's deposit address, the node says
   the launcher does not exist, the launcher is not a singleton launcher, or a
   legacy safe's owners do not make its address. A phantom row is left out of every
   list and removed unless a proposal names it. A `stale` row (a missed re-key) and
   an `unknown` one (node silent, lineage unreadable) are kept.

Read-only run on mainnet, 2026-10-24: both real rows are `live` at their tips
(`a2eefc1f…6936`, `5b1b6d57…965f`). The DAO lock's launch-time 1-of-2 policy is
`stale`. Three fabricated rows are `phantom`: an invented launcher, the dev
lock's launcher filed at the DAO's address, and the dev lock's policy filed as a
legacy safe at its own address (the PR 56 shape).

**What CHIP-0043 adds once the live locks re-key.** Steps 1 and 2 need Forge's
code. With the memo on the tip's creation, any SDK wallet can rebuild a lock from
its launcher id alone: parse the newest memo, check its `innerPuzzleHash()`
against the tip, and read the owners. `api/__checks__/lockMipsMemo.check.mjs`
proves that on a real launch build with the SDK alone. Forge's own reader keeps
using the `forge-vault/1` pair, which carries names and labels the standard has
no field for.

---

## Where these details come from

`chia_puzzles` (the `.clsp` sources for `restrictions`, `enforce_dpuz_wrappers`
and the members), `chia-wallet-sdk`'s `crates/chia-sdk-driver/src/primitives/mips`
and `crates/chia-sdk-types/src/puzzles/mips` for the composition and the
threshold dispatch, and this repository's `contracts/mips.py` and its two suites
for every claim about Forge.
