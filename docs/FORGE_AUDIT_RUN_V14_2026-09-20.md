# Forge V14 audit run — 2026-09-20

A re-run of the runbook in `skills/n-asset-pool-audit/SKILL.md` against the shipping
revision, after the run it repeats (`FORGE_AUDIT_RUN_V14_2026-09-19.md`) and after further
trading against the deployed testnet pools.

**Verdict: no finding against the puzzles.** Every adversarial probe that refused before
refuses now, on a simulator and on testnet11, and every compiled artefact is bit-identical
to a fresh recompile of its source. Two findings against the audit's own tooling, both
fixed in this run. Three residuals carried forward unchanged.

**Who ran this.** Claude Opus 5, working from the runbook alone. The point of running the
same runbook under a different model is that a runbook is only as good as what it makes an
unfamiliar reader do, and the two findings here are both places where it let a reader
proceed on something unverifiable: a fingerprint nobody could recompute, and a failure
whose message did not distinguish a stale record from a missing reserve. Neither is a
defect in the puzzles. Both are defects in the audit, which is what a second pass is for.

The previous run was Claude (Opus 5, then Fable 5.1) with a QA pass by GPT-6 Astra; its
findings and theirs are dispositioned there, not re-litigated here.

## Revision under audit

| | |
|---|---|
| Revision | V14, protocol 15 |
| Artefacts | `contracts/v14`, 89 files |
| Fingerprint | `6298777ef1ac2ecf835b2323d9f6a8805c7ced1c9a8764d195f5b9960e147ab4` |
| Recompute it | `python scripts/revision-fingerprint.py` |
| Toolchain | Python 3.11.9, chia-blockchain 2.5.5, chia_rs 0.27.0 |
| Chain | testnet11, peak 4,710,642 |

The fingerprint is a different number from the one the 2026-09-19 record carries. That is
finding A-1 below rather than a change to the puzzle: the old number cannot be recomputed
by anyone, including here. What establishes that the revision has not moved is stronger
than a digest anyway — `_test_v14_integrity.py` recompiles every source and compares it to
the shipped hex (122/122), and `_test_v14_provenance.py` checks every artefact against the
manifest as git tracks it (86/86).

## Lanes

Rule 0.5: pick the lane that can answer the question.

| Lane | What ran | Result |
|---|---|---|
| Offline | 32 suites, `chia_rs.get_conditions_from_spendbundle` | 30 pass, 1 fail, 1 skip as found; **31 pass, 0 fail, 1 skip** after the stale record was resynced (the skip closes at 86/86 with `FORGE_REPO` set) |
| Offline totals | every check in those suites | 1322 pass / 42 fail as found; **1364 pass / 0 fail** after the resync |
| Integrity | `_test_v14_integrity.py` | 122/122 |
| Provenance | `_test_v14_provenance.py`, `FORGE_REPO` at a tracking clone | 86/86, exit 0 |
| Simulator | `scripts/sim-v14.py` | 72/72 |
| Simulator | `scripts/sim-v14-chip0062.py` | 22/22 |
| Testnet11 | `scripts/v14-chip0062-live-probe.py --pools 32` | 34/34 across all 32 pools |
| Mutation | `scripts/mutate-v14.py` | 48 assertions, 24 killed, 24 UNREACHED each with a written argument, 0 UNBUILDABLE, exit 0 — unchanged from the previous run |

### What the chain lane proved

M-2, the forgeable TWAP, went to a public testnet11 node as a chained bundle — the pool
spent and its own successor spent inside one transaction — and was refused twice over:

```
[PASS] claiming the block it would land in (4,710,717): testnet11 refuses the chained
       bundle  [EPHEMERAL_RELATIVE_CONDITION]
[PASS] claiming the current peak (4,710,716): the leaf itself refuses it
```

Nothing moved. An `observe` pays nobody and a refused push spends nothing, which is why
this probe is free to run against live pools.

L-4, the oracle crediting elapsed time exactly, was recomputed from the chain's own state
for all 32 pools: the previous generation read out of the puzzle reveal the node holds for
the parent coin, and the credit between generations recomputed from it. Every one agreed,
including the carry — blocks between a claimed height and the height the spend landed at
go into the NEXT spend rather than being discarded. 34 checks, 0 failures, 0 skips.

**A note on how this number was nearly got wrong.** The first run of this probe covered 8
pools, and this record briefly carried a "coverage note" attributing that to the stale
index below — a tidy explanation that fitted the story already in hand. It was wrong. The
probe takes `--pools`, which defaults to 8:

```python
ap.add_argument("--pools", type=int, default=8, help="how many pools to check for L-4")
for record in state["pools"][: args.pools]:
```

Re-run with `--pools 32` it covers every pool and matches the previous run exactly. The
lesson is in the runbook now as rule 10: run a tool at the coverage the audit claims, not
at whatever its default is, and never explain a number before checking what produced it.

### What the mutation run means

`scripts/mutate-v14.py` deletes each of the 48 assertions in the revision in turn,
rebuilds, and reruns the suites. 24 died — some suite noticed. The other 24 survived, and
the runbook is explicit that a survivor is UNREACHED and never "redundant": each one
carries a written argument for why no suite reaches it, and the run fails if any argument
is missing. None was, and the split is identical to the previous run, so guard coverage
has not regressed.

The arguments are mostly second-layer: `forge_reserve_launcher.rue:29`'s
`assert amount > 0` survives because `valid_pool`'s `all_positive` (which is killed)
already refuses a zero-amount reserve at registration, and because `coinid()` of a zero
amount derives a coin the registry never names. That is a real defence in depth rather
than dead code, but it is also exactly the kind of claim that decays quietly — which is
why it is written down and re-argued every run rather than assumed.

## The one failing suite, and why it is not a finding

`_test_v14_discoverability.py`: 322/364, 42 failures, every one the same shape —

> recorded reserve `5c29ea57…` (33,511,732,491) is a live hinted coin

The recorded reserve coin is not among the live hinted coins for its pool. Read cold, that
says a pool's reserves have gone missing, which is why it was worth settling rather than
waving through.

Checked directly against testnet11: of the 69 reserve coins the local deployment index
records, **38 are spent, 31 unspent, 0 missing**. All 38 exist on chain; they have simply
been traded. And every one of the 38 has a **live successor coin at the same reserve
puzzle hash** — so each spend was a pool transition, not a drain. No value left a pool.

The cause is that the checkout's index is behind the chain. A deployment's responder keeps
its own index, and a working copy that has not settled those trades itself never learns
they happened: `.awizard/deployment-index.json` still names the pre-trade generation. The
suite is right and the pools are right; the record is stale.
`contracts/forge_resync.py` is the remedy — it walks the singleton lineage to the unspent
tip and refuses unless the rebuilt state re-curries to the tip's on-chain puzzle hash.

That the failure needed a chain query to interpret is itself a defect, and it is A-2.

### Confirmed by repairing it

The disposition was then tested the only way it can be: the record was resynced and the
suite re-run.

```
python scripts/v14-resync-records.py     # 19 record(s) advanced, 0 could not be read
node scripts/import-v14-pools.mjs        # 32 batches, all at V15
python contracts/_test_v14_discoverability.py
  364/364 discoverability checks passed   (was 322/364)
```

Every one of the 42 failures cleared, and no puzzle changed — `forge_v14_resync` replays a
stale snapshot's spends forward and refuses unless the rebuilt state re-curries to the
tip's on-chain puzzle hash, so what it produced is what the chain already held. A
disposition that survives being acted on is worth more than one that only sounds right,
and this is the cheap case where acting on it was possible.

## A-1 — The revision fingerprint could not be recomputed

- **Risk Level:** Low (audit assurance; no fund path)
- **Asset Class Impacted:** none — tooling
- **Target Source Block:** `docs/FORGE_AUDIT_RUN_V14_2026-09-19.md`, the Revision table

### 1. Observed condition

The completion gate requires that a report "identifies the exact compiled revision
tested". The record identifies it with
`858ea64356943ff96eecb02d45888c195b246a362b15b66c8e6ab73eadd8090b`, described in prose as
"sha256 over every `.rue`, `.hex`, `.hash` and the manifest under `contracts/v14`,
sorted".

That sentence has several readings — over file bytes or over their digests, raw or
LF-normalised, including `pins.json` or not — and the recorded value reproduces under none
of the ones tried here. The string appears nowhere in the repository except the record
that quotes it, so there was nothing to check it against and no script that emits it. An
outside auditor, whom the runbook explicitly asks to "say which build you tested", could
not have produced or verified that number.

The runbook's own rule for its samples applies: *run what you write here before proposing
it.*

### 2. Fix

`scripts/revision-fingerprint.py`. The recipe is code rather than prose:

```
digest = sha256 of one line per file, sorted by path:
    <path relative to contracts/v14>:<sha256 of the file's LF-normalised bytes>
```

LF-normalised because the same tree checks out CRLF on Windows and LF elsewhere, and a
fingerprint that changes with the checkout says nothing about the puzzle. Per-file digests
rather than concatenated bytes, so a mismatch can name the file that moved — which is the
question anyone actually has when two fingerprints disagree.

```bash
python scripts/revision-fingerprint.py                     # the digest
python scripts/revision-fingerprint.py --files             # and every file's line
python scripts/revision-fingerprint.py --expect <hex>      # exit 1 on mismatch
```

Verified deterministic across runs; `--expect` exits 0 on a match and 1 on a mismatch.

This is provenance, not integrity — it says the artefacts are the same bytes as last time,
not that they match their sources. The integrity and provenance suites answer the second
question, and both pass.

## A-2 — A stale record and a missing reserve failed identically

- **Risk Level:** Low (audit assurance; no fund path)
- **Asset Class Impacted:** none — tooling
- **Target Source Block:** `contracts/_test_v14_discoverability.py`, the recorded-reserve check

### 1. Observed condition

The check was called with no detail argument, so both of these printed the same line:

- the recorded generation was spent by an ordinary trade and the index has not caught up —
  a fact about the record, not the pool;
- a recorded reserve the chain never had — which would be serious.

Forty-two failures of the first kind are indistinguishable at a glance from one of the
second. Settling which had happened took a direct chain query per coin, in a suite whose
entire purpose is to compare the record against the chain and which already held the data
needed to tell them apart.

### 2. Fix

The failure now names the cause and the remedy, from `others`, which already carries the
spent flag:

```
[FAIL] recorded reserve 5c29ea57… (33511732491) is a live hinted coin
       this generation was SPENT on chain; the record is behind the chain
       (the live coin at this puzzle hash holds 90327801867)
       — resync with contracts/forge_resync.py
```

and, for the case that would matter:

```
       no coin at this puzzle hash and amount was ever hinted here, spent or unspent
       — this is NOT ordinary index drift
```

Re-run after the change: all 42 failures report the drift cause and **0** report the
serious one. The suite still fails, correctly — the record really is stale — but it now
fails legibly.

## Residuals, carried forward

Unchanged from the CHIP-0062 disposition. None is a fund-loss path and none is reachable
by a third party against someone else's pool.

- **L-3.** `dao_puzzle_hash` is in the registry's uniqueness key and unconstrained when
  `dao_fee_bps` is zero, so two economically identical pools can mint two keys. One clause
  in `valid_pool` closes it, reaching the registry alone. Queued.
- **L-5.** `reserves[i] > 0` is enforced by the registry, not the prologue. A zero
  denominator reserve divides by zero in the prologue; a zero numerator reserve is accepted
  by `observe` and records a spot price of zero. Both creator-self-inflicted on an
  unregistered pool. Pinned in `_test_v14_audit_qa.py`.
- **L-6.** Observation slots are write-only: no leaf sends the message `upstream/slot.rue`
  requires, so every slot is permanent amount-0 dust. A spender is designed
  (`FORGE_ORACLE_SLOT_SPEC.md`) and not built.

## Completion gate

| Requirement | Status |
|---|---|
| The exact compiled revision tested is identified | `contracts/v14`, 89 files, fingerprint `6298777e…47ab4`, recomputable with `scripts/revision-fingerprint.py` |
| The honest control passes in every lane | offline 1364 checks pass, 0 fail, after the resync; integrity 122/122; provenance 86/86; simulator 72/72 and 22/22; testnet11 34/34 across all 32 pools |
| Each finding has a reproducible probe, or is marked provisional | A-1 reproduced by running the fingerprint tool; A-2 by re-running the suite; the 42 failures reproduced by direct chain query (38 spent, 38 with live successors) |
| The fix is recompiled | no puzzle fix was needed; integrity 122/122 says no hash moved |
| Positive and negative regression cases rerun after the fixes | discoverability re-run after A-2: same 322/364, all 42 classified as drift and 0 as serious; after the resync, 364/364 and the whole offline lane re-run at 1364/0; provenance re-run with `FORGE_REPO`: 86/86, exit 0 |

## Re-running this

```bash
cd contracts && for t in _test_v14_*.py; do python "$t"; done   # exit 0 pass, 1 fail, 2 skip
FORGE_REPO=<a clone tracking projects/chia-cfmm> python contracts/_test_v14_provenance.py
python scripts/revision-fingerprint.py
python scripts/sim-v14.py
python scripts/sim-v14-chip0062.py
python scripts/v14-chip0062-live-probe.py
python scripts/mutate-v14.py
```

The discoverability suite compares the local record against the chain, so it fails on any
working copy whose index is behind. Resync first, or read its failures as the report on
the record that they are.
