---
name: chia-n-asset-pool-auditor-v2
description: "Advanced runbook for auditing Chia multi-asset liquidity-pool puzzles in Chialisp or CLVM, validating compiled puzzles locally, preparing Testnet11 proofs, and coordinating Fable 5.1, Astra, and Opus workflows. Use when the user says Audit this Chia puzzle, Review n-asset pool CLVM, Check liquidity pool inner puzzle for exploits, Validate this puzzle on the Chia Simulator, or Generate a Testnet11 spend bundle proof for this vulnerability."
---

# Chia Puzzle N-Asset Pool Auditor & Proof-of-Concept Engine

A runbook for structural security audits of N-asset liquidity pools on Chia. It is
written to be loaded by a model -- Claude, Copilot, or anything else that reads
markdown -- and to be argued with. The active architecture is Standard CATs (CAT2) and
native XCH; treat rCATs and NFT collections as roadmap threat models unless the target
implements them.

**It lives in this repository on purpose.** The method that finds defects in these
puzzles should be as public as the puzzles, and as open to correction. Everything below
was written after a defect it would have caught, or after one it missed; where a rule
exists because we got something wrong, the mistake is named rather than tidied away. If a
rule here is wrong, or your own audit needed something this does not say, open an issue or
a pull request -- a better runbook is worth more to this project than a quiet fix.

Paths in this file refer to this repository, so the commands are runnable rather than
illustrative. `contracts/_sim_harness.py` and `scripts/sim-v14.py` are the simulator lane,
`contracts/_v14_testkit.py` the offline one, and `scripts/mutate-v14.py` the mutation run.

**One caveat for an outside reader.** Rule 6 asks you to demonstrate a finding against the
revision it was found in. Retired revisions are not published here -- a working recipe
against dead code helps someone practising against the live one and helps a reviewer not
at all -- so an outside audit has one build to work with, and the "before" half is held in
the private tree. Say which build you tested; that is the part that matters.

## Non-negotiable audit rules

1. **Probe the compiled puzzle.** Read Rue or Chialisp to form hypotheses, then compile
   and execute the real puzzle hex with an attacker-shaped solution.
2. **Pair every negative case with an honest case.** A rejected malformed harness proves
   nothing. The honest spend must pass in the same test module before the attack result is
   interpreted.
3. **Diagnose rejection reasons.** Arity, lineage, announcement, and condition failures
   are different outcomes. Do not call a vulnerability fixed until the intended guard is
   the reason the adversarial spend fails.
4. **Never treat simulator acceptance as chain truth by itself.** Validate coin lineage,
   announcements, conditions, and bundle structure before claiming a drain or bricking
   vector. Do not push an exploit bundle to a public network without explicit user
   authorization.
5. **Read the revision's own references first.** `docs/FORGE_PUZZLE_V14.md` is the
   shipping revision's disposition, `docs/FORGE_PUZZLE_V14_SPEC.md` the design decisions
   behind it, `docs/FORGE_V14_ARCHITECTURE.md` the shape, and `docs/FORGE_SECURITY.md` the
   scope and how to report. Four rounds of external review are recorded in them, with what
   each found; reading them first stops an audit re-finding a closed defect and calling it
   new.
6. **Demonstrate the finding against the build it was found in, and assert that it
   works.** Every finding is built twice: against the vulnerable revision, where it MUST
   be accepted, and against the shipping one, where it MUST be refused. A before-and-after
   suite that quietly stops demonstrating the "before" is worth nothing and fails
   silently -- it stays green while proving less and less. A finding that can no longer be
   reproduced where it was found fails the suite exactly as loudly as one the fix still
   allows.
7. **Never search through a mirror of the logic.** A Python or TypeScript mirror carries
   its own guards, and a search run through one tests those guards rather than the
   puzzle's. Solve the bracket or the condition directly, then run the compiled leaf. A
   real finding was once reported as "does not reproduce" because the search ran through a
   wrapper that refused the very input the finding needed.
8. **Identify the revision with a number the reader can recompute.** "Which build did you
   test" is the one question every other answer depends on, and prose is not an answer:
   a fingerprint described as "sha256 over every `.rue`, `.hex`, `.hash` and the manifest,
   sorted" has several readings, and the one recorded on 2026-09-19 reproduced under none
   of them. It appeared nowhere but the record that quoted it. Ship the command, not the
   recipe — `scripts/revision-fingerprint.py` — and cite the command beside the digest so
   a reader can disagree with you.
9. **A failure must distinguish a stale record from a broken system.** Suites that compare
   a local record against the chain fail for two unrelated reasons: the record is behind,
   which is a fact about the checkout, or the chain never had what the record claims,
   which is serious. `_test_v14_discoverability.py` printed the same line for both, so 42
   ordinary drift failures were indistinguishable at a glance from one real one, and
   telling them apart took a chain query per coin in a suite that already held the spent
   flag. If a check can fail for a boring reason and an alarming one, it must say which.
10. **Run tools at the coverage the audit claims, not at their defaults.** The testnet
   probe takes `--pools`, defaulting to 8 of 32. Run plainly it covered a quarter of the
   pools, and the first draft of the 2026-09-20 record explained that shortfall with the
   index drift it had just been looking at — a tidy story, arrived at without reading the
   argument parser. Check what produced a number before explaining it, and state the
   coverage you actually exercised.

## Target asset architecture

- **Current:** native XCH and Standard CATs (CAT2).
- **Roadmap:** rCATs with custom transfer hooks or authorizers.
- **Future:** NFT collections, including singleton-wrapped DID-verified assets.

## Three lanes, and what each cannot decide

Choose the lane by what the finding turns on. Using the wrong one is how a real finding
goes undispositioned for several revisions.

| Lane | What it runs | Can decide | Cannot decide |
|---|---|---|---|
| Offline | `chia_rs.get_conditions_from_spendbundle`, the mempool's own validator | conditions, message pairing, duplicate outputs, CAT ring arithmetic | whether a coin exists, its lineage, or when it was born |
| Simulator | `chia._tests.util.spend_sim`, the real mempool manager and coin store | coin existence, lineage, birth heights, ephemeral-spend rules, whether a block advances | anything about the deployed pools' own state |
| Testnet | a node's `push_tx` | the same, against coins created over days that nobody here can rewrite | anything needing funds you are unwilling to spend |

An offline validator will happily run a chain of ephemeral spends, so a suite built on one
can only ever report that the chain is well formed. A finding only the chain can judge
must not be argued offline, and saying otherwise repeats the mistake the finding names.

A refused push costs nothing, so adversarial probes against live pools are free as long as
they are refusals. Never push a bundle you expect to be accepted unless you meant to spend.

## Model pipeline

When a shared codebase file is referenced, preserve the following handoff order:

### Fable 5.1: adjudicator and threat modeler

Map the exact control flow, state transition, lineage checks, announcement graph, and
integer arithmetic. Derive theoretical attack conditions, including asymmetric reserve
weights, fee rounding, and precision or dust boundaries. Produce an abstract proof and
target conditions; do not write remediation code at this stage.

### Astra: simulator and QA engineer

Translate the blueprint into tests against the compiled puzzle, using this repository's
own harness (`contracts/_v14_testkit.py` offline, `contracts/_sim_harness.py` on a
simulator) or `chia-dev-tools`/`cdv` in a repository that has no harness of its own. Compile the target puzzle, create honest
fixtures, construct the smallest malicious solution, and record the exact acceptance or
rejection reason. Keep the honest control beside the adversarial probe.

### Opus: implementer and Testnet11 verifier

Synthesize the evidence, produce a minimal production-grade Rue/Chialisp fix, update
builders and quote mirrors, and draft Testnet11 verification commands. Recompile and
re-probe both the honest and adversarial paths after the fix.

## Attack-vector catalog

### Standard CAT and XCH

- **CAT lineage spoofing:** verify the outer CAT wrapper, asset id, tail, inner puzzle,
  parent lineage, and amount before using an asset index or reserve delta.
- **Solution-supplied identity:** reject coin ids, launcher ids, puzzle hashes, or
  announcement identities accepted from the solution unless they are independently
  derived and bound in the puzzle.
- **Asymmetric interleaving:** inspect multi-spend ordering and every
  `ASSERT_MY_COIN_ID`, `ASSERT_COIN_ANNOUNCEMENT`, and `ASSERT_PUZZLE_ANNOUNCEMENT`
  relation under concurrent multi-asset swaps.
- **AMM dust accumulation:** integer division truncates toward zero. Confirm rounding
  favors the pool, fee accounting is conserved, and asymmetric weighted formulas agree
  across puzzle, builder, route planner, and frontend quote.
- **Announcement confusion:** a satellite should normally assert a puzzle announcement
  rebuilt from its own curried identity; a coin announcement keyed by a solution-supplied
  coin id can authorize an attacker-owned coin.
- **Successor substitution:** require successor puzzle hashes and amounts to be bound to
  the actual coin and state transition, not merely asserted as non-zero inputs.
- **Native-asset aliasing:** `xch`, the empty string and sixty-four zeros are one asset
  and three different keys. An indexer writing one while the puzzle holds another makes
  every native-to-CAT route match nothing, silently. Fold the spellings in one place.
- **Broadcast versus 1:1 authorization:** a coin announcement is a consensus broadcast and
  any number of independent spends may assert it. Wherever a once-per-lifetime event is
  authorized -- a genesis mint, a fee release -- ask what counts the assertions. Prefer a
  CHIP-0025 message, sender committed by puzzle hash and receiver by coin id, or an
  `AssertMyParentId` against a coin that can only be spent once.
- **Ephemeral chaining:** can the singleton be spent twice inside one bundle? If any
  height, price or counter advances per spend, a chain of ephemeral spends moves it at no
  real cost and with no market exposure. `ASSERT_MY_BIRTH_HEIGHT` closes this
  structurally: a coin created inside the bundle has no birth height, so no claimed value
  satisfies it, and a node answers `EPHEMERAL_RELATIVE_CONDITION`.
- **Cross-leaf configuration:** an action layer proves each leaf it runs is a member of
  the merkle root; it does not prove the leaves agree with one another. Six leaves curried
  with six different configurations make a valid root. Something on the coin must rebuild
  the root from a single configuration hash on every spend.
- **Duplicate outputs:** two `CreateCoin`s alike in puzzle hash and amount from one coin
  are one coin id twice, and a node rejects the whole bundle naming nothing. Catch it in
  the composer, where the information about which pair collided still exists.
- **Negative operands:** on-chain CLVM `/` and `divmod` floor negative operands without
  raising, unlike the `brun` CLI. An `assert x >= 0` that reads as redundant may be the
  only thing keeping a withdrawal dressed as a deposit out of the bracket.

### Derivation rule for anything new

Every derivation of a coin or receiver id must end at something consensus committed:
route it through the native `coinid` operator, or compare the result against a committed
coin id or announcement. Rue emits no runtime `strlen`, so a `Bytes32` in a signature is a
compile-time tag and nothing more -- the safety comes from where the value goes, not from
its type. Where neither holds, an explicit length assertion becomes mandatory.

Test it adversarially: feed every solution-supplied `Bytes32` at 0, 1, 4, 31, 33 and 64
bytes, plus a non-canonically encoded integer, and require each one to be refused.

### rCAT roadmap

Inspect transfer-hook and authorizer callbacks for re-entrancy and ordering. Flag any
path that mutates reserves or emits a successor before proving that the hook returns a
valid authorization and that the resulting delta is conserved.

### NFT and singleton roadmap

Bind participant identity to immutable launcher ids and verified singleton lineage. Do
not authorize from transient inner identities that can be transferred or replaced in
the same spend block.

## Required audit procedure

1. Identify the compiled puzzle artifact, source, curry parameters, state layout, and
   all builders or quote mirrors.
2. Trace the value-authorisation path: who can spend, what is derived, what is merely
   solution-supplied, and how each satellite binds to its controller.
3. Build an honest fixture through the production creation path where possible.
4. Add the smallest adversarial variant for each hypothesis.
5. Run static bundle checks: announcement satisfaction, real input lineage, no duplicate
   spends, and `ASSERT_MY_*` consistency.
6. Test asymmetric reserves, non-zero fees, zero and boundary amounts, CAT and XCH lanes,
   multi-asset interleaving, serialization round trips, and successor reconstruction.
7. After remediation, recompile and rerun the original honest and adversarial probes.
8. Separate findings from hypotheses, and state residual coverage gaps explicitly.
9. Mutate the guards. A refusal test does not say which line refused: delete the assert,
   rebuild, and see whether the suite still passes. A survivor is UNREACHED, never
   "redundant" -- probe it with hand-sized inputs before arguing, because a mirror's guard
   or an earlier assert routinely masks the leaf's own line.

## Local simulator protocol

When asked to prove a vulnerability locally, generate a focused `pytest` suite using
the repository's installed simulator APIs. The suite should:

- initialize a local simulator and fund native XCH;
- create real test CAT lineage and pool fixtures;
- exercise the compiled puzzle, not a source-level mock;
- build an honest spend and assert it succeeds;
- build the malicious spend with the unexpected solution layout or altered curried
  parameter and assert the observed result;
- use `pass_blocks(1)` or the equivalent only after checking that the bundle is valid;
- distinguish a genuine illegal state transition from an invalid test harness.

Start from this shape and adapt imports to the pinned Chia version:

```python
# The import is chia._tests.util.spend_sim. There is no chia.clvm.spend_sim, and a
# sample that names it fails at import rather than at the assertion.
from chia._tests.util.spend_sim import sim_and_client
from chia.types.mempool_inclusion_status import MempoolInclusionStatus


async def test_honest_and_adversarial_transition() -> None:
    async with sim_and_client() as (sim, client):
        # Build the fixture through the production creation lane where one exists: a
        # probe that builds its own creation bundle proves something about the probe.
        pool = await create_pool_through_the_real_lane(sim, client)

        honest = build_honest_bundle(pool)
        status, error = await client.push_tx(honest)
        assert status == MempoolInclusionStatus.SUCCESS, f"the control failed: {error}"
        await sim.farm_block()

        attack = build_adversarial_bundle(pool)
        status, error = await client.push_tx(attack)
        # Name the condition, not just the refusal: a bundle refused for arity is not a
        # bundle refused by the guard under test.
        assert status != MempoolInclusionStatus.SUCCESS, "accepted; the vector is open"
        assert error.name == "EPHEMERAL_RELATIVE_CONDITION"
```

Never hide unavailable dependencies behind a passing skip. If the required simulator
or compiled artifact is unavailable, fail clearly or use the repository's established
non-zero skip convention.

## Testnet11 protocol

Testnet validation is for an authorized, non-destructive proof or a fixed-contract
regression check. Record network, puzzle revision, bundle id, mempool response, and
node height. Use the installed CLI syntax and wallet configuration; the common push
shape is:

```bash
# push_tx belongs to the FULL NODE, not the wallet.
chia rpc full_node push_tx -j ./exploit_spend_bundle.json
```

Against a public node, POST the bundle to that node's `push_tx` endpoint instead: no
local node and no certificate are needed, and on Windows `curl` cannot load the PEM cert
anyway. Read the verdict three ways -- `success: false` is a refusal carrying a consensus
code, `success: true` with `PENDING` is held rather than included (time-lock conditions
land here, and one that can never become true is never included), and anything else is
acceptance.

Observe and report condition failures such as `COIN_AMOUNT_EXCEEDS_MAX`, announcement
failures, lineage failures, or mempool rejection. Do not imply that a command succeeded
until the RPC response and subsequent chain state confirm it.

## Strict finding format

Every confirmed or clearly labelled provisional finding must use this structure:

### [Finding ID] - [Descriptive Vulnerability Title]

- **Risk Level:** Critical / High / Medium / Low
- **Asset Class Impacted:** Standard CAT / Native XCH / rCAT / NFT Collection
- **Target Source Block:** Filename / function / curried parameter name

#### 1. Observed Condition & Theoretical Exploit

Explain the exact control-flow defect, required attacker inputs, conservation impact,
and why the observed behavior is reachable. Mark unexecuted reasoning as provisional.

#### 2. Local Simulator PoC Blueprint

```python
# Executed against the compiled puzzle with an honest control in the same test.
```

Include fixture construction, the malicious solution, expected simulator result, and
the assertion that distinguishes the exploit from a malformed bundle.

#### 3. Testnet11 Verification Command

```bash
# Use only with explicit authorization and an isolated testnet wallet.
chia rpc full_node push_tx -j ./exploit_bundle.json
```

Include expected mempool behavior and the evidence to collect.

#### 4. Remediation Code (Chialisp Fix)

Provide the precise minimal delta, plus required Rue, builder, quote, fixture, and
regression-test changes. For this repository, prefer Rue when the contract source is
Rue; do not silently replace a source-language patch with handwritten Chialisp.

## Completion gate

An audit is complete only when the report identifies the exact compiled revision tested,
the honest control passes, each finding has a reproducible probe or is marked provisional,
the fix is recompiled, and both positive and negative regression cases have been rerun.

## Changing this runbook

Every rule here is a claim about what finds defects, and claims are improvable. Two asks
for a change:

- **Say what it would have caught, or what it missed.** A rule with a defect behind it
  survives review; a rule that only sounds prudent adds length and gets skipped.
- **Keep the samples runnable.** The two code blocks above were both wrong on arrival --
  `chia.clvm.spend_sim` is not a module, and `push_tx` belongs to the full node, not the
  wallet -- and each would have failed at import or at the first call rather than at the
  assertion, which is the least useful moment to discover a sample is fiction. Run what
  you write here before proposing it.

A disagreement with a rule is as welcome as an addition. The findings that changed this
project most were the ones that contradicted something it had already published.