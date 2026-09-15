# Forge puzzle V13 -- the second-audit revision

Testnet research only; unaudited. Protocol 14 on chain.

On 2026-09-14 a six-agent re-audit of V12 (`contracts/v12`, commit `26338d5`) was
posted to the CHIP-0062 pull request (Chia-Network/chips#217, comment 5666370916). It
cross-checked every V11 finding, every reviewer finding, and the two cursor-bot findings
against the shipping code with `chia_rs`-executed bundles. This document is the
disposition of that report: which findings are true against the code, which are true
against the text, which are neither, and what V13 changes.

**Why a major increment.** Three of the fixes change what a pool coin *is*: the oracle
state gains a field, the finalizer's curried arguments gain the configuration binding,
and the registry's admission rules and `register` leaf change. Every puzzle hash moves,
the V12 registry cannot admit a V13 pool, and one finding is a demonstrated drain. That
is V13, protocol 14, not a point release. V12's liquidity comes out first, as V11's did.

## The findings, one line each

| # | Finding (auditor severity) | True? | V13 |
|---|---|---|---|
| M-4 | Cross-leaf config mismatch drains an unregistered pool (HIGH) | **True.** Each leaf is curried with its own `config_hash`; the action layer proves each leaf is in the root and nothing binds the six to one config. A `remove` leaf curried with a foreign `lp_tail_hash` answers the pool's release message with a worthless CAT. Only unregistered pools: `register` recomputes the root from one config. | The finalizer recomputes the six-leaf root from a single curried `CONFIG_HASH` and asserts the action layer's `Merkle_Root` equals it, every spend. A pool at the V13 finalizer runs only leaves of one configuration. |
| S3 | Oracle discards the interval between the previous spend's claimed `h` and its inclusion block (MEDIUM-HIGH for consumers) | **True.** `last_height = h` but only `h - birth` is credited; the tail `(h_prev, birth]` at the previous price is lost forever. An honest pool spent every block accumulates zero. | Exact accounting: state keeps the last spot price; the prologue credits `last_spot x (birth - last_height)` for the tail, then the current spot over `h - birth`. Every block is credited at the price in force. No window claim is needed any more. |
| S2 | MIN_LOCKED_LP burn is unimplemented in the reference creation path (MEDIUM) | **True against `forge_v12_create.py`** (the website lane pays the whole genesis supply to the creator) and against the V12 doc's "the creator locks". The deploy script did burn. The author's reply to CNI described the deploy script, not the website lane. | `register` asserts the LP settlement's announcement of a payment of `MIN_LOCKED_LP` to the zero puzzle hash under the launcher id. A registered pool has burned its floor by construction. Both creation lanes pay through the settlement. |
| -- | `total_lp >= MIN_LOCKED_LP` at registration leaves zero redeemable units at the boundary (LOW) | **True.** | `total_lp > MIN_LOCKED_LP` at registration; the prologue also asserts `total_lp >= MIN_LOCKED_LP` so an unregistered pool below the floor is unspendable rather than a trap. |
| M-3/L-3 | `pool_key` excludes `protocol_puzzle_hash`; the website accepts a client-supplied one; two pools differing only in fee recipient collide, and a squatter can take a market key with their own recipient (MEDIUM) | **True.** `api/forge-v12-create.js` defaults the recipient to the treasury but forwards whatever the client sends. | The registry pins `protocol_puzzle_hash`, `price_scale` and `oracle_window` to its curried constants. They are protocol parameters, not market parameters, so they neither belong in the key nor stay free. |
| -- | Unbounded `price_scale` and `oracle_window` let a registrar brick a pool on its first spend or remove the cadence bound | **True** for unregistered pools; registered ones are now pinned. | `valid_config` bounds both (`price_scale <= 2^64`, `oracle_window <= 4608`). |
| M-1 | `collect` emits duplicate `CreateCoin`s when protocol and DAO recipients coincide with equal amounts (MEDIUM, narrow) | **True.** | One payout when the recipients are equal. |
| INFO | `register` proves the genesis mint was authorized, not that it happened | **True.** | Closed by the burn assertion: the only source of that LP asset is the TAIL's genesis branch, so burning 1,000 units proves the mint ran. |
| L-7 | Duplicate outputs beyond `collect` (two `observe`s in one spend; `swap`+`remove` with coincident payouts) reject the bundle with no puzzle-level hint | True, not exploitable: the composer's own bundle fails. | Documented as a composition rule. The router never composes them. |
| L-6 | Observation slots are never spent by any leaf | True by design. | Unchanged. The slot is a record for a consumer that spends it with its own message; the announcement is the live path. Stated here rather than left to be discovered. |
| L-2 | No `strlen` checks on solution-sourced `Bytes32` (hardening) | rue 0.8.4 has no `strlen`. Every such value feeds a sha256 preimage whose result is compared against a consensus-committed id, so a wrong width yields an id that exists nowhere. | Unchanged; rationale recorded. |
| I-1 | `add`'s `deposit >= 0` is load-bearing and unpinned | **Does not reproduce.** Rebuilt with that line deleted, the leaf still refuses a negative deposit, and a search over deposit vectors with a negative slot found no mint the bracket accepts: `exact_invariant_lp_mint` is two-sided, and a negative slot drives `min_deposit_ratio` negative, which puts the target below `total_lp^K * old_product`, so no `lp_delta > 0` satisfies it. The assert is redundant here, not untested. | Unchanged, and kept for the reason the swap leaf's twins are kept: if the bracket were ever widened these become the defence. The mutation run records SURVIVED with this note. |
| F7 | Same-block successor "accumulates nothing" | Text only; already corrected in CHIP revision 4. | Superseded: with exact accounting the sentence is gone. |

Findings the audit confirms closed and V13 carries unchanged: C-1 genesis binds to one
eve; L-1 reserve parents in state; the fee unit; the non-empty action list.

## 1. One configuration per pool coin (M-4)

**The hole.** `upstream/action.rue` proves each selected leaf is a member of
`MERKLE_ROOT` and runs it. `PoolConfig` is curried into each leaf separately. Six leaves
curried with six different configs make a valid root, and each leaf validates only its
own config. A pool built that way is not a Forge pool by any registry's reckoning, but
nothing on the coin says so, and a depositor who trusts a puzzle hash is exposed.

**The fix.** The multi-reserve finalizer already carries, in its first curry, everything
that identifies the pool's reserves. V13 adds the leaf binding to the same curry:

```
fn main(
    ACTION_LAYER_MOD_HASH, RESERVE_FULL_PUZZLE_HASHES, RESERVE_INNER_PUZZLE_HASHES,
    RESERVE_AMOUNT_PROGRAM, HINT,
    CONFIG_HASH: Bytes32,              // V13
    LEAVES: LeafModHashes,             // V13: six leaf mod hashes, driver order
    POOL_SLOT_1ST_CURRY_HASH: Bytes32, // V13: the observe leaf's second curried value
    FINALIZER_SELF_HASH, Truth, last_action_output,
)
    assert Truth.Merkle_Root == six_leaf_root(
        curry_tree_hash(LEAVES.swap,    [CONFIG_HASH]),
        curry_tree_hash(LEAVES.add,     [CONFIG_HASH]),
        curry_tree_hash(LEAVES.remove,  [CONFIG_HASH]),
        curry_tree_hash(LEAVES.observe, [CONFIG_HASH, tree_hash_atom(POOL_SLOT_1ST_CURRY_HASH)]),
        curry_tree_hash(LEAVES.collect, [CONFIG_HASH]),
        curry_tree_hash(LEAVES.dao_fee, [CONFIG_HASH]),
    );
```

`six_leaf_root` moves from the registry into `forge_action_common` so both compute it
from one definition. The registry's `pool_full_puzzle_hash` curries the same three
values into the finalizer it rebuilds, so registration still matches exactly what the
launcher created.

**What it buys, precisely.** The binding does not stop someone writing an entirely
different puzzle; nothing can. What it does is make the leaf set *legible*. The
finalizer's curry now carries the configuration hash and the six leaf module hashes in the
clear, so a verifier reads them straight off the coin and compares them against the
published Forge leaves. If they match, every leaf behind that root is a Forge leaf of one
configuration, because `config_root` applies the same config hash to all six. The attack
shape -- five honest leaves and one poisoned, behind a root that looks ordinary -- has no
`(LEAVES, CONFIG_HASH)` that produces it, so the finalizer refuses the spend before any
reserve is messaged. An attacker who also rewrites the finalizer is no longer
impersonating a Forge pool: their leaf hashes do not match, and the mismatch is visible
before anyone deposits. The registry performs exactly this comparison against its own
pinned leaf hashes, which is why a registered pool needs no further checking. Cost: six
curry hashes and five pair hashes per spend.

**Test.** `_test_v12_v13_review_findings.py` builds both drains against the V12 build
(expected ACCEPTED, as the auditors found) and against V13 (expected REFUSED). The
forgery suite's case E gains the cross-leaf variant it was missing.

## 2. An exact oracle (S3)

**The hole.** Spend `k` is mined at block `M_k`, creating a coin with `birth = M_k`. It
claimed `h_k <= M_k`, and wrote `last_height = h_k`. Spend `k+1` credits the pre-spend
price over `h_{k+1} - M_k`. The interval `(h_k, M_k]`, during which the *previous*
state was in force, is credited by nobody. Claiming `h = birth` every time makes the
accumulator stand still while real blocks pass; an honest pool spent in every
transaction block does the same by accident.

**The fix.** The oracle remembers the spot price it last credited:

```
struct Oracle { last_height: Int, cums: List<Int>, last_spot: List<Int> }   // V13
```

and the prologue credits two intervals at two prices:

```
assert birth > state.oracle.last_height;   // consensus: h_k < M_k always
assert h >= birth;
tail    = birth - last_height              // the previous state, still in force after its spend was signed
current = h - birth                        // this state, since this coin was born
spot    = spots(reserves, weights, price_scale)
cums'   = cums + last_spot * tail + spot * current
last_spot' = spot; last_height' = h
```

Every block from the previous spend's `h` to this spend's `h` is credited exactly once,
at the price that was in force during it. Understating `h` defers credit to the next
spend; it no longer deletes it. Fabrication stays impossible for the reason V12 gave:
`birth` is a consensus fact and `h` cannot exceed the inclusion block.

`birth > last_height` holds for every real chain of spends because `ASSERT_HEIGHT_ABSOLUTE(h)`
is checked against the previous transaction block, which precedes the block that
includes the spend. Genesis has `last_height = 0` and `last_spot` all zero, so the first
tail credits nothing.

`oracle_window` is now bounded (4,608 blocks, about a day) and pinned by the registry.
With exact accounting it only limits how stale a signed spend may be; it has no bearing
on oracle accuracy, which is why the CHIP's "understate by at most the window" sentence
goes.

**Test.** The oracle suite gains the auditor's two scenarios: eight honest spends in
consecutive blocks now credit every block; ten `observe`s claiming `h = birth` credit
the same total as one observe at the end. `_test_v13_consensus_timelocks.py` keeps the
lying-birth half.

## 3. The floor is burned, and the registry knows it (S2)

**The hole.** The puzzle sees a total supply and a burn; it cannot see who holds the
floor. V12 relied on the creation driver to burn it. The deploy script did; the website
lane (`forge_v12_create.py`) paid the whole supply to the creator. The CHIP text and the
reply to CNI described the deploy script.

**The fix.** The genesis mint already pays through `CAT(lp_tail, OFFER_MOD)` in the
website lane; the deploy script now does the same. The settlement carries two notarized
groups under the launcher id: `[[0x00..00, MIN_LOCKED_LP]]` and
`[[recipient, total_lp - MIN_LOCKED_LP, [recipient]]]`. `register` asserts the first:

```
AssertPuzzleAnnouncement {
    id: sha256(cat_puzzle_hash(config.lp_tail_hash, OFFER_MOD_HASH)
               + tree_hash((launcher_id, [[zero_bytes32(), MIN_LOCKED_LP]]))),
}
```

Only the TAIL's genesis branch can create that asset, so the assertion also proves the
mint happened (the INFO finding). The units sit at a puzzle hash with no preimage: they
remain a claim on the reserves, and that claim belongs to nobody. Everyone else's
position redeems in full.

`valid_pool` requires `total_lp > MIN_LOCKED_LP` (the boundary finding), and the prologue
requires `state.total_lp >= MIN_LOCKED_LP` so a pool minted under the floor outside the
registry cannot be spent at all.

## 4. Registry admission (M-3, L-3)

`pool_key` stays `[asset_ids, weights, fee_bps, protocol_fee_bps, dao_puzzle_hash]`: the
market. The three values the audit found free are protocol parameters, and V13 pins
them to the registry's curried constants instead of adding them to the key:

```
struct RegistryConstants { ..., protocol_puzzle_hash: Bytes32, price_scale: Int, oracle_window: Int }
valid_pool: config.protocol_puzzle_hash == c.protocol_puzzle_hash
         && config.price_scale == c.price_scale
         && config.oracle_window == c.oracle_window
```

A registered pool's protocol fee therefore goes where the registry says, not where the
registrar says, and no registered pool can be bricked by an absurd scale.

**Deliberately unchanged.** No deregistration or expiry. A registered pool cannot die
(the floor keeps it alive) and a dust pool is a live market anyone can deepen, so "the
key is taken" means "the market exists", which is the permissionless norm. A minimum
reserve would not change that and would only price small markets out.

## 5. `collect` with one recipient (M-1)

```
if CONFIG.protocol_puzzle_hash == CONFIG.dao_puzzle_hash { payout(index, ph, fee + dao) }
else { payout(index, protocol_ph, fee); payout(index, dao_ph, dao) }
```

## Everything else that moves, mechanically

* `PROTOCOL_VERSION` 13 -> 14 in the TAIL, the registry, the driver, the API and the UI.
* `Oracle` gains `last_spot`; `forge_reserve_amount.rue` types it as `Any` and is unaffected.
* Snapshot, record, resync and index carry `last_spot`; the TypeScript reader ignores it.
* `make_registry` takes `protocol_ph`, `price_scale`, `oracle_window`.
* `deploy-v13-testnet.py` mints the genesis LP to the settlement and splits there.
* The publish slice ships `contracts/v13` and this document; V12 joins V11 as history.

## Order of work

1. `contracts/v13` from `contracts/v12`; the five puzzle changes above; build; pins.
2. Drivers, suites and scripts renamed to V13 with the review-findings suite exploiting
   V12 and showing V13 refusing; every offline suite green; mutation run.
3. Drain V12 on testnet11 (`scripts/v12-drain.py`, the V11 drain generalized).
4. Mint the V13 registry; re-create the matrix with the lock address as DAO recipient
   where V12 had it; deepen T6.
5. Lifecycle matrix (adds, swaps, collects, removes, observe, multihop) and the offer
   lane test through the API.
6. CHIP revision 5, local only.
