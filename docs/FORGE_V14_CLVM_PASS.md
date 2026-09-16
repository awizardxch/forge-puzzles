# Forge V14 — the written CLVM pass, leaf by leaf

The reading of every puzzle a V14 pool runs, in the order a spend meets them. Each
section states what the puzzle takes, what it asserts, what it emits, which suite pins
each refusal, and what remains open. Written against the V14 build (protocol 15, six
leaves, the reserve launcher), 2026-09-16.

V14 is the revision the fourth independent review forced (Chia-Network/chips#217,
trgarrett, 2026-09-15) and the fifth review of the documents shaped. Where a check is
new, this pass says which finding put it there; where a check is inherited unchanged, it
says so. The reviews are recorded in the findings log, which is held privately until the audit
request goes out, and disposed of in `FORGE_PUZZLE_V14.md`, which is published here.

Method: for each leaf, read the rue source top to bottom, list every `assert` and every
condition emitted, ask what an adversary controls in the solution and what they cannot,
and name the suite check that would go red if the assert were removed.
`scripts/mutate-v14.py` answers that question mechanically, by deleting each assert in
turn, rebuilding with rue, and re-running eleven suites — and V14 changes what its
verdict means. A line no suite reaches is **UNREACHED**, never "survived" and never
"redundant": R-2 was a line we had called redundant on the strength of a "survived"
verdict, and it was load-bearing. An unreached line now needs either a bracket-level
probe that reaches it or a written argument in `contracts/v14/mutation-arguments.json`,
and the run fails otherwise.

---

## 0. The frame: action layer, finalizer, reserves

**Upstream, pinned by hash.** `action.rue` (the CHIP-0050 action layer),
`finalizer.rue`, `slot.rue`, `p2_delegated_by_singleton.rue` are vendored from
Yakuhito/slot-machine at `2d37ba1`, recompiled by rue 0.8.4 and compared with the pins
in `pins.json` on every build. Their logic is not re-audited here beyond how Forge uses
it.

**The integrity suite verifies what it claims.** `_test_v14_integrity.py` (122 checks)
recompiles **every** puzzle in a scratch copy — the six leaves, the finalizer, the TAIL,
the registry leaves, the reserve launcher, the upstream pins, the curve exports — and
compares each shipped hex byte for byte against the fresh one. Without the compiler it
exits 2. V13's suite recompiled only the upstream pins and the curve exports and returned
0 without `rue` (fifth review).

**What the frame guarantees the leaves.** A spend names a list of leaves, a merkle proof
for each selector not yet proven in the spend, and a solution per leaf. The action layer
verifies each proof against the curried root, runs the leaves in order, threads
`(ephemeral . state)` from one to the next, and hands the finalizer the list of
condition lists and the final state.

**What the finalizer adds.** `forge_multi_reserve_finalizer.rue` — **Forge's own code,
not upstream's**, which the CHIP now says (revision 7) — re-creates the singleton, strips
`(-42 index . condition)` markers into per-reserve buckets, fails the spend on an index
outside `0..N-1`, asserts that the action layer's merkle root is the six-leaf root of
the one curried configuration (V13, M-4), and sends one mode-23 message per reserve,
every spend, to a receiver derived from the reserve's parent id in **state**, its
curried full puzzle hash and its *pre-spend* amount read from the truth. The successor
records the coins just messaged as the next parents (V12, P1). Unchanged in V14.

**What that means for an adversary.** They cannot run a leaf that is not in the tree,
change the root or the finalizer, assemble a pool whose leaves disagree, aim a
reserve's message at a coin that is not the current reserve, or make a reserve pay out
more than the leaves tagged.

Pinned: `_test_v14_finalizer.py` (42 checks) and, on a real coin store,
`scripts/sim-v14.py` (a decoy reserve that genuinely exists cannot stand in for the
real one; an impostor singleton at the pool's launcher cannot be spent).

## 1. The prologue (`forge_action_common.rue::prologue`)

Every leaf's first call. Two branches on `truth.ephemeral_state`.

**First action of a spend (`ephemeral_state is nil`).** Asserts, in order:

| Assert | What it closes | Pinned by |
|---|---|---|
| `valid_config(config)` | a pool whose curried config is out of bounds cannot move | mutation: UNREACHED, argued — the config is curried, so a different config is a different pool, and the registry refuses one at admission (`_test_v14_registry.py`) |
| `count(reserves) == n`, `count(fees_owed) == n`, `count(dao_owed) == n` | state lists sized to the asset count, so `nth`/`add_at` never run past the end silently | mutation: UNREACHED, argued — the registry's `eve_state_hash` fixes the shape at genesis and the finalizer rewrites it from the same lists; a mis-sized state is a different puzzle hash |
| `total_lp >= LOCKED_BURN` | a pool minted below the floor outside the registry cannot be spent at all, so it cannot trap a later depositor's LP (V13) | `_test_v14_actions.py` "a pool minted below LOCKED_BURN cannot be spent at all"; mutation: **killed** |
| `birth > oracle.last_height` | the successor is born after the block its predecessor's claimed height was checked against, so both credited intervals are non-negative (V13) | `_test_v14_oracle.py`, `_test_v14_review_corrections.py`; mutation: **killed** |
| `h >= birth` | the interval since this coin was born cannot be negative; with `ASSERT_MY_BIRTH_HEIGHT` it also makes a same-block successor — and, as V14 learned, a same-block *eve* — unspendable | `_test_v14_actions.py`, `_test_v14_consensus_timelocks.py`; mutation: **killed** |
| `dao_fee_bps` in `[0, MAX_DAO_FEE_BPS]`, zero when `dao_puzzle_hash` is zero | a fee with no recipient would be burned; the cap bounds the composed fee | `_test_v14_dao_fee.py`; mutation: **killed** |

Emits `ASSERT_MY_BIRTH_HEIGHT birth`, `ASSERT_HEIGHT_ABSOLUTE h`,
`ASSERT_BEFORE_HEIGHT_ABSOLUTE h + oracle_window`, `ASSERT_MY_AMOUNT 1`.

**The oracle** (V13, unchanged). State carries `last_spot`; the prologue credits two
intervals, `(last_height, birth]` at `last_spot` and `[birth, h]` at the spot on the
pre-spend reserves. Every block is credited once at the price in force.

**O-1, closed for the right reason this time.** V13's pass said a future-dated spend
"has `birth >= h`, so `h == birth`". That step was false — `birth` is the coin's
*creation* height, pinned by `ASSERT_MY_BIRTH_HEIGHT`, and naming a future `h` delays
inclusion without moving it. The conclusion survives on the accounting alone: this
spend credits `[birth, h]`, the next credits the tail `(h, inclusion]` at the spot this
one recorded. Measured in `_test_v14_review_corrections.py`: `h = birth + 5` is accepted
and the accumulator equals `last_spot × (birth − last_height) + spot × (h − birth)`
exactly; `h < birth` and `birth ≤ last_height` are refused.

**Later actions (`ephemeral_state == h`).** Only `assert ephemeral == h`. Mutation:
**killed**.

## 2. `swap`

Solution: `h, birth, asset_in, asset_out, gross_input, claimed_output,
settlement_parent, settlement_amount`.

Asserts distinct indices within `0..n-1`, `gross_input > 0`, `claimed_output > 0`, and
`exact_swap_output(...)` — the curve *bracket*, exact on both sides (moved from V10
byte-identical, `_test_v14_curve_equivalence.py`, 6,043 checks). Two fee slices, each
floored on its own. State: `reserves[in] += gross`, `reserves[out] −= claimed`,
`fees_owed[out] += protocol_fee`, `dao_owed[out] += dao_fee`.

**Emits the settlement binding (V14) and the payout.** The binding is two conditions
from `forge_action_common::settlement_binding`:

```
assert settlement_amount >= gross_input;
id = coinid(settlement_parent, settlement_puzzle_hash(asset_in), settlement_amount)
ASSERT_PUZZLE_ANNOUNCEMENT sha256(settlement_puzzle_hash(asset_in) + tree_hash((id . nil)))
ASSERT_CONCURRENT_SPEND id
```

then a tagged `CREATE_COIN` for reserve `out` paying `claimed − protocol_fee − dao_fee`
to the offer settlement puzzle.

**What changed, and why it is not cosmetic.** V13 took `settlement_coin_id` from the
solution and asserted only the puzzle announcement. The announcement's nonce is whatever
the *solver* wrote into the settlement's solution, so it bound the asset and never the
amount; the value flow was guarded by the CAT ring and bundle conservation — sound, and
entirely outside the puzzle. `_test_v14_before_after.py` shows the consequence on V13: a
swap naming a one-mojo settlement as its 250,000 input is **accepted** when another coin
in the bundle supplies the value. A V14 leaf derives the id from the parent and amount it
is given; a coin id commits to both, so the coin that satisfies
`ASSERT_CONCURRENT_SPEND` holds exactly the amount named, and the inequality ties that
amount to the action. The amount bound is the settlement's own, because the router
carves its fee out of the same coin: `settlement.amount = gross + router_fee` on the
public lane, and a binding on `gross` would have refused every fee-paying swap — caught
in the simulator before the build.

Pinned, with every attack bundle value-balanced by a plain coin so only the puzzle can
refuse (`_test_v14_settlement_amount.py`, 12/12; `_test_v14_action_binding.py`, 11/11):
a one-mojo settlement named with a 250,000 amount, a different parent, and an XCH coin
named for a CAT leg are each `ASSERT_CONCURRENT_SPEND_FAILED`; a settlement short of the
input is refused by the leaf before consensus sees it; a one-mojo settlement *can* make
the puzzle announcement for a 250,000 coin's id (the nonce route binds nothing);
`OFFER_MOD` makes no coin announcement (that route cannot bind).

**Two swaps naming one settlement: corrected 2026-09-16.** An assertion is not consumed, so
one coin satisfies both leaves' `ASSERT_CONCURRENT_SPEND`. Conservation decides the pair --
and conservation is a property of the whole bundle, not of an action. With nothing spare it
refuses (`MINTING_COIN`, pushed at a live pool by `scripts/v14-slack-probe.py`). With slack
it does not: on H6 at 4,693,721 the pair confirmed, the second swap funded out of the
bundle's 5 XCH network fee, the fee actually paid coming to 4,995,000,000 and the pool
receiving full value for both swaps. So the leaf binds **which coin and its amount**; it
does not make a settlement single-use, and "refused by conservation" holds only where the
bundle has no slack. `_test_v14_action_binding.py` carries both halves.

Mutation: `settlement_amount >= at_least` is **killed**; the zero-parent and
positive-amount guards are unreached and argued — a zero parent derives an id no coin
has, and a non-positive amount fails the inequality first.

**The four index and sign asserts stay UNREACHED, and stay.** Deleting each leaves the
suites green because the curve is unsatisfiable for a non-positive amount and an
out-of-range index fails the reserve lookup first. The argument is written down in
`mutation-arguments.json`, and they are kept because TibetSwap V2 was drained in August
2026 by exactly the gap they close. Only `asset_in != asset_out` is independently
**killed**.

## 3. `add`

Solution: `h, birth, deposits, lp_delta, lp_parent_id, settlement_parents,
settlement_amounts`.

Asserts `lp_delta > 0`, `deposit >= 0` per asset, `any_positive(deposits)`, and
`exact_invariant_lp_mint(...)`, the two-sided mint bracket with the imbalance fee.
`zip_add` fails unless `deposits` has exactly one entry per reserve. Emits, per
**positive** deposit, the settlement binding with `at_least = deposit` (a zero deposit
has no settlement and its two solution slots are ignored — the same branch that already
skipped its announcement), then the LP handshake to the mint eve.

**R-2: `assert deposit >= 0` is load-bearing, and V13's pass said the opposite.** On the
actions-suite pool, `deposits = [−100000, +500000]` satisfies the bracket at
`lp_delta = 36611` — a withdrawal of 100,000 wearing an `add`, which also mints LP. The
line is the only thing that refuses it. Our earlier search had run through
`forge_math.invariant_lp_mint`, whose wrapper refuses negative deposits before the
bracket is reached: we tested the guard we were trying to test, through a mirror carrying
the same guard. The vector is pinned in `_test_v14_actions.py` and
`_test_v14_second_review.py`, and the mutation run reports the line **killed**. The
mirror's guard is named in the second-review suite so this is not repeated.

`lp_delta > 0` and `any_positive` remain UNREACHED and argued: the bracket refuses a
non-positive mint and an empty deposit on its own. The bracket is exact both ways —
one LP less than the mirror's figure is refused as surely as one more
(`_test_v14_review_corrections.py`), correcting the architecture's `<=`.

## 4. `remove`

Solution: `h, birth, burn, lp_parent_id, payouts`.

Asserts `burn > 0` (**killed**) and `burn <= total_lp − LOCKED_BURN` (**killed**), then
`exact_withdrawal(...)` — payouts exactly pro-rata, floored per asset, with the vault fee
on a single-asset pool; one mojo under the share is refused as surely as one over.
`zip_sub` fails unless `payouts` has one entry per reserve. Emits a tagged payout per
positive amount and the LP handshake to the melt coin. No settlement, so no binding.

`LOCKED_BURN = 1` (V14). One unit, burned at genesis and verified by the registry, is
the register minimum, the remove cap and the prologue floor. Every unit not burned is
redeemable: on a real node the wallet holds exactly `total_lp − 1` and burns all of it
(`scripts/sim-v14.py`); burning the floor itself is refused by the leaf.

## 5. `observe`

Solution: `h, birth`. Unchanged from V13: creates the observation slot valued
`(h . cums)` and announces `["forge-observe-v1", h, cums]`. L-6 (slots are never spent
by a leaf) is recorded as a decision, not fixed.

## 6. `collect`

Solution: `h, birth, indices`. Unchanged from V13, including the single payout when the
protocol and DAO recipients coincide (M-1). Non-empty indices and `fee > 0 || dao > 0`
are **killed**.

## 7. `dao_fee`

Solution: `h, birth, new_bps`. Unchanged from V13; all three asserts **killed** by
`_test_v14_dao_fee.py` (37/37). See `FORGE_DAO_FEE_V14.md`.

## 8. The LP TAIL (`forge_lp_cat_tail.rue`)

Curried `(launcher_id, protocol_version)`; the tree hash is the LP asset id. Asserts
`protocol_version == 15`, so protocol-14 LP can never be authorized by a protocol-15
pool. The message tag is `forge-lp-v14`. Everything else is V13's: the genesis branch
asserts the launcher's announcement of `[pool_puzzle_hash, 1, [expected_delta,
cat_my_coin_id]]` with the eve's own id from the CAT truths (C-1); the message branch
requires the pool's mode-23 message; a melt needs a CAT parent
(`_test_v14_lp_receive_forgery.py`, `_test_v14_genesis.py`).

## 9. The reserve launcher (`forge_reserve_launcher.rue`) — new in V14

Solution: `created_puzzle_hash, amount, launcher_id`. No curried arguments, so its mod
hash is a constant the registry pins. Asserts `amount > 0` and emits exactly three
conditions:

```
ASSERT_MY_AMOUNT amount
CREATE_COIN created_puzzle_hash amount [launcher_id]
CREATE_COIN_ANNOUNCEMENT "forge-reserve-v14" + tree_hash([created_puzzle_hash, amount, launcher_id])
```

The whole coin becomes the reserve; nothing else is created. For a CAT reserve the
launcher is the inner of a CAT coin: the layer wraps `created_puzzle_hash` (the reserve's
`p2_delegated_by_singleton` inner) into the reserve's full hash and passes the
announcement through. The ASCII prefix matters: the CAT layer refuses an inner coin
announcement beginning `0xcb`, its ring marker, and a bare tree hash would hit that one
time in 256.

**Why a coin announcement from a derived id, and not something simpler** (spec §1.2,
all simulated against consensus before this was built). A puzzle announcement binds the
puzzle and never the coin. A coin announcement from an arbitrary coin binds the coin and
never the deed. A coin id commits to its puzzle hash — so if `register` derives the id,
the puzzle at that id is pinned too, and the puzzle does nothing but create the reserve.
Spending the eve at genesis instead cannot be built: `EPHEMERAL_RELATIVE_CONDITION`.

**Cannot:** create less than it announces (`ASSERT_MY_AMOUNT` and the id's amount
agree), create a different puzzle than it announces, or announce under another pool's
launcher id. Pinned in `_test_v14_reserves_proved.py` (21/21). Mutation: `amount > 0` is
argued — a zero-amount launcher has a coin id no `register` derives, since
`valid_pool` requires every reserve positive.

## 10. The registry (`forge_registry_init`, `forge_registry_register`)

A second CHIP-0050 singleton with the upstream default finalizer and no reserve. `init`
sets `initialized = 1` once. `register` takes a launcher parent id, the config, the
genesis reserves, `total_lp`, the opening `dao_fee_bps`, **the reserve grandparents**
(V14), the eve coin id and the two neighbour slots.

It asserts `valid_pool`, **derives the reserve parents** —

```
launcher_hash_i = RESERVE_LAUNCHER_HASH                      (XCH)
                = cat_puzzle_hash(asset_i, RESERVE_LAUNCHER_HASH) (CAT)
P_i             = coinid(grandparent_i, launcher_hash_i, reserves[i])
```

— rebuilds the pool's **exact** full puzzle hash from constants, config and those
parents, asserts the launcher's coin announcement of that hash with `[total_lp,
eve_coin_id]`, **asserts one coin announcement per reserve from `P_i`** that it created
`inner_hash_i` for `reserves[i]` under this launcher id, asserts the creation fee's
settlement announcement and the burn announcement (`LOCKED_BURN` to the zero puzzle
hash), spends the two neighbours by message and re-creates them around the new key, and
announces `forge-registered-v14`.

**R-1, the fourth review's finding.** V13's `register` took `reserve_parents` in its
solution and used it only to rebuild the pool's hash. Nothing in the bundle spent or
asserted a reserve; `valid_pool` read the *claimed* amounts. A registration naming
parents that named nothing was accepted, the slot taken, the pool coin created, and every
later spend — by anyone — failed message pairing against coins that did not exist. The
dilution defence assumed a squatter who funds `[1, 1]`; this one funded nothing.
Reproduced offline and on a real coin store before anything changed. Now the parents in
the eve state are launcher coins the bundle spent, and the launchers created the
reserves in the same transaction. `reserve_parents` left the solution: derived, not
claimed — the rule V12 applied to the pool's own solution, applied to the registry's.

`valid_pool` requires, beyond V10's config validation and the LP asset id tied to this
launcher: `total_lp > LOCKED_BURN`; the protocol fee recipient, price scale and oracle
window equal to the registry's constants; every reserve positive and one per asset.
**Mutation, V14:** the run first reported `assert valid_pool(...)` UNREACHED — the
registry suite had handed its squat and boundary pools another pool's neighbours, so the
key bracket refused them before `valid_pool` was consulted. Bracket-level probes were
added (each pool between its own neighbours; the protocol-recipient squat, a genesis of
exactly the floor, a DAO rate with no recipient), and the line is **killed**. That is
the UNREACHED rule doing what it is for.

**Cannot:** everything V13's registry refused (39 checks, now 43 in
`_test_v14_registry.py`), plus: register with a grandparent nobody answers to, with
honest launchers but another grandparent in the solution, with an imposter that
announces without creating, with a launcher that creates one mojo less, at another
puzzle, or under another launcher id, with only one of two reserves launched, or with the
V13 construction itself (`_test_v14_reserves_proved.py`). Against both builds:
`_test_v14_before_after.py`.

**Open, and argued rather than deferred.** No deregistration or expiry, as before. A
squatter who funds `[1, 1]` and registers 2 LP is diluted to nothing by the first real
deposit — 0.01 XCH against `[1, 1]` mints 7,073,534,532 LP, the figure V13's document
quoted beside the wrong deposit amount. Under V14 the squatter has at least funded the
reserves.

## 11. Cross-cutting

**Fee composition, intra-bundle manipulation, L-7, L-2:** as V13
(`_test_v14_payout_audit.py` 52/52, `_test_v14_manipulation.py` 14/14,
`_test_v14_multipool.py` 26/26, `_test_v14_route_lane.py` 136/136,
`_test_v14_offer_lane.py` 108/108). Every lane now names settlements by parent and
amount, including a hop whose settlement is the previous pool's payout and an entry coin
that fans out to children — the composer knows every parent and amount it creates.

**Asset scope.** A reserve behind a revocation (CHIP-0038) or fee (CHIP-0056) layer has
a full hash the registry never computes and a launcher announcement it never asserts.
That refusal was an accident of hash arithmetic; `_test_v14_asset_scope.py` (8/8) makes
it a test. Both layers stay out of scope (`FORGE_PROJECTS.md`).

**The replay beside the puzzles.** `forge_v14_resync.replay_spend` rebuilds a pool's state
from an on-chain spend, and it read the action layer's solution by zipping `puzzles` against
`solutions`. `puzzles` lists each distinct leaf once; `solutions` has one per action. A
spend that ran `swap` twice therefore replayed as one swap, producing a state that never
existed -- and the browser's repair path rests on this replay. Each action's leaf is now
resolved through its selector, and `_test_v14_replay.py` (9/9) pins the repeat case, the
mixed case, the order of three actions and the refusal of a malformed solution. Not a
puzzle defect; found by pushing a repeated leaf at a live pool.

**Two lanes, one bundle.** `_test_v14_lanes_agree.py` builds one pool from one set of
creator coins through the deploy lane and the website lane and requires the same
launcher coins, byte-identical announcements and byte-identical `register` solutions.
S2 lived exactly where two lanes differed and one was checked.

**Mutation results (2026-09-16).** 48 assertions across ten files: **21 killed, 27 unreached, 0 unbuildable**, every unreached line argued, exit 0. Every assertion V14 introduced or corrected is killed: the settlement binding's `settlement_amount >= at_least`, the registry's `valid_pool` (unreached in the first run because the suite handed its probes another pool's neighbours; killed once each pool was bracketed by its own), `remove`'s floor cap (unreached in the first run because the Python mirror's own guard raised first; killed with payouts sized by hand), the prologue's `dao_fee_bps >= 0` (killed by a pool minted with a negative rate in state) and `add`'s `deposit >= 0` (R-2). The unreached lines are the documented defence-in-depth twins in `swap` and `add`, the structural list-length checks, and the zero-id guards on values the puzzle now derives.

**Findings of this pass.** No assert without a pin or a written argument. O-1 closed on
the accounting rather than a false step; I-1 (R-2) corrected and pinned; the settlement
amount, which V13 never bound, bound; the reserve, which V13 never proved, proved.

## Where the rest lives

- [FORGE_PUZZLE_V14.md](FORGE_PUZZLE_V14.md) — the disposition of every fourth- and fifth-review finding.
- [FORGE_PUZZLE_V14_SPEC.md](FORGE_PUZZLE_V14_SPEC.md) — the specification the build followed, with the simulated routes.
- [FORGE_V14_ARCHITECTURE.md](FORGE_V14_ARCHITECTURE.md) — the coin topology and the off-chain shape.
- The findings log, including every external review — **private** until the audit request; not in this repository.
- [FORGE_DAO_FEE_V14.md](FORGE_DAO_FEE_V14.md) — the DAO-fee design as built, with its threat model checked off.
