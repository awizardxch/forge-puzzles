# Forge against the TibetSwap findings

TibetSwap was the Chia AMM Forge is closest to in shape, and it has now failed
twice in public. Both failures are documented, both are in Forge's problem
domain, and one of them is a bug class Forge itself shipped and fixed. This
document asks the only useful question: **would either bug work against Forge,
and what is the evidence?**

Sources, read 2026-09-10:

- [TibetSwap V1 — Post-Mortem](https://blog.kuhi.to/tibetswap-v1-post-mortem) (May 2023)
- [TibetSwap V2 — Post-Mortem](https://blog.kuhi.to/tibetswap-v2-post-mortem) (10 September 2026)
- [`Yakuhito/tibet`](https://github.com/Yakuhito/tibet) — archived 10 September 2026, read-only

The repository's own README names the two V2 vulnerabilities outright:
"swapping allows negative input amounts" and "`remove_liquidity` never forces
the LP melt" (the latter reported by splitXCH).

---

## 1. V1: an announcement that did not name the singleton

**Their bug.** The pair singleton coordinated with the liquidity TAIL by
announcement, and "the pair singleton coin id is not included in any of the
announcements." Two different singleton instances could therefore produce
identical announcements for the same CAT, so an attacker could "burn liquidity
twice by 'locking in' two versions of the pair singleton to the same CAT."
Their fix: "Include the pair coin id in the announcements, thus preventing two
singletons from locking onto the same CAT by making sure they produce and
consume different announcements for each spend."

**Forge shipped this same class, and fixed it.** Earlier revisions bound a
reserve release to an announcement rather than to the singleton's identity —
the same mistake, arrived at independently. Those revisions are retired and
unmintable, and the mechanics are deliberately not written down here: the record
lives in the private findings log and will be published alongside the audit
request, when there is somewhere to report and a version worth attacking.

**Where Forge stands now.** V11 does not bind by announcement at all. The pool
*derives* the LP action coin's id rather than accepting one:

```
lp_action_coin_id = sha256(lp_parent_id
                           + cat_puzzle_hash(lp_tail_hash, inner_hash)
                           + amount)
```

— parent, the CAT puzzle hash with the **pinned** inner for that mode, and the
exact amount ([`forge_action_common.rue:345`](../contracts/v12/puzzles/forge_action_common.rue)).
It is delivered by a CHIP-0025 `SendMessage` with mode `SENDER_PUZZLE |
RECEIVER_COIN`, so consensus itself commits both the sending puzzle and the
receiving coin. An impostor coin has a different id and never receives the
message. The melt inner's only behaviour is to destroy its whole amount, and the
TAIL additionally requires a CAT parent, so a coin fabricated from ordinary
mojos cannot stand in for a burn.

This is strictly stronger than the V1 fix: Tibet added an identifier to a
message anyone could still emit; Forge moved the binding into consensus and
stopped accepting the identifier as input.

**Status: closed, structurally.** Pinned by `_test_v12_actions.py` —
*"an eve that mints a different amount than the pool authorized is refused by the
TAIL"* and *"fabricated melt coin (finding 4) refused"*.

**Action: none.** Recorded here so the lineage is not lost.

---

## 2. V2, primary: negative swap input amounts

**Their bug.** `swap.clsp` lines 40–44 enforced that one reserve rise and the
other fall, "but it never required the user-supplied input amount to be
positive." A negative amount is a reversed trade, and critically "the sign of
the 0.7% trade fee is also reversed" — so the fee pays the swapper rather than
the pool. Repeated against one pair, amplified by flash loans, it drains the
reserves. Their recommended fix: "add a `(> amount 0)` assertion in the swap
path for both standard and rCAT pairs."

This drained the XCH-PIZZA pair on 24 August 2026 and led to a 7.5-hour whitehat
rescue of all 367 pairs.

**Forge is not vulnerable, and has two independent layers.**

*Layer 1 — explicit.* The swap leaf asserts both signs, which is more than the
recommended fix (they suggest the input; Forge checks the output too), along
with index bounds:

```rue
assert asset_in != asset_out;
assert asset_in >= 0 && asset_in < n;
assert asset_out >= 0 && asset_out < n;
assert gross_input > 0;
assert claimed_output > 0;
```

*Layer 2 — structural.* `exact_swap_output` is unsatisfiable for a negative
input. Verified rather than assumed:

- the curve accepts the honest pair for a +250,000 input exactly, and rejects
  honest ± 1 — the bracket is exact;
- brute-forcing the plausible output range for a −250,000 input found **no**
  satisfying output;
- the Python mirror refuses a negative input outright
  (`swap reserves and input must be positive`).

**What the mutation test showed, and why it matters.** Sign-refusal cases were
added to `_test_v12_actions.py`. They pass — but recompiling the swap leaf with
*both* sign assertions deleted makes them pass too, and the full sweep below
later confirmed the same for all seven sign and bounds guards in `swap` and
`add`. So the curve, not the assertions, is the load-bearing defence; the `> 0`
lines are a redundant second layer.

That is worth stating plainly. Anyone reading the leaf would take the assertions
for the defence, and if the bracket were ever widened they would silently
*become* it. The tests now pin the property that matters — such a solution is
refused at the leaf — and the comment records which layer is actually holding.

**Status: not vulnerable.** Two layers, one of them verified unsatisfiable.

**Actions taken.**

- [x] Sign-refusal cases added to `_test_v12_actions.py` for the swap and remove
      paths: negative and zero input, negative and zero claimed output, negative
      and out-of-range asset index, negative and zero LP burn.
- [x] The load-bearing layer identified by mutation and written into the suite,
      so the next reader is not misled.

- [x] **Sign-assertion coverage for `add`** (2026-09-10). Four more probes:
      negative and zero `lp_delta`, a negative deposit in one slot, and a
      deposit of nothing at all. The deposit path guards it with three
      assertions rather than one — `lp_delta > 0`, `deposit >= 0` per asset, and
      `any_positive(deposits)` — because a zero on *one* asset is a legitimate
      off-ratio add, so emptiness has to be checked separately. Twelve sign
      probes now, across all three leaves; 78/78 action checks pass.
- [x] **A standing mutation harness** (2026-09-10): `scripts/mutate-v12.py`.
      See below.

---

### The harness, and what it found

`scripts/mutate-v12.py` deletes each `assert` in Forge's leaves one at a time,
rebuilds with `rue`, and runs the suites against the result. A mutant that
**survives** — every suite still passes without the line — is not automatically a
bug; it is a question with two acceptable answers, *redundant* or *untested*, and
the point is to know which. Nothing is written into `contracts/v12/compiled`:
each mutant is built in a temporary copy and the suites are pointed at it with
`FORGE_V11_COMPILED`, so a broken build can never be left behind in the project.

One design note learned the hard way. Run against a single suite, the harness
reported the DAO fee's **monotonic-decrease guarantee**
(`new_bps < p.state.dao_fee_bps`) as unpinned — which would have been a serious
false alarm, since `_test_v12_dao_fee.py` kills it immediately. A mutant is
therefore killed if *any* suite in the run fails, and the output names which one
did it.

Full sweep, 32 assertions across seven files, against the actions, DAO fee and
registry suites:

**10 killed, 22 survived, 0 unbuildable** — about 40 seconds.

The seven sign and bounds assertions in `swap` and `add` are all survivors. That
generalises the hand-check above: **the curve is the defence against the whole
TibetSwap negative-amount class**, and the `> 0` guards cannot be pinned
independently because they are unreachable — there is no solution that satisfies
`exact_swap_output` while violating them. Both leaves now carry a comment saying
exactly that, so the next reader does not mistake decoration for the defence, nor
delete it on the grounds that a test still passes.

**Action outstanding.**

- [ ] **Triage the 22 survivors.** Most are structurally unreachable, like the
      sign guards. A few are worth a closer look because no other check
      obviously covers them: `settlement_coin_id != zero_bytes32()`,
      `lp_parent_id != zero_bytes32()`, `valid_config(config)` and
      `payout == 0`. Each ends as either a comment or a test.
      *2026-09-11:* the TAIL was run separately (11 asserts). Its two melt-side
      locks — `effective_delta == expected_delta` and `parent_is_cat ||
      expected_delta > 0` — were both survivors under the default suites; the
      latter because the finding-4 probe is refused by the former first. Both
      are now killed by `_test_v12_lp_receive_forgery.py`, which the harness
      runs by default. The nine that remain are the genesis branch and the
      shape checks (`launcher_id != 0`, protocol version, `expected_delta != 0`,
      `new_total_lp >= 0`, `amount > 0`, `pool_inner_puzzle_hash != 0`, and the
      three genesis asserts); they belong to this item.

---

## 3. V2, secondary: `remove_liquidity` never forces the LP melt

**Their bug.** Named in the tibet README and attributed to splitXCH's Fable 5
security scans. Unexploited; fix suggestions were to be published in the
repository. The shape is that a removal could take the payouts without the LP
actually being destroyed.

**Forge forces the melt in the same spend.** `forge_action_remove.rue` ends with

```rue
lp_handshake(CONFIG, lp_parent_id, LP_MELT_INNER_HASH, burn,
             0 - burn, new_total_lp, tree_hash(new_state))
```

The handshake is emitted unconditionally — there is no path through the leaf
that pays out without it — and it names a receiver coin the pool *derived*, at
the pinned melt inner, for exactly `burn`. Mode 23 makes consensus require that
receiver to be spent in the same bundle. The melt inner's only behaviour is to
destroy its whole amount. So the payout and the destruction are one atomic fact,
not two steps that could come apart.

`burn < total_lp` also holds, so the pool always outlives the withdrawal.

**Status: closed, structurally.** Pinned by *"payout +1 refused"*, *"fabricated
melt coin (finding 4) refused"* and *"burning the whole supply is refused"*.

**Re-examined 2026-09-11, on the sharper form of the concern.** The version put
to us was not "the payout can skip the melt" but "the LP coin's *inner puzzle*
can emit conditions that *look* like the TAIL's receive, instead of running the
TAIL." That deserved puzzles run, not prose, because it turns on a fact about
CAT2 rather than about Forge. `_test_v12_lp_receive_forgery.py` establishes:

- **The pass-through is real.** CAT2 (`37bef360…`) hands an inner puzzle's bare
  `RECEIVE_MESSAGE` straight out; the TAIL never runs. Any CAT pool whose
  receiver is not pinned to a specific inner puzzle is exposed to exactly this.
- **Forge is not, for one reason.** `remove` derives its receiver from the
  *pinned* melt inner's hash — a curried constant whose program decodes to a
  single `-113` condition and nothing else — and mode 23 makes consensus require
  *that* coin to answer. A byte-perfect forgery on the attacker's own LP coin is
  a receive on the wrong coin id: refused with `MESSAGE_NOT_SENT_OR_RECEIVED`.
- **The zero-delta variant** (a coin *at* the pinned melt hash, `extra_delta = 0`,
  ring balanced by a sibling) is refused by the TAIL's delta lock,
  `effective_delta == expected_delta`. Mutation: deleting that assert flips the
  case to accepted and nothing else in the suites notices — so that suite is
  its pin.
- **Finding 4's lock was unpinned.** That is the CAT-parent lock,
  `parent_is_cat || expected_delta > 0`. The existing probe fabricates the melt
  coin with `extra_delta = -burn`, which the delta lock refuses *before* the
  CAT-parent lock is reached; a mutation run showed the latter surviving every
  suite. Reaching it needs `extra_delta = -2·burn` — and CAT2's ring then
  charges for it, forcing a real sibling to shrink by `burn`. Against a TAIL
  with the CAT-parent lock deleted that bundle is **accepted, and it is an
  honest burn**: real supply falls by exactly what the pool paid for. So the
  assert is defence in depth — it removes the dependence on the ring's sign
  convention and requires the messaged coin to hold real supply — not the sole
  lock its comment claimed. The comment now says so, and case C pins it as
  policy.

No revision is forced by any of this. The comparison-worthy point is that the
Tibet bug and the Forge non-bug sit one design decision apart: whether the pool
names *which* coin must answer, or merely *that* a coin at some puzzle must.

**Action outstanding.**

- [ ] **Re-read when their fix is published.** The tibet repository is archived
      but the README says the finding will be documented there with suggested
      fixes. If their analysis names a mechanism Forge has not considered, this
      section should be revisited rather than assumed closed.

---

## 4. Process findings — the part that generalises

The V2 post-mortem is blunt about how the bug survived: **two in-house AI
security audits from Chia Network (23 April and 5 June 2026) did not flag "this
bug or anything in the same class"**, and multiple reviews by community Chialisp
experts also missed it. What eventually found it was a newer model. The second
vulnerability was found the same way, by splitXCH running Fable 5 over the code.

Their conclusion, which Forge should adopt rather than admire:

> "Current AI model capability means on-chain projects have to spend more effort
> on security than the last few years required."

**Actions outstanding.**

- [ ] **Re-audit on every capable model release, not on a schedule.** The
      post-mortem's own recommendation is to run audits "as soon as a new
      capable model is available." Forge's CLVM audit method already exists
      (`clvmPuzzleAudit` skill, `FORGE_V11_CLVM_PASS.md`); what is missing is
      the trigger.
- [ ] **A bug bounty with published terms**, advertised where Chia developers
      actually are. Tibet's post-mortem lists this as a gap; Forge has no bounty
      at all.
- [ ] **Live exploit monitoring.** Forge watches pool state for its own
      indexing; it does not watch for the shapes an exploit makes — a reserve
      falling without a matching settlement, a swap whose output exceeds the
      curve, an LP supply that moves without a melt. The data is already being
      read.
- [ ] **Publish the puzzles for outside review.** This is the reason the
      puzzle repository is being split out. Tibet's code was public and still
      took three years and two failures; Forge's has never been read by anyone
      outside this workspace.

---

## Summary

| Finding | Forge | Evidence |
|---|---|---|
| V1 — announcement without singleton id | **Closed structurally.** Same class shipped in V4–V9, fixed in V10, made impossible in V11: the receiver coin id is derived, not supplied, and committed by CHIP-0025 mode 23 | `forge_action_common.rue:345`; TAIL and melt-coin refusal tests |
| V2 — negative swap input | **Not vulnerable.** An unsatisfiable curve bracket, with sign asserts as a second layer | Full mutation sweep; brute-force search; 12 refusal cases across three leaves |
| V2 — remove without melt | **Closed structurally.** The handshake is unconditional and atomic with the payout | `forge_action_remove.rue`; payout and melt refusal tests |
| Process — audit cadence, bounty, monitoring | **Gaps.** All three outstanding | This document |
| Tooling — which assertions are load-bearing | **Answered, and now repeatable** | `scripts/mutate-v12.py`: 10/32 killed |

Nothing here is a clean bill of health. Forge's pool has never been audited by
anyone outside this workspace, and the V2 post-mortem's most uncomfortable fact
is that expert human review and two AI audits all missed a bug that a newer
model found in one pass.
