# Forge puzzle V14 -- the fourth-review revision

Testnet research only; unaudited. Protocol 15 on chain.

On 2026-09-15 a fourth independent review of V13 (Chia-Network/chips#217, trgarrett)
landed two findings, and a fifth review of the published documents landed six more. Both
were reproduced from the reviewers' own parameters before anything was changed
(the fourth-review reproduction suite and the two review notes, both held in the private
repository with the retired V13 sources). This
document is the disposition of both: what each finding is against the code, what V14
changes, and what was measured rather than argued. The design decisions were settled in
the simulator first (`FORGE_PUZZLE_V14_SPEC.md`) and this is what was built from them.

**Why a major increment.** The registry's admission rule changes -- `register` derives
every reserve parent from a grandparent and a new puzzle, the reserve launcher -- and the
`swap` and `add` leaves change their solutions. Every pool puzzle hash moves, the V13
registry cannot admit a V14 pool, and R-1 is a demonstrated denial: a market key taken
forever for one creation fee. That is V14, protocol 15. V13's liquidity came out first
(sixteen drain batches, 2026-09-15), as V12's did.

## The findings, one line each

| # | Finding | True? | V14 |
|---|---|---|---|
| R-1 | `register` admits a pool whose `reserve_parents` name coins that do not exist; the slot is taken by a pool no one can ever spend, for one creation fee | **True.** Nothing in the registration bundle spent or asserted a reserve; `valid_pool` read only the claimed amounts. Reproduced offline and on a real coin store. | The reserve parent is **derived, not claimed**: `P_i = coinid(grandparent_i, RESERVE_LAUNCHER_HASH_i, reserves[i])`, and `register` asserts the launcher's coin announcement that it created the reserve. `reserve_parents` leaves the solution. §1 |
| R-2 | `add`'s `assert deposit >= 0` is load-bearing; V13's "does not reproduce" was wrong | **True.** `[-100000, +500000]` satisfies the mint bracket at `lp_delta = 36611`. Our search had run through a mirror carrying the same guard. | The line stays and is pinned (`_test_v14_actions.py`, `_test_v14_second_review.py`); the mutation run reports it **killed**. The method changes: a mutation verdict is UNREACHED, never "redundant", and an unreached line needs a probe or a written argument. §4 |
| 5-1 | The integrity suite recompiles only upstream pins and curve exports, and returns 0 without a compiler | **True.** | `_test_v14_integrity.py` recompiles **every** puzzle in a scratch copy and compares each shipped hex byte for byte; without `rue` it exits 2. §4 |
| 5-2 | The O-1 closure argument was wrong: a future-dated `h` does not force `h == birth` | **True** (the accounting was right, the argument was not). | Measured: `h = birth + 5` is accepted and the accumulator is `last_spot x (birth - last_height) + spot x (h - birth)` exactly. `_test_v14_review_corrections.py` |
| 5-3 | The architecture wrote `lp_delta <= mint` and `payout <= share`; the leaves enforce exact brackets | **True.** | One less is refused as surely as one more, on both leaves, measured. `_test_v14_review_corrections.py` |
| 5-4 | The CHIP said the finalizer was upstream's; it is Forge's own | **True.** | CHIP revision 7 names the multi-reserve finalizer as the code to read first. |
| 5-5 | Review provenance was unattributed | **True.** | Attribution table in the spec §4.4; `_test_v14_provenance.py` checks the manifest against the committed tree and every cited path. |
| 5-6 | Reserves behind a revocation (CHIP-0038) or fee (CHIP-0056) layer were refused by accident of hash arithmetic, not by a test | **True.** | `_test_v14_asset_scope.py` makes the refusal a test. Both layers stay out of scope (`FORGE_PROJECTS.md`). |
| -- | The input binding was half outside the puzzle: a leaf bound the settlement's asset and coin id, never its amount; only conservation guarded value | **True**, sound today, undocumented. Shown: V13 accepts a swap whose named settlement holds one mojo when the value arrives from another coin in the bundle. | Every `swap` and `add` names its settlement by **parent and amount**, derives the id, and asserts `ASSERT_CONCURRENT_SPEND` on it plus `amount >= input`. Both halves now hold. §2 |
| -- | `MIN_LOCKED_LP = 1000` locked a thousand units the puzzle never needed | Design choice, not a finding. | `LOCKED_BURN = 1`: one unit, burned at genesis, is the register minimum, the remove cap and the prologue floor. §3 |

Findings from earlier reviews that V14 carries unchanged: one configuration per pool
coin (M-4), the exact oracle (S3), the burned floor asserted by `register` (S2), the
registry's pinned protocol parameters (M-3/L-3), single-recipient `collect` (M-1),
genesis bound to one eve (C-1), reserve parents in state (L-1).

## 1. The reserve is proved to exist (R-1)

**The hole.** `register` took `reserve_parents` in its solution and used it in one place:
rebuilding the pool's puzzle hash. Nothing in the bundle spent a reserve, and none of the
three announcements it asserted (the launcher's, the fee's, the burn's) came from one. An
attacker registered with parents that named nothing; the slot was taken, the pool coin
created, and every later spend -- by anyone -- failed message pairing against coins that
did not exist. The dilution defence assumed a squatter who funds `[1, 1]`; this one
funded nothing.

**Why the obvious fixes fail** (all simulated before this was chosen; spec §1.2). A
settlement announcement binds the puzzle, never the coin. Spending the eve at genesis
cannot be built: `ASSERT_MY_BIRTH_HEIGHT` on a same-block coin is refused with
`EPHEMERAL_RELATIVE_CONDITION` -- a consensus rule we had recorded for successors and not
noticed applies to the eve. Dropping that assert at genesis puts a branch in the oracle's
one load-bearing condition. Two-phase registration leaves the squat window open until the
confirm.

**The fix.** A coin id commits to its puzzle hash. So if `register` derives the reserve's
parent, the puzzle at that parent is pinned too. A new puzzle, `forge_reserve_launcher`,
does one thing:

```
fn main(created_puzzle_hash, amount, launcher_id) -> List<Condition> {
    assert amount > 0;
    [
        AssertMyAmount { amount },
        CreateCoin { puzzle_hash: created_puzzle_hash, amount, memos: [launcher_id] },
        CreateCoinAnnouncement { message: "forge-reserve-v14" + tree_hash([created_puzzle_hash, amount, launcher_id]) },
    ]
}
```

`register` is given each reserve's **grandparent** and computes

```
P_i = coinid(grandparent_i, launcher_hash_i, reserves[i])
       where launcher_hash_i = RESERVE_LAUNCHER_HASH                    for XCH
                             = cat_puzzle_hash(asset_i, RESERVE_LAUNCHER_HASH)   for a CAT
AssertCoinAnnouncement { id: sha256(P_i + "forge-reserve-v14" + tree_hash([inner_hash_i, reserves[i], launcher_id])) }
```

and uses `P_i` as the reserve parent in the eve state it rebuilds. Any coin able to make
that announcement **is** a reserve launcher, because nothing else hashes to
`RESERVE_LAUNCHER_HASH`; and a reserve launcher that announced has created the reserve,
at the inner hash the registry computed (the CAT layer wraps it), for exactly
`reserves[i]`, hinted with this pool's launcher id. The registry pins
`RESERVE_LAUNCHER_HASH` in its curried constants.

The ASCII prefix on the message is not decoration: the CAT layer refuses an inner coin
announcement whose first byte is `0xcb` (its ring marker), and a bare tree hash would hit
that one time in 256.

**What it reduces to.** Registering a market requires funding its reserves. A squatter
may still register `[1, 1]`, and that is the case the dilution arithmetic already
answered -- with the units corrected: 0.01 XCH against `[1, 1]` at 1,001 LP mints
7,073,534,532 LP, not 10 XCH.

**Measured** (`_test_v14_reserves_proved.py`, 21/21): the honest registration is
accepted and creates both reserves hinted; a grandparent nobody answers to, an imposter
that announces without creating, a launcher creating one mojo less, at another puzzle,
or under another launcher id, one of two reserves launched, funded reserves recorded
under the wrong parent, and the V13 construction itself are each refused. The registered
pool then swaps, adds and removes, so the finalizer is messaging the launchers' children.
`_test_v14_before_after.py` runs the V13 construction against both builds: V13 accepted,
V14 `ASSERT_ANNOUNCE_CONSUMED_FAILED` — and `scripts/v14-squat-probe.py` pushed that construction
at the live V14 registry on testnet11, where the node refused it with the same code.

## 2. The input binding lives in the puzzle (spec §1.8)

**What was half outside.** A leaf asserted
`AssertPuzzleAnnouncement(sha256(settlement_puzzle_hash(asset) + tree_hash((coin_id, nil))))`,
which binds the asset and the id the *solver* wrote into the settlement's nonce, and says
nothing about the amount. The value flow was guarded by the CAT ring and bundle
conservation -- sound, and entirely outside the code a reviewer reads.
`_test_v14_before_after.py` shows the consequence on V13: a swap naming a one-mojo
settlement as its 250,000 input is **accepted** when another coin in the bundle supplies
the value.

**The fix.** The leaf takes the settlement's parent and amount, derives the id, and
asserts the coin itself:

```
export fn settlement_coin_id(asset_id, settlement_parent, settlement_amount) -> Bytes32 {
    assert settlement_parent != zero_bytes32();
    assert settlement_amount > 0;
    coinid(settlement_parent, settlement_puzzle_hash(asset_id), settlement_amount)
}
export fn settlement_binding(asset_id, settlement_parent, settlement_amount, at_least) -> List<Condition> {
    assert settlement_amount >= at_least;
    let id = settlement_coin_id(asset_id, settlement_parent, settlement_amount);
    [settlement_assert(asset_id, id), AssertConcurrentSpend { coin_id: id }]
}
```

`swap` binds with `at_least = gross_input`; `add` binds each positive deposit with
`at_least = deposit` and skips a zero deposit in the branch that already skipped its
announcement. `remove`, `observe`, `collect` and `dao_fee` carry no settlement and emit
nothing. The amount bound is the settlement's **own**: the router carves its fee out of
the same coin, so on the public lane `settlement.amount = gross + router_fee`, and a
binding on `gross` would have refused every fee-paying swap. Caught in the simulator
before the build, not on testnet.

**Measured** (`_test_v14_settlement_amount.py`, 12/12; `_test_v14_action_binding.py`,
11/11), with every attack bundle value-balanced so only the puzzle can refuse: a one-mojo
settlement named with a 250,000 amount, a different parent, and an XCH coin named for a
CAT leg are each `ASSERT_CONCURRENT_SPEND_FAILED`; a settlement short of the input is
refused by the leaf before consensus sees it. The two routes that do not bind are pinned:
a one-mojo settlement *can* make the puzzle announcement for a 250,000 coin's id (the
nonce is solver-chosen), and `OFFER_MOD` makes no coin announcement at all.

**A correction, measured on chain 2026-09-16.** We wrote here that two swaps in one spend
naming the same settlement are "refused by conservation". That is true only of a bundle
with **nothing spare in it**. An assertion is not consumed, so one coin satisfies both
leaves' `ASSERT_CONCURRENT_SPEND`; what decides the pair is conservation, and conservation
is a property of the **whole bundle**, not of an action. A network fee is something spare.
On pool H6 at height 4,693,721 exactly this pair confirmed: the bundle carried a 5 XCH fee,
the second swap was funded out of it, and the fee actually paid came to 4,995,000,000.
Nothing was stolen -- reserve 0 grew by the full 10,000,000, so the pool received full value
for both swaps and the trader overpaid -- but the published claim was wrong as written.
Both halves are now pinned: `scripts/v14-slack-probe.py` pushes the pair with a zero fee
(`MINTING_COIN` from the node) and with a fee (confirmed), and
`_test_v14_action_binding.py` carries both offline.

What the binding does guarantee is unchanged and worth stating exactly: **which coin, and
its amount**. What it does not guarantee is that a settlement pays for one action only.
Whether that matters depends on who supplies the slack; in a bundle a trader builds for
themselves the slack is their own fee, and they simply overpay. In an aggregated bundle it
would be another party's, which is why this is written down rather than left implicit.

**On the live chain:** `scripts/v14-settlement-probe.py` pushed the
one-mojo settlement named as 10,000,000 at pool H6, with conservation satisfied and the
announcement nonce forged; testnet11 refused it with `ASSERT_CONCURRENT_SPEND_FAILED`, and
the honest control with the same coins confirmed at 4,693,692.

## 3. One unit, burned (`LOCKED_BURN = 1`)

`MIN_LOCKED_LP = 1000` locked a thousand units for no property the puzzle needs. One
constant now does the four jobs: the genesis settlement burns `LOCKED_BURN` to the zero
puzzle hash and `register` asserts that group; `valid_pool` requires
`total_lp > LOCKED_BURN`; `remove` caps at `total_lp - LOCKED_BURN`; the prologue refuses
`total_lp < LOCKED_BURN`. Every unit not burned at genesis is redeemable, the pool
outlives every withdrawal, and the burned unit proves the genesis mint ran. Measured from
both sides on both builds in `_test_v14_before_after.py`, and on a real node in
`scripts/sim-v14.py`, where the wallet holds exactly `total_lp - 1` and burns all of it.

## 4. Method (R-2, 5-1)

**Mutation verdicts.** `scripts/mutate-v14.py` deletes each of 48 assertions across ten
files -- the six leaves, the common module, the registry leaves and the reserve launcher
-- rebuilds, and runs eleven suites. A line no suite reaches is reported **UNREACHED**,
never "survived" and never "redundant", and the run fails unless every unreached line has
either a bracket-level probe or a written argument in
`contracts/v14/mutation-arguments.json`. That is the rule R-2 taught: "survived" meant
"no probe reaches it", and we read it as "the bracket covers it".

**The integrity suite** recompiles every puzzle from source in a scratch copy and
compares each shipped hex byte for byte -- leaves, finalizer, TAIL, registry, reserve
launcher, the upstream pins, the curve exports. Without the compiler it exits 2. 122/122.

## 6. Three off-chain defects the second pass found

None is in a puzzle; all three are in code that ships beside them, and all three were
found by pushing shapes at live pools rather than by reading.

**`forge_v14_resync.replay_spend` under-replayed a repeated leaf.** It read the action
layer's solution by zipping `puzzles` against `solutions` -- but `puzzles` lists each
*distinct* leaf once while `solutions` has one entry per action, so a spend that ran `swap`
twice replayed as **one** swap and produced a state that never existed. The browser's
repair path depends on this: a pool behind the chain is resynced rather than refused, so a
wrong replay would hand the next spend a coin that does not exist. Found when H6's
double-settlement spend read back as a single swap. Each action's leaf is now resolved
through its selector (`selectors_and_proofs` is in reverse execution order; selectors run
2, 5, 11, ... over `puzzles` in order of first use), a mismatched selector count is refused
rather than half-replayed, and `_test_v14_replay.py` (9/9) pins it -- including the exact
shape that misread. Verified against the chain: the fixed replay reproduces H6's reserves
to the mojo.

**The ops builders trusted a peak that can lag.** `push_and_wait` returns as soon as the
successor coin record exists, but the node's reported peak can still be one block behind
it. A vault remove built straight after a confirmed add claimed `h = 4,693,823` for a coin
born at 4,693,824, and the prologue's `h >= birth` refused it locally (clvm raise 80).
`v14_ops.claim_height` now clamps every builder to `max(peak, birth)`. Claiming the birth
height is always legal, and understating `h` only defers oracle credit to the next spend --
nothing is lost. The puzzle was right; the builder was reading a stale number.

**The router sized a wrap's backing on money the router had already taken.** A route that
mints LP part-way through is funded twice over by the same offered XCH: the entry swap
spends some of it, and the rest has to back the LP the vault mints. `wrap_backing` split
the offer between those two, and it split the **gross** -- while `_Composer.feed` takes the
router's rate off the hub's whole amount first and then prorates what is left across the
declared shares. The backing therefore arrived short by the fee's share of it and the wrap
was refused. Live, on 2026-09-16: *"XCH in the offer (12068) does not cover the wrap's LP
backing (12124)"*.

Two fixes were wrong before the third was right, and both were wrong in a way the offline
suite could not see:

* Splitting `offered - fee` instead of `offered` does avoid the shortfall, but it declares
  the **net** as the entry, which the hub then nets a second time. The route fills about
  3% small and the difference comes back as change: no loss, a quote that does not mean
  what it says.
* Netting each share by its own percentage is a mojo out, because the hub prorates
  (`share * spendable // offered`) rather than taking a percentage of each share. One mojo
  short is refused exactly like a large shortfall.

`wrap_backing` now takes `fee_bps` and bisects for the largest declared entry whose backing
still covers the mint, evaluating both sides through the hub's own arithmetic. The two
declared shares add to the whole offer, and the mojo or two of proration dust leaves with
the trader's output rather than stopping at the router.

The offline suite passed **before and after** the first attempt, which is what proved it
was not covering this at all: its wrap case ran the router at **zero** bps, and at zero bps
gross and net are the same number. `_test_v14_route_lane.py` now carries the same route at
300 bps, asserts the declared shares against the hub's arithmetic, and fails without the
fix with the live refusal's own message.

The same defect sat in the quote the website shows. `assembleMultiHopQuote` iterated its
wrap fixed point on the gross entry too, so the interface quoted a wrap route roughly the
router's rate better than it could fill -- an over-quote, which is refused after signing
rather than costing a mojo. It now runs the fixed point on the declared shares and prices
each at what the hub leaves it.

## 5. Everything else that moves, mechanically

* `PROTOCOL_VERSION` 14 -> 15 in the TAIL, the registry, the driver, the API and the UI.
* `RegistryConstants` gains `reserve_launcher_mod_hash`; `make_registry` pins it.
* `register`'s solution: `reserve_grandparents` replaces `reserve_parents`.
* `swap`: `settlement_parent, settlement_amount` replace `settlement_coin_id`;
  `add`: `settlement_parents, settlement_amounts` replace `settlement_coin_ids`.
* The LP message tag is `forge-lp-v14`; the registration announcement is
  `forge-registered-v14`.
* Every creation lane -- deploy script, website (`forge_v14_create`), simulator -- creates
  a launcher per reserve and spends it in the registration bundle.
  `_test_v14_lanes_agree.py` requires the deploy and website lanes to produce the same
  launcher coins, byte-identical announcements and byte-identical `register` solutions.
* `forge_stdin` and `forge_resync` dispatch protocol 15 to the V14 lanes; V13 stays
  readable while its record exists.
* The publish slice ships `contracts/v14` and this document; V13 joins V12 as history.

## Order of work, as it happened

1. Simulations first (`_sim_v14_*`, fifteen files, all green) -- five R-1 routes, the
   settlement binding against every action shape, the layered-CAT scope.
2. `contracts/v14` from the retired V13 tree (not published here): `LOCKED_BURN`, protocol 15, the launcher,
   `register`'s derivation, the bindings in `swap` and `add`. Build; pins hold.
3. Driver, lanes, twenty ported suites and eight new ones, all green offline.
4. The simulator on an in-process node (`scripts/sim-v14.py`): 72 checks, all passed.
   The V13 simulator turned out to have been refused by every node since the genesis
   burn arrived; the offline suites built the burn themselves and never noticed. Fixed.
5. The mutation run under the UNREACHED rule.
6. V14 registry (height 4,692,794) and the 32-pool matrix (4,692,825 – 4,692,961) on
   testnet11, then the lifecycle matrix: 32 adds, 29 swaps, 25 collects, 6 removes,
   3 observes, one multi-hop — 130 confirmed transactions, 2026-09-16 — and a real swap
   offer through the website's keyless responder, settled at 4,693,502.
7. A second pass over the shapes the first did not reach: reverse swaps (a CAT settlement
   in, XCH out, so the derived id goes through `cat_puzzle_hash`), single-sided adds, vault
   adds and removes, wrapper swaps -- plus the refusal probes at live pools. That pass is
   what found the first two off-chain defects in section 6.
8. The two route shapes that had only ever run offline, settled on chain. An **LP burn in
   the middle of a trade** at 4,694,251: one bundle in which a vault ran `forge_action_remove`
   and the pool holding its LP ran `forge_action_swap`. An **LP mint in the middle of a
   trade** at 4,694,314: one bundle in which a swap pool ran `forge_action_swap`, the vault
   it feeds ran `forge_action_add`, and the pool holding that vault's LP ran
   `forge_action_swap` again -- the entry asset crossing a vault as freshly minted LP and
   leaving as XCH, atomically. The mint half is what found the third defect in section 6;
   it could not settle until that was fixed.
