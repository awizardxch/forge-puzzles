# Forge V13 — the written CLVM pass, leaf by leaf

> **Erratum (2026-09-15).** Two corrections. (1) Section 3's "Finding I-1 does not
> reproduce" is wrong: the assert is load-bearing — see the erratum in
> `FORGE_PUZZLE_V13.md`. (2) Section 1's closure of O-1 rests on a false step. It says a
> future-dated spend "has `birth >= h`, so `h == birth`"; but `birth` is the coin's
> *creation* height, pinned by `ASSERT_MY_BIRTH_HEIGHT`, and naming a future `h` delays
> inclusion without moving it. The conclusion survives for a different reason: the exact
> two-interval accounting credits `[birth, h]` in this spend and the tail `(h, inclusion]`
> in the next, at the spot this one recorded. Measured across a 20-block future-dating gap,
> every block is credited exactly once (`_sim_v14_review_corrections.py`).

The reading of every puzzle a V13 pool runs, in the order a spend meets them.
Each section states what the puzzle takes, what it asserts, what it emits, which
suite pins each refusal, and what remains open. Written against the V13 build
(protocol 14, six leaves), 2026-09-15.

V13 is the revision a second independent review forced. Where a check is new,
this pass says which finding put it there; where a check is inherited unchanged,
it says so. The two reviews are recorded in `FORGE_SECURITY_AUDIT.md`.

Method: for each leaf, read the rue source top to bottom, list every `assert` and
every condition emitted, ask what an adversary controls in the solution and what
they cannot, and name the suite check that would go red if the assert were
removed. A leaf with an assert no suite pins is a finding of this pass — and
`scripts/mutate-v13.py` answers that question mechanically, by deleting each
assert in turn, rebuilding with rue, and re-running the suites.

---

## 0. The frame: action layer, finalizer, reserves

**Upstream, pinned by hash.** `action.rue` (the CHIP-0050 action layer),
`finalizer.rue`, `slot.rue`, `p2_delegated_by_singleton.rue` are vendored from
Yakuhito/slot-machine at `2d37ba1`, recompiled by rue 0.8.4 and compared with the
pins in `pins.json` on every build (`_test_v13_integrity.py`, 102 checks). Their
logic is not re-audited here beyond how Forge uses it.

**What the frame guarantees the leaves.** A spend names a list of leaves, a merkle
proof for each selector not yet proven in the spend, and a solution per leaf. The
action layer verifies each proof against the curried root, runs the leaves in
order, threads `(ephemeral . state)` from one to the next, and hands the finalizer
the list of condition lists and the final state.

**What the finalizer adds, and what V13 changed.** `forge_multi_reserve_finalizer.rue`
re-creates the singleton, strips `(-42 index . condition)` markers into per-reserve
buckets, fails the spend on an index outside `0..N-1`, and sends one mode-23
message per reserve, every spend, to a receiver derived from the reserve's parent
id, its curried full puzzle hash and its *pre-spend* amount read from the truth.
Two changes since V11:

| Change | The finding | What it closes |
|---|---|---|
| Reserve parents are read from **state**, not the solution, and the successor records the coins just messaged | CNI review P1 | a decoy coin at the reserve's puzzle hash and amount could be named in the solution, orphaning the real reserve |
| `assert Truth.Merkle_Root == config_root(LEAVES, CONFIG_HASH, POOL_SLOT_1ST_CURRY_HASH)` | second review, M-4 (HIGH) | six leaves curried with six different configurations form a valid root, and each leaf validates only its own; a `remove` curried with a foreign LP TAIL released 99.80% of both reserves against a worthless self-minted CAT |

The binding is worth stating precisely, because it is easy to overclaim. It does
not prevent an unrelated puzzle existing; nothing can. It makes the leaf set
**legible**: `config_root` applies one configuration hash to all six leaves, so
there is no `(LEAVES, CONFIG_HASH)` that yields a mixed root, and the six leaf
module hashes sit in the finalizer's curry in the clear for anyone to compare
against the published set. The registry performs exactly that comparison with its
own pinned hashes, which is why a listed pool needs no further checking, and an
unlisted one can be checked by its depositor before depositing.

**What that means for an adversary.** They cannot: run a leaf that is not in the
tree (proof fails), change the root or the finalizer (curried), assemble a pool
whose leaves disagree (root assert), aim a reserve's message at a coin that is not
the current reserve (receiver derived from truth and state), or make a reserve pay
out more than the leaves tagged.

Pinned: `_test_v13_finalizer.py` (42 checks — tag index equal to N, negative
index, a decoy reserve, a reserve run with a puzzle the singleton did not send, a
reserve naming a different sender, a curried state that misstates a pre-spend
amount, pool B's singleton curried with pool A's reserves) and
`_test_v12_v13_review_findings.py` (the cross-leaf drain, ACCEPTED against V12 and
REFUSED against V13).

**Open.** None on the frame.

## 1. The prologue (`forge_action_common.rue::prologue`)

Every leaf's first call. Two branches on `truth.ephemeral_state`.

**First action of a spend (`ephemeral_state is nil`).** Asserts, in order:

| Assert | What it closes | Pinned by |
|---|---|---|
| `valid_config(config)` | a pool whose curried config is out of bounds cannot move; V13 also bounds `price_scale <= 2^64` and `oracle_window <= 4608` | `_test_v13_registry.py` refusals; mutation: SURVIVED (redundant — the config is curried, so a different config is a different pool) |
| `count(reserves) == n`, `count(fees_owed) == n`, `count(dao_owed) == n` | state lists sized to the asset count, so `nth`/`add_at` never run past the end silently | the registry's `eve_state_hash` fixes the shape at genesis |
| `total_lp >= MIN_LOCKED_LP` | **V13**: a pool minted below the floor outside the registry cannot be spent at all, so it cannot trap a later depositor's LP | `_test_v13_actions.py` "a pool minted below MIN_LOCKED_LP cannot be spent at all"; mutation: **killed** |
| `h > oracle.last_height` | the height moves forward; a stale `h` is refused before the chain sees it | `_test_v13_actions.py` "a height not above the oracle's last height is refused" |
| `birth > oracle.last_height` | **V13**: the successor is born after the block its predecessor's claimed height was checked against, so both credited intervals are non-negative by construction | `_test_v13_oracle.py` "a birth at the last claimed height is refused"; mutation: **killed** |
| `h >= birth` | the interval since this coin was born cannot be negative; with `ASSERT_MY_BIRTH_HEIGHT` it also makes a same-block successor unspendable | `_test_v13_actions.py`, `_test_v13_consensus_timelocks.py`; mutation: **killed** |
| `dao_fee_bps` in `[0, MAX_DAO_FEE_BPS]`, and zero when `dao_puzzle_hash` is zero | a fee with no recipient would be burned; the cap bounds the composed fee | `_test_v13_dao_fee.py`; mutation: **killed** for the cap and the recipient rule |

Emits `ASSERT_MY_BIRTH_HEIGHT birth`, `ASSERT_HEIGHT_ABSOLUTE h`,
`ASSERT_BEFORE_HEIGHT_ABSOLUTE h + oracle_window`, `ASSERT_MY_AMOUNT 1`.

**The oracle, which is the substance of V13.** State carries `last_spot`, the spot
prices the previous spend credited. The prologue credits two intervals:

```
spot   = spots(reserves, weights, price_scale)        // on the PRE-spend reserves
cums'  = cums + last_spot * (birth - last_height) + spot * (h - birth)
last_spot' = spot ;  last_height' = h
```

V12 credited only `h - birth` and still wrote `last_height = h`, so the blocks
between a spend's claimed height and the block that included it were credited by
nobody and could never be credited afterwards. That is not an illegal negative
interval but a legal zero one, and it is free: ten permissionless `observe` spends
claiming `h = birth` drove a reported TWAP to zero across 310 real blocks while the
spot never moved, and a pool spent honestly in every transaction block did the same
by accident. Measured on chain after the redeployment: a pool created at 4,686,415,
deposited at a claimed 4,686,597 that was included at 4,686,602, then swapped at
4,686,863, credits 182 blocks at the genesis spot, the 5-block tail at that same
spot, and 261 blocks at the post-deposit spot — matching the formula to the mojo,
where V12 credited 443 of those 448 blocks. Pinned: `_test_v13_oracle.py` runs the
reviewers' two scenarios directly, and `assert last_spot is nil` inside `credit`
(the list-length check) is **killed** by it.

**Later actions (`ephemeral_state == h`).** Only `assert ephemeral == h`: every
action in one spend names one height, and the config, state shape and oracle were
checked once. Mutation: **killed**.

**Observation O-1, now closed.** V11's pass recorded that a bundle could name a
*future* `h` and wait in the mempool for it, under-weighting one observation by up
to the window. Under V13 that shape collapses: `ASSERT_HEIGHT_ABSOLUTE h` prevents
inclusion before `h`, so a future-dated spend has `birth >= h`, while the prologue
requires `h >= birth`. The only future-dated spend that can exist has `h == birth`,
which credits nothing for the current interval — and the tail it leaves is credited
by the next spend at the spot it recorded. Deferred, not destroyed.

**Observation O-2 (carried).** `valid_config` accepts a zero fee with a zero
recipient but refuses a nonzero fee with one. The same argument is applied to the
DAO fee in state. Consistent; no action.

## 2. `swap`

Solution: `h, birth, asset_in, asset_out, gross_input, claimed_output,
settlement_coin_id`.

Asserts distinct indices within `0..n-1`, `gross_input > 0`, `claimed_output > 0`,
and `exact_swap_output(...)` — the curve *bracket*: the claimed output holds the
invariant and one more would break it, so the output is pinned exactly with
integer arithmetic (moved from V10 byte-identical,
`_test_v13_curve_equivalence.py`, 6,043 checks). Then two fee slices, each floored
on its own rather than one floor on a summed rate:
`protocol_fee = protocol_fee_owed(claimed, protocol_fee_bps)` and
`dao_fee = protocol_fee_owed(claimed, state.dao_fee_bps)`.

State: `reserves[in] += gross`, `reserves[out] -= claimed`,
`fees_owed[out] += protocol_fee`, `dao_owed[out] += dao_fee`. Both fees stay
physically inside the out reserve, owed, until `collect`.

Emits the settlement assertion and a tagged `CREATE_COIN` for reserve `out` paying
`claimed - protocol_fee - dao_fee` to the offer settlement puzzle hash.

**What the adversary cannot do:** claim more than the curve, or less to pocket the
difference (the bracket is exact on both sides), swap an asset for itself, use an
out-of-range index, or take the payout without funding the input.

**The four index and sign asserts are documented as defence in depth**, and the
mutation run agrees: deleting each leaves the suite green, because the curve is
unsatisfiable for a non-positive amount and an out-of-range index fails the reserve
lookup first. They are kept because TibetSwap V2 was drained in August 2026 by
exactly the gap they close, and if the bracket were ever widened they become the
defence. Only `asset_in != asset_out` is independently **killed**.

**A builder-side note, not a puzzle finding.** The leaf takes two slices; a builder
that subtracts only the protocol fee names a payout coin the pool never creates.
Such a bundle passes local validation, which has no coin store, and the node
refuses it as `UNKNOWN_UNSPENT`. That happened to `v13_ops.swap_bundle` on exactly
the nine DAO-bearing pools of thirty-two and is fixed in both the single-swap and
multi-hop builders.

## 3. `add`

Solution: `h, birth, deposits, lp_delta, lp_parent_id, settlement_coin_ids`.

Asserts `lp_delta > 0`, `deposit >= 0` per asset, `any_positive(deposits)`, and
`exact_invariant_lp_mint(...)`, the two-sided mint bracket with the imbalance fee.
`zip_add` fails unless `deposits` has exactly one entry per reserve. Emits one
settlement assertion per positive deposit and the LP handshake to the mint eve.

**Finding I-1 of the second review does not reproduce.** The report held that
`assert deposit >= 0` was load-bearing and that deleting it accepted a disguised
withdrawal-as-deposit that also minted LP. Rebuilt with the line deleted, the leaf
still refuses a negative deposit, and a search over deposit vectors with a negative
slot found no `lp_delta` the bracket accepts at all: `exact_invariant_lp_mint` is
two-sided, and a negative slot drives `min_deposit_ratio` negative, which puts the
target below `total_lp^K * old_product`, so no positive mint satisfies the lower
bound and `assert lp_delta > 0` covers the rest. Checked against a compiled mutant
rather than argued from the source. The mutation run reports SURVIVED for that
line, and here SURVIVED is genuine redundancy rather than a missing test. The line
stays, for the reason the swap leaf's twins stay.

## 4. `remove`

Solution: `h, birth, burn, lp_parent_id, payouts`.

Asserts `burn > 0` (**killed**) and `burn <= total_lp - MIN_LOCKED_LP`
(**killed**), then `exact_withdrawal(...)` — payouts exactly pro-rata, floored per
asset, with the vault fee on a single-asset pool. `zip_sub` fails unless `payouts`
has one entry per reserve. Emits a tagged payout per positive amount and the LP
handshake to the melt coin.

The floor is what makes every holder's position redeemable: the last depositor can
take out their whole position, because the locked thousandth was never theirs. It
was burned at genesis and the registry verified the burn (section 9). `payout == 0`
on a zero entry is SURVIVED and redundant — a zero `CreateCoin` would simply not be
emitted.

## 5. `observe`

Solution: `h, birth`. State is unchanged beyond the prologue. Creates the
observation slot valued `(h . cums)` and announces
`["forge-observe-v1", h, cums]`.

The announcement rather than a message is deliberate: a message must name its
receiver and fails the spend if nothing receives it, so an observe with no consumer
present could never land.

**L-6, recorded rather than fixed.** No leaf ever spends an observation slot, so
the slots accumulate unspent. That is by design in this revision: the slot is a
record for a future consumer to spend with its own message, and the live path is
the announcement, which a same-bundle consumer asserts. Naming it here so it is a
decision rather than something for a reader to discover.

## 6. `collect`

Solution: `h, birth, indices` (non-empty, **killed**). Per index, asserts
`fee > 0 || dao > 0` (**killed**), zeroes both, and pays each to its recipient.
Naming one index twice fails on the second visit, since the first zeroed it.

**V13 pays one coin when `protocol_puzzle_hash == dao_puzzle_hash`.** Two identical
`CreateCoin`s from one reserve are one coin id twice, which consensus rejects as
`DUPLICATE_OUTPUT`; because `collect` is the only exit for accrued fees, that
bricked the exit for exactly the pools whose recipients coincided, permanently
unless the DAO could lower its own rate and a further swap shifted the amounts
apart. Second review, M-1. Pinned in `_test_v13_actions.py` (the merged coin equals
protocol + DAO) and in the before-and-after suite, which reproduces the V12
`DUPLICATE_OUTPUT`.

## 7. `dao_fee`

Solution: `h, birth, new_bps`. Asserts `new_bps >= 0`, `new_bps < state.dao_fee_bps`
and a nonzero recipient, then requires a mode-23 `RECEIVE_MESSAGE` from a coin whose
puzzle hash *is* the DAO recipient, carrying `dao_fee_message(new_bps)`. Consensus
commits the receiver to this pool coin, so the message cannot be replayed against
another pool or another spend. All three asserts **killed** by `_test_v13_dao_fee.py`.

Irreversibility is topology rather than a flag: a pool at zero contains no
transition to a nonzero rate.

**Open.** The DAO coin is any coin whose puzzle hash equals the recipient. The
design intends a multisig or vault puzzle there, and nothing in the leaf changes
when it is. On testnet11 the DAO-bearing pools pay the multisig lock, and the lock
has been paid in all four test CATs.

## 8. The LP TAIL (`forge_lp_cat_tail.rue`)

Curried `(launcher_id, protocol_version)`; the tree hash is the LP asset id, unique
per pool and per revision. Asserts `protocol_version == 14` — protocol-13 LP can
never be authorized by a protocol-14 pool, and the asset ids differ anyway —
`expected_delta != 0`, `new_total_lp >= 0`, the acting coin's amount positive,
`parent_is_cat || expected_delta > 0` (a melt needs a CAT parent), and
`effective_delta == expected_delta`, where a mint from nothing counts the coin's own
amount.

Two branches. **Message:** requires `RECEIVE_MESSAGE` mode 23 from the pool's full
puzzle hash, rebuilt from the launcher and the inner hash the solution names, with
the same message the leaf sent. **Genesis:** requires the launcher's own coin
announcement of `[pool_puzzle_hash, 1, [expected_delta, cat_my_coin_id]]`, no CAT
parent, and `new_total_lp == expected_delta`.

The eve's coin id comes from the **CAT truths**, not the solution, so exactly one
coin can assert the launcher's single announcement. V11 fixed that list to
`[expected_delta]` alone and any funded coin could assert it: two eves each minted
the whole supply, which is CNI review finding P0. Pinned by `_test_v13_genesis.py`
and by the V11-to-V12 before-and-after.

**Cannot:** mint or melt without the pool's message (147), mint a different amount
than authorized, melt a fabricated coin, or claim a genesis a second time.

**Open.** None. The inner puzzles (mint inner with the CHIP-0020 hint, melt inner
byte-identical to V10) are pinned by hash in the common module and checked by the
build.

## 9. The registry (`forge_registry_init`, `forge_registry_register`)

A second CHIP-0050 singleton with the upstream default finalizer and no reserve.
`init` sets `initialized = 1` once. `register` takes a launcher parent id, the
config, the genesis reserves, `total_lp`, the opening `dao_fee_bps`, the genesis
reserve parents, the eve coin id and the two neighbour slots.

It asserts `valid_pool`, rebuilds the pool's **exact** full puzzle hash from
constants and config, asserts the launcher's coin announcement of that hash with
`[total_lp, eve_coin_id]`, asserts the creation fee's settlement announcement,
asserts the **burn** announcement, spends the two neighbours by message and
re-creates them around the new key, and announces the registration.

`valid_pool` in V13 requires, beyond V10's config validation and the LP asset id
tied to this launcher:

| Requirement | Why |
|---|---|
| `total_lp > MIN_LOCKED_LP` | strictly above: a genesis of exactly the floor leaves the creator nothing redeemable, which is the position the floor exists to prevent |
| `config.protocol_puzzle_hash == c.protocol_puzzle_hash` | were the fee recipient free, a registrant could hold a market's key with a pool paying the protocol fee to themselves, and two pools differing only in recipient would collide on one key |
| `config.price_scale == c.price_scale` | an absurd scale bricks a pool on its first spend while occupying the key forever |
| `config.oracle_window == c.oracle_window` | an unbounded window removes the inclusion bound |

`pool_full_puzzle_hash` curries the configuration hash, the six leaf module hashes
and the pool's slot hash into the finalizer it rebuilds, so registration and the
finalizer's own root assertion agree by construction.

**The burn assertion is the answer to the second review's MEDIUM finding.** The
pool cannot check that the floor was put beyond recovery — it sees a total supply
and a burn, never who holds which unit — so V12 relied on the creation driver. The
deploy script burned it; `forge_v12_create.py`, the lane the website actually uses,
paid the whole genesis supply to the creator, and the reply given to CNI described
the deploy script as though it were the implementation. V13 does not rely on either
driver: the genesis supply passes through `CAT(lp_tail, OFFER_MOD)`, whose payments
are announced, and `register` asserts the announcement of a payment of
`MIN_LOCKED_LP` to the zero puzzle hash under the launcher id. Since only the
TAIL's genesis branch can create that asset, the same assertion also proves the
mint happened rather than merely that it was authorized — which closes the review's
INFO finding that a bundle omitting the genesis eve still registered.

**The key** is `tree_hash([asset_ids, weights, fee_bps, protocol_fee_bps,
dao_puzzle_hash])`: the market. The three pinned values are protocol parameters, so
they belong in the constants rather than the key. `lp_tail_hash` needs neither: it
is already required to be the TAIL curried with this launcher, so it is determined
rather than chosen.

**Cannot:** register before init, init twice, register a duplicate key on either
side, bracket with the wrong neighbours, underpay or skip the fee, register without
the launcher spend, register a launcher that minted a different config, name an LP
that is not this launcher's TAIL, register an unfunded genesis, mint one LP more
than the launcher named, register a genesis that burns nothing or burns short of
the floor, register at exactly the floor, register a pool whose protocol recipient
or oracle parameters are not the registry's, or spend a slot from a coin that is not
the registry. All pinned in `_test_v13_registry.py` (39 checks).

**Open, and argued rather than deferred.** There is no deregistration or expiry
path. A registered pool cannot die, because the floor keeps it alive, and a thinly
funded pool is a live market anyone may deepen, so "the key is taken" means "this
market exists". That is pinned rather than asserted: a pool registered at the
smallest thing the registry admits, reserves `[1, 1]` and 1,001 LP, accepts a real
deposit of 10 XCH and 5,000 CAT, mints 7,073,534,532 LP, and the squatter's single
remaining unit is diluted to nothing. A minimum reserve would price small markets
out without changing what one creation fee buys.

## 10. Cross-cutting

**Fee composition.** Swap fee (curve), protocol fee and DAO fee (two slices of the
release, each floored on its own), router fee (outside every puzzle, charged on the
entry). `_test_v13_payout_audit.py` pins that the trader's floor and the exact
protocol slice hold at any router rate and that trader + router + protocol equals
the release; the DAO slice joins the same identity in `_test_v13_dao_fee.py`.

**Intra-bundle manipulation.** Chained actions in one spend leave the actor poorer
at pre-sequence prices; value per LP never falls across any action
(`_test_v13_manipulation.py`). Across pools (`_test_v13_multipool.py`, 26 checks)
every composer lane leaves every pool it touches with its value per LP intact. An
actor can gain across pools when they disagree on a price; that is arbitrage,
bounded by the disagreement, and the pools are brought toward one price rather than
drained. The V13 testnet matrix is sized from one price table so the matrix itself
is arbitrage-free at genesis, which means a profitable route says something about
the router rather than about the numbers it was handed.

**L-7, recorded.** Duplicate-output collisions beyond `collect` — two `observe`s in
one spend, or a `swap` and a `remove` with coincident net payouts — reject the whole
bundle with no puzzle-level hint. They fail the composer's own bundle rather than
anyone else's, so this is a composition rule rather than a vulnerability; the
router's lanes were checked and none composes a pair that can collide.

**L-2, recorded.** rue 0.8.4 emits no runtime `strlen`, so a solution-sourced
`Bytes32` of the wrong width is not rejected on width. Every such value in this
design feeds a sha256 preimage whose result is compared against a
consensus-committed coin id or announcement, so a wrong width produces an
identifier that exists nowhere rather than a collision. Left as hardening rather
than paying cost on every spend for a property the comparison already gives. Worth
revisiting if a future revision ever compares a derived hash against something that
is not a committed id.

**Mutation results (2026-09-15).** 34 assertions across seven files: 13 killed, 21
survived, 0 unbuildable. Every assertion V13 introduced is killed — the floor in the
prologue, `birth > last_height`, `h >= birth`, and the `last_spot` length check. The
survivors are the documented defence-in-depth twins in `swap` and `add`, whose
brackets already refuse what they cover, and the structural list-length asserts. No
survivor is an untested defence.

**Findings of this pass.** No assert without a pin or a recorded reason. One
observation closed since V11 (O-1, the future-dated height), one carried (O-2), and
one finding of the second review that does not reproduce (I-1, section 3).

## Where the rest lives

- [FORGE_PUZZLE_V13.md](FORGE_PUZZLE_V13.md) — the protocol reference this pass reads against.
- [FORGE_V13_ARCHITECTURE.md](FORGE_V13_ARCHITECTURE.md) — the coin topology and the off-chain shape.
- [FORGE_SECURITY_AUDIT.md](FORGE_SECURITY_AUDIT.md) — the findings log, including both external reviews.
- [FORGE_DAO_FEE_V13.md](FORGE_DAO_FEE_V13.md) — the DAO-fee design as built, with its threat model checked off.
