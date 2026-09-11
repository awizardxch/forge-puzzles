# The lock, as a Safe

Forge's locks are meant to work the way Safe (safe.global) works: the owners are
custody of an address, the address can do anything a wallet can do, every action
needs the threshold, and anyone can push the final transaction. This maps Safe's
model onto what a Chia singleton lock actually is, says which parts already
match, which differ and why, and which are missing.

Read alongside `FORGE_MULTISIG.md`, which describes the puzzles themselves.

---

## 1. The transaction

**Safe.** A transaction is a `SafeTx`:

    to, value, data, operation, safeTxGas, baseGas, gasPrice, gasToken,
    refundReceiver, nonce

The owners sign `safeTxHash`, the EIP-712 hash of that struct. `execTransaction`
takes the same fields plus packed `signatures`, checks them against the
threshold, and performs a `CALL` or `DELEGATECALL` to `to`.

**The lock.** A transaction is the **delegated puzzle**: a list of Chia
conditions that the lock's singleton runs, plus one delegated puzzle per funds
coin the action spends. The owners sign the delegated puzzle hash.

`to`, `value` and `data` do not survive the translation, and should not. On
Ethereum a transaction is a message sent to an address, so the destination is a
field. On Chia a spend *is* the effect: `CREATE_COIN` says where value goes,
`RESERVE_FEE` says what is burned, an announcement says what it is paired with.
The condition list is the `data` field, and it already carries everything
`to` and `value` would have said.

`operation` has no analogue. There is no `DELEGATECALL` because there is no
contract to borrow code from; a coin runs its own puzzle, always.

| Safe | the lock |
|---|---|
| `to`, `value`, `data` | the condition list |
| `operation` | not applicable |
| `safeTxHash` | the delegated puzzle hash |
| `signatures` | the M revealed `bls_member` leaves, each signing that hash |
| `nonce` | the coin id (see below) |
| `safeTxGas`, `baseGas`, `gasPrice`, `gasToken` | not applicable |
| `refundReceiver` | inverted, see the fee section |

---

## 2. The nonce we do not need

Safe needs an explicit `nonce` because a Safe is a contract whose storage
persists: without one, the same signed `safeTxHash` could be replayed against the
same contract forever, and there would be no defined order between two pending
transactions.

A lock has no such problem. **The nonce is the coin id.** Every spend names
specific coins, a coin can be spent exactly once, and the signature is over a
delegated puzzle whose conditions are bound to those coins. A second submission
of the same signed material is not a replay, it is a spend of coins that no
longer exist, and the mempool refuses it.

That also gives ordering for free. Two proposals that touch the same funds coin
cannot both execute, because the first spends it; the second becomes stale, which
is exactly what a nonce collision means in Safe.

The `nonce` field that does exist in a plan is unrelated: it pairs the lock's
announcement with the sponsor's, so the two halves of a bundle cannot be
broadcast apart. It is a pairing token, not a replay guard.

---

## 3. Propose, confirm, execute

Safe's transaction service and Forge's registry do the same job: hold a proposed
transaction and the signatures gathered so far, off chain, until there are
enough.

| Safe | the lock |
|---|---|
| `proposeTransaction(safeAddress, safeTransactionData, safeTxHash, senderAddress, senderSignature)` | `POST /api/multisig-proposals` — the plan, its message, its coin ids, and the proposer's share |
| `confirmTransaction(safeTxHash, signature)` | `POST /api/multisig-sign` — a share verified against the plan before it is stored |
| `executeTransaction(...)` | `POST /api/multisig-execute` — assembles the bundle and pushes it |
| the service's pending list | `.awizard/multisig-index.json` |

Two differences worth stating plainly.

**Signatures are checked before they are stored.** Safe's service accepts a
signature and lets the contract judge it at execution. The lock's service
re-derives the plan, materialises the spend the signer's selection implies, and
verifies the share against it, so a share that would fail at execution is
rejected when it is offered.

**There is no on-chain reject.** Collected shares are bearer material until the
coins move, which is the same property Safe has before its nonce advances.
Cancelling in the interface is bookkeeping; to kill a fully signed proposal, spend
its coins.

---

## 4. Owner management is a transaction to itself

Safe changes owners with `addOwnerWithThreshold`, `removeOwner`, `swapOwner` and
`changeThreshold`, and each is an ordinary Safe transaction whose `to` is the
Safe. The owners approve it at the current threshold, exactly like a payment.

The lock does the same thing and arrives at it from the other direction: a
**re-key** is a delegated puzzle that recreates the singleton with a new inner
puzzle hash, so the new policy is written by the very transaction the current
owners approve. The address does not change, because funds live at
`p2_singleton_via_delegated_puzzle` curried with the singleton, not with the
policy.

This is the one place the lock is simpler than Safe. Safe needs four functions
because its owner list is contract storage that must be mutated field by field.
The lock's policy is a merkle root, so the whole owner set and threshold move in
one write.

---

## 5. Who pays

Safe assumes the executor pays gas and may be **refunded** from the Safe:
`gasPrice`, `gasToken`, `baseGas` and `refundReceiver` exist to compute and send
that refund.

The lock inverts this, deliberately: **the signer who pushes pays the fee, and is
not refunded.**

Until 2026-09-06 that was only half true. The fee came from a *sponsor* coin
chosen when the proposal was **built**, read out of the proposer's wallet and
bound into the delegated puzzle the owners signed. So the proposer paid, not the
pusher; every proposal needed a wallet coin read before it could exist; and a
proposal made without a sponsor went out with no fee at all.

The fee is now attached at **execute** time, by whoever is executing:

1. The lock's spend always emits `CREATE_COIN_ANNOUNCEMENT <nonce>`, whether or
   not anybody has offered to pay. That announcement is a handle, and it costs
   one condition.
2. When a proposal is pushed, the pusher's wallet builds one coin spend that
   reserves the fee, returns its own change, and **asserts that announcement**.
   The wallet signs it; no owner signs again and the proposal is untouched.

The binding is one way on purpose. The fee spend asserts the lock, so it is
worthless in any other bundle and cannot be lifted out of the mempool to pay for
somebody else's transaction. The lock does not assert the fee spend in return,
because it could not: the owners signed before that coin was chosen. The cost of
that asymmetry is that the lock's spend can be pushed with no fee attached, which
takes nothing from anyone — it simply waits for a quiet mempool.

The older sponsor path still works and is still bound both ways; a proposal that
carries one is not charged twice. The lock therefore needs no XCH of its own to
act, and its own funds are still spendable by the transaction — that is the point
of it — but they pay out, they do not pay the fee.

Proposing costs nothing on chain, so it now reads no wallet at all. A DID the
lock creates is funded by the lock's own coin, one mojo, which is what the
launcher becomes.

---

## 6. Modules and guards

**Modules.** In Safe, an enabled module calls `execTransactionFromModule` and
executes *without* the owner threshold, on the strength of its own validation.
The documentation is blunt about the risk: a module can execute arbitrary
transactions, so only trusted and audited ones should be enabled.

The lock has no modules yet, and the design note already describes what the first
one should be: the Balancer's keeper, holding a signing view scoped to one
delegated-puzzle shape, authorised to fire only when the realised output is not
worse than an offer the owner already agreed to. That is a Safe module in
everything but name, and it should be built as one — enabled by a re-key-style
vote, scoped by the shape it may sign, never holding a key that can do anything
else.

**Guards.** Safe guards check a transaction before and after execution. The lock
has none. The natural Chia form is an extra condition the delegated puzzle must
carry, checked by the tool when a plan is built and by `validate_vault_bundle`
before a share is accepted.

---

## 7. What this means for the builder

Safe's `SafeTx` is general from the first line: `data` is arbitrary bytes, so a
Safe can do anything an account can do. The lock's substrate is equally general —
`p2_singleton_via_delegated_puzzle` runs whatever conditions the singleton
authorises — but the **builder** is not. `build_vault_plan` accepts two shapes:

- `outputs`, which become payments;
- `successor`, which becomes a re-key.

So the lock can today express Safe's "send" and Safe's owner management, and
nothing else. Widening it is not a rewrite; it is letting a proposal carry a
condition list directly, the way `SafeTx` carries `data`.

**The rule that makes that safe.** The signature covers the delegated puzzle hash
and nothing else. Anything that rides along in the bundle but is not committed by
an assertion *inside* that delegated puzzle has not been approved by the signers.
Conditions are inside it by construction, so arbitrary conditions are safe to
allow. Extra coin spends are not, unless something in the signed conditions binds
them — which is why a DID launch is admissible: the standard launch conditions
assert the launcher's own announcement, and that announcement commits the eve
singleton's puzzle hash, and therefore its owner.

For that reason the widening is staged:

1. **Arbitrary conditions** on the lock's spend. Covered by the signature by
   construction. **Built.** A proposal takes `conditions`, capped at 64, parsed
   strictly (hex, numbers and nesting only; booleans, odd-length hex and bare
   strings refused). Its kind reads `action`. Proven by building two plans that
   differ in one condition and seeing the signed message differ.

2. **Riding spends from known builders. Built, with the DID launch as the first.**
   A plan carries `riders`: spends that go into the bundle without a signature of
   their own. They are never caller-supplied. They come from a builder that runs
   *after* coin selection, because a launcher coin is derived from the coin that
   pays for it, so its conditions cannot be written until that coin is chosen.
   The builder also declares `action_value`, what its conditions spend beyond the
   outputs, or the change would over-create and the spend would fail its amount
   rule.

   A **DID proposal** (`kind: "did"`) is the whole thing end to end: the lock's own
   coin is the launcher's parent, the lock's owners approve it at their threshold,
   and the DID's owner is the lock's deposit puzzle from its first coin. Nothing is
   minted elsewhere and sent. Verified: the eve singleton the launcher creates
   hashes to exactly a DID curried with the lock's deposit puzzle hash, naming a
   different DID changes the signed message, the plan survives its JSON round trip
   with the rider intact, and `validate_vault_bundle` accepts the three-spend
   bundle.

3. **Modules and guards**, once 1 and 2 are settled. Guards are no longer ours
   to invent: adopting MIPS brings `timelock`, `prevent_condition_opcode` and
   `prevent_multiple_create_coins`, held together by `enforce_dpuz_wrappers` so
   a wrapper cannot be dropped from a spend. See `FORGE_LOCK_MIPS.md`. Modules
   are still open.

---

## Where these details come from

Safe's own documentation: the smart account overview, the `execTransaction`
reference, the transaction service guides, and the modules page. The field names
and the propose/confirm/execute flow above are quoted from those pages; the
mapping and every claim about the lock is from this repository's own
`contracts/vault_tool.py`.
