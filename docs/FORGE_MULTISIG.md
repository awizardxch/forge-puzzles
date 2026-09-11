# 🔐 Locks — M-of-N safes on CNI's `p2_m_of_n_delegate_direct`

In the app the feature is just the lock: the tab is `🔐` with no word, and a
safe is called a **lock** in copy ("Create Lock", "Observe lock"). The lock
states your access: 🔒 watching, 🔐 you hold a key, 🔓 inside one you hold a
key to. The word "safe" stays in code, registry and on-chain tags; only the
surface is branded. The noun and mark live in one constant, `SIGIL`, in
`MultisigPanel.tsx`.

Status: **shipping on testnet11, not externally audited.** Added 2026-09-03 while
the pool protocol is paused for feedback.

The Multisig tab is a Safe-style front end: a shared address owned by several
keys, a queue of proposed transactions, each owner signing from their own
wallet, execution once the threshold is met. The puzzle underneath is Chia
Network's own, unchanged; Forge adds the coordination service and the UI.

---

## Protocol

### The puzzle

`p2_m_of_n_delegate_direct` ships in `chia_puzzles_py` (mod hash
`0f199d5263ac1a62b077c159404a71abd3f9691cc57520bf1d4c5cb501504457`) and has
been deployable since the first Chia wallets. Curried with `(M, pubkeys)`; solved
with `(selectors, delegated_puzzle, delegated_solution)`:

1. `selectors` is a 0/1 list, one entry per key. The puzzle asserts exactly `M`
   are set.
2. For each selected key it emits `AGG_SIG_UNSAFE(key, sha256tree(delegated_puzzle))`.
3. It then runs `delegated_puzzle` with `delegated_solution` and returns its
   conditions.

The safe's puzzle hash is the tree hash of the curried puzzle; its address is
that hash in bech32m. A CAT held by the safe is an ordinary CAT whose inner
puzzle is the safe puzzle, so it lives at `CAT(asset_id, safe)`'s hash.

### What the signature covers, and why every spend pins its coin

The message is the delegated puzzle hash and **nothing else**: no coin id, no
genesis challenge. A signature over "create coin X for Alice" would therefore
be valid for any coin at the safe's address, forever. Forge closes this the
standard way: every delegated puzzle the tool builds contains
`ASSERT_MY_COIN_ID <coin>`. The signature then only satisfies that one coin,
and the moment that coin is spent — by this proposal or any other — the
signature is inert. This is also why there is no nonce: a proposal is
invalidated by moving its coins, not by a counter.

`validate_bundle` in `contracts/multisig_tool.py` refuses to hand out a
signing view, and refuses to assemble, unless every spend asserts its own coin
id and emits exactly `M × spends` AGG_SIG conditions whose messages are the
plan's.

### Shape of a proposal's spends

- **Primary spend** — the first coin selected. Its delegated conditions carry
  the outputs, the change back to the safe, `RESERVE_FEE`, `ASSERT_MY_COIN_ID`,
  and `CREATE_COIN_ANNOUNCEMENT nonce`.
- **Every other spend** — `ASSERT_MY_COIN_ID` and
  `ASSERT_COIN_ANNOUNCEMENT sha256(primary_id ‖ nonce)`, so no subset of the
  bundle can be broadcast without the spend that carries the outputs.
- **CATs** spend as a ring per asset through
  `unsigned_spend_bundle_for_spendable_cats`; the inner solution is the M-of-N
  solution. Outputs carry the `[puzzle_hash]` hint memo so the recipient's
  wallet finds them. A fee on a CAT proposal is paid by an XCH coin of the
  safe, linked by the same announcement; a safe with no XCH cannot pay a fee.
- Coin selection is largest-first. At most 40 coins per proposal.

### Signing without knowing who will sign

`M` must be met exactly at solve time, so the selectors — and thus the exact
AGG_SIG conditions — depend on which owners sign. But the message does not.
So the service hands each owner a *signing view*: the same spends with
selectors set to that owner plus the first `M−1` others. Sage runs the
puzzles, finds the AGG_SIG conditions, signs the ones for keys it holds
(`partial: true`), and returns one aggregate. The service then finds, by
`aggregate_verify` over subsets of the selected keys, exactly which owners
that aggregate covers — the client's claim of who signed is never trusted.

At execution the service picks non-overlapping shares that cover exactly `M`
owners, rewrites the selectors to that set, aggregates the shares, re-runs
every puzzle, verifies the aggregate against the emitted conditions, and
pushes to the node. A wallet that holds two owner keys returns a two-key
share; that is accepted as long as the total can still be made exactly `M`.

### Which key an owner is

Sage signs for the keys in its derivations table — the synthetic keys of its
addresses — and for the master key. `chip0002_getPublicKeys` returns the
derivation keys, so the first key the wallet reports over WalletConnect is
exactly a key Sage will sign with. The UI shows it as "Your signer key"
together with that key's own wallet address; no import or export is involved,
and only the public key ever leaves the wallet.

`chip0002_getPublicKeys` is a *required* WalletConnect method for Forge (it
was optional before the Multisig tab, and Sage skips optional methods at
pairing). A session approved before that change reports no key; the tab then
falls back to the local Sage RPC's first derivation — the same key — via
`/api/multisig-signer-key`, and offers **Reconnect wallet** to re-pair with
the full method list so the key is available away from a local Sage. Checked
against a live Sage: its reported address for derivation 0 equals the address
the tool derives from the key.

### The on-chain profile: which safes are yours to see

The safe list is per key, and the preference is not stored by Forge. It is a
**1-mojo coin the user sends to their own signer address** whose memos name
the safes; memos live in the spend that created the coin, so the record is
permanent, readable by anyone from the public key alone, and updated by
sending a newer one — newest wins. Layout of the CREATE_COIN memos:

```
memo[0]  signer puzzle hash            (the standard hint; the wallet sees the coin)
memo[1]  "forge-multisig-profile/1"
memo[2…] "<name>|<m>|<label>=<pubkey>;<label>=<pubkey>…"   one per safe
```

Reading (`contracts/multisig_profile.py read`) lists the signer address's
coins including spent ones, keeps the 1-mojo ones newest-first, fetches the
parent spend of each and runs it to recover the memos — the consensus
condition parser drops memos, so the puzzle is run directly — and returns the
first tagged record. Publishing (`build-publish`) takes the wallet's own XCH
coins with their puzzle reveals (`chip0002_getAssetCoins`), builds a standard
`(() (q . conditions) ())` spend creating the profile coin plus change, links
extra coins by announcement, round-trips the memos before handing the spends
out, and the wallet signs and sends as for any other Forge transaction.

### The safe manifest: observing by address alone

An address cannot reveal its owners, so a safe describes itself: a 1-mojo
**manifest** coin at the safe's own address, memos
`[hint, "forge-multisig-safe/1", "<name>|<m>|<label>=<pubkey>;…"]`. It is
self-verifying — the reader recomputes the policy's puzzle hash and accepts
the record only if it equals the address it sits at — so anyone may post it
(the creator usually does, from the safe's page: 1 mojo plus fee from their
wallet) and nobody can forge one for a safe they do not know. The tests cover
a forged newer record being skipped in favor of the genuine older one.

With a manifest, a profile entry can be **by address**: `"<display name>|<puzzle
hash>"`. Reading the profile resolves each such entry from its manifest; the
display name in the profile wins over the manifest's name when set, and an
entry whose manifest cannot be found is reported unresolved rather than
dropped. Safes without a manifest are stored in full.

In the tab: **Mine** shows safes this key owns (registry) plus everything the
profile names; **All** is the registry. **Observe safe** takes just the safe's
address, looks up the manifest, shows the policy for confirmation with an
optional display name of your own, and marks the safe *Pending*; without a
manifest it offers the manual owners form. **Publish to chain** writes the
whole draft as one record. The safe page shows whether the safe is described
on chain and offers **Publish manifest** when it is not. Safes named on chain
that the registry does not know are registered on read, so their queue and
balance work. Labels and names are on chain in plain text.

## Vault locks — the shipping design from 2026-09-04

New locks are **vaults** on CNI's vault puzzles (`chia_puzzles_py`:
`M_OF_N`, `BLS_MEMBER`, `DELEGATED_PUZZLE_FEEDER`, `P2_SINGLETON_VIA_DELEGATED_PUZZLE`,
`SINGLETON_TOP_LAYER_V1_1`), built by `contracts/vault_tool.py`:

- The lock is a **singleton**; its inner puzzle is `delegated_puzzle_feeder(m_of_n(M, merkle_root))`
  where the leaves are `bls_member(owner_key)` puzzles and the tree is chia's
  `MerkleTree` (ceil split). A spend reveals M members as `(() member . ())` leaves
  and the hashes of the rest; each revealed member emits `AGG_SIG_ME(key, delegated_puzzle_hash)`.
- Funds live in `p2_singleton_via_delegated_puzzle` coins curried with the singleton
  struct (aggregator disabled with `(x)`): the **deposit address**, fixed for the life of
  the lock. A funds coin spends only when the singleton, in the same bundle, announces
  `sha256tree([funds_coin_id, funds_delegated_puzzle_hash])` (CREATE_PUZZLE_ANNOUNCEMENT),
  and the singleton in turn asserts each funds coin's `$` announcement, so neither runs alone.
- A **payment** is the singleton spend (the vote) plus the funds spends it authorizes; the
  singleton recreates itself with the same inner hash.
- A **re-key** is the singleton spend whose delegated puzzle recreates it with the new inner
  hash (new M, new merkle root) and writes the new policy in the recreated coin's memos
  (`forge-vault/1`, encoded policy). Nothing else moves. The launcher's key/value list holds
  the first policy, so `read` walks launcher → tip and returns every policy in order.
- **Launch** is one wallet spend by the creator: launcher coin (1 mojo), the eve singleton via
  the launcher spend, a 1-mojo **pointer** coin at the deposit address with memos
  `[hint, forge-vault/1, launcher_id]` (so the vault is findable from its address), and
  optionally the creator's profile record listing the new lock by address.
- Fees on a re-key come from the proposer's wallet **sponsor** spend (as with the older
  puzzle); on a payment the funds coin pays unless a sponsor is given.

### The custody layer is now Chia's, not Forge's (2026-09-06)

The composition above — `delegated_puzzle_feeder(m_of_n(M, merkle_root))` over
bare `bls_member` leaves — is Forge's own shape. Chia ships one, MIPS, and locks
minted from 2026-09-06 use it: `index_wrapper(nonce, …)` around every member and
around the whole custody puzzle, `one_of_n` rather than `m_of_n` at a threshold
of one, `n_of_n` at a threshold of N. A Forge lock's inner puzzle hash is now
literally a Chia vault's custody hash, and `chia-wallet-sdk` computes the same
value for the same owners.

The two shapes are **not** byte-compatible, so a lock carries its format:
`forge/1` or `mips/1`, written as a fourth field in the policy memo. An existing
memo has three fields, so a lock minted before the adoption reads back as
`forge/1` and rebuilds exactly the puzzle the chain holds. Nothing moves, and the
deposit address is unaffected either way, because it is curried with the
singleton struct and never with the policy.

What this buys: passkey and secp members, a `singleton_member` seat whose
authority is held by whoever can spend an NFT or a DID, and the `timelock` and
`prevent_*` guards — all shipped and audited by CNI rather than written here.
`docs/FORGE_LOCK_MIPS.md` records the composition, the measured hashes, the
migration rule and the two restriction-layer corrections it took to match.

### Why a confirmed DID did not appear in Assets (2026-09-06)

The lock's first DID launched, confirmed on chain, and the assets list stayed
empty. Three separate faults, each enough on its own:

1. **A launched singleton is not hinted to its owner.** The singleton launcher
   emits a bare `CREATE_COIN` with no memo, so the eve coin carries no hint; the
   hint is only written when that eve coin spends itself. The scan looked only at
   hints, so a DID the lock had just created was invisible to it.
2. **`match_did_puzzle` returns an iterator or `None`, not a pair.** The
   classifier unpacked it as `matched, curried`, which raises either way, and the
   exception was swallowed. No DID ever matched. The DID a wallet had already
   sent to the lock was listed as `unknown` for the same reason.
3. **The launch pointer coin was listed as a singleton.** The 1-mojo coin planted
   at the deposit address at launch is odd-amount and hinted, so it passed the
   filter and sat in the list permanently as `unknown`. It is XCH the balance
   already reports.

The fix for (1) does not need an index or any memory of the proposal, because
both halves are deterministic: a launcher's coin id is
`Coin(payer, SINGLETON_LAUNCHER_HASH, 1)`, so every spent coin at the deposit
address names one candidate launcher; and a DID owned by this lock has exactly
one possible eve puzzle hash, the singleton wrapper around `create_innerpuz`
curried with this lock's deposit puzzle hash and that launcher id. A hit is
proof rather than a guess — a DID owned by anyone else cannot land at that hash.

The DID tab now distinguishes two things that were previously conflated.
**Hinted to the lock is not owned by the lock.** A DID minted in a wallet and
sent over is hinted, but its owner puzzle is still the sender's, so the owners
cannot spend it and cannot mint under it; it is badged accordingly. A DID the
lock launched is badged *owned by this lock*, and while it is still an eve coin
it is badged *not yet published* — real, spendable by the owners, but not
findable by other wallets or explorers until its first spend writes the hint.
That eve spend is the next piece of work; it is an ordinary proposal, since
spending the DID means spending the lock's own deposit puzzle.

### The first DID was inert, and why nothing caught it (2026-09-06)

The DID the lock launched is **permanently unspendable**, and the fault is one
argument. `did_action` passed the lock's deposit puzzle **hash** to
`create_innerpuz`, which accepts either a puzzle or a hash and says so in its own
docstring: *"receiving a standard P2 puzzle hash wouldn't calculate a valid
puzzle."* `did_innerpuz` runs its owner with `(a INNER_PUZZLE inner_solution)`,
so a 32-byte atom there is executed as a program and fails; and mode 0, recovery,
is closed off because the recovery list is empty and
`NUM_VERIFICATIONS_REQUIRED` is zero. Both doors are shut.

The DID hashed correctly, launched correctly, confirmed on chain, and read back
as owned by the lock. Every check Forge ran was a hash comparison, and hashes
cannot tell the two shapes apart — only running the puzzle can. Launcher
`06f46654…` is inert; its 1 mojo stays where it is.

`did_inner_puzzle` now builds the inner puzzle and refuses an atom outright, and
`_test_vault_did.py` **runs** every DID puzzle it builds and reads the conditions
back. That suite fails on the shipped shape and passes on the corrected one, so
it is the check that was missing.

The assets list reports the inert DID rather than hiding it, badged *cannot be
spent*, and it no longer blocks creating a replacement. It also lists only DIDs
the lock actually owns: a DID minted in a wallet and sent here is hinted to the
address but its owner puzzle is still the sender's, so the owners cannot spend
it or mint under it. Those move to `other_singletons`.

### Publishing a DID (2026-09-06)

A launched singleton carries no hint, so a new DID is invisible to every wallet
and explorer until it spends itself once. **Publish** is that spend, and it is an
ordinary proposal: the DID's owner is the lock's deposit puzzle, so spending it
takes the threshold like spending the lock's coins.

The spend changes nothing about the DID — same inner puzzle hash, same owner,
same amount. The only new thing on chain is the memo naming the lock, which is
what indexers search by. Mechanically it is a `FundsSpend` of kind `did`: the
same delegated puzzle and p2 solution an ordinary coin uses, wrapped by the DID
inner puzzle's mode-1 branch (which passes conditions through unchanged) and then
by the singleton layer, which morphs the odd-amount `CREATE_COIN` back into a
singleton. `SingletonHost` carries the launcher id, inner puzzle and lineage
proof needed to rebuild both layers across the plan's JSON round trip.

Verified by running the puzzles in `_test_vault_did.py`; not yet exercised on
chain, because the only DID the lock holds is the inert one.

### The fee moved to push time (2026-09-06)

Proposing no longer reads the proposer's wallet. It never needed to: proposing
costs nothing on chain, a DID the lock creates is funded by the lock's own coin
(one mojo, which is what the launcher becomes), and the fee belongs to whoever
pushes.

The fee is now built when a proposal is **executed**, from the pusher's coins. It
cannot be inside what the owners signed, because it did not exist then, so it
binds itself one way instead: the lock's spend always emits
`CREATE_COIN_ANNOUNCEMENT <nonce>`, and the fee spend asserts it. That makes the
fee spend worthless in any other bundle, while leaving the lock's spend
independent of it — push without a fee and the transaction simply waits.

`vault_tool fee-spend` builds it, `/api/multisig-fee-spend` serves it to a signed
proposal, the wallet signs it, and `/api/multisig-execute` takes it as
`feeSpends` plus `feeSignature` and aggregates. A proposal that already carries a
sponsor is not charged twice. Pinned by `_test_vault_fee.py`, which checks the
binding is to this lock spend and this nonce and to nothing else.

### An owner is any of the wallet's keys, not its first (2026-09-06)

Switching Sage to the second owner's wallet showed no locks at all. The wallet
was right; the identity was wrong. Forge read one key — the wallet's first
unhardened derivation — and compared it against the owner list. The live lock's
`fish` owner is that wallet's derivation **19**, so the comparison failed and the
owner was told they owned nothing.

A wallet hands out addresses in order, and an owner key can come from any of
them, so one key is never enough to answer "is this person an owner". The signer
lookup now takes every owner key the registry holds and asks the wallet which of
them it has, searching a window of 250 unhardened derivations — one RPC call
either way. It reports the matching key, its index, and the key the wallet
offered instead.

**The rule that keeps that honest:** a key is substituted for the one a session
volunteered only when the volunteered key is itself in the same wallet's
derivations. That is the proof the connected session and the local wallet are
the same wallet. Without it, a WalletConnect session could be silently
relabelled with the local Sage's identity and shown somebody else's locks.
`choose_owner_derivation` is pure so that rule can be tested without a wallet;
`_test_signer_identity.py` pins it, including the case where the local wallet
does hold an owner key and the session is still left alone.

The panel says when a substitution happened, because otherwise the key shown
here does not match the one the wallet's own screen shows first.

### The address directory (2026-09-06)

Adding a co-owner by address depended on that address having spent. An address
is a hash, so a key cannot be computed back out of it; it has to be read from a
puzzle reveal. A co-owner with a fresh address could not be added at all, and the
tab could only tell them to go and spend something first.

Forge learns keys constantly and was throwing them away: from every wallet that
connects, from the owners of every lock it registers, from each lookup that
succeeded. Those are now kept in the registry as `keys`, filed by the address
each one derives.

**Why a cache is safe here.** An address *is* the hash of the standard puzzle
curried with its key, so a remembered key either hashes back to the address it is
filed under or it does not. `multisig_profile.key_for_address` runs exactly that
check before returning one, and falls through to the chain when it fails. A
stale, wrong or tampered entry cannot put the wrong key in front of somebody
adding an owner — it costs a chain read and nothing else. The directory is a
shortcut, never an authority, and that is what lets it be trusted at all.

The result carries `source`, `known` or `chain`, so it is always visible which
answered. Owners of existing locks backfill the directory the first time the tab
loads, so it starts full rather than empty.

### A launch that hung, and the row it left behind (2026-09-06)

Creating a lock begins by reading the wallet's coins. That request asks the user
nothing and shows them nothing — the wallet answers or it does not — and it had
no timeout and no liveness check. So a stalled session meant a promise that never
settled: no prompt in Sage, no error, and a Create Lock button disabled by its
own `busy` flag forever. From the outside it looked exactly like the button was
broken.

`getAssetCoins` now checks the session first and bounds each page at 30 seconds,
so a dead session costs seconds and says so. The signing request is left
unbounded on purpose — a person has to read and approve it, and rushing that is
worse than waiting — but it too checks liveness first, because a dead session
will never show them anything to approve. The create form also shows which step
it is on, so a slow wallet reads as slow rather than broken.

**The row it left behind.** A launch that failed after the record was written and
was then retried spent the same wallet coin, so it derived the same launcher and
the same deposit address: one lock, two rows, both "launching". Registering a
vault now updates an existing record at that deposit address instead of adding a
second. The duplicate already in the registry was merged after checking both rows
named the same launcher and custody hash and that no proposal referenced the one
removed.

### Coins come from the chain, not from the wallet (2026-09-06)

The evidence that settled this: a session with **12 approved methods including
`chip0002_getAssetCoins`**, answering pings, signing spends and creating offers,
that still left a coin read unanswered for 30 seconds. The wallet was reachable
and simply would not answer that call.

It never needed to. A standard address is the tree hash of
`p2_delegated_puzzle_or_hidden_puzzle` curried with one of the wallet's public
keys, and the wallet shares its keys readily. So Forge derives the addresses
itself, asks the **node** which coins sit at them, and rebuilds each puzzle
reveal from the key it came from. The result is exactly what the wallet would
have returned. Measured against the live FISH wallet: two keys, 21 coins,
0.877 XCH — the same balance the wallet's own menu showed, with no wallet call at
all.

**The keys are captured at connect**, which is the other half of the idea: that
is the moment the session has just proved it answers, and the keys do not change
while the session lasts. Asking again later would put a relay round trip in front
of every flow, which is the thing being removed.

So after pairing, the only thing left to ask the wallet for is a **signature**.
That is the one request that must go to the wallet, because it is the one that
needs its owner. `chip0002_getAssetCoins` remains as the fallback for what the
chain read cannot see — coins at derivations the wallet did not share — and when
it is used the panel says so, because it is the slow path.

This replaces the earlier local-Sage rescue, which was the wrong shape: it only
worked on a machine running Sage beside Forge. Reading from the node works
everywhere Forge works, browser included.

### Coin reads: WalletConnect first, local Sage as a rescue (2026-09-06, superseded)

Every lock flow starts by reading the wallet's coins, and that read was failing:
the session paired, Sage listing Forge, Sage RPC reporting 323 of 323 coins
synced, and the request simply never answered.

The first attempt at a fix was wrong and is worth recording as such. It preferred
the local Sage RPC whenever it was available, which made every run here pass
while leaving the path that actually has to work — WalletConnect, in a browser,
with no local wallet — untested. **Forge cannot assume a local Sage.** Run as an
app inside Sage it is there; run in a browser it is not, and that is the case
that must be verified.

So the order is: **WalletConnect is the method.** It is asked first, every time.
The local Sage is a rescue — tried only after the relay has actually failed, only
when the fingerprints prove it is the same wallet, and never silently. When it
answers, the panel says so in the notice, because a fallback that hides a broken
relay is worse than the failure it covers.

Three things make the relay path fail legibly instead of hanging:

- **An unapproved method is named.** `chip0002_getAssetCoins` missing from the
  session's method list is not a slow request; the wallet will never route it.
  That is now checked first and says to reconnect, rather than spending the
  timeout to arrive at "no answer".
- **A dead session is found in seconds**, by the liveness ping, and deleted so
  the next attempt re-pairs.
- **A live but unanswering session** hits a 30-second bound per page.

**Check wallet connection**, on the signer key card, asks the wallet for its
coins and nothing else — no transaction, no signature — and reports how long it
took and what came back. On a failure it also reports what the session actually
granted, because a method the wallet never approved can never be answered however
long it is given, and that is fixed by re-pairing, while a relay dropping traffic
is not. It exercises exactly the step every lock flow starts with, so a session
can be tested before it is relied on.

### Identity without a local Sage (2026-09-06)

Matching an owner key against the wallet's derivations was only possible through
the local Sage RPC, so a browser session with Sage RPC off showed the wallet's
first key, matched nothing, and told a genuine owner that no lock was theirs.

The wallet can answer this itself. `chip0002_getPublicKeys` is now asked for a
window of keys rather than one, and the panel matches them against every owner
key the registry knows, once the locks have loaded. An owner key the wallet holds
wins over the key it volunteered, which is almost always derivation 0 and owns
nothing.

**One trap worth recording.** The local route is amount-driven: it asks Sage to
select coins for a `min_amount` and returns what Sage picked. Every builder
downstream spends ONE coin and needs it to cover the whole amount by itself, so a
covering *set* of small coins is a correct answer to a different question. The
rescue passes the amount it needs and refuses its own answer unless one returned
coin covers it.

### The signing request handed the wallet a launcher (2026-09-07)

Sage **crashed** on every attempt to sign a proposal: no prompt, no signature, no
error. The capture of what was sent has the answer in it — three coin spends,
and only one of them was anything to do with the wallet:

| spend | signatures it asks for |
|---|---|
| the lock's singleton | `AGG_SIG_ME` for the owner |
| the lock's funds coin | none — authorised by the singleton's announcement |
| the **singleton launcher** | none, and a puzzle no wallet is meant to interpret |

The sign request sent the whole bundle, so a wallet was being asked what to do
with a launcher spend. It is not merely wasteful: a wallet handed a coin whose
puzzle it cannot interpret has to decide something about it, and this one fell
over.

The request now carries exactly the spends that ask this signer for a signature,
found by running each spend and looking for an `AGG_SIG` naming one of its keys.
For a lock that is the singleton spend alone — one spend of 1.7 KB instead of
three including a launcher. A sponsor's own coin is included when there is one,
because it carries its own `AGG_SIG_ME` on the wallet's synthetic key.

**Narrowing changes nothing about the signature.** `AGG_SIG_ME` binds its message
to its own coin, so the condition is identical whether or not the rest of the
bundle travelled with it, and the service still validates the whole bundle before
and after. `_test_vault_sign_request.py` pins both halves: what is chosen, and
that what is signed is unchanged by choosing it.

Confirmed live: the signature and transaction populated in Sage and went to the
network.

### The DID is published (2026-09-07)

The lock's DID is live and hinted on chain: launcher `7a69131d…`, owned by the
lock's own deposit puzzle, findable by any wallet or explorer that searches by
hint. That closes the run that began with a DID whose owner was curried as a
hash and could never be spent at all.

Two things the landing exposed:

- **The queue said "In mempool" after the block had taken it.** The chain settles
  it, and the queue asks the chain every 20 seconds, but between those moments
  the honest question is whether it is really still there. A submitted proposal
  now carries a **Check the chain** button beside its spend bundle.
- **The settle handler only understood re-keys.** Any proposal that lands changes
  what the lock holds, so the assets are re-read for all of them, and the notice
  says what actually happened — published, created, or confirmed — instead of
  going quiet for everything that is not a re-key.

And one regression worth recording, because it was exactly backwards: a DID lost
its **name** the moment publishing made it findable. The name lives in the
launcher's key/value list and nowhere else, and only the launch-derived scan was
reading it; the hint scan that takes over after publishing was not. Both read it
now.

### A lost answer is asked for once more (2026-09-07)

The relay was losing answers, not requests. Twice in a row the prompt appeared in
Sage, the person approved it, and nothing came back: a signature given and thrown
away, and an execute that was signed but never pushed. That is the worst failure
of the lot, because the work was done and the page reported nothing.

Requests to the wallet now retry **once**, with the relay socket reopened first,
since a closed socket is the usual reason an answer has nowhere to land. Applied
to the two calls that were losing them: `chip0002_signCoinSpends` and
`chip0002_sendTransaction`.

Two rules keep the retry honest:

- **A refusal is never repeated.** A no is a real answer, and asking again would
  put the same prompt back in front of someone who already declined. Detected by
  the WalletConnect rejection codes and by the wording of the error.
- **It is only safe because the answer is deterministic.** The same key over the
  same message makes the same signature, so a repeat cannot produce a conflicting
  share. `_test_vault_sign_request.py` pins that rather than assuming it.

Signing also checks the queue before asking at all: a share already recorded
means nobody is prompted again, which matters exactly when an answer was lost on
the way back but the share did arrive.

### The page was running out of memory (2026-09-07)

The crash was **the Forge tab, not Sage**: `Error code: Out of Memory`. That
reframes every "the wallet never answered" symptom in this stretch — a page
drowning in work cannot deliver a request or receive an answer, and looks from
the outside exactly like a dead relay.

The relay redelivers a message until the client acknowledges it, and a client can
only acknowledge what it can decrypt. Keys for a session that is gone are gone
too, so its queued traffic is redelivered forever and fails forever — the
`failed to process an inbound message` loop this file already documented, every
couple of seconds, for as long as the page is open. Each pass allocates.

`sweepOrphanedRelayState` only fires when there is **no** session and **no**
pairing, so it never touched the case that hurts: a working session alongside a
backlog for dead topics. `sweepUndeliverableMessages` runs before the client
subscribes to anything — afterwards is too late, the loop starts with the
subscription — and drops queued traffic keyed by topics nothing live owns. Live
traffic is untouched.

One guard on the guard: a backlog large enough to be a problem is too large to
inspect, so past 8 MB the queue is cleared without being parsed. Parsing it would
be an allocation big enough to kill the tab, which is the failure being
prevented. Nothing durable is lost; the relay resends what a live subscription
still needs.

### Every signature request has the same shape now (2026-09-07)

Re-key, DID creation and DID publish are one request shape, which is what makes
a failure in any of them mean something:

| kind | spends sent | of bundle | partial | reveal | solution | signatures |
|---|---|---|---|---|---|---|
| re-key | 2 | 2 | no | 1672 B | 581 B | 1 each |
| create DID | 1 | 3 | no | 1672 B | 449 B | 1 |
| publish DID | 1 | 2 | no | 1672 B | 449 B | 1 |

The re-key sends two because it carries a sponsor, whose own coin asks for its
own signature. Everything else the bundle contains — funds coins authorised by
announcement, launcher riders, the DID spend itself — asks the wallet for
nothing and is not sent.

**A signature here is not only a vote.** It is the authorisation the puzzle
demands: `AGG_SIG_ME` from M owners, without which the bundle cannot be pushed at
all. Execute assembles and broadcasts; it cannot manufacture consent. Recording
a yes off chain instead would be a permissions system, not a lock. What is now
uniform is the *request* — and that is what needed to be.

### Partial signing was decided by a race (2026-09-07)

After the sign request was narrowed, creating a DID signed cleanly and
publishing one still crashed Sage — on a request that decoded to the *same*
shape: one spend, the same 1672-byte reveal, the same 449-byte solution, the same
seven conditions. Nothing in the payload could explain it.

The difference was not in the payload. The browser decided whether to ask for a
**partial** signature by comparing the spend's owners against the wallet's key
list, and that list is fetched asynchronously. Before it arrived the comparison
found nothing and fell back to partial — the rarer request, and the one wallets
handle worst. So the same proposal was asked one way or the other depending on
when the button was pressed, which is why the failure looked random and why the
two attempts differed with nothing else to distinguish them.

Whether a partial signature is needed is a **fact about the spends being sent**:
do they ask for more than one key. `sign-request` computes it from the conditions
it just read and returns it; the browser obeys. No timing, no guess, no key list
needed to answer it.

### Signing as a key the wallet does not hold (2026-09-07)

A DID proposal on the 1-of-2 lock sat at "awaiting signatures 0/1" with the
request out and no prompt in Sage. The signature chips said which key the app
thought it was: **hearts**, the other owner — not the key the connected wallet
holds.

Both owners are valid signers of a 1-of-2, so the service answered the signing
request happily for either. The wallet could not: it was being asked for a
signature from a key it does not have, so nothing came back and nothing could.
From the outside that is indistinguishable from a wallet that never answered.

The cause is that identity was resolved **once, across every lock**. That is
right for the list — "which of my keys owns anything here" — and wrong inside a
lock, where the question is narrower: *which of this lock's owners can this
wallet sign as*. A wallet may hold a different key on each lock, or none.

Each lock now answers that itself, from the keys the wallet shared, and signs as
that key. When the wallet holds none of a lock's owner keys the lock says so
plainly and reads as an observer, instead of offering a Sign button that cannot
produce anything.

### A refresh dropped the wallet while Sage kept it (2026-09-06)

Reloading the page left the app disconnected while Sage still listed The Forge as
paired. Three things on the restore path could each cause it, and all three were
the same mistake: acting destructively on a guess.

- **The purge deleted sessions it could not read.** It ran on every page load and
  removed any stored session whose Chia namespace did not look right to this
  build — our reading of a record another program wrote. Read it wrong once and
  the connection is gone on refresh, unrecoverably in appearance, because the
  wallet still shows itself connected and reconnecting seems to do nothing. Only
  genuinely **expired** sessions are deleted now, which is objective: the session
  itself says when it ends. Anything unreadable is logged, with its namespaces
  and chains, and left alone.
- **The restore reported nothing when no session matched.** It now prefers a
  well-formed session and falls back to the newest unexpired one, saying so. A
  session the wallet believes in is worth trying; a request on it fails loudly
  and specifically if it really is unusable, which is better evidence than the
  guess.
- **The key read could hang the restore.** `chip0002_getPublicKeys` had no
  timeout, so a quiet relay left the session restored but keyless, and every flow
  that needs the keys waiting forever. It is bounded at 20 seconds — nobody
  approves a key read, so it should be quick.

The keys are also **remembered per session topic** now. They belong to the
session and cannot change while it lasts, so a refresh uses them immediately and
re-reads the window in the background. Keyed by topic so a new pairing never
inherits an old wallet's keys, dropped when the session ends, and public keys
only.

### A re-key was mirrored at the wrong moment (2026-09-06)

A re-key confirmed and moved to History, and the header and Owners tab went on
showing the policy it had just replaced until the page was reloaded by hand.

The mirroring ran the instant the transaction was **submitted**. At that point
the new policy is not on chain yet, so the read came back with the old one and
cached it, and nothing re-read it afterwards — the whole point of the refresh
was defeated by running it a block too early.

It now runs when the proposal **confirms**. The queue already polls while
anything is live, so the transition is noticed there; a re-key that has landed
mirrors its policy into the registry, re-reads the lock and the balance, and
replaces the "submitted" line with what the chain now says. A set of already
mirrored ids keeps it to once per re-key rather than once per poll.

The Owners tab also named the wrong puzzle: every vault was described as
`m_of_n` over `bls_member` leaves. MIPS picks `one_of_n`, `n_of_n` or `m_of_n`
by the threshold, and a 1-of-2 lock is `one_of_n` with each leaf behind an
`index_wrapper`. It now names what the lock actually uses, which matters because
the wrong name sends an auditor looking for a puzzle that is not there.

### The liveness ping was deciding things it could not know (2026-09-06)

Signing reported "the wallet session stopped answering the relay, twice" on a
session that was plainly connected — Sage listing Forge, the app showing the
fingerprint. That message came from a `wc_sessionPing` check, and it was wrong
twice over.

**A ping is its own round trip that the wallet answers itself.** A wallet can
ignore or lag on pings and still handle real requests perfectly, so a failed ping
is not evidence of a dead session. Acting on it did two harmful things: it
**deleted the pairing**, throwing away a session that still worked, and it put up
to 23 seconds in front of every request before the request the user asked for had
even been sent.

The ping is now advisory. It runs in the background, its result reaches the
console and nothing else, and it never blocks or deletes. Every request carries
its own budget and reports the method it was trying, which is a real observation
about a real call — that is what a failure should be based on.

The one thing that did belong to the old path moved to where it is true: clearing
a topic's undelivered relay traffic now happens on **disconnect**, when the
session is genuinely over, rather than when a ping timed out on a live one.

### Signing over WalletConnect: ask the ordinary way when you can (2026-09-06)

The Sign button on a proposal sent nothing to the wallet, on a session where
creating a lock had signed successfully minutes earlier. The difference is one
flag: launching a lock asks for an ordinary `chip0002_signCoinSpends`, while a
proposal asked with `partialSign: true`.

Partial signing exists for a real reason — a lock's spend carries one AGG_SIG
condition per revealed owner, and a wallet must sign its own and leave the others
untouched for their owners to sign later — but it is a far less exercised
request. **When every key a spend needs belongs to the connected wallet there is
nothing to leave alone**, so the ordinary request is used instead. A 1-of-1 lock
is the common case, and it stops depending on the rarer path for no benefit. With
the wallet's keys unknown, partial signing remains the assumption.

The wallet's keys are already known: they are captured at pairing for the coin
read, and the signing view names which owners a spend reveals, so the comparison
costs nothing.

**And the request is now bounded.** It had no timeout at all, so a wallet that
never routes it left a promise that never settled: no prompt, no error, a Sign
button stuck forever. The budget is five minutes, because a person has to read
and approve, and it is shared across the whole shape loop — retrying a signing
request that may yet be sitting in front of somebody risks a second prompt for
the same spend. The panel also reports which step it is on, so waiting on Sage
reads as waiting rather than as nothing happening.

### A profile record that could never settle (2026-09-06)

The profile bar read "awaiting confirmation" while the lock beside it read
"Published". Both were telling the truth about different things: the lock was on
chain, and the *record that was sent* had not landed as sent.

A submitted record only cleared when the on-chain profile matched it exactly, and
after locks were merged and set aside there was nothing on chain it could ever
match. The bar had no way out either: Discard only appears for pending changes,
so the record was stuck saying "waiting for a block" about a block long past.

It now clears itself when it has been **overtaken**: the chain's profile is newer
than the record that was sent and says something else, so that send did not win.
That test is exact rather than a guess, because the profile carries the timestamp
it was published at. A ten-minute fallback covers the case where the chain does
not give one. When it clears, the bar says what the chain actually holds instead
of pretending to wait.

### The wallet check tests the path in use (2026-09-06)

"Check wallet connection" originally asked the wallet for its coins, because that
is what the lock flows did. They no longer do — coins are found on chain from the
keys shared at pairing — so that check would report a failure for a path nothing
uses, which is worse than no check at all.

It now checks what a flow actually needs, in the order it needs it: the keys the
wallet shared, the coins found at their addresses on chain, and whether the
session can sign. A missing key share and a session without signing approval are
named separately, because they are fixed the same way but mean different things.

### Setting a lock aside (2026-09-06)

Superseded locks — an older puzzle, or the custody shape from before Forge
adopted Chia's — crowd the list without being finished: they still hold their
assets and can still be spent. They can now be **set aside**: hidden from the
list, counted on a button that brings them back. Nothing on chain changes and
nothing is deleted. A vault also shows a `pre-standard` badge when its policy
format is not `mips/1`, so it is clear which is which before setting anything
aside.

The older `p2_m_of_n_delegate_direct` locks remain readable and spendable as **legacy**;
their re-key still moves to a successor address. Routes dispatch on `safe.puzzle`
(`api/_multisigTool.js: toolFor`), the registry stores `launcherId`/`launch`, and
`multisig-refresh` mirrors a vault's on-chain policy into the registry on every read —
that is also how a launch is confirmed and how a re-key lands.

### What a lock owns: coins, and singletons (2026-09-06)

XCH and CATs are found **at** the lock's deposit address, by puzzle hash. An NFT
or a DID is not: a singleton sits at its own puzzle hash, which changes every
time its inner puzzle does, so nothing about a transfer leaves a coin at the
owner's address. What ties one to an owner is the **hint** the transfer wrote,
which is why wallets discover NFTs that way and why the balance scan could not
see them.

`vault_tool.scan_singletons` reads `get_coin_records_by_hint(deposit_ph)`, keeps
unspent odd-amount coins (a singleton's amount is odd; the launcher's own coin is
not), and classifies each by reading the **parent's** spend — the reveal there is
the puzzle the coin was created from, and uncurrying it tells NFT from DID. A
coin whose parent cannot be read is reported as `unknown` with the reason, never
guessed at.

It is opt-in, because it costs a parent lookup per hinted coin:
`GET /api/multisig-balance?safeId=…&singletons=1`, and the frontend asks only for
vault locks, since a legacy lock has no singleton of its own. The response omits
the fields entirely when not asked, so the interface can tell "no NFTs" from
"not looked".

Verified against the live vault lock on testnet11: the scan finds the lock's own
1-mojo pointer coin and correctly declines to call it an NFT. The NFT and DID
branches of the classifier are **not yet exercised** — the test wallet holds
neither — so they are written and typed but unproven.

### Two panel fixes found by using it (2026-09-06)

**A re-key that executed did not reach the screen.** `handleExecute` refreshed
the proposal list and nothing else, so the header and the Owners tab kept
showing the policy the transaction had just replaced until the page was
reloaded. The new policy is on chain by then and `multisig-refresh` is what
mirrors it into the registry, so an execute that carries a successor now calls
that route and asks the parent to re-list the safes.

**The Owners tab explained the wrong puzzle.** It described
`p2_m_of_n_delegate_direct` on every lock, including vaults, and said the owner
set is part of the address and cannot be edited in place — on the very screen
where a vault had just changed owners in place at the same address. The copy is
now chosen by `safe.puzzle`.

The Assets tab is also split into **Tokens / NFTs / DID**, the way Liquidity
splits add from remove, so the singleton views have somewhere to live.

### Giving a lock a DID (2026-09-06)

A DID's owner is not attached after the fact. It is the `p2_puzzle_hash` curried
into the DID inner puzzle when the singleton is launched, so the launch can name
the lock and the DID belongs to it **from its first coin**. That is what
`contracts/did_launch_tool.py` builds: it picks the funding coin, derives the
launcher from it (the inner puzzle is curried with the launcher id, so the parent
has to be chosen first), curries the lock's deposit puzzle hash as owner, and
emits two spends — the funding coin's, and the launcher's, which needs no
signature. `POST /api/multisig-did` returns them unsigned and the wallet signs
and broadcasts. One transaction, one approval.

**Why not Sage.** `create_did` always mints to the wallet running it. Passing
`address`, `target_address`, `owner` or `puzzle_hash` changes nothing — probed,
and the built spends pay the wallet's own addresses in every case. Minting there
and transferring afterwards would mean two transactions, a window in which a
person owns the protocol's DID, and a stranded DID if the second one does not
land. All three happened while this was being built, which is why it is done
directly now.

**Build, never submit.** Every call passes `auto_submit: false` or builds the
spends outright, so the server can only ever produce unsigned coin spends and the
sole thing that can sign is the wallet, in front of the user. The first cut used
`auto_submit: true` and the user rightly asked why there had been no approval:
Sage's local RPC is an operator interface, and a client holding its certificate
signs and broadcasts with no prompt. Right for a deploy script, wrong for a
button.

**Two ways to pay, and the lock's is now the real one.** `did_launch_tool.py`
builds a launch funded by the caller's wallet, which signs it — the way a
proposer's wallet sponsors a re-key. That was the interim.

The proper shape is now built and wired: a **DID proposal** on the lock itself
(`kind: "did"`), created from the DID tab's **Propose DID** and shown in the
Queue with its own card, signed and executed like any other proposal.
Two guards had to learn the new kinds on the way: the proposals route required at
least one output, which a DID launch does not have, and the index normaliser
collapsed every kind that was not `rekey` down to `send`, so a stored DID
proposal came back looking like a payment. The lock's own coin is the launcher's parent, its owners approve
it at their threshold like any other proposal, and the launcher spend rides in
the bundle unsigned, pinned by an assertion inside the conditions the owners
sign. See `FORGE_LOCK_SAFE_MODEL.md` for why a rider is safe and what it costs to
add another one.

**The tab shows what the LOCK owns, never the wallet.** An earlier cut listed the
wallet's DIDs with a "move to lock" button, which conflated what this lock owns
with what the person looking at it happens to hold. Removed. Vault locks only: a
legacy lock has no singleton to authorise moving a DID later.

**Off unless the host says otherwise** (`MULTISIG_DID_CREATE_ENABLED=true`).

### What a lock owns: coins, and singletons (2026-09-06)

XCH and CATs are found **at** the lock's deposit address, by puzzle hash. An NFT
or a DID is not: a singleton sits at its own puzzle hash, which changes every
time its inner puzzle does, so nothing about a transfer leaves a coin at the
owner's address. What ties one to an owner is the **hint** the transfer wrote,
which is why wallets discover NFTs that way and why the balance scan could not
see them.

`vault_tool.scan_singletons` reads `get_coin_records_by_hint(deposit_ph)`, keeps
unspent odd-amount coins (a singleton's amount is odd; the launcher's own coin is
not), and classifies each by reading the **parent's** spend — the reveal there is
the puzzle the coin was created from, and uncurrying it tells NFT from DID. A
coin whose parent cannot be read is reported as `unknown` with the reason, never
guessed at.

It is opt-in, because it costs a parent lookup per hinted coin:
`GET /api/multisig-balance?safeId=…&singletons=1`, and the frontend asks only for
vault locks, since a legacy lock has no singleton of its own. The response omits
the fields entirely when not asked, so the interface can tell "no NFTs" from
"not looked".

Verified against the live vault lock on testnet11: the scan finds the lock's own
1-mojo pointer coin and correctly declines to call it an NFT. The NFT and DID
branches of the classifier are **not yet exercised** — the test wallet holds
neither — so they are written and typed but unproven.

### Two panel fixes found by using it (2026-09-06)

**A re-key that executed did not reach the screen.** `handleExecute` refreshed
the proposal list and nothing else, so the header and the Owners tab kept
showing the policy the transaction had just replaced until the page was
reloaded. The new policy is on chain by then and `multisig-refresh` is what
mirrors it into the registry, so an execute that carries a successor now calls
that route and asks the parent to re-list the safes.

**The Owners tab explained the wrong puzzle.** It described
`p2_m_of_n_delegate_direct` on every lock, including vaults, and said the owner
set is part of the address and cannot be edited in place — on the very screen
where a vault had just changed owners in place at the same address. The copy is
now chosen by `safe.puzzle`.

The Assets tab is also split into **Tokens / NFTs / DID**, the way Liquidity
splits add from remove, so the singleton views have somewhere to live.

### Giving a lock a DID (2026-09-06)

A lock cannot mint its own DID. A DID is a singleton launched by a wallet spend
and the lock holds no key of its own, so the connected Sage **builds** the spends
(`create_did`, then `transfer_dids`) and `POST /api/multisig-did` hands them back
for the wallet to sign and broadcast. The **transfer** is the part that matters:
afterwards the DID's inner puzzle is the lock's deposit puzzle, so moving it
needs the lock's own singleton to authorise, exactly like its XCH and CATs. It is
two transactions, because the DID has to exist before it can be moved, so
**Create DID** is one action with two approvals: mint, wait for the mint to
confirm, transfer onto the lock.

**The tab shows what the LOCK owns, never the wallet.** An earlier cut listed the
wallet's DIDs there with a "move to lock" button, which conflated two different
things: what this lock owns and what the person looking at it happens to hold. A
lock's Assets tabs answer only the first. The one exception is a DID this flow
just minted and has not finished moving, remembered in component state so a
half-done mint is not stranded; the tab never enumerates the wallet. (The flow
does read the wallet's DID list transiently, to learn which DID it just minted,
because Sage's build-only mint does not return the launcher id. Deriving it from
the launcher coin in the returned spends would remove even that read.)

**Build, never submit — a correction (2026-09-06).** The first cut called Sage
with `auto_submit: true`, and the user asked the right question: why was there no
wallet approval? Because Sage's local RPC is an *operator* interface. A client
holding its certificate signs and broadcasts with no prompt, which is how the
deploy scripts push transactions all day. Behind a button in a web interface that
is wrong: it minted an identity and spent funds with no confirmation. Every call
now passes `auto_submit: false`, so the server can only ever produce unsigned
coin spends and the sole thing that can sign them is the wallet, in front of the
user. Verified by counting the wallet's DIDs either side of a `build-create`:
three coin spends came back and nothing was created.

Why a lock would want one: a DID can own a collection, so NFTs minted under it
carry the lock's identity and group together in a wallet or on an explorer.

**Off unless the host says otherwise.** The route spends from whichever Sage the
server is connected to. On a developer's machine that is their own wallet; in a
hosted deployment it would be the operator's, minting and paying for a DID that
then belongs to someone else's lock. `MULTISIG_DID_CREATE_ENABLED=true` turns it
on, and it is refused with that explanation otherwise — the same stance
`api/sage-offers.js` takes about creating offers. Vault locks only: a legacy
lock has no singleton to authorise moving a DID once it holds one.

### The Assets tab acts on what it lists (2026-09-07)

The tab used to be a read-out: asset, balance, coin count, asset id. The coin
count was the least useful of those — a number with nothing to do about it — and
what was missing was worth and a way to act.

**Value** comes from the same place Markets and Holdings get it: the pools'
own reserves (`buildXchPrices`), with `xchPerLp` for LP CATs and `/api/xch-price`
for the USD line. A CAT no Forge pool prices shows `—`, never `$0.00`, and the
total says so rather than quietly dropping it. `useAssetValues()` lives in the
panel, not the card, so moving between tabs does not start the pool read again.

**Send** opens the existing composer with that row's asset already chosen — the
same proposal as before, reached from where the person was already looking.

**Combine and Split** are proposals whose outputs go back to the lock's own
address. Nothing new was needed on the contract side: `build_plan` selects the
lock's coins, the outputs name `safe.address`, and a CAT output carries the
lock's hint the way any payment does. What changes is the *shape* of the
holding, and shape decides how much can happen at once — a proposal is pinned to
the coins it selected, so two coins can be signed and pushed independently where
one cannot. Combine is offered when an asset sits in more than one coin; Split
takes a part count up to `MAX_SPLIT_PARTS` (12, well under the service's 40-coin
ceiling) and puts the odd mojos on the first coin. `splitAmounts` and
`coinShapeOutputs` are pure and pinned by `src/lib/__checks__/coinShape.check.ts`
(40 checks): nothing created, nothing destroyed, no zero-amount output, and an
amount too small to go round left whole.

A combine of XCH spends the 1-mojo pointer coin planted at launch along with the
rest, and that is safe: `find_launcher_from_deposit` asks the node with
`include_spent_coins` and reads the launcher out of the memos on the coin's
*parent* spend, which is history and cannot be undone. The form says so where
someone would otherwise have to work it out.

Both proposals carry no fee of their own, like a DID proposal: whoever pushes
pays, so proposing costs the lock nothing.

**Swap from the lock is not in this** — see the gap below.

### An NFT the lock owns (2026-09-07)

Two things, and they compose: where the NFT goes, and which DID it belongs to.

**Sending is a payment in disguise.** The lock's own puzzle sits at the bottom of

    singleton( nft_state_layer( metadata, updater,
                 nft_ownership_layer( owner_did, transfer_program,
                   p2 = the lock's deposit puzzle )))

so it says only `CREATE_COIN(where it goes)` and the layers above rebuild the NFT
around that address, carrying metadata, royalties and owner across for free. Each
layer wraps the solution one deeper — the state layer takes `(inner_solution)`,
and with an ownership layer that inner solution is itself `(p2_solution)`, which
is what `SingletonHost.layers = "nft"` encodes.

**Assigning a DID is not.** The condition is `-10`
(`new_owner, trade_prices, new_did_inner_hash`), and
`nft_ownership_transfer_program_one_way_claim_with_royalties.clsp` then demands
an `ASSERT_PUZZLE_ANNOUNCEMENT` from the DID's *full singleton puzzle* carrying
this NFT's launcher id. In other words the DID has to be spent in the same
transaction and say yes. For a lock that owns both, that is the easy case: one
proposal carries both singleton spends and one signature covers them, which is
the batching property already proved. Clearing an owner needs no approval — the
assertion only appears when the new owner is set and differs from the current
one.

`publish_did_spend` therefore takes extra conditions: a DID that is approving an
NFT emits the announcement in the same spend that recreates it, because a coin
can only be spent once. `cmd_propose` collects singleton spends per launcher for
exactly that reason — a DID that is both published and giving its approval does
both at once.

**Nothing is rebuilt on trust.** An NFT's current puzzle is reconstructed from
its parent's spend (`recurry_nft_puzzle` for the ownership layer,
`get_metadata_and_phs` for metadata) and then checked against the coin's own
puzzle hash; a mismatch refuses the spend instead of building it. That is the
inert DID's lesson applied: hashing something is not the same as its being right.

Pinned by `contracts/_test_vault_nft.py` (39 checks), which RUNS the puzzles
rather than hashing them. The check that carries the weight builds both halves of
an assignment and compares them: the announcement the DID emits against the one
the NFT's transfer program demands, matched the way consensus matches it —
`sha256(puzzle_hash || message)`. It also pins that the lock's own gate survives
under both NFT layers, so an NFT still cannot move without the lock authorising
that exact coin with that exact delegated puzzle.

**Not yet exercised on chain:** the live lock holds no NFT, so every check here is
against puzzles run locally. The first real move is the proof.

### Many actions, one vote (2026-09-07)

The question was whether a lock could queue several things for signature and then
push them together. It can do the useful half of that, and the reason needs no
new puzzle: **CNI's vault already batches.** The singleton's delegated puzzle
emits one `CREATE_PUZZLE_ANNOUNCEMENT` per funds coin, naming that coin together
with the delegated puzzle it is allowed to run, and asserts each coin's own `$`
announcement back. So one singleton spend authorises an arbitrary set of coins,
each doing something different, and the owners sign exactly one message for the
whole set.

What cannot be done is the other half: **merging proposals after they are
signed.** Each owner's signature is `AGG_SIG_ME` over the delegated puzzle hash
*and the tip coin id*. Two proposals are two different spends of the same tip —
they cannot both be in one bundle, and merging them into a third plan destroys
the hash both signatures were given for. That is also why executing any proposal
supersedes the rest, which the queue has always said and now has a reason on
record for.

So the composing happens **before** the vote. The panel keeps a batch: staged
actions accumulate in a card above the tabs, and *Propose all* sends them as one
proposal — one signature round, one push. Every composer that makes a payment can
stage instead of proposing (send, combine, split), as can a DID publish;
`publish_did` now takes a list, since `build_vault_plan` always took a list of
singleton spends.

Two things stay out of a batch:

* **An offer.** Its bundle is incomplete until a taker completes it, so anything
  sharing its singleton spend would wait for that taker. The tool refuses the
  combination rather than producing a batch that silently never settles.
* **A second DID launch.** The launcher is derived from a chosen coin of the
  lock's, and two launches would want the same one.

Pinned by `contracts/_test_vault_batch.py` (30 checks): one message whatever the
batch holds, every coin authorised by its own delegated puzzle hash, every coin
required to be present so no part of an approved batch can be dropped on the way
to the chain, and the arithmetic staying right per asset as actions pile up.
Verified live on the `aWizard` lock: a combine and a split staged together became
one proposal, three outputs across two assets, four coins, one signature line.

### A lock writes its own offer (2026-09-07)

Send, combine and split are *payments*: the lock spends its coins and names the
puzzle hashes they land on, and the transaction is finished the moment the owners
have signed it. A swap is not that. Forge settles swaps from an **offer**, and an
offer is written by whoever gives up the asset — so for a lock to swap, the lock
has to be the maker.

The shape is the one any wallet builds, which is the point: the result is an
ordinary offer file that Forge's router, Dexie, or a person with a wallet can
take without knowing what a Forge lock is.

1. choose the lock's coins for what is being offered;
2. notarize what is wanted back, stamped with a nonce derived from exactly those
   coin ids, so the file cannot be pointed at a different set;
3. spend the chosen coins into the settlement puzzle, asserting the puzzle
   announcements those payments imply. Nothing satisfies them yet: whoever takes
   the offer satisfies them by paying.

What makes it a *lock's* offer is only where the authorisation comes from. The
singleton spend rides in the bundle and announces each funds coin exactly as it
does for a payment, and the owners sign one delegated puzzle. **The vote is
unchanged; what it approves is an offer instead of a transfer.** Nothing new was
needed in the signing path, the wallet request, or the share bookkeeping —
`build_vault_plan` gained one hook (`asset_conditions_builder`, conditions that
belong to one asset's spend, run after coin selection because the nonce depends
on which coins were taken) and `Output` gained `hint=False`, because a settlement
coin nobody owns should not carry a hint naming a puzzle no wallet watches.

**Executing an offer produces a file, not a transaction.** `cmd_assemble` refuses
to push one and refuses a fee for it — the announcements are unsatisfied by
design, so there is nothing a node would accept and nothing a fee could buy.
Whoever takes it pays to settle it. The proposal moves to a status of its own,
`offered`, which the queue treats as live: the coins are still the lock's, it can
still be taken, and it can still be cancelled.

**Cancelling is a vote, because there is no on-chain cancel.** An offer stops
being takeable when the coins it names are gone, so "Propose cancelling it"
creates an ordinary proposal paying the offered asset back to the lock's own
address — the combine from the assets table, reused. Once it is signed and
pushed the offer can never be filled. Whether an offer that vanished was *taken*
or *cancelled* is invisible from the offered coins, which are spent either way;
what tells them apart is whether the settlement coins exist on chain, and
`multisig-refresh` asks exactly that before deciding between `confirmed` and
`stale`.

**The router's fee is a leg of the same offer.** A Forge route owes a payment to
somewhere that is not the lock, so a requested payment may name its own
destination; two legs of one asset to two addresses are two payments under one
nonce. An offer that quietly sent that leg home would look correct and never be
taken.

Pinned by `contracts/_test_vault_offer.py` (61 checks), which builds the offer and
then asks `chia.wallet.trading.offer.Offer` — the class Sage, Dexie and the router
all read one with — what it sees, rather than checking intermediate values against
expectations of my own.

### What this design cannot do

- **(Legacy locks only.) Owners and threshold are the address.** Changing either is a payment to a
  new safe — and therefore a decision of the lock, taken at its threshold. Vault locks do not have this limit.
  The Owners tab's 🗝️+ control opens a **re-key proposal**: the successor
  policy is chosen, a proposal on the current lock is built that pays all its
  XCH and CATs to the successor address *and* creates the successor's 1-mojo
  manifest coin in the same spend, and the proposer signs it in Sage on the
  spot. The other owners then sign (yes) or reject (no) from the queue; once
  M have signed, the final transaction executes and the successor is
  described on chain from that block. A rejection cannot undo a signature,
  but once the refusals leave fewer than M owners able to sign, the proposal
  is marked rejected. A successor identical to the current policy is refused.
  Until the vote passes the successor is only a card in the queue; it is
  registered as a lock when the re-key executes, so no half-made lock ever
  sits in the list.
- **The proposer's wallet sponsors the re-key.** The lock's coins carry the
  authorization, but the fee and the successor's manifest mojo come from a
  *sponsor spend*: one of the proposer's own XCH coins, spent by its standard
  puzzle (AGG_SIG_ME, signed by the proposer's Sage in the same prompt as their
  yes). Sponsor and lock primary assert each other's coin announcements, so
  neither half can be broadcast alone; `validate_bundle` expects exactly one
  AGG_SIG_ME (message extended with coin id and the network's genesis
  challenge) and `choose_shares` requires exactly one share to carry it. The
  lock therefore needs no XCH of its own to change hands — one mojo suffices. CNI's vault member puzzles (`M_OF_N` over a merkle tree inside a
  singleton, `BLS_MEMBER`, `TIMELOCK`, the secp/passkey members) keep the
  address across rekeys and are the intended successor; the plan format is
  versioned so that adapter can slot in when Sage signs for them.
- **No on-chain reject.** Collected signatures are bearer material until the
  safe's coins move — the same property Safe has before the nonce advances.
  Cancel in the UI is bookkeeping; to kill a fully signed proposal, spend its
  coins (a payment from the safe to itself).
- **AGG_SIG_UNSAFE, not AGG_SIG_ME.** Mitigated by the coin-id pin above; a
  future revision on the vault puzzles moves to `AGG_SIG_ME` for free.
- **An outstanding offer is cancelled by any other spend.** Every proposal spends
  the singleton tip, and an offer's bundle names *this* tip; execute anything
  else and the offer stops being valid. That is what makes cancelling possible at
  all, and it also means a lock can have one offer outstanding or ordinary
  business, not both. The queue shows an offer as live for exactly that reason.
- **The route is quoted when the proposal is written, not when it is signed.**
  What the offer fixes is a floor — the least the lock will accept. Pools move
  while owners are signing; a floor survives that in the lock's favour and simply
  goes untaken if the market runs the other way, which is the safe direction for
  a trade nobody is watching. An order large enough to move the pools gets a wide
  floor, and the composer says by how much rather than burying it.

---

## Surface

### Files

| Layer | File | Role |
|---|---|---|
| Puzzle logic | `contracts/multisig_tool.py` | derive · balance · propose · sign-request · verify-share · assemble · status. One JSON in, one JSON out. |
| Tests | `contracts/tests/test_multisig_tool.py` | XCH and CAT proposals against a fake node, any-two-of-three assembly, forged/crossed signatures refused, replay guard, two-key shares. |
| Registry | `api/_multisigIndex.js` | `.awizard/multisig-index.json`: safes and proposals with their shares. |
| Profile & manifest | `contracts/multisig_profile.py`, `api/multisig-profile.js`, `api/multisig-manifest.js`, `contracts/tests/test_multisig_profile.py` | The on-chain safe list and the safe's self-description: read newest record, build the publishing spends, resolve by-address entries; round-trip, forgery and refusal tests against a fake chain. |
| Routes | `api/multisig-safes.js` `-balance.js` `-proposals.js` `-sign.js` `-execute.js` `-refresh.js` `-cancel.js` `-signer-key.js` | Thin: validate, call the tool, persist. `-signer-key` reads the local Sage's first key (`contracts/sage_signer_key.py`) or derives an address for a given key. |
| Offers | `contracts/vault_offer.py`, `contracts/_test_vault_offer.py` | The lock as an offer maker: notarized payments, settlement outputs, assembly into a bech32 offer. |
| Client | `src/lib/multisig.ts` | Typed fetchers and `signProposalWithWallet`. |
| Offer routing | `src/lib/lockOffer.ts` | Hands a lock's offer to the lane that quoted it; a failure leaves the offer intact and says so. |
| NFTs | `contracts/vault_nft.py`, `contracts/_test_vault_nft.py` | Sending an NFT and putting one under a DID, with the DID's approval built into the same spend. |
| UI | `src/components/MultisigPanel.tsx` | Safe list · create · safe view (Assets / Queue / History / Owners). |

The route check `api/__checks__/multisigIndex.check.mjs` runs under
`npm run check:quoting`; the Python suite under
`.venv/Scripts/python -m pytest contracts/tests/test_multisig_tool.py`.

### Lifecycle

1. **Create.** The creator is the first owner implicitly — their signer key
   needs no typing, and a safe with only them is 1 of 1. Co-owners are added
   by **wallet address**: an address is a hash, so the key cannot be computed
   from it, but any spend the address has made reveals its puzzle on chain and
   the synthetic key inside it (`multisig_profile.py key-for-address`, via
   `/api/multisig-signer-key?address=`). Publishing a profile is such a spend,
   so anyone who has used the tab is findable; a never-spent address falls
   back to pasting the signer key. The address
   is derived from that policy. With a wallet connected, creating is one
   signed transaction from the creator's wallet that posts the safe's
   manifest at the safe's address and the creator's updated profile at their
   own: the chain then carries both the safe's description and the creator's
   claim to it. Without a wallet the safe is only registered and the profile
   entry stays pending. Registering the same policy twice returns the
   existing safe. Each owner's own wallet address — the standard puzzle
   for their key, i.e. where that wallet would be paid directly — is derived
   at the same time and shown beside the safe (yours if you are an owner,
   otherwise the first owner's) and in the Owners tab; click to copy in full.
2. **Fund.** Send XCH or any CAT to the address from any wallet. Assets shows
   XCH plus every CAT Forge knows about (swap list, pool reserves, LP CATs);
   an unknown CAT is not scanned.
3. **Propose.** An owner fills recipient, asset, amount, fee. The service
   builds the spends against the safe's current coins. Proposals for the same
   coins conflict: the first to execute makes the rest *superseded*.
4. **Sign.** Each owner opens the queue, clicks Sign, approves the partial
   signature in Sage. Progress shows which owners have signed.
5. **Execute.** Once `M` have signed anyone may execute. The proposal moves
   to *In mempool* with its spend bundle id, then to *Confirmed* in History
   when the node reports its coins spent. A push rejection stays on the card
   and the proposal remains open, since the shares are still valid.

### Environment

- `MULTISIG_NODE_URL` — override the coinset endpoint the tool reads from and
  pushes to (default per network: `https://testnet11.api.coinset.org`,
  `https://api.coinset.org`).
- `VITE_CHIA_NETWORK=mainnet` — switches the tab, like the rest of the app,
  to mainnet addresses and node.
- `AWIZARD_STATE_DIR` — relocates the registry, as for the other indexes.

### Errors you will see

| Message | Meaning |
|---|---|
| `insufficient XCH: need N mojos, safe holds M` | Amount plus fee exceeds the safe's confirmed XCH. |
| `a CAT minted straight into the safe cannot be spent from here` | The CAT coin's parent is not a CAT; no lineage proof can be built. Send it through a wallet first. |
| `signature does not verify for any owner key` | The wallet signed with a key that is not an owner, or signed a different proposal. |
| `This signature overlaps an owner who already signed` | A wallet holding two owner keys signed after one of them had signed separately; shares cannot be split. |
| `cannot reach the threshold ... with non-overlapping shares` | Signed keys exist but no combination of whole shares makes exactly `M`. |
| `push_tx rejected: ...` | The node refused the bundle; usually fee. The proposal stays open. |
