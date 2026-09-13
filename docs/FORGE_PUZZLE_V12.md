# Forge puzzle V12 -- the CHIP-0062 review revision

> **Why a revision.** CHIP-0062's Draft review (Chia-Network/chips#217, greimela for
> CNI, 2026-09-11) requested changes on five findings, two of them *reproduced with
> spend bundles accepted by consensus validation*. Those two are exploits of the
> shipping V11.1 puzzles, so V11 closes and V12 replaces it. A sixth finding
> (cursor-bot) and the editor's formatting asks ride along.
>
> **Scope rule.** Every change below is the smallest auditable answer to a named
> finding. Nothing else moves. Where a bigger redesign was tempting it is recorded
> as rejected, with the reason.
>
> **Status (2026-09-13). Live on testnet11.** Protocol version **13**, 33 pools
> deployed behind the current registry, and every lane settled on chain through the
> path the website actually uses -- a wallet-signed offer handed to a keyless
> responder. Direct swap in both directions, deposit, withdrawal, a two-hop route and
> a three-branch split have each moved real coins; a full 100% withdrawal has been
> proven against a pool whose locked LP floor was burned at genesis.
>
> Offline: eighty suites green, full mutation evidence, and each finding below
> demonstrated against the superseded build and refused by this one with no chain
> involved.
>
> **The registry was re-minted (2026-09-12).** The rename pass that moved the last V11
> references out of the V12 lane touched one string inside `forge_registry_register`
> (`forge-registered-v11`), which was corrected and, in the same edit, enriched to carry
> the pool's identity. The six pool leaves, the LP TAIL, the finalizer and the
> reserve-amount program are **byte-identical** -- the pool merkle root is still
> `46cf70c0…`, so pool puzzle hashes and LP asset ids never moved and every piece of
> audit evidence still describes the shipping bytes. Only the registry root moved
> (`e187e48d…` to `12e15e97…`).
>
> Testnet11 halted 2026-09-11 15:16 and resumed 2026-09-12 22:06, a gap of about 30
> hours with no reorg: the old registry, A1 and A1's launcher all survived, and A2 --
> still in a node's mempool through the whole outage -- was mined at 4,677,842 the
> moment blocks resumed. A1 and A2 therefore belong to the superseded registry. A pool
> can only be registered in its creation bundle, so they cannot be re-registered; their
> record is archived as `.awizard/v12-testnet.pre-rename-A1A2.json` and they stay fully
> spendable from it. The current registry is
> `a12f0ca8e87881cc260568a53bbe7643ea55f090b6873a92d71fec66d1edde5b`, minted at
> 4,677,846, with the 20-pool matrix launching behind it.

---

## The findings, one line each

| # | Sev | Finding | V11 truth | V12 answer |
|---|---|---|---|---|
| 1 | P0 | Genesis can authorize more LP than state records | Any number of eves can assert the launcher's one announcement | The announcement names the eve's coin id |
| 2 | P1 | Successive generations can backfill oracle time | Elapsed = claimed `h` minus previous claimed `h`; both solver-chosen inside the window | Elapsed = claimed `h` minus **this coin's birth height**, asserted by consensus |
| 3 | P1 | The final LP position cannot redeem | `burn < total_lp` and no minimum at genesis | `MIN_LOCKED_LP` enforced at registration; `burn <= total_lp - MIN_LOCKED_LP` |
| 4 | P1 | Protocol-fee units disagree | Code is bps; the CHIP said ppm | The CHIP is corrected; the puzzle is unchanged |
| 5 | P1 | Reserve receiver derivation trusts solution data | The finalizer takes reserve parent ids from its solution | Reserve parent ids live in state, written by the finalizer |
| 6 | High (bot) | Empty action list not forbidden | Upstream `action.rue:119` already asserts non-empty | A test and a CHIP sentence; no puzzle change |

---

## 1. Genesis binds to one eve (P0)

**The hole.** The TAIL's genesis branch authorizes a mint from nothing by asserting the
launcher's coin announcement `sha256(launcher_id ‖ tree_hash([pool_ph, 1, [expected_delta]]))`.
The launcher emits that once; *any coin* can assert it. Two eves of one mojo each, both
asserting it, both mint `expected_delta`. State recorded 5,000,000; 10,000,000 existed.

**The fix.** Put the eve's own coin id in the announced list:

```rue
// forge_lp_cat_tail.rue, genesis branch
AssertCoinAnnouncement {
    id: sha256(launcher_id + tree_hash([
        action.genesis_pool_puzzle_hash, 1,
        [action.expected_delta, cat_my_coin_id(truths_any)],   // NEW: this coin, and only this coin
    ])),
}
```

`cat_my_coin_id` reads the CAT2 truths exactly as `cat_my_amount` does. A second eve
has a different id, so the announcement it needs was never made.

The launcher's key-value list becomes `(total_lp, eve_coin_id)`, and the registry's
`register` asserts the same list (it already takes `total_lp` from its solution; it
takes `eve_coin_id` the same way). The creation flow is unchanged: the funding coin
already creates the eve, so its id is known before the launcher spend is built.

**Rejected alternative.** Message-based genesis through a first `add` (eve state
`total_lp = 0`). Cleaner in principle -- it deletes the genesis branch entirely -- but it
needs zero-amount reserve coins at creation, a genesis mint rule in `add`, and a
`total_lp >= 0` prologue with per-leaf guards. Four files change instead of one, on a
creation path that has run 20 pools. Not for this revision.

**Tests.** `_test_v12_genesis.py`: one eve accepted; a second eve asserting the same
announcement refused; an eve whose id is not in the list refused; registry refuses a
list that omits the eve id.

## 2. Oracle elapsed time binds to birth height (P1)

**The hole.** The prologue accepts any `h` in `(last_height, actual + window)` and
accumulates `price × (h − last_height)`. Spend generation N at `h = H − 31` with a swap
that moves the price, then spend the ephemeral successor at `h = H` in the same bundle:
31 blocks of the manipulated price, and no block elapsed.

**The fix.** The pool coin asserts its own birth height and measures from it:

```rue
// forge_action_common.rue, prologue, first action only
// solution gains `birth: Int`
AssertMyBirthHeight { height: birth },        // consensus: this coin was created at `birth`
assert h >= birth;
assert h > state.oracle.last_height;          // kept: timestamps stay monotone
cums: accumulate(reserves, weights, cums, price_scale, h - birth),   // was h - last_height
```

Why this is enough: the pre-spend state came into force when this coin was born and
stays in force until this spend lands. `h − birth` is that interval measured from a
consensus fact, with a solver-chosen `h` that can only *understate* it (by at most the
window). Nothing can be fabricated. A successor spent in the same block has
`birth = H` and `h ≤ H`, so it accumulates zero.

**On ephemeral coins -- settled.** Consensus refuses a birth condition on a coin created
in the same bundle outright: `EPHEMERAL_RELATIVE_CONDITION`. So same-block chaining of
pool generations is not merely detected, it is impossible, whatever birth the successor
claims; claim its *true* birth and the prologue's own `h >= birth` refuses it one step
earlier still, because the bundle must also be mined at or before `h`. That costs
nothing: the route composer emits exactly one spend per pool per bundle
(`forge_v12_route.py` `assemble`), threading every action for a pool through it.

**Tests.** `_test_v12_oracle.py` proves the arithmetic: an honest spend accumulates
`h − birth`, a same-block successor accumulates zero, `h < birth` is refused.
`_test_v12_consensus_timelocks.py` proves the rest offline by calling
`chia.consensus.check_time_locks`, the function the mempool itself calls -- V11's
two-generation backfill is ACCEPTED there, V12's cannot even be validated, and a single
generation understating its birth is refused `ASSERT_MY_BIRTH_HEIGHT_FAILED`. A real
node confirms it in `scripts/sim-v12.py`, including the sharper case of a pool claiming
a height that is genuinely the birth of another coin spent in the same bundle: still
refused, because the condition binds to the birth of the coin being spent.

## 3. A bounded minimum liquidity, locked at genesis (P1)

**The hole.** `burn < total_lp` means the last unit is never redeemable, and the
registry accepted any positive genesis supply, so the trapped share was unbounded and
undisclosed.

**The fix.** A constant, enforced where supply is set and where it is reduced:

```rue
// forge_action_common.rue
export inline const MIN_LOCKED_LP: Int = 1000;

// forge_registry_register.rue
assert total_lp >= MIN_LOCKED_LP;              // NEW

// forge_action_remove.rue
assert burn > 0;
assert burn <= p.state.total_lp - MIN_LOCKED_LP;   // was: burn < total_lp
```

The creator locks 1,000 LP units at genesis, as Uniswap V2 locks `MINIMUM_LIQUIDITY`.
Every later holder can redeem every unit they hold. The pool always outlives every
withdrawal, so the registry slot is never dead. The trapped value is bounded, known at
creation, and paid by the creator, not by the last depositor.

**Rejected alternative.** A terminal close path (`burn == total_lp` pays out every
reserve and the pool dies). Kills the registry slot for that market forever, or else
needs a re-seed rule that is genesis-by-message in disguise. The reviewer listed both
answers as acceptable; this one is two lines.

**Tests.** `_test_v12_actions.py` remove: `burn == total_lp − MIN_LOCKED_LP` accepted (a
holder redeems everything above the lock); `+1` refused; registry refuses
`total_lp = MIN_LOCKED_LP − 1`.

## 4. Fee units (P1)

The puzzle has always been basis points: `protocol_fee_bps`, `MAX_PROTOCOL_FEE_BPS =
100`, `dao_fee_bps`. CHIP line 200 said "parts per million". **The CHIP is wrong; the
puzzle does not change.** The CHIP's configuration paragraph, the leaf table, and the
security section are made to say basis points, denominator 10,000, with the names the
code uses.

## 5. Reserve parent ids move into state (P1)

**The hole.** The finalizer's solution carries `reserve_parent_ids`, and each reserve's
receiver is `coinid(parent_id, full_hash_i, pre_spend_amount)`. Anyone can create a coin
at `full_hash_i` holding exactly `pre_spend_amount` and name *its* parent. The finalizer
then messages the decoy; the decoy runs the delegated puzzle, pays the trader, and
recreates the reserve lineage from itself. The real reserve is orphaned with its full
balance, its amount no longer matching state, unreachable forever. It is a griefing
attack at 1:1 cost, not a theft, which is why the reviewer graded it P1 and tested it
against compiled puzzles rather than with a bundle.

**The fix.** The finalizer already computes every current reserve coin id; it writes them
into the successor state and reads them from the predecessor state:

```rue
// forge_action_common.rue
export struct ForgeState {
    ...
    dao_owed: List<Int>,
    reserve_parents: List<Bytes32>,     // NEW: parent id of each reserve coin, in asset order
}

// forge_multi_reserve_finalizer.rue
// `...reserve_parent_ids` is REMOVED from the solution.
let parent_ids = initial_state.reserve_parents;                       // from the truth
let current_ids = reserve_coin_ids(parent_ids, RESERVE_FULL_PUZZLE_HASHES, initial_state);
let committed_state = ForgeState { ...new_state, reserve_parents: current_ids };
// recreate the singleton with tree_hash(committed_state); message each reserve at current_ids[i]
```

Actions never touch `reserve_parents`; they pass it through. At genesis the eve state
carries the creation coins' parents, which `cmd_create_pool` already holds as
`reserve_coins`. The only thing a solution can no longer do is name a coin.

**Tests.** `_test_v12_finalizer.py`: honest spend advances `reserve_parents` to the
spent reserves' ids; a decoy reserve at the right hash and amount, created in the same
bundle, is not messaged and the real reserve is; the old solution shape (extra list) is
refused.

## 6. Empty action list (cursor-bot, High)

Upstream `action.rue:119`: `assert !(selectors_and_proofs is nil);`. Already refused.
V12 adds `_test_v12_actions.py` "a spend with no actions is refused" so it is pinned,
and the CHIP states it under **Authorization**.

---

## Everything else that moves, mechanically

- `PROTOCOL_VERSION` 12 → **13** (TAIL, driver, `api/_forgeVersion.js`,
  `src/lib/poolIndexer.ts`). A fresh LP asset id per pool; V11 LP can never be
  authorized by a V12 pool.
- Message tags: `"forge-lp-v11"` → `"forge-lp-v12"`; `"forge-observe-v1"` stays (the
  observe payload's shape is unchanged).
- New leaf set → new merkle root → new TAIL version → **new registry** (its constants
  carry the leaf hashes). The V11 registry `599ba997…` is retired.
- `contracts/v12/` is a copy of `v11/` with these edits; `scripts/build-v12.py`;
  `forge_v12_driver.py`; `_v12_testkit.py`; every `_test_v11_*` suite copied to
  `_test_v12_*` and extended per the tests above. `mutate-v12.py` gains `--project v12`.
- V11 stays in the tree until the drain is complete, because draining V11 pools needs
  V11 puzzles. Then it moves to `development/` per the house rule.

## Implementation notes (2026-09-11, offline complete)

What the build taught, beyond the design above.

- **The oracle finding is provable offline after all, both halves.** The validator has no
  coin records, so it cannot judge a birth assert -- but consensus decides that question
  in a pure function, `chia.consensus.check_time_locks`, and the mempool calls exactly
  it. Hand it the records the chain would have and it answers offline with a node's own
  error codes. `_test_v12_consensus_timelocks.py` does that over the reviewer's own
  bundles: V11's two-generation backfill is **accepted** by both the validator and the
  time-lock rule, integrating 31 blocks of a price the bundle set in one block; a V12
  spend understating its birth by 31 blocks is refused `ASSERT_MY_BIRTH_HEIGHT_FAILED`;
  and the claimed height is boxed in from both sides by the prologue's own
  `ASSERT_HEIGHT_ABSOLUTE` / `ASSERT_BEFORE_HEIGHT_ABSOLUTE`.
- **V12's fix is stronger than this document claimed.** The design said a same-block
  successor "accumulates nothing". It cannot be spent at all: consensus forbids a
  relative or birth condition on a coin created in the same bundle, and every V12 pool
  spend asserts its own birth, so the aggregate is refused
  `EPHEMERAL_RELATIVE_CONDITION` whatever birth is claimed. Claim the successor's *true*
  birth -- the block it is created in -- and the prologue's own `h >= birth` refuses it
  one step earlier, because the same bundle must be mined at or before `h`. The
  two-generation shape is gone rather than merely detected, and nothing legitimate wants
  it: several actions run inside one pool spend, and a route spends different pools.
- **All four puzzle findings have a before-and-after, offline.**
  `_test_v11_v12_review_findings.py` builds each attack against the V11 build and
  requires it to be ACCEPTED, then against V12 and requires it to be REFUSED. V11 mints
  10,000,000 LP against a state recording 5,000,000 from two eves on one launcher
  announcement (V12: `ASSERT_ANNOUNCE_CONSUMED_FAILED`); V11 accepts a decoy reserve
  named through the finalizer's solution (V12: `MESSAGE_NOT_SENT_OR_RECEIVED`); V11
  refuses a full redemption and floors at one trapped unit, V12 floors at exactly
  `MIN_LOCKED_LP` and refuses one unit past it. A finding that stops reproducing against
  V11 fails the suite too -- otherwise it would quietly stop testing anything.
- **Every leaf solution is `[h, birth, ...]`.** The driver inserts `pool.birth` in one
  place (`with_birth`) on all three assembly paths -- `run_leaf`, `spend_action`,
  `spend_actions`. Missing the third was the one bug of the port: on chain every
  argument shifted one place and every leaf raised. Offline, `birth` defaults to the
  state's `last_height`; live, it is the height the previous spend confirmed at,
  carried in the record.
- **`reserve_parents` must be the parents of the coins the pool will actually spend.**
  `make_pool` writes them from whatever reserves it builds, even when a caller re-seeds
  a pool from an earlier state; only real `reserve_coins` carry their own. The
  successor's parents are the spent reserves' ids, so `advance()` and
  `successor_puzzle_hash()` apply the finalizer's rewrite (`committed()`) before
  hashing. Snapshots and records carry `reserve_parents` and `birth`.
- **The responder picks a lane by revision.** `forge_stdin` keeps one module set per
  protocol version (12 → V11 modules, 13 → V12) and refuses a bundle that mixes them.
- **Mutation, swept in full (2026-09-11):** the leaves, 12 of 33 killed; the TAIL,
  registry, finalizer and reserve-amount puzzles, 8 of 24 killed; nothing unbuildable
  in either set, and every mutant run against all seven V12 suites. The survivors are
  V11's known structural set (sign guards the curve already makes unsatisfiable, shape
  counts, `valid_config`); both new asserts -- `h >= birth` and
  `burn <= total_lp - MIN_LOCKED_LP` -- are killed. The two locks that live inside
  conditions rather than `assert` lines were mutated by hand: dropping the eve id from
  the TAIL's announced list fails the genesis suite; relaxing the registry to
  `total_lp > 0` fails the registry suite.
- **Two suites were reading the wrong build, and the port hid it.** `_test_v12_integrity`
  and `_test_v12_curve_equivalence` locate the compiled puzzles by path rather than
  through the driver, and the copy kept V11's path. Both passed, so nothing looked
  wrong; both were re-proving V11 while reporting V12. Repointed at
  `contracts/v12/compiled`, they pass unchanged -- the V12 build does hold its pins,
  and its curve does answer like V10 -- but for a day the two checks that exist to
  catch a stale or edited build could not have caught one. Every other suite loads
  through `forge_v12_driver`, which reads `FORGE_V12_COMPILED` or `contracts/v12/
  compiled`, so a suite that names a directory literally is the shape to distrust in
  the next port.
- **Offline suites, all green against the V12 build:** integrity 101 (including a fresh
  `rue` recompile from source in a scratch copy), actions 81, route lane 106, offer lane
  98, finalizer 42 (with the decoy reserve), dao_fee 37, registry 32, multipool 26,
  payout audit 23, create 20, discoverability 15, manipulation 14, curve equivalence
  6,043, plus genesis, oracle and receive-forgery. The quoting and responder checks run
  579 green. The compiled merkle root over the six leaves is `46cf70c0…`; upstream pins
  hold.

## The CHIP revision

Two commits on `chip-awizard-weighted-n-asset-amm` (now `CHIPs/chip-0062.md`):

1. **Now, documentation-only:** the editor's asks (paragraphs reflowed to single lines,
   the 25 U+2014 characters replaced), the fee-unit correction, and the non-empty
   action sentence.
2. **After V12 is live on testnet11:** the four design changes, each in the section
   the reviewer anchored on, plus a "Revision history" noting what CNI's review found
   and that V11.1 was never deployed to mainnet.

The reply on the PR acknowledges each finding by severity, states which two were
reproduced, and links this document.

## Order of work

1. ~~Drain V11 liquidity~~ **done 2026-09-11.** 20 pools, wrappers B2/F2 before B1/F1,
   `burn = total_lp − 1`; the terminal-LP finding demonstrated live on every pool.
2. ~~V12 puzzles, driver and suites, offline green, mutation run per leaf~~ **done.**
   Twenty suites; both mutation sets swept in full; all four findings exploited against
   V11 and refused by V12 with no chain involved.
3. ~~V12 on a real node without testnet~~ **done 2026-09-12**, while testnet11 was
   halted: `scripts/sim-v12.py` farms, issues tokens, mints the registry, creates and
   registers every pool shape including a vault and a wrapper over another pool's LP,
   then swaps, deposits, redeems and collects. The birth-height lock, the decoy reserve
   and the impostor singleton are all refused by consensus rather than argued for.
4. Launch V12 on testnet11 from the same matrix (`FORGE_LAUNCH_MATRIX.md`) -- **running
   2026-09-12** behind registry `a12f0ca8e87881cc…` -- then deepen, run the lifecycle
   matrix and the live probes (forgery, oracle two-generation, decoy).
5. CHIP commit 2 and the PR reply.
