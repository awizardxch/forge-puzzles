# Forge V11.1 — the written CLVM pass, leaf by leaf

The internal audit's reading of every puzzle a V11.1 pool runs, in the order a
spend meets them. Each section states what the puzzle takes, what it asserts,
what it emits, which suite pins each refusal, and what remains open. Written
against the V11.1 build (protocol 12, six leaves), 2026-09-05. Where a check is
inherited from V10 unchanged, it says so and points at the V10 pass in
`FORGE_SECURITY_AUDIT.md`.

Method: for each leaf, read the rue source top to bottom, list every `assert`
and every condition emitted, ask what an adversary controls in the solution and
what they cannot, and name the suite check that would go red if the assert
were removed. A leaf with an assert no suite pins is a finding of this pass.

---

## 0. The frame: action layer, finalizer, reserves

**Upstream, pinned by hash.** `action.rue` (the CHIP-0050 action layer),
`finalizer.rue`, `slot.rue`, `p2_delegated_by_singleton.rue` are vendored from
Yakuhito/slot-machine at `2d37ba1`, recompiled by rue 0.8.4 and compared to the
pins in `pins.json` on every build (`_test_v11_integrity.py`, 101 checks).
Their logic is not re-audited here beyond how Forge uses it.

**What the frame guarantees the leaves.** A spend names a list of leaves, a
merkle proof for each selector not yet proven in the spend, and a solution per
leaf. The action layer verifies each proof against the curried root, runs the
leaves in order, threads `(ephemeral . state)` from one to the next, and hands
the finalizer the list of condition lists and the final state. The finalizer
(`forge_multi_reserve_finalizer.rue`) re-creates the singleton at the action
layer curried with its own hash, the same root, and the new state; strips
`(-42 index . condition)` markers into per-reserve buckets; fails the spend on
an index outside `0..N-1`; and sends one mode-23 message per reserve, every
spend, to a receiver derived from the reserve's parent id (solution), its
curried full puzzle hash, and its *pre-spend* amount read from the truth.
The message is the tree hash of the delegated puzzle `(1 . conditions)` the
reserve will run.

**What that means for an adversary.** They cannot: run a leaf that is not in
the tree (proof fails), change the root or the finalizer (curried), aim a
reserve's message at a coin that is not the current reserve (receiver is
derived from truth), or make a reserve pay out more than the leaves tagged
(the reserve runs exactly the delegated puzzle the message named, and its own
amount rule re-creates it at `reserves[i] + fees_owed[i] + dao_owed[i]`).
Pinned: `_test_v11_finalizer.py` (tag index equal to N, negative index,
missing/extra/wrong/swapped parent ids, a reserve run with a puzzle the
singleton did not send, a reserve naming a different sender).

**Open.** None on the frame. The one property that rests on upstream's shape
rather than Forge's own assert — that an action's conditions cannot reach the
singleton's own condition list except through the base bucket — is upstream's
and is pinned by the "a proof omitted for a selector never verified is
refused" and "a sixth leaf with a valid-looking proof is refused" checks in
`_test_v11_actions.py`.

## 1. The prologue (`forge_action_common.rue::prologue`)

Every leaf's first call. Two branches on `truth.ephemeral_state`.

**First action of a spend (`ephemeral_state is nil`).** Asserts, in order:

| Assert | What it closes | Pinned by |
|---|---|---|
| `valid_config(config)` | a pool whose curried config is out of bounds cannot move (V10's validate_config, every spend) | `_test_v11_registry.py` refusals; genesis shapes in `_test_v11_actions.py` |
| `count(reserves) == n`, `count(fees_owed) == n`, `count(dao_owed) == n` | state lists sized to the asset count, so `nth`/`add_at` never run past the end silently | V11.1: a malformed genesis state cannot be minted (registry `eve_state_hash` fixes the shape) |
| `total_lp > 0` | a pool with no LP cannot act; `remove` keeps `burn < total_lp` so it never reaches zero | `_test_v11_actions.py` "burning the whole supply is refused" |
| `h > oracle.last_height` | the height moves forward; a stale `h` is refused before the chain sees it | `_test_v11_actions.py` "a height not above the oracle's last height is refused" |
| `dao_fee_bps` in `[0, MAX_DAO_FEE_BPS]`, and zero when `dao_puzzle_hash` is zero | V11.1: a fee with no recipient would be burned; the cap bounds the composed fee | `_test_v11_dao_fee.py` "a genesis rate above MAX is refused", "a nonzero rate with a zero recipient is refused" |

Emits `ASSERT_HEIGHT_ABSOLUTE h`, `ASSERT_BEFORE_HEIGHT_ABSOLUTE h +
oracle_window`, `ASSERT_MY_AMOUNT 1`. Accumulates the oracle on the pre-spend
reserves for `h − last_height` blocks and sets `last_height = h`; sets
`prev_root = tree_hash(state)`, the state the spend started from.

**Later actions (`ephemeral_state == h`).** Only `assert ephemeral == h`: every
action in one spend names one height, and the config, state shape and oracle
were checked once. Pinned: "two actions naming different heights are refused".

**Observation O-1 (from the live stale-height probe, 2026-09-05).** The height
is bound from below and above, but the bundle may name a *future* `h` and wait
in the mempool for it. Its oracle accumulation then weights the pre-spend
price by `h − last_height` blocks, while inclusion happens anywhere in
`[h, h + oracle_window)`, so a builder can under-weight one observation by up
to `oracle_window` blocks. Bounded by the window, by the pool coin staying
unspent until then, and by the fact that the price recorded is still the
pre-spend one. Low severity; no fund-loss path. A tighter window is the lever
if it ever matters; a leaf cannot learn the current height.

**Observation O-2.** `valid_config` accepts `protocol_fee_bps == 0` with a zero
recipient and `dao_fee_bps == 0` with a zero recipient, but a pool with a
nonzero protocol fee and a zero recipient is refused (a burn). The same
argument is applied to the DAO fee in state. Consistent; no action.

## 2. `swap`

Solution: `h, asset_in, asset_out, gross_input, claimed_output,
settlement_coin_id`.

Asserts: distinct indices within `0..n-1`; `gross_input > 0`;
`claimed_output > 0`; `exact_swap_output(r_in, r_out, r_in + gross, r_out −
claimed, w_in, w_out, fee_bps)` — the curve *bracket*: the claimed output holds
the invariant and one more would break it, so the output is pinned exactly with
integer arithmetic (moved from V10 byte-identical, `_test_v11_curve_equivalence.py`
6,043 checks). Then `protocol_fee = protocol_fee_owed(claimed, protocol_fee_bps)`
and, V11.1, `dao_fee = protocol_fee_owed(claimed, state.dao_fee_bps)` — two
floors, not one on a summed rate.

State: `reserves[in] += gross`, `reserves[out] −= claimed`, `fees_owed[out] +=
protocol_fee`, `dao_owed[out] += dao_fee`. Both fees stay physically inside the
out reserve, owed, until `collect`.

Emits: `settlement_assert(asset_in, settlement_coin_id)` — an
`ASSERT_PUZZLE_ANNOUNCEMENT` that the trader's settlement coin (an offer
settlement puzzle, plain for XCH or CAT-wrapped) was spent in this bundle under
its own id with no payments; and a tagged `CREATE_COIN` for reserve `out`
paying `claimed − protocol_fee − dao_fee` to the offer settlement puzzle hash.

**What the adversary controls:** every solution field. **What they cannot do:**
claim more than the curve (bracket, "one mojo more refused"), claim less to
pocket the difference ("one mojo less refused too": the bracket is exact both
ways, so a router cannot skim), skip the settlement (announcement unmet), name
a settlement not in the bundle (refused), swap an asset for itself (refused),
claim a pre-spend quote after an earlier action in the same spend (the state is
threaded; "sandwich" refused). Pinned in `_test_v11_actions.py`,
`_test_v11_manipulation.py` (the pre-sequence quote on the swap back), and
`_test_v11_payout_audit.py` (trader floor, protocol slice, conservation at any
router rate).

**Open.** None. Note the finalizer, not the leaf, is what makes the reserve
release exactly `claimed`: the leaf changes the state figure, the reserve's
amount rule re-creates the coin at the new figure, and the tagged payout is the
only other output. Conservation is enforced by the reserve coin itself.

## 3. `add`

Solution: `h, deposits[N], lp_delta, lp_parent_id, settlement_coin_ids[N]`.

Asserts: `lp_delta > 0`; some deposit positive; `zip_add` (fails unless
`deposits` has exactly N entries); `exact_invariant_lp_mint(reserves,
reserves + deposits, weights, total_lp, lp_delta, fee_bps)` — the mint bracket
with the imbalance fee (V10, byte-identical); each positive deposit's
settlement announcement (`deposit_asserts`), a zero deposit needs none.

Emits: the deposit announcements; the LP handshake `SEND_MESSAGE` mode 23 to
the LP action coin id derived as `sha256(lp_parent_id || CAT(lp_tail,
MINT_INNER) || amount)` with message `tree_hash(["forge-lp-v11", lp_delta,
new_total_lp, next_state_root])`. The eve (one mojo, the pinned mint inner)
must exist at exactly that id to receive it; the TAIL then requires the same
message from the pool's full puzzle hash (section 8).

**Cannot:** mint one LP more than the invariant (refused), have the eve mint a
different amount than authorized (TAIL refuses: message mismatch), deposit
without a settlement (refused), add nothing (refused), mint above the
moved-state mirror after an earlier action in the spend
(`_test_v11_manipulation.py`).

**Open.** None. The genesis mint is the TAIL's other branch (section 8).

## 4. `remove`

Solution: `h, burn, lp_parent_id, payouts[N]`.

Asserts: `burn > 0`; `burn < total_lp` (the pool outlives every withdrawal);
`zip_sub` (exactly N payouts); `exact_withdrawal(reserves, reserves − payouts,
burn, total_lp, vault_fee_bps(reserves, fee_bps))` — proportional, floored per
asset, with the vault's crossing fee when N = 1 and none otherwise (V10,
byte-identical); each positive payout goes to the offer settlement puzzle and a
zero payout must be exactly zero.

Emits: tagged `CREATE_COIN`s per reserve for the payouts; the LP handshake to
the melt coin id derived with the pinned melt inner and `amount = burn`. The
TAIL additionally requires the melt coin to have a CAT parent, so a coin
fabricated from ordinary mojos cannot stand in for a burn (finding 4 of the
V10 audit, still pinned).

**Cannot:** pay one mojo above the share (refused), burn everything (refused),
melt a fabricated coin (TAIL refuses), pay above the moved-state share after an
earlier action (`_test_v11_manipulation.py`).

**Open.** None.

## 5. `observe`

Solution: `h`. Curried additionally with the pool's observation-slot first-curry
hash (nonce 1).

Asserts: only the prologue's. Emits a slot coin (amount 0) whose puzzle is the
slot curried with `tree_hash((h, cums))`, hinted with the slot's first-curry
hash as upstream does, and a `CREATE_PUZZLE_ANNOUNCEMENT` of
`tree_hash(["forge-observe-v1", h, cums])`. State unchanged beyond the
prologue.

**Property:** the cums are the *pre-spend* accumulation, so a swap earlier in
the same spend cannot poison the observation ("observe cannot record a
same-spend price", `_test_v11_actions.py`). An announcement rather than a
message, so an observe with no consumer still lands.

**Open.** Reading a past slot back inside an action (a same-bundle TWAP
consumer) is not implemented in this revision; the slot exists for a future
consumer to spend by message. Not a safety gap.

## 6. `collect`

Solution: `h, indices` (non-empty).

Asserts: for each index, `fees_owed[i] > 0 || dao_owed[i] > 0`; naming an
index twice fails on the second visit (the first zeroed it). Emits, per index,
a tagged `CREATE_COIN` to `protocol_puzzle_hash` for the protocol slice and,
V11.1, one to `dao_puzzle_hash` for the DAO slice, each hinted with its
recipient, each only when positive. Zeroes both owed balances.

**Property:** permissionless — the caller chooses only *when*. Recipients are
config, amounts are state. Pinned: "collecting a reserve with no fee owed is
refused", "naming the same index twice is refused", "collect with no indices
is refused"; V11.1 "collect pays both recipients from the same reserve" and
"both owed balances are zeroed" (`_test_v11_dao_fee.py`).

**Open.** None.

## 7. `dao_fee` (V11.1)

Solution: `h, new_bps`.

Asserts: `new_bps >= 0`; `new_bps < state.dao_fee_bps`; `dao_puzzle_hash !=
zero`. State: only `dao_fee_bps` changes. Emits `RECEIVE_MESSAGE` mode 23
(sender by puzzle hash = `dao_puzzle_hash`, receiver by coin id = this pool
coin) with message `tree_hash(["forge-dao-fee-v1", new_bps])`.

Against the design's vectors (`FORGE_DAO_FEE_V11.md`):

| Vector | How it is closed | Pinned |
|---|---|---|
| V1 successor substitution | the recipient is curried; the rate is the one state field this leaf writes; every other field is the prologue's | "the rate is the only field that moved" |
| V2 authorization forgery | the message must come from a coin at the DAO's puzzle hash; consensus commits the receiver to this coin | "no message", "another puzzle", "a different rate" all refused |
| V3 value smuggled | no tagged conditions; every reserve re-created at its current amount | "every reserve is re-created at its current amount" |
| V4 replay | `new < current` fails once it holds; at zero no transition exists | "the same rate is refused", "at zero, no decrease exists" |
| V5 interleaving | a swap after the decrease in the same spend is charged at the new rate; a raise cannot occur | "dao_fee then swap in one spend" |
| V7 caps | `MAX_DAO_FEE_BPS` and the zero-recipient rule, in the prologue on every action | "above MAX refused", "nonzero with zero recipient refused" |

**Open.** The DAO coin is any coin whose puzzle hash equals the recipient. A
recipient that is an ordinary wallet address is a single key; the design
intends a multisig or vault puzzle there, and nothing in the leaf changes when
it is. Governance ceremony lives in that coin, as the design says.

## 8. The LP TAIL (`forge_lp_cat_tail.rue`)

Curried `(launcher_id, protocol_version)`; the tree hash is the LP asset id,
unique per pool and per revision. Asserts `protocol_version == 12` (V11.1: a
protocol-11 LP can never be authorized by a protocol-12 pool, and the asset ids
differ), `expected_delta != 0`, `new_total_lp >= 0`, the acting coin's amount
positive, `parent_is_cat || expected_delta > 0` (a melt needs a CAT parent),
and `effective_delta == expected_delta` where a mint from nothing counts the
coin's own amount.

Two branches. **Message:** requires `RECEIVE_MESSAGE` mode 23 from the pool's
full puzzle hash, rebuilt from the launcher and the inner hash the solution
names, with the same message the leaf sent. **Genesis** (`genesis_pool_puzzle_hash`
nonzero): requires the launcher's own coin announcement
`sha256(launcher_id || tree_hash([pool_puzzle_hash, 1, [expected_delta]]))`,
no CAT parent, `new_total_lp == expected_delta`. Finding V11-1 of the audit
log records why this branch exists.

**Cannot:** mint or melt without the pool's message (147), mint a different
amount than authorized, melt a fabricated coin, use a protocol-11 pool's
message (asset id differs anyway).

**Open.** None. The inner puzzles (mint inner with the CHIP-0020 hint, melt
inner byte-identical to V10) are pinned by hash in the common module and
checked by the build.

## 9. The registry (`forge_registry_init`, `forge_registry_register`)

A second CHIP-0050 singleton with the upstream default finalizer and no
reserve. `init` sets `initialized = 1` once. `register` takes a launcher parent
id, the config, the genesis reserves, `total_lp` and, V11.1, the opening
`dao_fee_bps`, plus the two neighbour slots. It asserts `valid_pool` (V10's
config validation, the LP asset id tied to this launcher, every reserve
funded, and V11.1's DAO bounds), rebuilds the pool's **exact** full puzzle
hash from constants and config (`pool_full_puzzle_hash`: reserve inner and
full hashes, the finalizer's two curries, the six-leaf root in the
wallet-sdk tree shape, the eve state hash with the opening DAO rate), asserts
the launcher's coin announcement of that hash with `[total_lp]`, asserts the
creation fee's settlement announcement, spends the two neighbours by message
and re-creates them around the new key, and announces `["forge-registered-v11",
key, launcher_id]`.

**The key** is `tree_hash([asset_ids, weights, fee_bps, protocol_fee_bps,
dao_puzzle_hash])` — V11.1 adds the recipient, so a different DAO may list its
own pool of a pair while a duplicate market is still refused. The opening rate
is not in the key: it is state.

**Cannot:** register before init, init twice, register a duplicate key on
either side, bracket with the wrong neighbours, underpay or skip the fee,
register without the launcher spend, register a launcher that minted a
different config, name an LP that is not this launcher's TAIL, register an
unfunded genesis, mint one LP more than the launcher named, or spend a slot
from a coin that is not the registry. All pinned in `_test_v11_registry.py`.

**Open.** A pool nobody registers is creator-trusted at genesis (as V10) and
undiscoverable; the frontend lists only registered pools. Recorded, not a gap.

## 10. Cross-cutting

**Fee composition.** Swap fee (curve), protocol fee and DAO fee (two slices of
the release), router fee (outside every puzzle, the surplus above the trader's
floor). `_test_v11_payout_audit.py` pins that the trader's floor and the exact
protocol slice hold at any router rate and that trader + router + protocol
equals the release; the DAO slice joins the same identity in
`_test_v11_dao_fee.py`.

**Intra-bundle manipulation.** Swap/add/swap and swap/remove/swap as one
three-action spend leave the actor poorer at pre-sequence prices; value per LP
never falls across any action (`_test_v11_manipulation.py`, 400 random mirror
sequences). Across pools (`_test_v11_multipool.py`, 26 checks) the pin moves
to the pools: every composer lane, built and validated, leaves every pool it
touches with its value per LP intact, and 300 random cross-pool sequences do
the same after every action. An actor CAN gain across pools when they
disagree on a price; that is arbitrage, bounded by the disagreement, and the
pools are brought toward one price rather than drained.

**A second driver.** `scripts/wallet-sdk/second-driver.mjs` rebuilds the
singleton, reserve and LP spends for swap, add and remove on chia-wallet-sdk's
CLVM from this document's description and matches the Python driver byte for
byte (29/29). **Observation O-3:** chia-wallet-sdk 0.36's `Clvm.int(bigint)`
folds integers past 64 bits — the 2^64 `price_scale` came out as `1` — so any
driver on that SDK must encode CLVM integers itself; a config curried through
the SDK's `int` would hash to a different pool and be refused, never accepted
with a wrong scale, but the failure would read as a mystery hash mismatch.

**Findings of this pass.** No assert without a pin. Three observations (O-1
the future-dated height, O-2 the zero-recipient symmetry, O-3 the SDK integer
fold), none a fund-loss path. No open lane: the multi-pool shapes have their
suite.

## Where the rest lives

- [FORGE_PUZZLE_V11.md](FORGE_PUZZLE_V11.md) — the protocol reference the pass reads against.
- [FORGE_SECURITY_AUDIT.md](FORGE_SECURITY_AUDIT.md) — findings and the V10 pass.
- [FORGE_DAO_FEE_V11.md](FORGE_DAO_FEE_V11.md) — the vectors section 7 checks off.
- [FORGE_V11_FOUNDATIONS.md](FORGE_V11_FOUNDATIONS.md) — the suites, the live runs, phase 5.
