# Audit run — Forge V14, 2026-09-19

The runbook in [`skills/n-asset-pool-audit/SKILL.md`](../skills/n-asset-pool-audit/SKILL.md),
executed against this repository as it stands — from a clone of the public tree, not from
the private monorepo it is sliced out of — so that what is recorded here is what an outside
auditor gets by running the same commands. It is the runbook's first execution against the
published tree, and the first thing it found was in the tooling, not the puzzles.

**Verdict: no new finding against the puzzles.** Seven findings against the published
repository's own test tooling, documents and mutation arguments — five from the baseline
run and two from an independent QA pass by a second model — all fixed in this run and
re-verified on two versions of the Chia stack. Three residuals already known from the
CHIP-0062 disposition, restated below rather than rounded to closed.

**Who ran what.** The baseline run and its fixes: Claude (Opus 5, then Fable 5.1). The QA
pass, in the runbook's simulator-and-QA role: GPT-6 Astra (Copilot), working from a clone
of this repository with a *newer* Chia install than the baseline — which is what found
QA-1. Its results and corrections are merged below rather than appended, and the record
says where they changed something.

## Revision under audit

| | |
|---|---|
| Repository | `awizardxch/forge-puzzles`, `main` @ `5f26138ccef8190a23e6d99b4a7a5a86e781275f` (2026-09-16) |
| Puzzles | `contracts/v14` — protocol 15, live on testnet11 with 32 pools since 2026-09-16 |
| Compiled by | rue 0.8.4, `contracts/v14/compiled/manifest.json` built 2026-09-16T06:40:11Z, 41 outputs |
| Fingerprint | sha256 over every `.rue`, `.hex`, `.hash` and the manifest under `contracts/v14`, sorted: `858ea64356943ff96eecb02d45888c195b246a362b15b66c8e6ab73eadd8090b` |
| Toolchain, baseline | Python 3.11.9, chia-blockchain 2.5.5, chia_rs 0.27.0 |
| Toolchain, QA pass | Python 3.11.4, chia-blockchain 2.7.4, chia_rs 0.48.0, pytest 8.4.2 — deliberately kept different after QA-1 |
| Chain | testnet11: peak 4,706,461 when the run began, 4,706,559 when the chain lane ran |

The fingerprint was taken before and after the sync that published the runbook and the
fixes below, and is identical: the sync touched suites, a driver and documents, never a
puzzle. The integrity suite says the same thing from the other direction — every shipped
hex equals a fresh recompile of its source (`_test_v14_integrity.py`, 122/122) and every
artefact matches the manifest (`_test_v14_provenance.py`, 86/86, checked against git in
this clone).

## Lanes

The runbook's Rule 0.5: pick the lane that can answer the question.

### Offline — `chia_rs.get_conditions_from_spendbundle`

32 suites in `contracts/`: **28 passed, 0 failed, 4 skipped with exit 2.**

| Suite | Result |
|---|---|
| `_test_v14_actions.py` | 89/89 |
| `_test_v14_action_binding.py` | 13/13 |
| `_test_v14_asset_scope.py` | 8/8 |
| `_test_v14_create.py` | 22/22 |
| `_test_v14_curve_equivalence.py` | 6043/6043 |
| `_test_v14_dao_fee.py` | 38/38 |
| `_test_v14_finalizer.py` | 42/42 |
| `_test_v14_genesis.py` | all passed |
| `_test_v14_integrity.py` | 122/122 |
| `_test_v14_lanes_agree.py` | 10/10 |
| `_test_v14_lp_receive_forgery.py` | all passed |
| `_test_v14_manipulation.py` | 14/14 |
| `_test_v14_message_binding.py` | all passed |
| `_test_v14_multipool.py` | 26/26 |
| `_test_v14_offer_lane.py` | 108/108 |
| `_test_v14_oracle.py` | all passed |
| `_test_v14_payout_audit.py` | 52/52 |
| `_test_v14_provenance.py` | 86/86 |
| `_test_v14_registry.py` | 43/43 |
| `_test_v14_replay.py` | 9/9 |
| `_test_v14_reserves_proved.py` | 21/21 |
| `_test_v14_review_corrections.py` | 11/11 |
| `_test_v14_route_lane.py` | 150/150 |
| `_test_v14_second_review.py` | 15/15 |
| `_test_v14_settlement_amount.py` | 12/12 |
| `_test_v14_solution_widths.py` | 33/33 at first run; 49/49 after T-5 and QA-2 added their lanes — new in this run, see below |
| `_test_version_hygiene.py` | every crossing argued |
| `_test_mips.py` | 146 |
| `_test_v14_before_after.py` | skip: the V13 build is not published (was a crash — T-1 below) |
| `_test_v14_consensus_timelocks.py` | skip: the V11 control build is not published |
| `_test_v14_curve_mirror.py` | skip: needs the generated cases file from `run-checks.mjs` |
| `_test_v14_discoverability.py` | skip: needs the private deployment record |

The four skips are the runbook's caveat for an outside reader made concrete: retired
revisions and the deployment record are not published, so the "before" half of every
before-and-after suite, and every suite that reads live pool records, runs from the
private tree only. A skip that exits 2 is the honest way to say so; one of them did not
(T-1).

### Simulator — `chia._tests.util.spend_sim`

`scripts/sim-v14.py`: **72/72** on a real mempool manager and coin store. Registry
genesis; six pools including a single-asset vault and a pool whose reserve is another
pool's LP; observe, swap, add, remove and collect through the production creation lane;
the locked floor on a real node; the birth-height lock judged by the node (an overstated
birth refused `ASSERT_MY_BIRTH_HEIGHT_FAILED`, a future height refused
`ASSERT_HEIGHT_ABSOLUTE_FAILED`, a borrowed birth that is genuinely another coin's
refused); a decoy reserve that really exists on chain refused
(`MESSAGE_NOT_SENT_OR_RECEIVED`); an impostor singleton that really exists on chain
refused (`ASSERT_MY_PARENT_ID_FAILED`).

`scripts/sim-v14-chip0062.py`, published with this run so the lane is reproducible from
this tree: the chained-bundle TWAP forgery (CHIP-0062 audit M-2) refused by the node as
`EPHEMERAL_RELATIVE_CONDITION` for every birth height an attacker could claim; a second
genesis eve appended to a real creation bundle refused; the observation slot shown to be
unspendable by construction (L-6); a thinly funded market-key squat shown to be a live
market a third party can deepen (M-3). **22/22** in this clone: the chained bundle refused for a claimed birth of 14 and of 15 (`EPHEMERAL_RELATIVE_CONDITION` both times), the second eve refused `ASSERT_ANNOUNCE_CONSUMED_FAILED`, the slot refused `MESSAGE_NOT_SENT_OR_RECEIVED` alone and beside a pool spend, the squat deepened 50x by a third party.

### Chain — testnet11

Run against the 32 deployed pools at **peak 4,706,559**, from the private tree, because
the chain lane builds against the deployment record and that record is not published.
The commands are `scripts/v14-chip0062-live-probe.py --pools 32` and
`contracts/_test_v14_discoverability.py`; what they did is reproducible by anyone who
holds a record of the same pools, and the bundles pushed are refusals, so nothing moved.

- **M-2 at the public node.** The pool spent twice inside one bundle, the successor
  claiming the block the bundle would land in (4,706,562): refused,
  `EPHEMERAL_RELATIVE_CONDITION`. The claim of the current peak is refused by the leaf
  itself, before the node is asked. 2/2.
- **L-4 against the chain's own state.** For every one of the 32 pools, the previous
  generation's state was read out of the puzzle reveal the node holds for the parent coin,
  and the credit between the two generations recomputed: `last_spot × (birth −
  last_height)` plus `spot × (h − birth)`, with the parent's confirmed height as the
  birth. Equal on all 32. The gap between each claimed height and its inclusion block —
  three to eleven blocks — is carried into the next spend, not discarded. 32/32.
- **Discoverability.** Every pool coin, every reserve, every LP coin and every registry
  slot found on chain by hint, lineages unbroken across every generation, LP supply
  reconciled to `total_lp` on every pool, every registration's configuration readable
  from the registry's own spends. **364/364.**

Together with the simulator this is the lane the runbook says a chained-bundle finding
needs: a validator cannot judge it, a node can, and this is the node that holds the
pools people are trading.

### Adversarial widths — the test the CHIP-0062 audit asked for

`_test_v14_solution_widths.py`, new in this run: the solution-supplied `Bytes32` fields
in the table below fed values of 0, 1, 4, 31, 33 and 64 bytes, with the honest 32-byte
control beside each. Four fields across three puzzles — not every solution field the
design has, and the suite's own header says which.

| Puzzle | Field | Control | Widths refused |
|---|---|---|---|
| `remove` leaf | `lp_parent_id` | accepted | 6/6 |
| `remove` leaf | `burn` encoded non-canonically (`0x0003e8` for `0x03e8`) | — | refused |
| `swap` leaf | settlement parent | accepted | 6/6 |
| LP TAIL | `pool_inner_puzzle_hash` | accepted | 6/6 |
| LP TAIL | `next_state_root` | accepted | 6/6 |

No length is asserted in these fields, and the refusal mechanism is field-dependent —
a distinction the QA pass drew and the baseline had blurred. The swap's settlement parent
fails in the **native `coinid` operator**, which hard-rejects any operand that is not 32
bytes (`Invalid Parent Coin ID, not 32 bytes`) before any hash is compared. The other
three feed a hand-written sha256 preimage whose result is compared against something
consensus committed — a coin id the bundle must spend or an announcement a coin must make
— so a wrong width names a coin that exists nowhere and fails on message pairing. The
suite pins the refusal for the covered fields; it does not claim explicit length checks
are unnecessary everywhere, and it fails the day a derived hash is compared against
anything consensus did not commit.

### Mutation — `scripts/mutate-v14.py`

48 assertions across 10 leaf and library files, each deleted in turn, the leaf rebuilt
with rue in a temporary copy, and the suites that exercise it re-run.

**24/48 killed, 24 unreached, 0 unbuildable; every unreached line carries a written
argument; exit 0.** This is the run after T-5's probes were added. The first run of this
audit reported 21 killed and 27 unreached, with three of the four zero-parent asserts
among the survivors on an argument that held only on chain.

| File | Killed | Unreached (argued) |
|---|---|---|
| `forge_registry_register.rue` | 5 | 0 |
| `forge_action_collect.rue` | 2 | 0 |
| `forge_action_dao_fee.rue` | 2 | 1 |
| `forge_action_remove.rue` | 2 | 1 |
| `forge_action_common.rue` | 10 | 11 |
| `forge_action_add.rue` | 1 | 2 |
| `forge_action_swap.rue` | 1 | 4 |
| `forge_registry_common.rue` | 1 | 4 |
| `forge_reserve_launcher.rue` | 0 | 1 |

What the killed set contains is the point of the exercise, not the ratio: every
authorisation and accounting line that can be reached by a solution is reached by a
probe -- the birth-height pair (`birth > last_height`, `h >= birth`), the locked floor,
the settlement amount binding, the DAO rate bounds, the registry's key ordering and
`valid_pool`, and now all three zero-parent asserts that are a coin's only offline
refusal. The unreached set is of three kinds, each argued beside its line in
`contracts/v14/mutation-arguments.json`: **structural** length checks on lists whose
shapes the registry pinned at genesis and the finalizer rewrites (`right is nil`,
`weights is nil`, the three `count_ints`); **second layers** the curve makes unreachable
(`gross_input > 0`, `claimed_output > 0`, `lp_delta > 0`, `payout == 0`, an out-of-range
asset index failing `nth_int` first); and one **second layer that holds in every lane**,
`lp_parent_id != zero_bytes32()` (`forge_action_common.rue:474`), because a melt coin
with a zero parent cannot carry a CAT lineage and the TAIL's melt lock -- itself killed
-- refuses it first. T-5 below is the story of the other three.

A survivor is never called redundant here. The tool fails the run if any unreached line
lacks an argument, and this audit found that an argument can be true in one lane and
false in another -- which is why three of them became probes instead.

## Findings

None against the puzzles. Five against the published repository's tooling, found by
running the runbook where an outside auditor stands. They are recorded in the runbook's
format because that is what the runbook asks for, and because the same rule applies to a
test as to a puzzle: a claim is worth what its execution is worth.

### T-1 — A skipped suite crashed instead of skipping

- **Risk:** none to funds; misleads a reader
- **Source:** `contracts/_test_v14_before_after.py`, the import of `_v13_testkit`
- **Observed:** in the public clone the module is absent (retired revisions are pruned
  from the slice), so the suite died with `ModuleNotFoundError` and exit 1. To the first
  outside auditor running the suites, that reads as a failing test on the shipping
  revision. It is the opposite: a control that has nothing to control against.
- **Demonstrated:** first run of this audit, exit 1 with a traceback.
- **Fixed:** the import is guarded; the suite prints why it cannot run here and exits 2.
  Re-run: exit 2. In the private tree it still runs, 13/13.
- **Cost:** a suite, not a puzzle. No hash moves.

### T-2 — A skip named the wrong build

- **Risk:** none; misdirects the fix
- **Source:** `contracts/_test_v14_consensus_timelocks.py`
- **Observed:** the skip message said to run `scripts/build-v14.py`, twice. The build it
  needs is V11's, the control, which the public tree does not carry.
- **Fixed:** the message says which build is absent and why.

### T-3 — The link checker, run in the public clone, reported 17 true citations as broken

- **Risk:** none to funds; a checker that cries wolf is a checker nobody runs
- **Source:** `scripts/check-doc-links.py`
- **Observed:** `--publish-only` in the public clone reported 18 unresolved references.
  17 were paths into the interface repository (`api/`, `src/`) that the documents citing
  them declare as such; the declaration was honoured by the slice-gap check but not by the
  existence check, and in the public clone those paths exist nowhere. The 18th was
  `SKILL.md → docs/FORGE_SECURITY.md`: the file exists under exactly that name here, but
  the checker mapped it to its monorepo source name first and looked for that.
- **Fixed, in two rounds.** First: the interface declaration is honoured before the
  existence check, and a cited name that exists on disk is used as written and aliased
  only when it does not. That cleared the 17 and the existence half of the 18th -- and
  the re-run in the clone then reported the same file as a *slice gap*, because the
  publish check did not know a renamed-on-publish file is published. Second: it does. The
  monorepo run passed after round one; only the clone caught round two, which is the point
  of running the runbook where the reader stands. Re-run after both: 0 unresolved, exit 0,
  in the clone and the monorepo.

### T-4 — A published document linked a retired revision's source

- **Risk:** none to funds; a dead link in the audit-facing tree
- **Source:** `docs/FORGE_AUDIT_TIBETSWAP.md`, line 49
- **Observed:** the LP action coin's derivation was cited at line 439 of the *V13* copy
  of `forge_action_common.rue`. V13 is retired and pruned from this repository; the link
  resolved nowhere an outside reader could follow. (The checker flagged this very
  paragraph too, when it first quoted the dead path as a citation -- which is the checker
  working.)
- **Fixed:** the citation names V14's `lp_action_coin_id` at
  `contracts/v14/puzzles/forge_action_common.rue:473`, and says the earlier line went
  with its revision.

### T-5 — Four mutation arguments claimed a second layer that exists only on chain

- **Risk:** none to funds; a mutation argument that is true in one lane and false in
  another is the runbook's Rule 0.5 failing inside the evidence itself
- **Source:** `contracts/v14/mutation-arguments.json`, the entries for
  `launcher_parent_id != zero_bytes32()` (`forge_registry_register.rue:73`),
  `grandparent != zero_bytes32()` (`forge_registry_common.rue:156`),
  `lp_parent_id != zero_bytes32()` and `settlement_parent != zero_bytes32()`
  (`forge_action_common.rue:474`, `:440`)
- **Observed:** the first mutation run reported all four UNREACHED, each with the
  recorded argument that a zero parent derives a coin which cannot exist, "so its
  announcement is never made -- second layer". Probed rather than believed: a mutant with
  line 73 deleted was built in a temporary copy and given a registration whose launcher
  claims parent `0x00..00`. **The offline validator accepted it.** The bundle fabricates
  the zero-parent launcher and so makes the announcement itself; only a node, which
  knows no such coin exists, refuses. The argument is true on chain and false offline. In
  the offline lane the assert is not a second layer -- it is the only one.
- **Demonstrated:** against the shipped build the same registration is refused by the
  leaf (`clvm raise`, line 73); against the mutant, `get_conditions_from_spendbundle`
  returns conditions with no error.
- **Fixed:** the four arguments now say which lane they hold in. More usefully, the
  runbook's preferred answer -- write the probe -- was taken: `_test_v14_solution_widths.py`
  feeds the zero hash to every solution-supplied `Bytes32` and gains a registry lane for
  the launcher parent and the reserve grandparent, and the mutation tool now runs that
  suite, so **three** of the four lines read KILLED rather than argued; the fourth,
  `lp_parent_id`, stays UNREACHED with its CAT-lineage argument, as the mutation section
  says. (The baseline text of this bullet said all four; the QA pass caught the
  inconsistency.) The mutation table above is the run after that change.
- **Cost:** a suite, a tool list and a JSON file. No hash moves.

### QA-1 — Five suites failed on a current Chia install

- **Risk:** none to funds; an outside auditor on today's dependencies would see five
  failing suites and could not tell them from findings
- **Source:** `forge_v14_driver.validate()`, which every suite reads refusals through; the
  time-lock suite's imports and its call
- **Observed (QA pass, chia-blockchain 2.7.4 / chia_rs 0.48.0):** `_test_v14_asset_scope`
  7/8, `_test_v14_reserves_proved` 14/21, `_test_v14_settlement_amount` 6/12,
  `_test_v14_message_binding` six failures, `_test_v14_consensus_timelocks` an import
  error. Every one a genuine refusal misread: chia_rs 0.48 raises
  `ValueError("ValidationError", 12, "validation error: ...")` — a flat three-tuple —
  where 0.27 raised a bare `TypeError: 12`, and the suites read the code off the end of
  the message. The time-lock suite imported two modules that 2.7 moved into `chia_rs`,
  and the Rust `check_time_locks` gained a fifth argument, `nowrap`, and returns an
  integer code.
- **Fixed:** at the one funnel. `validate()` normalises both shapes to a message ending
  in the numeric code, so no suite changed its parsing. The time-lock suite falls back
  to the 2.7 locations, passes `nowrap` exactly as the node does
  (`peak >= HARD_FORK2_HEIGHT`), and converts an integer result to `Err`. Both wordings of
  the native `coinid` refusal are recognised as the interpreter refusing.
- **Re-verified on both stacks, whole set:** chia 2.5.5 — 32 suites pass, 1 skips
  (provenance, which needs a git remote to check against); chia 2.7.4 — the same 32
  pass, the same 1 skips. Nothing else changed between the two.

### QA-2 — A broken probe could count as a refusal

- **Risk:** none to funds; audit assurance — a harness that never reaches the puzzle
  would pass forever
- **Source:** `refuses()` in `_test_v14_reserves_proved.py` and
  `_test_v14_settlement_amount.py`; `outcome()` in `_test_v14_solution_widths.py`
- **Observed (QA pass, by fault injection):** a callable raising
  `AttributeError("synthetic harness defect; no puzzle executed")` was reported by the
  reserve helper as a refusal, and a synthetic `TypeError` by the width helper as
  refused. The real probes in this run refused through real puzzle and consensus errors;
  the defect is that a future broken one would not be noticed. This is the runbook's
  "a failure for the wrong reason is not a fix" rule, turned on the helpers themselves.
- **Fixed:** one predicate in the driver, `refusal_reason()`. A refusal is consensus
  saying no (`Rejected`) or the interpreter saying no (a `ValueError` carrying a CLVM
  failure: `clvm raise`, `div with 0`, the `coinid` wordings, and the rest of the
  interpreter's vocabulary). Anything else — an `AttributeError`, a `TypeError` from a bad
  call, a Python `ValueError` — is BROKEN and fails the check. All three helpers use it.
  The fault injection is kept as a regression: `_test_v14_solution_widths.py` feeds its
  own `outcome()` a synthetic `AttributeError`, `TypeError` and Python `ValueError` and
  requires each to read BROKEN, and a `Rejected` and a `clvm raise` to read refused.

### What the QA pass added beside the findings

`_test_v14_audit_qa.py`, written by the QA pass and ported into the tree: on a real node,
each reserve launcher omitted, mis-targeted, mis-hinted or short by one mojo is refused
with the exact consensus code (`ASSERT_ANNOUNCE_CONSUMED_FAILED` four ways,
`ASSERT_MY_AMOUNT_FAILED` once) and the unchanged honest registration then confirms; L-3
reproduced — two zero-rate pools differing only in an inert DAO recipient register under
two keys; L-5 narrowed as above. 7/7 on both stacks. One of its assertions matched on
the wrong error text on arrival (`divmod|zero`, for an interpreter that says
`div with 0`) and was corrected in the port.

## Residuals, restated

From the CHIP-0062 audit disposition (2026-09-18). None is a fund-loss path and none is
reachable by a third party against someone else's pool, which is why V14 stays on chain
while the external audit runs; they are named here so this run cannot be read as "clean".

- **L-3.** `dao_puzzle_hash` is part of the registry's uniqueness key and is unconstrained
  when `dao_fee_bps` is zero, so two economically identical pools can mint two keys. A
  zero-rate pool can never raise its rate, so the field is economically inert; what it
  buys is near-duplicate listings, each of which must fund real reserves (V14's reserve
  proof). One clause in `valid_pool` closes it, reaching the registry alone. Queued.
- **L-5.** `reserves[i] > 0` is enforced by the registry, not the prologue. Narrowed by
  the QA pass, which ran the compiled leaf rather than reading it: a zero **denominator**
  reserve divides by zero in the prologue (`div with 0`); a zero **numerator** reserve is
  accepted by `observe` and records a spot price of zero. Both are creator-self-inflicted
  on an unregistered pool and unreachable through the registry, which enforces positive
  reserves; neither is an exploit of a registered pool. Pinned in
  `_test_v14_audit_qa.py`.
- **L-6.** Observation slots are write-only: no leaf sends the message `upstream/slot.rue`
  requires, so every slot is permanent amount-0 dust and the live consumer path is the
  same-bundle announcement. A spender is designed (folded into `observe`, not a seventh
  leaf) and not built.

## Completion gate

| Requirement | Status |
|---|---|
| The exact compiled revision tested is identified | `5f26138c`, fingerprint `858ea643…8090b`, 41 manifest hashes |
| The honest control passes in every lane | offline 32 suites pass on chia 2.5.5 and again on 2.7.4, simulator 72/72 and 22/22, QA 7/7, testnet11 34/34 and 364/364, every width sweep's control accepted |
| Each finding has a reproducible probe, or is marked provisional | T-1–T-4 reproduced by running the suites and the checker in this clone; residuals cite their suites |
| The fix is recompiled | no puzzle fix was needed; no hash moved (fingerprint identical) |
| Positive and negative regression cases rerun after the fixes | offline lane re-run in the clone after T-1..T-4: 28 passed, 0 failed, 4 skipped with exit 2 (T-1's crash now a skip); `_test_v14_solution_widths.py` 44/44 in the clone, with the zero-hash width case, the registry lane and the consistent zero-parent lane added for T-5; checker exit 0 in both modes; fingerprint unchanged |

## Re-running this

From a clone of this repository, with `rue` on `PATH` and a Python environment carrying
chia-blockchain 2.5.5:

```
cd contracts && for t in _test_v14_*.py; do python "$t"; done   # exit 0 pass, 1 fail, 2 skip
python scripts/sim-v14.py                                       # simulator lane
python scripts/sim-v14-chip0062.py                              # ephemeral chaining, genesis, slots
python contracts/_test_v14_solution_widths.py                  # adversarial widths
python scripts/mutate-v14.py                                    # every assert deleted in turn
python scripts/check-doc-links.py --publish-only               # every citation resolves here
```

A suite that exits 2 is telling you which half of the evidence lives in the private tree.
