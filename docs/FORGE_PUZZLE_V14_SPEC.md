# Forge V14 — specification of the improvements

**Status: spec, for review. Nothing built, nothing pushed.**
Testnet research only; unaudited. V14 is **protocol 15**.

V13 (protocol 14) shipped on 2026-09-15 and a fourth adversarial review landed the
same day (Chia-Network/chips#217, comment 5683642051, filed at
the fourth-review note (private, with the retired V13 sources)). Seven of the nine V13 answers held
up under re-test. Two did not, and both are reproduced independently in
the V13 fourth-review reproduction suite (private, with the retired sources) (15/15) — from the reviewer's parameters,
without running the reviewer's script, so the reproduction is ours.

This document specifies what V14 changes, why each change is the right shape, what
it deliberately does not change, and how it will be tested. It is written to be
read before any code exists.

---

## 0. Why a major increment

| | |
|---|---|
| **R-1** | `register` takes the reserve parent on the registrant's word, so a pool can be registered against reserves that were never funded — or funded but wrongly parented. Either way the pool is unspendable and the market key is taken permanently, for one creation fee. **Live gap in V13.** |
| **R-2** | `add`'s `assert deposit >= 0` is load-bearing, not redundant as we published. **Not exploitable in V13** — the line is present — but the claim and the coverage behind it were wrong. |

R-1 adds a puzzle, changes what `register` asserts and removes a field from its
solution, so every registry puzzle hash moves and the V13 registry cannot admit a V14 pool. That is a
major increment, not a point release. V13's liquidity comes out first, as V12's did.

R-2 requires no puzzle change at all. It requires a test, a correction, and a change
to how we read mutation results — which is the finding with the longest reach.

---

## 1. R-1 — the reserve parent must be derived, not claimed

### 1.1 The gap, precisely

`register` takes `reserve_parents` in its solution and uses it in exactly one place:
building the pool's full puzzle hash (`forge_registry_register.rue:71`). `valid_pool`
checks `all_positive(reserves)` — the **claimed** amounts. Nothing in the registration
bundle spends a reserve, and none of the three announcements `register` asserts (the
launcher's, the creation fee's, the LP burn's) comes from one.

So an attacker registers without funding the reserves. Registration succeeds: the
launcher spend, the fee and the genesis LP burn are all things they genuinely do. The
slot is taken and the pool coin is created at a puzzle hash whose state contains
parents that name nothing.

From then on the finalizer derives every reserve coin id from those parents, so the
**first** real interaction anyone attempts — including the dilution deposit our own
defence relies on — fails `MESSAGE_NOT_SENT_OR_RECEIVED`. The pool can never be spent
by anyone, including the squatter.

> This does not defeat the dilution defence; it removes its premise. The defence
> dilutes a squatter who funds `[1, 1]`. This attacker funds nothing.

"The key is taken" stops meaning "this market exists" and starts meaning "this market
is gone". That is the gap M-3 originally named — `register` proving *authorization of
a config* rather than *existence of the thing it configures* — surviving under a
cheaper construction than the one V13 closed.

### 1.2 What we simulated, and what consensus said

Five candidates were run against the real V13 puzzles before one was chosen. The
simulations are kept as `contracts/_sim_v14_r1_routes.py` (6/6) and
`contracts/_sim_v14_r1_candidates.py` (9/9), so the rejected routes are evidence
rather than recollection.

| Candidate | Result | Why |
|---|---|---|
| **Settlement announcement** | **fails** | A puzzle announcement is keyed by `sha256(puzzle_hash + message)`: it binds the *puzzle*, never the announcing coin. Simulated with reserves genuinely funded and the recorded parent wrong — **accepted**. It forces the attacker to fund `[1,1]` and changes nothing else. |
| **Spend the eve at genesis** | **cannot be built** | Refused with `EPHEMERAL_RELATIVE_CONDITION` (141) even when every parent is honest. A coin created and spent in the same block may not carry a birth-height condition, and the prologue emits `ASSERT_MY_BIRTH_HEIGHT` on every pool spend. Each half validates alone; the combination cannot exist. |
| **Drop the birth assert at genesis** | available, **rejected** | Confirmed mechanically: without the condition the same-block spend is accepted. But `ASSERT_MY_BIRTH_HEIGHT` is what makes `birth > last_height` and `h >= birth` meaningful, which is the S3 oracle fix. A genesis exemption puts a branch in the one assert the third review checked specifically. |
| **Two-phase registration** | works, **rejected** | A later block's first spend does catch a wrong parent (`MESSAGE_NOT_SENT_OR_RECEIVED`, 147). It needs provisional slots, a confirm path and a rule for what an unconfirmed slot means to everyone else — and the squat window stays open until the confirm lands. |
| **Reserve launcher** | **adopted** | §1.3. |

> The eve-spend result is the one that could not have been reasoned to. Our own CLVM
> pass already recorded the rule for *successors* — "with `ASSERT_MY_BIRTH_HEIGHT` it
> also makes a same-block successor unspendable" — and we did not notice it applies to
> the eve for the same reason. Simulating first is what caught it.

**A genesis melt, recorded as informative only.** A melt needs the pool's own mode-23
message, which needs the pool spent — so it is the eve-spend route wearing a different
hat, and dies on the same consensus rule. Not explored further; no claim is made here
about a variant that avoids it.

### 1.3 The fix: a reserve launcher whose announcement commits to what it creates

Two primitives, each insufficient alone:

- a **puzzle** announcement binds the puzzle but not *which* coin made it
- a **coin** announcement binds the coin but not what that coin *did*

The fix gets both at once from a fact about coin ids: **a coin id commits to its puzzle
hash.** So if `register` derives the id itself, the puzzle at that id is pinned too.

`register` is given each reserve's **grandparent** rather than its parent, and computes

```
P_i = coinid(grandparent_i, RESERVE_LAUNCHER_HASH, reserves[i])
```

then asserts a coin announcement from `P_i` carrying `(reserve_full_hash_i, reserves[i],
launcher_id)`.

`forge_reserve_launcher.rue` is a small puzzle that creates the reserve coin — hinted
with the launcher id — and announces exactly what it created. Any coin able to satisfy
the assertion therefore **is** a reserve launcher, because nothing else hashes to
`RESERVE_LAUNCHER_HASH`; and a reserve launcher that announces has created the reserve.

Simulated: the honest bundle is accepted, and an imposter that announces the right
message without creating the reserve has a different coin id, so its announcement cannot
satisfy the assertion — refused with `ASSERT_ANNOUNCE_CONSUMED_FAILED` (12).

For a CAT reserve the launcher is CAT-wrapped, so `RESERVE_LAUNCHER_HASH` becomes
`cat_puzzle_hash(asset_id, RESERVE_LAUNCHER_HASH)` and the same derivation holds. The
amount is `reserves[i]`, which `register` already has, so the only new solution field is
the grandparent — and naming a wrong one produces a `P_i` that no coin in the bundle
answers to.

### 1.4 The consequence: `reserve_parents` leaves the solution

`register` computes `P_i` and uses it as the reserve parent. The field is **derived, not
claimed**, so it is dropped from `register`'s solution entirely.

This is the same move V12 made when it took reserve parents out of the *pool's* solution
and put them in state, and the same principle both times: **there is nothing left to
name, rather than a check that naming must fail.** Every V13 finding and two of the
three V12 findings are one mistake in different costumes — a puzzle taking an
authorizing identifier from its own solution.


### 1.5 What R-1 reduces to

With this in place, registering a market requires actually funding its reserves. A
squatter may still register `[1, 1]` — and that is the case we already answered, and
answered correctly: a genuine deposit dilutes them to nothing. The reviewer confirmed
that scenario is real, with one correction we should carry: **our stated arithmetic
had a units slip**, and it checks out. Against `[1, 1]` at 1,001 LP with 5,000 CAT,
the mint is:

| Deposited | LP minted |
|---|---|
| 0.01 XCH | **7,073,534,532** — the figure our docs quote |
| 1 XCH | 70,735,354,306 |
| 10 XCH | 223,684,832,872 — what our docs *say* was deposited |

So the published figure is right and the amount beside it is wrong by a factor of a
thousand. The conclusion is unchanged — dilution to nothing either way — but the pair
must be made consistent wherever it appears.

### 1.6 Changes required

| Layer | Change |
|---|---|
| `forge_reserve_launcher.rue` | **New.** Creates one reserve coin hinted with the launcher id, and announces `(reserve_full_hash, amount, launcher_id)` |
| `forge_registry_register.rue` | Derive `P_i` from the grandparent and the launcher hash; assert one coin announcement per reserve; drop `reserve_parents` from the solution |
| `forge_registry_common.rue` | `PROTOCOL_VERSION` 14 → 15; curry `RESERVE_LAUNCHER_HASH` into the registry constants |
| `forge_action_common.rue` | `MIN_LOCKED_LP = 1000` → `LOCKED_BURN = 1` (§3) |
| `forge_action_remove.rue` | cap at `total_lp - LOCKED_BURN` |
| `forge_lp_cat_tail.rue` | asserts `protocol_version == 15` |
| `forge_v14_driver.py` | `register_solution` takes grandparents; creation routes every reserve through a launcher |
| `deploy-v14-testnet.py` and the website lane | Both must build reserves through the launcher, and produce byte-identical announcements |

> **The lane asymmetry is the thing to watch.** S2 — the unburned floor — existed because
> the deploy script and the website lane did different things and only one was checked.
> `_test_v14_lanes_agree.py` exists for that reason.

### 1.7 Tests

- A registration naming a grandparent no coin answers to: **refused** (`ASSERT_ANNOUNCE_CONSUMED_FAILED`).
- A coin that announces the right message without creating the reserve: **refused**.
- A launcher creating the reserve at the wrong amount, or the wrong puzzle hash, or without the launcher hint: **refused**.
- An honest registration: **accepted**, and the derived `P_i` equals the created reserve's parent.
- The honest pool then swaps, adds and removes, proving the derived parents are the coins the finalizer messages.
- The V13 construction — unfunded reserves — replayed against V14: **refused**; against V13: **accepted**.
- The funded-but-wrong-parent construction, which the settlement route would have allowed: **refused** against V14.

### 1.8 The same lever, applied to settlements

**Half of an action's binding lives outside our puzzle, and nothing recorded that.**

A leaf ties itself to the trader's input with

```
AssertPuzzleAnnouncement(sha256(settlement_puzzle_hash(asset_id) + tree_hash((coin_id, nil))))
```

which binds the **asset** and the **coin id** and says nothing about the **amount**. The
settlement is spent with no payments at all — its value is simply released into the
bundle — so what stops a one-mojo settlement funding a 250,000 input is the CAT ring and
the bundle's value balance, not the leaf.

That is sound today. Every attack against it is refused
(`_sim_v14_action_binding.py`, 8/8), including the sharpest one: **an announcement is not
consumed by an assertion**, so two swaps in one spend can name the same settlement and
both assertions are satisfied by the one announcement it makes. That is refused — with
`MINTING_COIN`, from conservation, because the pool would create value nothing supplied.
Nothing in the action layer catches it.

> **Corrected 2026-09-16, from a live push.** That refusal depends on the bundle having
> nothing spare. Conservation is a whole-bundle property, and a network fee is spare value:
> on H6 at height 4,693,721 the same pair confirmed, the second swap funded out of a 5 XCH
> fee, and the fee actually paid came to 4,995,000,000. The pool received full value, so
> this is not a drain -- but the binding does not make a settlement single-use, and this
> document should not have implied it did. Both halves are pinned by
> `scripts/v14-slack-probe.py` and `contracts/_test_v14_action_binding.py`.

The risk is not a hole, it is that the guarantee is **split across two mechanisms and
only one is ours**. A future change to how value is accounted — a leaf that credits state
without a ring-visible transfer, a settlement shape that pays elsewhere — would silently
remove half of it, and no suite would notice.

**The puzzle can bind the amount itself.** A coin id commits to its parent, its puzzle
hash and its amount, so a leaf given the settlement's *parent* can derive the rest:

```
ASSERT_CONCURRENT_SPEND( sha256(parent + settlement_puzzle_hash(asset_id) + amount) )
```

Measured (`_sim_v14_settlement_amount.py`, 6/6): the honest settlement satisfies it, a
one-mojo settlement is refused with `ASSERT_CONCURRENT_SPEND_FAILED`, and the right
amount at the wrong puzzle is refused. Two routes that look plausible do not work and are
recorded so they are not retried — encoding the amount in the announcement's **nonce**
binds nothing, because the nonce is chosen by whoever solves the settlement; and
`OFFER_MOD` makes no **coin** announcement, so the assertion that would bind a coin id
cannot be satisfied by it.

The leaf would take the settlement's parent instead of its coin id — one field of the
same width — and derive the id. Nothing else changes.

| | |
|---|---|
| **Buys** | A second, independent binding. The puzzle would assert asset *and* amount, so a change to value accounting could not quietly remove half the guarantee. It also makes the action's intent legible on chain: the condition names the exact coin that pays for it |
| **Costs** | One condition per settlement per action — cost, and a larger solution for multi-settlement adds. It duplicates a guarantee conservation already gives, which is both the definition of defence in depth and the definition of something that can rot untested |

**It does not replace conservation.** A settlement can be spent with its value sent
somewhere other than the reserve, and only the ring notices that. The two bind different
halves and both are still needed.

**The amount bound is the settlement's own, not the action's input.** The router carves
its fee out of the same settlement coin, so `settlement.amount == gross_input + router_fee`
on every swap on the public lane. A binding on `gross_input` would have refused all of
them. The leaf therefore takes the settlement's **parent and amount**, derives the id from
those, and ties it to the action with an inequality:

```
ASSERT_CONCURRENT_SPEND( sha256(parent + settlement_puzzle_hash(asset_id) + settlement_amount) )
assert settlement_amount >= gross_input        // swap
assert settlement_amount >= deposit            // add, per POSITIVE deposit only
```

Run against every action shape before building
(`_sim_v14_binding_all_actions.py`, 14/14): a router-fee swap, a zero-fee swap, a
two-asset add, a multi-hop settlement parented by a reserve coin, and the four actions
that carry no settlement at all. Two build notes fell out of it — the binding must be
skipped for a **zero deposit**, in the same branch `deposit_asserts` already skips, and it
must not be emitted by `remove`, `observe`, `collect` or `dao_fee`, which would otherwise
be unspendable.

**Decision: adopt it, and say why in the CLVM pass.** The cost is one condition on a path
that already carries several, and the alternative is leaving a load-bearing property
undocumented and enforced entirely outside the code we audit. If the cost measurement
comes back worse than expected on a five-asset add, it can be dropped to the swap leaf
alone — where a single settlement makes it cheapest and the value at risk is largest.

---

## 2. R-2 — the line stays, the claim goes, the method changes

### 2.1 What is true

`add`'s `assert deposit >= 0` is **load-bearing**. On `[10,000,000; 20,000,000]` with
`total_lp = 5,000,000` and `fee_bps = 30`, the vector `[-100,000, +500,000]` satisfies
the mint bracket at `lp_delta = 36,611`: reserves become `[9,900,000; 20,500,000]` and
supply becomes `5,036,611`. A withdrawal of 100,000 wearing an `add`, that also mints
LP. The shipped leaf refuses it (`clvm raise 80`). **No puzzle change is needed.**

### 2.2 The arithmetic we got wrong

A negative slot does drive `min_deposit_ratio` negative — but that also *shrinks* the
balanced amount subtracted from the positive slot's excess, which **inflates**
`effective_product` rather than only depressing it:

```
[-100000, +500000] at ratio -0.01
  slot 0 effective = 9,900,000
  slot 1 effective = 20,498,950      (excess is LARGER because balanced is negative)
  effective_product = 2.0294e14  >  old_product = 2.0e14   -> a positive mint exists
```

The bracket closes only while the positive slot is too small to overcome the negative
slot's drag. `[-100,000, +200,000]` is refused even on the mutant — and that is the
region our search stayed in.

### 2.3 The method we got wrong, which matters more

The search ran through `forge_math.invariant_lp_mint`, whose wrapper raises
`"deposits cannot be negative"` **before the bracket is ever reached**. We tested the
guard we were trying to test, through a mirror carrying the same guard, and read its
refusal as the bracket's. The search could not have found the counter-example.

Three changes follow, and they are the substance of this section:

**(a) A SURVIVED verdict is a coverage report, not a verdict.** It means *no probe in
the suite reaches this line*. It never means the line is redundant. Every survivor now
requires a bracket-level probe that reaches the line and a written argument — not one
or the other. `scripts/mutate-v14.py` emits SURVIVED as **`UNREACHED`** to stop the
word doing work it cannot do.

**(b) The mirrors must separate the guard from the bracket.** `forge_math` grows
bracket-only entry points — `invariant_lp_mint_bracket`, `swap_output_bracket` — that
take whatever they are given and answer only "does the bracket accept this". The
guarded wrappers stay for callers. Any search for whether a guard is load-bearing must
go through the bracket-only path, and the suites must fail if a mutation search calls
the guarded one.

**(c) Every existing "defence in depth" verdict is re-derived.** Already done for the
swap twins, at the bracket rather than through the mirror: across equal and unequal
weights no negative `gross_input` admits a positive claimed output, because a negative
effective input forces the surviving out-reserve *above* where it started. Those are
genuinely redundant, and that now rests on evidence. Any remaining survivor gets the
same treatment before V14 ships.

### 2.4 Tests

- `_test_v14_actions.py` carries the `[-100,000, +500,000]` probe at
  `lp_delta = 36,611`, so the line is **killed** rather than unreached.
- The honest control — the same positive slot without the negative — accepted.
- A property probe: for a grid of pools, weights and fee rates, no vector with any
  negative slot is accepted by the leaf; and for each, whether the *bracket alone*
  would have accepted it is recorded, so the suite states which cases the assert is
  actually holding back rather than assuming.

---

## 3. The locked floor — one unit, and what it is actually for

### 3.1 The purpose, stated plainly

The burned LP exists so that **the pair never dies**. Whatever is burned can never be
claimed, so the reserves matching it stay in the pool forever: the pool remains
spendable, remains priced, and remains open to new liquidity, no matter how many
holders leave.

It is not there to reward the other holders, and it does not. A burn transfers units to
an unspendable puzzle hash; `total_lp` is unchanged, so every remaining holder's claim
is identical before and after. The burner hands their share to the pool permanently.

### 3.2 The permanent base grows on its own

This is why the *creation* burn does not need to be large. The permanent base is grown
by the market, not set at launch, through two mechanisms that compound:

**Trading fees accrue to it.** The curve fee is left in the reserves rather than paid
out, so value per LP rises for every holder — including the one that can never claim.
Measured against the real curve on a 100 TXCH / 200,000 CAT pool:

| Trades | Burn's claim (mojos TXCH) | Growth |
|---:|---:|---:|
| 0 | 100,000,000 | — |
| 1,000 | 101,490,651 | 1.49% |
| 10,000 | 114,911,104 | 14.91% |
| 100,000 | 249,364,560 | **149.36%** |

The burn address never acts and its stake more than doubles.

**Voluntary burns add to it.** Any holder may hand their position to the base. Since
`total_lp` does not move, nobody else's claim changes:

| Event | Burn units | Share of pool |
|---|---:|---:|
| genesis (burn = 1) | 1 | 0.0001% |
| an LP burns 50,000 | 50,001 | 5.00% |
| another burns 120,000 | 170,001 | 17.00% |
| a third burns 200,000 | 370,001 | 37.00% |

And a later depositor enters at the **current** rate — priced off live reserves and
live `total_lp` — so they are neither charged for nor gifted the permanent base.

### 3.3 The constant

```
LOCKED_BURN = 1
```

- `register` asserts a burn of `LOCKED_BURN` to the zero puzzle hash
- `register` requires `total_lp > LOCKED_BURN`
- `remove` caps at `total_lp - LOCKED_BURN`
- the prologue asserts `total_lp >= LOCKED_BURN`

One constant, four jobs, no separate minimum. V13's `MIN_LOCKED_LP = 1000` conflated a
burn with a minimum supply, and the minimum turned out to have no content: a creator who
wants a coarse pool mints `M+1` whatever `M` is, and one who wants a fine pool mints
millions whatever `M` is. It forbade the smallest label and constrained nothing anyone
actually ends up with.

### 3.4 What 1000 cost, measured

Reserves left behind are `burn / total_lp` of the pool. Across the 32 V13 pools as they
were actually drained, the median pool stranded **2.21%** of its liquidity and the worst
**4.91%** — an accident of how much LP each pool happened to mint, not a chosen depth:

| Pool | Genesis mint | at 1000 | at 1 |
|---|---:|---:|---:|
| D3 | 20,353 | 4.913% | 0.0049% |
| A1 | 97,695 | 1.024% | 0.0010% |
| I2 | 846,543 | 0.118% | 0.0001% |

The withdrawal arithmetic itself is exact: what remains is precisely `burn / total_lp`
and nothing more, identical whether a pool is emptied in one removal or a hundred. There
was never any rounding to fix — only the burn's own claim to shrink.

### 3.5 What the burn guarantees, and what it does not

**Guaranteed at any positive burn, including 1.** The pool is permanently spendable
(`total_lp >= LOCKED_BURN`), permanently priced (the invariant and `spots` are defined
at any positive reserve), and permanently re-enterable. Tested against A1 exactly as the
drain left it — reserves `[374,275,040, 5]` — a 1 TXCH deposit mints 99.96% of the
supply at every burn size. **The market can always be restarted.**

**Not guaranteed by any LP-denominated constant.** That the residual is *deep enough to
trade without someone re-entering first*. Tradeability depends on the absolute size of
the residual reserves, which a constant counted in LP units cannot see. Measured on our
own drained pools at `MIN_LOCKED_LP = 1000`, the smallest trade producing even one mojo
of output was 25.08% of the reserve on A1 and 14.33% on A2. Those residual markets were
already dead at 1000; the thin side had floored to single-digit mojos.

Guaranteeing depth would need a minimum reserve denominated in assets, which the third
review considered and set aside because it prices small markets out. This spec does not
add one, and does not claim the burn provides one.

### 3.6 Why 1 is enough as evidence, from the review record

**The cursor bot, round 2 (2026-09-14)** found the CHIP text called the floor "the
creator's" holding when LP is a transferable CAT — so a later *buyer* could be the one
permanently stuck. Burning answers that at any positive amount.

**The third review's INFO finding** — `register` proves the genesis mint was
*authorized*, not that it *happened* — is closed because the LP asset can only exist via
the TAIL's genesis branch, so a settlement paying any positive amount of it to the zero
address proves the mint ran. The quantity is pinned elsewhere entirely: the launcher
announcement commits to `total_lp`, and the TAIL requires
`new_total_lp == expected_delta`. One unit closes this exactly as well as a thousand.

**The LOW boundary finding** — a pool at exactly `total_lp == 1000` has zero redeemable
units — becomes `total_lp > LOCKED_BURN`, i.e. at least one redeemable unit must exist.
Same rule, expressed against the burn rather than a separate floor.

### 3.7 One finding this improves rather than preserves

The third review found that *"an unregistered pool created with `total_lp < 1000` has no
floor enforced anywhere in the prologue, so a depositor's later LP can be partly
trapped."*

The trap is concrete: a pool at `total_lp = 1` takes a deposit that mints 5, reaching 6;
the depositor then tries to withdraw and `burn <= 6 - 1000` is negative, so **no
withdrawal is possible at all**. V13's answer was the prologue assert, which makes such a
pool unspendable so nobody can deposit into it.

With `remove` capped at `total_lp - 1`, that depositor burns their 5 and leaves whole.
**The trap stops being blocked and becomes impossible.**

### 3.8 Every holder can always exit in full

Worth stating because it is the property the burn must not break. `remove` caps at
`total_lp - LOCKED_BURN`, and the burned units are held by the burn address — so the cap
always covers every unit any real holder owns. Verified at burns of 1, 100, 1,000 and
10,000: a later depositor recovers **100.0000%** of what they deposited in every case,
and even the last holder out finds the cap lands on exactly their position, never below
it.

The burn is a one-time cost to the launcher at genesis, not an ongoing tax on liquidity
providers.

### 3.9 Tests

- Mint `N`, burn 1, withdraw everything: the holder receives exactly pro-rata and the
  reserves left equal `1/N` of the pool, to the mojo.
- The same in 1 / 2 / 5 / 20 / 100 removals: identical result, so partial withdrawal
  costs nothing.
- A later depositor, who did not launch the pool, exits 100%.
- The last real holder exits 100%.
- The trap case: an unregistered pool at `total_lp = 1` accepts a deposit and the
  depositor then withdraws in full.
- `total_lp == LOCKED_BURN` at registration: refused. `LOCKED_BURN + 1`: accepted.
- A registration burning zero: refused. Burning 1: accepted.
- A pool at the floor is re-enterable: a deposit mints, and the pool quotes.

### 3.10 The risk, named

This changes a constant three reviews have examined, and `1,000` appears in the CHIP
text. Reviewers will expect it and will ask. The answer is §3.4 and §3.5, and it belongs
loudly in both the spec and the CHIP revision rather than slipped in.


## 4. Provenance — two gaps in what we published

### 4.1 The manifest's source hashes are unverifiable by anyone but us

Ten `.rue` sources are stored with LF in git and sit as CRLF in the Windows working
copy, so the `source_sha256` recorded at build time matches no checkout but ours. The
reviewer's count of ten is exact. The bytecode is fine — the compiler does not care —
so this is provenance metadata, not a shipped-versus-source divergence. But a
provenance record nobody else can reproduce is worse than none, because it looks like
one.

**Fix, both halves:**
- A `.gitattributes` pinning `*.rue`, `*.clvm`, `*.hex` to `text eol=lf` so the working
  copy and the blob agree.
- `source_sha256` computed over **LF-normalized** content, so the figure is stable
  across platforms whatever git does.

Pinned by an integrity check that compares the manifest against `git show` output
rather than the working copy — the check that would have caught this.

### 4.2 The integrity suite verifies less than the security document claims

`FORGE_SECURITY.md` says "built hex matches source". The suite recompiles and compares
only the **pinned upstream names** and the **curve exports**. Forge's own leaves and
finalizer — the custom code a reviewer is there to check — get a `source_sha256`
staleness test and nothing more. And when `rue` is absent the recompile step prints
`[skip]` and the suite returns 0, so a checkout with no compiler passes the check that
is supposed to prove the compiler's output.

Fifth review, and it is right that this is an engineering gap rather than wording.

**Fix:** the scratch build recompiles **every** puzzle and each shipped hex is compared
byte-for-byte against the fresh one — leaves, finalizer, TAIL, registry, reserve
launcher. When `rue` is absent the suite exits 2 (nothing verified), never 0. The
security document's row says exactly what is checked and by what.

### 4.3 Two documentation claims that were wrong, and the measurements that replace them

**The O-1 proof did not hold.** The V13 pass said a future-dated spend "has
`birth >= h`, so `h == birth`". `birth` is the coin's *creation* height, pinned by
`ASSERT_MY_BIRTH_HEIGHT`; naming a future `h` delays inclusion and moves `birth` not at
all. The argument was wrong. The conclusion survives for a different reason, now
measured (`_sim_v14_review_corrections.py`): with a 20-block gap between the claimed `h`
and actual inclusion, the spend credits `[birth, h]` and the **next** spend credits the
tail `(h, inclusion]` at the spot this one recorded — 165 blocks, none dropped, none
double-counted. That is the proof the V14 pass carries.

**The mint and the payouts are exact, not upper bounds.** The V13 architecture wrote
`lp_delta ≤ invariant mint` and `payouts ≤ pro-rata`. Measured: an `add` asking for one
LP less than the bracket is refused, one more is refused, and a `remove` asking for one
mojo less than pro-rata is refused. An implementer who requested less defensively — a
natural thing to do — would be refused, and the document would have told them it was
allowed. The V14 architecture says **exact** in both places, and the Architecture page
is corrected now.

### 4.4 Who has actually reviewed this

`FORGE_SECURITY.md` says no revision has been reviewed outside the team;
`FORGE_DAO_FEE_V13.md` cites a "second external review". Both cannot be true and a
reader cannot tell which. The V14 security document replaces both sentences with:

| Kind | What | Who |
|---|---|---|
| Commissioned audit | **none** | — |
| External community review, human | CNI review of the CHIP PR | Chia Network, PR 217 |
| External community review, human + AI-driven | the six-agent audit; the re-test | trgarrett, PR 217 |
| Automated | cursor[bot] | — |
| Internal | the CLVM passes, the mutation runs, every simulation, this revision's reviews | ours |

### 4.5 A suite our public documents cite is not public

`_test_v12_v13_review_findings.py` is excluded from forge-puzzles on purpose: it
matches `_test_v12_*.py` and exploits the withdrawn V12. But `FORGE_PUZZLE_V13.md`,
`FORGE_V13_CLVM_PASS.md` and `FORGE_DAO_FEE_V13.md` cite it as evidence, so for every
external reader the citation resolves to nothing.

The same review found two more: `FORGE_V13_CLVM_PASS.md` links a findings log
(`FORGE_SECURITY_AUDIT.md`) that is not published, and `FORGE_AUDIT_TIBETSWAP.md` links
into the retired V12 tree (private), which was withdrawn. Three unreachable citations, one cause: the
publish slice and the documents are checked separately.

**Fix:** the before-and-after suite is republished as `_test_v14_before_after.py`,
written against **V14 and the shipped V13** rather than a withdrawn revision, so it can
be public without publishing an exploit for code that still holds testnet liquidity.
Where a finding can only be demonstrated against a withdrawn revision, the citation
says so explicitly instead of naming a file nobody can open. The findings log is
published, or the link to it removed. The TibetSwap document's link is repointed at the
V13 source, where the same line exists.

> This is the publish-slice lesson again, from the other side: last time we published
> something we meant to withhold, this time we cited something we meant to withhold.
> The slice and the documents have to be checked against each other, not separately.
> A check that every path named in a published document resolves inside the published
> tree belongs in the sync script.

---

## 5. Which assets a pool may hold

V14 states this because today it holds **by accident**. A reserve's puzzle hash is exactly
`CAT(asset_id, p2_delegated_by_singleton)` and the finalizer is curried with it, so an
asset carrying an extra inner layer cannot match. That arithmetic is currently the only
thing standing between a Forge pool and an issuer with a kill switch, and nothing records
it as a defence.

**Revocable CATs (CHIP-0038) and fee CATs (CHIP-0056) are out of scope.** The reasoning,
and the simulations behind it, are in `FORGE_PROJECTS.md` §2 and §3. The short version:

- A revoked reserve is not a smaller reserve, it is an **unreachable** one. The finalizer
  addresses every reserve by coin id on every spend, so one revoked reserve freezes every
  other asset in the pool permanently. Measured on a five-asset pool.
- A fee CAT's normal path requires a `SetCatTradeContext` the finalizer's delegated puzzle
  cannot emit, and its layer re-wraps every child so a reserve would stop matching its own
  curried hash. Forge enforces its fees in the pool puzzle already, so nothing is lost.

**What V14 changes: nothing in the puzzle.** The refusal already happens. V14 makes it
deliberate rather than incidental:

- a comment at the reserve construction saying why the exact hash is load-bearing
- `_test_v14_asset_scope.py`, pinning a layered reserve as refused

That is the whole of it. The point is that nobody later "adds rCAT support" by wrapping
the reserve inner and hands every issuer a kill switch over whole pools.

> One rule from elsewhere is worth carrying into V14's own design, and §1 arrives at it
> independently: CHIP-0056 reconstructs the settlement puzzle hash **on-chain** rather
> than trusting a supplied one, so a coin cannot satisfy its own assertion. Never accept
> an identifier you can derive.


## 6. What V14 does not change

Named so the spec does not quietly widen.

- **The curve.** Byte-identical since V10 and not reopened.
- **The oracle.** The reviewer reconstructed our on-chain example independently and
  got 182 / 5 / 261, matching to the block, and found no residual. Unchanged.
- **The config-root binding.** Confirmed closing both shapes, as a hard `clvm raise`
  for anyone. The reviewer withdrew the implication that it was merely legible; we
  should carry that correction rather than repeat our weaker claim.
- **Genesis burn, the three pinned registry fields, the `>` boundary, the merged
  `collect` payout.** All re-tested, all held.
- **Deregistration or expiry.** Still deliberately absent. With R-1 fixed, "the key is
  taken" once again means "this market exists", which is the premise that argument
  needs — and it is only true once reserves are proved.
- **A pool built from genesis with six custom but mutually consistent leaves.** Real,
  out of scope for the coin, and the registry is the right place to close it — the
  reviewer agrees. The registry already pins the six leaf module hashes, so a
  *registered* pool is covered; an unregistered one is not listed or routed. V14
  states this rather than re-litigating it.

---

## 7. Order of work

1. **Validate the review.** Done — `_test_v13_second_review.py`, 15/15.
2. **This spec, reviewed.** ← we are here
3. **Unpair all V13 LP; clean the wallet.** Keep TXCH, T6, T11, t8, t14; everything
   else to the burn address. Detailed in §8.
4. **Build V14**, then the full simulator run in §7.
5. **Review.**
6. **Testnet launch.**

Nothing is pushed to GitHub before step 4 is green.

### 7.1 Build state — 2026-09-16: built, proved, launched

**Puzzles** (`contracts/v14`): `forge_reserve_launcher.rue` new; `register` derives the
reserve parents from grandparents and asserts each launcher's announcement;
`settlement_binding` in `forge_action_common` used by `swap` and `add`; `LOCKED_BURN = 1`;
protocol 15. Build green, upstream pins hold, integrity 122/122.

**Offline suites, all green:** the twenty ported V13 suites (actions 89, registry 43,
finalizer 42, dao_fee 38, offer lane 108, route lane 136, multipool 26, payout audit 52,
curve equivalence 6,043, manipulation 14, message binding, genesis, oracle, LP forgery,
consensus timelocks, create 22, curve mirror, discoverability, second review 15) and the
eight new ones (§9): reserves proved 21, before/after 13, settlement amount 12, action
binding 11, review corrections 11, asset scope 8, lanes agree 10, integrity 122;
provenance exits 2 until `contracts/v14` is committed.

**Simulator** (`scripts/sim-v14.py`, in-process node): 72 checks, all passed. Found on the
way: the V13 simulator had been refused by every node since the genesis burn arrived,
because its creation lane paid the whole supply to the wallet; fixed in V14's.

**Mutation run** (`scripts/mutate-v14.py`, the UNREACHED rule): 48 assertions, 21 killed,
27 unreached and every one argued in `contracts/v14/mutation-arguments.json`; three lines
the first run reported unreached were masked by a mirror's guard or a wrong bracket and
are killed by new probes. Every V14 assertion is killed.

**Testnet11:** registry launcher
`60b5d21f83ffd1e5d4795904dde60098d4dcee2254554b6ab5b8d60665657f9d` at height 4,692,794;
the 32-pool matrix from one price table via the generated launch script (private: it carries wallet paths), heights 4,692,825 to
4,692,961. Lifecycle matrix (the lifecycle driver (private: it drives our wallet)): 32 adds, 29 swaps, 25
collects, 6 removes, 3 observes and the A1 → A3 multi-hop (10 spends, 464,844,079 cost),
heights 4,692,971 to 4,693,475 — 130 confirmed transactions in the record. The offer lane:
a real swap offer built from the website's snapshot, signed by Sage and settled by the
keyless responder at 4,693,502, the quote matching the payout to the mojo (the first attempt
was refused on index lag, `pool pays 57595, trader asks 61292`, because the index had been
imported at launch and the lifecycle had moved the pool; refreshed and retried). R-1 at the
live registry: the V13 construction pushed by `scripts/v14-squat-probe.py` is refused by the
node with `ASSERT_ANNOUNCE_CONSUMED_FAILED`. The settlement binding at a live pool:
`scripts/v14-settlement-probe.py` pushes a swap whose settlement holds one mojo but is named
as 10,000,000, conservation satisfied and the announcement nonce forged; the node refuses it
with `ASSERT_CONCURRENT_SPEND_FAILED`, and the honest control confirmed at 4,693,692. Owed: the
public repository push.

**Documents:** `FORGE_PUZZLE_V14.md`, `FORGE_V14_ARCHITECTURE.md`, `FORGE_V14_CLVM_PASS.md`,
`FORGE_DAO_FEE_V14.md`; the public README, SECURITY and publish slice prepared for V14 (V13
sources and documents pruned, as V12's were). Not pushed to the public repository yet.

## 8. Retirement and wallet cleanup

V13 comes out the way V12 did. Two properties matter and both must be checked rather
than assumed:

- **Unpair every V13 position first.** A remove cannot cross the burned floor, so each
  pool ends at `MIN_LOCKED_LP` with its LP CAT held by the burn address. That is the
  expected resting state, not an error.
- **Then the wallet keeps only the base CATs**: TXCH, T6, T11, t8, t14. Every LP CAT
  and every other test CAT goes to the burn address.

Sequence: drain → verify every pool is at the floor → inventory the wallet → **show the
burn list for approval before burning anything** → burn → re-inventory. The burn is
irreversible, so the inventory is presented before it runs, not after.

The V13 record becomes history, as V11's and V12's did, and V13's sources are withdrawn
from the public repository on the same rule as its predecessors.

---

## 9. Test plan

Nothing reaches testnet until all of this is green in the simulator.

**Suites, ported to V14** — integrity, curve equivalence, finalizer, actions, registry,
oracle, DAO fee, genesis, create, offer lane, route lane, discoverability, payout audit,
manipulation, multipool, curve mirror, message binding, LP receive forgery, consensus
timelocks.

**New for V14:**

| Suite | Proves |
|---|---|
| `_test_v14_reserves_proved.py` | §1.7 in full — every refusal, the accepted case, the derived parents |
| `_test_v14_before_after.py` | Each finding accepted against V13, refused against V14 |
| `_test_v14_lanes_agree.py` | The deploy lane and the website lane produce byte-identical registration announcements |
| `_test_v14_provenance.py` | Manifest hashes match `git show`, not the working copy; every path cited in a published document resolves inside the published tree; **every shipped hex equals a fresh recompile, and the suite exits 2 rather than 0 when the compiler is absent** |
| `_test_v14_asset_scope.py` | A reserve wrapped in a revocation or fee layer is refused; the refusal is a test rather than an accident of hash arithmetic |
| `_test_v14_action_binding.py` | Every action is bound to the assets it moves, not just the first: a settlement of the wrong asset, a settlement too small to fund the input, **two actions asserting the same settlement**, a later action trying to burn past the floor, and a minted LP coin standing in for a real one |
| `_test_v14_review_corrections.py` | The corrected O-1 accounting under a future-dated `h`, and the mint and payout brackets pinned as exact — one less refused, one more refused |
| `_test_v14_settlement_amount.py` | The derived-id amount binding: the honest settlement passes, a short one is refused, the right amount at the wrong puzzle is refused; and the two routes that do not bind are pinned as not binding |

**Matrix run in the simulator**, the full 32-pool shape: creation through the real
offer-driven path, then adds (proportional, single-sided, mixed), swaps on every
multi-asset pool including the DAO-bearing ones, collects including the merged-recipient
case, removes to the floor, observes on 2-, 4- and 5-asset pools, multi-hop, split,
vault route, routed deposit, zap, wrap — and the **offer lane end to end**, because
driver suites do not exercise the path users touch.

**Audit lanes:** the flash-loan analog, hostile router payout audit, hostile route
plans, intra-bundle manipulation, cross-pool value-per-LP, the stale-height window.

**Mutation run under the new rule:** every assertion deleted in turn; every UNREACHED
line gets a bracket-level probe or a written argument, and the run fails if any line is
both UNREACHED and unargued.

---

## 10. Open, and honest about it

**No external audit.** Four independent reviews have now run against this code and
every one of them found something real, including two against revisions we had already
called done. The reviewer notes their own work was six adversarial passes, not a
security firm. Nothing here is a substitute for an audit, and the trend — each review
finding something — is the argument for one, not against it.

**The corrections owed publicly.** Two, and they should go together. Our published
documents state that I-1 does not reproduce; it does. And the CHIP says the pool is
"built from CHIP-0050's upstream action layer and finalizer with no changes to either" —
the action layer is upstream and unchanged, but the pool's finalizer is
`forge_multi_reserve_finalizer.rue`, Forge's own, and it is exactly where M-4 was closed.
The upstream finalizer serves only the registry. Vendoring it unchanged does not make the
pool's finalizer upstream code, and a reviewer reading the CHIP would look in the wrong
place for the custom code. The CHIP text must say so. That correction should go to PR #217 plainly — that they were
right and we were wrong — rather than waiting for V14 to ship. The reviewer also
offered the serialized bundle; we did not need it, and should say so and thank them
rather than leave the offer hanging.
