# Forge puzzle V11 — the shipping revision

**Updated 2026-09-05.** V11 is the Forge pool on the CHIP-0050 action layer:
one singleton whose inner puzzle is the upstream action layer curried with a
finalizer, a merkle root over five leaves, and the state; reserves that obey it
through CHIP-0025 messages; an LP CAT whose TAIL takes its authority from the
pool's message; and a registry that lists every pool once. V10's curve is moved
into it byte for byte. **Not externally audited. Testnet11 only.**

This is the protocol document: what the puzzles do and will accept, whichever
interface is in front of them. The built shape as diagrams is
`FORGE_V11_ARCHITECTURE.md`; the record of how it was built and probed is
`FORGE_V11_FOUNDATIONS.md`; the method for auditing it is
`docs/skills/clvmPuzzleAudit.md`.

---

## The shipping set

Everything lives in `contracts/v11` as a rue 0.8.4 project (`Rue.toml`,
`puzzles/`, `compiled/`), built by `scripts/build-v11.py`, pinned by
`pins.json` and checked by `_test_v11_integrity.py`.

| puzzle | role | note |
|---|---|---|
| `upstream/action.rue` | the action layer: proves and runs leaves, hands conditions to the finalizer | Yakuhito/slot-machine @ 2d37ba1, hash `afa03f29…` |
| `upstream/finalizer.rue`, `upstream/reserve_finalizer.rue` | upstream finalizers, vendored for the pins | not used directly by a pool |
| `upstream/p2_delegated_by_singleton.rue` | the reserve coin's puzzle | hash `3f2358e5…` |
| `upstream/slot.rue`, `slot_helpers.rue` | slots for the registry list and oracle observations | hash `8ab9f3b5…` |
| `forge_multi_reserve_finalizer.rue` | Forge's finalizer: recreates the singleton, messages N reserves | curried `[ACTION_LAYER_HASH, FULL_HASHES, INNER_HASHES, RESERVE_AMOUNT, HINT]`, then itself |
| `forge_reserve_amount.rue` | `(state, i) -> reserves[i] + fees_owed[i]` | what a reserve coin holds |
| `forge_action_common.rue` | `PoolConfig`, `ForgeState`, the prologue, the helpers | |
| `forge_curve.rue` | V10's curve functions | `exact_swap_output`, `pow_int`, `sum_weights`, `vault_fee_bps` byte-identical to V10 |
| `forge_action_{swap,add,remove,observe,collect}.rue` | the five leaves | curried `PoolConfig` (observe also `SLOT_1ST_CURRY_HASH`) |
| `forge_lp_cat_tail.rue` | the LP CAT TAIL | curried `(launcher_id, 11)`; its hash is the LP asset id |
| `clsp/forge_lp_mint_inner.clsp`, V10's melt inner | the pinned LP action inners | mint `66811aed…` (hinted), melt `8b5b948a…` (V10's) |
| `forge_registry_{common,init,register}.rue` | the registry's leaves | one registry per deployment |

The five-leaf merkle root over the leaf hashes (in `compiled/merkle.json`) is
`ba08efba…`; a pool's registration recomputes it from the leaf hashes the
registry carries, so a pool with any other leaf set cannot register.

---

## Coin layout

```
pool singleton    singleton_top_layer_v1_1( action_layer( finalizer, merkle_root, state ) )   amount 1
reserve i         p2_delegated_by_singleton( SINGLETON_MOD_HASH, struct_hash, i )              amount reserves[i] + fees_owed[i]
                  bare for XCH; CAT-wrapped for a CAT asset
LP eve            CAT( lp_asset_id, forge_lp_mint_inner )                                      amount 1, no CAT parent
LP melt coin      CAT( lp_asset_id, forge_lp_melt_inner )                                      amount = burn, CAT parent required
settlement        OFFER_MOD, or CAT( asset, OFFER_MOD )                                        the trader's input, or a payout
observation slot  slot( struct, nonce 1 ).curry( tree_hash((h . cums)) )                       amount 0
registry          singleton( action_layer( finalizer, registry_root, RegistryState ) )
registry slot     slot( struct, nonce 1 ).curry( tree_hash([key, launcher, left_key, right_key]) )
```

Every coin a pool spend creates is hinted with the launcher id (reserves,
successor); the LP mint inner hints its recipient; slots hint their first-curry
hash. Everything is findable from one hint (`_test_v11_discoverability.py`).

### Config (curried into every leaf, permanent)

```
PoolConfig {
  asset_ids            List<Bytes32>   strictly ascending; ZERO_32 is XCH
  weights              List<Int>       integer units, 1..8 each, sum ≤ 20, sum ≥ N
  fee_bps              Int             0..200      the liquidity fee, stays in the reserve
  protocol_fee_bps     Int             0..100      accrues to fees_owed; needs a recipient if > 0
  protocol_puzzle_hash Bytes32
  lp_tail_hash         Bytes32         the LP asset id
  price_scale          Int             2^64
  oracle_window        Int             32 blocks
}
```

`valid_config` is asserted by the first action of every spend; the registry's
`valid_pool` asserts the same at creation. N is 1 to 10.

### State (curried into the singleton, changes every spend)

```
ForgeState {
  reserves    List<Int>               the tradable amounts
  total_lp    Int                     > 0 always
  fees_owed   List<Int>               protocol fee accrued per reserve, uncollected
  oracle      { last_height, cums[] } price-time accumulators, one per non-base asset
  prev_root   Bytes32                 tree_hash of the state this spend started from
}
```

The reserve coin holds `reserves[i] + fees_owed[i]`: the fee sits physically
in the reserve until `collect` pays it out. `prev_root` chains the states, so
the history is replayable from the spends alone.

---

## The prologue

Every leaf calls `prologue(config, truth, h)`:

- **first action of a spend** (`ephemeral_state` is nil): `valid_config`; state
  shape (one reserve and one fee per asset); `total_lp > 0`;
  `h > oracle.last_height`; conditions `ASSERT_HEIGHT_ABSOLUTE h`,
  `ASSERT_BEFORE_HEIGHT_ABSOLUTE h + oracle_window`, `ASSERT_MY_AMOUNT 1`;
  the oracle accumulates the **pre-spend** price weighted by
  `h - last_height`; `prev_root = tree_hash(state)`; ephemeral becomes `h`.
- **later actions**: `ephemeral_state == h`, nothing else.

So a spend is bound to a 32-block window, several actions in one spend share
one height, and the oracle records the price the pool had before anyone in the
spend traded.

---

## The invariant

Unchanged from V10, moved not rewritten (`_test_v11_curve_equivalence.py`,
6,043 checks over 81 recorded V10 spends and 300 randomized pools). The puzzle
never solves for an output; it verifies one by bracketing:

```
swap   in^k_in · out^k_out ≥ reserve_in^k_in · reserve_out^k_out, and one more mojo out breaks it
mint   (lp + d)^K · ∏ old^k ≤ lp^K · ∏ effective^k < (lp + d + 1)^K · ∏ old^k
burn   each payout is the floored proportional share; a one-asset pool withholds the liquidity fee
```

`K` is the sum of the weights. Untouched reserves cancel out of a swap.

---

## The five leaves

| leaf | solution | must hold | tagged conditions (routed to a reserve) | other conditions |
|---|---|---|---|---|
| **swap** | `[h, i_in, i_out, gross, claimed, settlement_coin_id]` | `i_in ≠ i_out`, both in range; `gross, claimed > 0`; `exact_swap_output` on the pair | reserve `i_out` creates `OFFER_MOD` for `claimed − protocol_fee` | asserts the settlement's `(coin_id . nil)` announcement |
| **add** | `[h, deposits[N], lp_delta, lp_parent_id, settlements[N]]` | `lp_delta > 0`; some deposit > 0, none < 0; `exact_invariant_lp_mint` | none (reserves grow through the state) | one settlement assertion per positive deposit; `SEND_MESSAGE` mode 23 to the eve at `coinid(lp_parent, mint full hash, 1)` |
| **remove** | `[h, burn, lp_parent_id, payouts[N]]` | `0 < burn < total_lp`; `exact_withdrawal` with `vault_fee_bps` | each positive payout: reserve `i` creates `OFFER_MOD` for it | `SEND_MESSAGE` to the melt coin at `coinid(lp_parent, melt full hash, burn)` |
| **observe** | `[h]` | prologue only | none | creates the slot `(h . cums)`; announces `tree_hash(["forge-observe-v1", h, cums])` |
| **collect** | `[h, indices]` | non-empty; each named `fees_owed[i] > 0` (naming one twice fails) | reserve `i` creates `protocol_puzzle_hash` for `fees_owed[i]`, hinted | none |

State changes: swap moves the pair and accrues the fee to `fees_owed[i_out]`;
add and remove change reserves and `total_lp`; collect zeroes what it paid;
observe changes nothing beyond the prologue.

The **LP message** is `tree_hash(["forge-lp-v11", lp_delta, new_total_lp, tree_hash(new_state)])`,
sent with mode 23 (sender committed by puzzle hash, receiver by coin id).

---

## The finalizer

`forge_multi_reserve_finalizer.rue` runs once per spend after every leaf.

1. Walks the conditions the action layer accumulated, last action first.
   Untagged conditions are the singleton's own. A tagged condition
   `(-42 index . condition)` is prepended to reserve `index`'s list; an index
   out of range fails.
2. Recreates the singleton at the new state with `ASSERT_MY_PUZZLEHASH`,
   hinted with the launcher.
3. For each reserve `i`: `SEND_MESSAGE` mode 23 to
   `coinid(parent_i, full_hash_i, reserves'[i] + fees_owed'[i])` with message
   `tree_hash((recreate_i . conditions_i))`, where `recreate_i` is
   `CREATE_COIN inner_hash_i amount (hint launcher)`.

The reserve's delegated puzzle is `(1 . [recreate_i, *conditions_i])`. The
reserve runs exactly what the message names, so a reserve is always recreated
at the amount the state says and can only emit what a leaf asked for.

**Ordering, which drivers must mirror:** each action's tagged conditions
reversed, concatenated in execution order. Any other order changes the message
and fails the pairing (consensus error 147).

---

## Authorization

| binding | mechanism |
|---|---|
| pool → reserve | `SEND_MESSAGE` mode 23; receiver id derived from the reserve's parent, full hash and the new amount; the reserve receives against the pool's full puzzle hash |
| pool → LP eve or melt coin | `SEND_MESSAGE` mode 23 to the derived coin id; the TAIL receives against the pool's full puzzle hash rebuilt from the curried launcher id and `pool_inner_puzzle_hash` |
| launcher → genesis mint | `AssertCoinAnnouncement sha256(launcher_id + tree_hash([pool_ph, 1, [expected_delta]]))`; `!parent_is_cat`; `new_total_lp == expected_delta` |
| leaf → settlement coin | `ASSERT_PUZZLE_ANNOUNCEMENT sha256(settlement_ph + tree_hash((coin_id . nil)))` |
| registry → pool | recomputes the pool's full puzzle hash from launcher id + config + genesis state; asserts the launcher's `[total_lp]` announcement and the fee settlement's `(launcher_id, [[treasury, fee, [treasury]]])` announcement |

No puzzle takes an authorizing coin id from its own solution. The settlement
assertion binds a coin and not an amount, which is what lets one payout coin
be the next pool's settlement inside a bundle, and one entry coin fan out to
several pools.

### The TAIL's two locks

`parent_is_cat || expected_delta > 0`: a melt can only come from a coin that
held supply (finding 4; CHIP-0040's standard TAIL lacks this line and was
declined). `effective_delta == expected_delta`: the ring's real supply change
is the one the pool's message named. The message also commits the new supply
and the next state root.

---

## Fees

| fee | where | when | paid to |
|---|---|---|---|
| liquidity fee, ≤ 200 bps | in the leaf | every swap; a one-asset pool's redeem | stays in the reserve, to LP holders |
| protocol fee, ≤ 100 bps | in the leaf | swaps | accrues to `fees_owed`; `collect` pays it to `protocol_puzzle_hash` |
| router surplus | outside the puzzle | when we settle | the gap between the curve's payout and the trader's request |
| creation fee | the registry | once | the treasury, asserted at registration |

The protocol fee is a slice of the swap output, accrued rather than paid per
swap, so a swap creates one payout coin and `collect` pays the accrued fees in
batches. The reserve coin carries the accrued fee until then.

---

## The registry

One singleton per deployment on the same action layer, with
`RegistryState {initialized, pool_count}` and two leaves:

- **init**: one shot; creates the two sentinel slots `0x00…` and `0xff…`.
- **register** `[launcher_parent, config, reserves, total_lp, (left_launcher, left_far), (right_launcher, right_far), left_key, right_key]`:
  derives the launcher id from its parent; `valid_pool`; recomputes the pool's
  full puzzle hash; `left_key < key < right_key`; asserts the launcher's
  announcement with kv `[total_lp]` and the fee settlement's announcement;
  spends both neighbour slots by message and recreates them around the new
  slot; announces `("forge-registered-v11", key, launcher)`.

The key is `tree_hash([asset_ids, weights, fee_bps, protocol_fee_bps])`.
Adjacency is structural (a neighbour's pointer is the same variable as the
other neighbour's key), so a key is admitted exactly once: a second pool with
the same configuration cannot register, whatever its launcher.

---

## Names

The puzzles carry no label. A pool's name and symbol are memos: on the
launcher's creation (inception) and on renames the deployer's address writes
for itself, hinted with the launcher id (`forge_v11_names.py`). They are
metadata, never inputs; the asset id and the recomputed puzzle hashes are the
truth.

---

## V11.1 — the DAO fee revision (protocol 12)

Shipped 2026-09-05 as the revision the registry and the LP TAIL call protocol
12. Everything above holds; this section is the delta.

**Config** gains one field at the end: `dao_puzzle_hash` (Bytes32, zero when
the pool has no DAO). It is curried into every leaf like the rest, so it never
changes.

**State** gains two fields at the end: `dao_fee_bps` (Int) and `dao_owed`
(List<Int>, one per reserve):

    [reserves, total_lp, fees_owed, (last_height . cums), prev_root, dao_fee_bps, dao_owed]

The prologue validates them on the first action of every spend: `dao_owed` has
one entry per asset, `0 <= dao_fee_bps <= MAX_DAO_FEE_BPS (100)`, and the rate
is zero unless the recipient is set (a fee with no recipient would be burned).

**Swap** takes a second slice of `claimed_output` at the state's rate,
`protocol_fee_owed(claimed_output, dao_fee_bps)`, floored on its own; the
payout is `claimed − protocol_fee − dao_fee`; the slice is owed in
`dao_owed[asset_out]` inside the same reserve. **Collect** pays both slices of
each named index, the protocol's to `protocol_puzzle_hash` and the DAO's to
`dao_puzzle_hash`, and qualifies an index when either is positive. The
**reserve amount rule** is `reserves[i] + fees_owed[i] + dao_owed[i]`.

**A sixth leaf, `dao_fee`** (solution `h, new_bps`): asserts `0 <= new_bps <
dao_fee_bps` and a set recipient, writes only the rate, and requires
`RECEIVE_MESSAGE` mode 23 — sender by puzzle hash, receiver by coin id — from a
coin at `dao_puzzle_hash` carrying `tree_hash(["forge-dao-fee-v1", new_bps])`.
The DAO proves control of the recipient by spending a coin at it; consensus
commits the receiver to this pool coin, so the message cannot be replayed
against another pool or another spend. At zero there is no transition to a
higher rate: irreversibility is topology. The merkle root is over six leaves in
the order `[swap, add, remove, observe, collect, dao_fee]`, split 3 + 3 in the
wallet-sdk shape.

**Registry.** `pool_key` adds `dao_puzzle_hash`, so two DAOs may list one pair
while a duplicate market is still refused; `register` takes the opening
`dao_fee_bps` after `total_lp`, checks it against the cap and the recipient,
and rebuilds the eve state with it. The LP TAIL asserts `protocol_version ==
12`, so LP of the two revisions can never cross.

**Off chain.** The rate is read from state, not from the creation record —
`api/pools.js` resolves it once in `resolvedDaoFeeBps` and both the pool view and
the state view call it, because they had drifted apart once (finding V11-2 in
`FORGE_SECURITY_AUDIT.md`) and a quote built on the creation figure prices a fee
the puzzle no longer charges. The recipient is config and comes from the
snapshot, which is where a pool created before the field carries it.

quote takes the DAO slice at the pool's current rate. `deploy-v11-testnet.py
create-pool --dao-ph --dao-fee-bps` opens a pool with a DAO, and its `dao-fee
--label --new-bps` lowers the rate live from the recipient wallet.

Suites: `_test_v11_dao_fee.py` (31) checks the design's vectors one by one;
the written pass is `FORGE_V11_CLVM_PASS.md`.

## Cutting a new revision

A V11 pool's leaves and finalizer are fixed at creation and old pools stay on
chain, so a revision is a new leaf set, a new merkle root, a new TAIL version
and a new registry (its constants carry the leaf hashes). The DAO fee field, a
name inside the slot, and any change to the prologue are all revisions; they
are decided together (phase 8.8). Bump `PROTOCOL_VERSION` in
`forge_v11_driver.py` and the TAIL, `FORGE_PROTOCOL_VERSION` in
`api/_forgeVersion.js` and `src/lib/poolIndexer.ts`, rebuild with
`scripts/build-v11.py`, re-pin, and run the eight suites.
