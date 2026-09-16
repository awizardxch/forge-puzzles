# Forge on the CHIPs — the workflow as built

*For the CNI audit request and the community thread. Written 2026-09-05 against
V11.1 (protocol 12), live on testnet11. The earlier thread in the CNI Discord
described the hand-bound design; this is the update, with the action layer and
finalizer in place of that design's own binding checks.*

Forge is an N-asset weighted constant-function market maker on Chia. One pool is
one singleton; its assets sit in one reserve coin each; liquidity is a CAT whose
supply only the pool can move. This document says which CHIPs the design stands
on, walks one spend from the trader's signature to the successor coins, lists
what changed since the last thread, and ends with short answers written to be
pasted into Discord.

Companion documents: `FORGE_V11_ARCHITECTURE.md` (diagrams of the same
mechanism), `FORGE_PUZZLE_V11.md` (the puzzle set field by field),
`FORGE_V11_CLVM_PASS.md` (the written pass, leaf by leaf, with every assert and
its test), `FORGE_SECURITY_AUDIT.md` (the findings log and what the internal
audit probed), `FORGE_V11_FOUNDATIONS.md` (the phase record with chain
heights).

---

## 1. Which CHIPs, for which part

The composition is the contribution, so each part names its standard.

| CHIP | Used for | Where in Forge |
|---|---|---|
| **0050** Action Layer and Slots | The pool singleton's inner puzzle: a merkle tree of six leaves, ephemeral and persistent state threaded action to action, a finalizer that runs once per spend. Slots hold oracle observations and the registry's sorted pool list. | `action.rue`, `slot.rue`, `p2_delegated_by_singleton.rue` vendored from the reference implementation and pinned by hash; Forge writes only the leaves, the finalizer and the TAIL |
| **0025** Message Conditions | Every authorization in the design. Mode 23 (sender by puzzle hash, receiver by coin id): pool → each reserve, leaf → the LP eve or melt coin, DAO coin → pool. No puzzle takes an authorizing coin id from its own solution. | `forge_multi_reserve_finalizer.rue`, `forge_action_add/remove/dao_fee.rue`, `forge_lp_cat_tail.rue` |
| **0020** Hinted Coin Discovery | Every coin the pool creates carries a hint: the launcher id on pool coins, reserves and renames; the recipient on LP and payouts. Wallets and the indexer find everything cold. | the finalizer's recreate conditions, the leaves' payout conditions |
| **0014** `ASSERT_BEFORE_*` | The prologue pins the spend to a height `h` (`ASSERT_HEIGHT_ABSOLUTE h`) and bounds it (`ASSERT_BEFORE_HEIGHT_ABSOLUTE h + oracle_window`), so an oracle observation is made against a height the chain agrees on. | `forge_action_common.rue::prologue` |
| Singleton top layer, CAT2, settlement payments (`OFFER_MOD`) | Unchanged standard puzzles: the pool is a singleton, reserves and LP are CATs, and a trade is a signed Offer whose settlement coins the pool consumes and whose notarized payments the pool's payout coin fulfills. | `forge_v11_driver.py`, `forge_v11_offer.py` |
| **0040** `everything_with_singleton` TAIL | Evaluated, not adopted: Forge's TAIL also binds the supply change to the pool's next state root and requires a melt's parent to be a CAT of the same asset. Recorded as a candidate once the standard TAIL carries an equivalent line. | `forge_lp_cat_tail.rue` |
| **0051** Reward Distributor | Evaluated, declined for trading fees: it distributes by staking, and Forge's fees accrue to the reserve and are paid by the pool's own `collect` leaf. | — |

The reference puzzles are compiled from source by rue and compared to the pins
on every build (`_test_v11_integrity.py`, 101 checks). Forge has not edited
them; editing one would move it into the audit's scope and remove the reason for
adopting the framework.

---

## 2. One spend, the detail in the middle

The product path, written the way it is reviewed: the mechanism in the middle at
full depth, and a thin line on either side for what feeds it and what falls out.
A reviewer who has the middle can infer the flanks. The trader holds the only key
that matters; the router holds none.

### What enters — two lines

- **The trader's Offer.** Signed in Sage. Offered coins go to the settlement
  puzzle; the requested payments are notarized. This is the only signature over
  the trader's coins, and it fixes what they give and the least they accept.
- **The router's solution.** Built with no key from the pool's current state:
  the distinct leaves, a merkle proof per selector in reverse execution order
  (a proof only on a selector's last use), one solution per action in execution
  order, one parent id per reserve.

### The spend itself — where the detail lives

1. **The prologue runs once, in the first action.** It pins the spend to a
   height `h` (`ASSERT_HEIGHT_ABSOLUTE`) and bounds it at `h + oracle_window`
   (`ASSERT_BEFORE_HEIGHT_ABSOLUTE`), accumulates the oracle on the pre-spend
   price, and records `prev_root`, the tree hash of the state this spend started
   from. Every later action in the same spend must name the same `h` out of
   ephemeral state, so a bundle cannot hold two opinions about when it happened.

2. **Each leaf is proven, then run.** The action layer verifies the leaf against
   the merkle root curried into the pool and runs it on `((ephemeral . state) .
   solution)`. The leaf returns that pair and its conditions; the new state feeds
   the next action. Six leaves exist — `swap`, `add`, `remove`, `observe`,
   `collect`, `dao_fee` — and the set *is* the root, so a pool cannot grow an
   action later.

3. **Conditions come back in two kinds.** Untagged ones are the singleton's own.
   A condition tagged `(-42 i . condition)` belongs to reserve `i`. `swap`
   asserts the settlement coin's announcement, `sha256(settlement_ph,
   tree_hash((coin_id . nil)))`, binding a coin and not an amount — which is what
   lets one pool's payout coin be the next pool's settlement inside one bundle,
   and one entry coin fan out to several pools. `add` and `remove` send the LP
   message.

4. **The finalizer runs once, and it is the whole authorization story.** It
   recreates the singleton at the same puzzle with the new state, buckets the
   tagged conditions per reserve, and fails the spend on an index outside
   `0..N-1`. Then one CHIP-0025 mode-23 message per reserve, every spend, whether
   that reserve was touched or not: the receiver id is derived from the reserve's
   parent id (solution), its curried full puzzle hash, and its **pre-spend amount
   read from the truth**; the message is the tree hash of the delegated puzzle
   the reserve will run. The receiver comes from the truth, never from the
   solution — that is the line that removes the "any coin can announce" class.

5. **Each reserve runs exactly the puzzle it was named.**
   `p2_delegated_by_singleton` receives the message for that delegated puzzle and
   nothing else: recreate itself at `reserves[i] + fees_owed[i] + dao_owed[i]`,
   hinted with the launcher, then whatever the leaves asked — such as the payout
   coin that fulfills the offer's notarized payments through the settlement
   puzzle. A reserve has no other way to move.

6. **LP supply moves only on the pool's word.** The eve coin (mint) or melt coin
   under the LP TAIL receives a mode-23 message from the pool's full puzzle hash
   carrying `["forge-lp-v11", lp_delta, new_total_lp, next_state_root]`. The TAIL
   checks the effective supply change against it and requires a melt's parent to
   be a CAT of the same asset. The first mint has no pool spend to vouch for it,
   so it is bound to the launcher's announcement instead, and the registry's
   `register` asserts the same announcement, so a registered pool's genesis
   supply equals its eve state.

### What leaves — one line each

- **The successor singleton**: same puzzle, new state, chained to the last by
  `prev_root`.
- **The reserves and the payout**: recreated at the amount rule; the payout coin
  settles the trader's requested payments.
- **The LP change**: minted or melted by exactly the delta the pool's message
  named.
- **One fee coin, then the push**: the router signs only its own coin, the
  offer's aggregated signature covers the trader's. It can decline to settle,
  never alter what was signed.
- **Hints on everything**: an indexer that fell behind rebuilds the state by
  replaying spends along the `prev_root` chain (`forge_v11_resync.py`).

**Creating a pool** is the same shape once. A launcher mints the singleton; the
reserves are created at the eve amounts; the genesis LP is minted by the
launcher's announcement; the registry singleton, itself on the action layer,
inserts the pool into a sorted slot list keyed by `(assets, weights, fees, DAO
recipient)` so a duplicate market is refused; the registry's creation fee settles
in the same bundle. A creator signs one offer; the router's `prepare-create` and
`create` build the rest with no key.

---

## 3. What changed since the earlier thread

The design discussed before bound reserves and LP with its own announcement
checks: about twenty-one Forge-written binding conditions, each a place to get
wrong, and the findings log shows that class getting wrong more than once. The
current design has about four Forge-written bindings, all of them CHIP-0025
messages whose sender or receiver is fixed by the framework rather than by a
value read from a solution. The reserve-only failure class cannot be expressed
in it: a reserve moves only for the delegated puzzle its singleton named, to a
receiver derived from the singleton's own truth.

Beyond the frame:

- **An oracle** (price-time accumulators, TWAP by two observations) as a leaf,
  because the action set is fixed at creation and anything a pool may ever
  need has to be there from the start.
- **A registry** of pools on the same framework, sorted slots, duplicate keys
  refused, a creation fee to the treasury.
- **A DAO fee (V11.1).** A second slice of every swap's output to a recipient
  curried at creation, at a rate held in state that can only fall. The sixth
  leaf lowers it on a mode-23 message from a coin at the recipient's puzzle
  hash; at zero there is no path back up. Live on testnet11: rate lowered at
  height 4,650,872, a swap accrued both slices at 4,650,884, `collect` paid
  both recipients at 4,650,887.
- **Composition off chain.** Multi-hop, split and cycle routes, a vault's LP
  minted mid-route, routed deposits and zaps are all one bundle of ordinary
  pool spends chained through settlement coins; the puzzle knows nothing of
  routes.
- **A second driver** on chia-wallet-sdk rebuilds swap, add and remove byte for
  byte from the protocol description (29/29), so the description and the code
  agree.

Suites at the time of writing: 6,738 checks across 13 suites, every internal
audit lane green, the written CLVM pass complete with no open lane.

---

## 4. Notes for the reviewers

Things found on the way that a CHIP author or the SDK team may want.

- **CHIP-0050, ordering.** The action layer prepends each action's condition
  list and the finalizer walks them last-first, so a finalizer that buckets
  conditions per reserve sees each action's tagged conditions **reversed**,
  concatenated in execution order. A driver that builds the reserve's delegated
  puzzle in any other order fails the message pairing. Worth a sentence in the
  CHIP for finalizer authors.
- **CHIP-0050, the `-42` marker.** An index outside the asset count must fail
  the spend; letting an unrecognized tag fall through to the singleton's own
  condition list would hand a leaf the singleton's voice. Forge's finalizer
  fails; the reference finalizer's behavior is the one to document.
- **CHIP-0050, slots and discovery.** A slot is singleton-owned and has no
  lookup mechanism of its own, which is right for a reader holding the launcher
  id and useless as an index. Forge pairs every slot-bearing design with
  CHIP-0020 hints.
- **CHIP-0025, the receiver from truth.** Deriving the receiver coin id from the
  reserve's parent id, curried full hash and pre-spend amount read from the
  singleton's truth is the pattern that removes the "any coin can announce"
  class. It generalizes to any singleton that owns a set of coins.
- **Mempool, a future-dated height.** A spend whose `ASSERT_HEIGHT_ABSOLUTE h`
  names a height ahead of the peak is accepted PENDING and settles when the
  chain reaches `h`; behind the window it is refused by the puzzle. Not a CHIP
  matter; an operator should know the oracle's weight for that block is bounded
  by the window.
- **chia-wallet-sdk 0.36, `Clvm.int(bigint)`.** Folds integers past 64 bits
  (the pool's `2^64` price scale came out as `1`). A config curried through it
  hashes to a different pool and is refused, never accepted with a wrong scale,
  but the failure reads as a mystery hash mismatch. Drivers on that SDK should
  encode CLVM integers themselves until it is fixed.

---

## 5. For Discord

Short answers, written to be pasted as they are. They name no protocol
versions, no vulnerability mechanics and no puzzle hashes; the record of those
is in the audit documents, not the thread.

**TL;DR.** The Forge is an N-asset weighted AMM on Chia built on CHIP-0050's
action layer with CHIP-0025 messages for every authorization. A trade is an
Offer you sign in Sage; a keyless router settles it against the pool; the pool
tells each reserve exactly what it may do; nothing else can move a reserve or
mint LP. Live on testnet11 with a registry, an oracle, a DAO fee and routes
across pools.

**What is the Forge?**
A weighted constant-function market maker where one pool holds any number of
assets in one singleton, with a reserve coin per asset and an LP CAT the pool
alone can mint or burn. Pools register in a sorted registry so no market exists
twice, every pool carries a price oracle, and a pool may carry a DAO fee that
can only ever go down.

**How does a swap settle if the Forge has no key?**
You sign an Offer in Sage: what you give, the least you take. The router reads
the pool, builds the pool's spend around your settlement coins, and pushes. Your
signature covers your coins and nothing else; the router's covers one fee coin.
The pool pays your requested amount out of its reserve; if it cannot, the whole
bundle fails and nothing moves.

**What is the action layer doing here?**
The pool's puzzle is a merkle tree of six actions, swap, add, remove, observe,
collect and lower-the-DAO-fee. A spend names the actions it runs, proves each
against the tree, and threads the pool's state from one to the next. The set is
fixed the day the pool is made, so what a pool can ever do is known up front.

**What is the finalizer?**
The one piece that runs after the actions. It recreates the pool with its new
state and sends each reserve a message saying precisely which delegated puzzle
to run: recreate yourself at this amount, pay this coin. A reserve moves for
that message and no other. Reserves are never bound by hand; the framework
binds them.

**Why messages instead of announcements?**
An announcement is a string any coin can shout. A CHIP-0025 message names its
sender and receiver in the condition itself, and here the receiver is derived
from the pool's own record of its reserve, not from anything in the solution.
That is the difference between "someone announced this" and "my pool told my
reserve."

**How is LP supply controlled?**
The LP is a CAT whose TAIL accepts a supply change only on a message from the
pool's own puzzle hash carrying the exact change and the pool's next state.
The very first mint has no pool spend to vouch for it, so it is bound to the
launcher's announcement instead, and the registry checks the same announcement
when it lists the pool.

**What is the DAO fee?**
An optional second slice of every swap's output paid to a recipient fixed when
the pool is created. The rate lives in the pool's state and has one direction:
down. The recipient lowers it by sending the pool a message from a coin they
control; at zero it stays at zero. A depositor prices the worst case once and
it never gets worse.

**Which CHIPs are involved?**
CHIP-0050 (action layer, slots) for the pool and the registry; CHIP-0025
(message conditions) for every authorization; CHIP-0020 (hints) so wallets and
indexers find every coin; CHIP-0014 (`ASSERT_BEFORE_*`) for the height window
the oracle uses. Standard singleton, CAT2 and settlement-payment puzzles
underneath. The reference action-layer puzzles are used as published and
pinned by hash.

**What changed since the last thread?**
The design moved onto the action layer. The pool's own binding checks went from
about twenty-one to about four, all of them messages the framework fixes the
ends of. An oracle, a registry, a DAO fee and off-chain routing across pools
were added on the same frame. A second, independent driver rebuilds the spends
byte for byte from the written description.

**What is the audit status?**
Internal: every adversarial lane in the findings log is green, a written pass
covers every leaf with each assert tied to a test, and the pool set has been
running on testnet11 across a twenty-pool matrix. External: we are asking CNI
to review the puzzle set and this composition; the documents above are the
package. Mainnet follows the review, not before.

---

## 6. Evidence

- Chain: registry `599ba997bc5ec16d185f2bfde20f5e3e8ac70cee42ef94fc0ebeacc9c8110f24`
  on testnet11, twenty pools from height 4,650,812; the lane table with every
  height is in `FORGE_V11_FOUNDATIONS.md`.
- Suites: `contracts/_test_v11_*.py` (integrity, curve equivalence, finalizer,
  actions, registry, offer lane, route lane, creation, manipulation, payout
  audit, DAO fee, multipool, discoverability) and the second driver under
  the wallet-sdk fixtures (private); the one-line invocation is in
  `FORGE_V11_FOUNDATIONS.md`.
- Pins: the V11 pin file (private; V14's is `contracts/v14/pins.json`), checked by `_test_v11_integrity.py`.
