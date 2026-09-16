# Forge V14 — architecture, as built


> Paths under `api/` and `src/` are in the **interface repository** (forge-ui), not this
> one. They are named so a reader knows which file carries the behaviour; they do not
> resolve inside this repository.

**Updated 2026-09-16.** This describes the V14 that runs on testnet11: the
CHIP-0050 action layer under `contracts/v14`, the reserve launcher, the registry,
the LP CAT, the keyless responder and its lanes, and the naming layer. V14 is
**protocol 15**. It replaces V13 (protocol 14), which a fourth independent review
retired after showing that its registry admitted a pool whose reserves were never
funded -- a market key taken forever for one creation fee; V13's liquidity was
withdrawn, as V12's was before it.

Where V14 differs from V13, the difference is called out inline rather than left
for the reader to spot. The leaf-by-leaf reading of the compiled puzzles is
`FORGE_V14_CLVM_PASS.md`; the disposition of the findings is `FORGE_PUZZLE_V14.md`;
the specification the build followed is `FORGE_PUZZLE_V14_SPEC.md`.

Contents:

1. [Coin topology of one pool spend](#1-coin-topology-of-one-pool-spend)
2. [Spend anatomy: action layer and finalizer](#2-spend-anatomy-action-layer-and-finalizer)
3. [State and config](#3-state-and-config)
4. [Authorization: what binds what](#4-authorization-what-binds-what)
5. [The six leaves](#5-the-six-leaves)
6. [The registry and the creation bundle](#6-the-registry-and-the-creation-bundle)
7. [Names: inception, name, symbol](#7-names-inception-name-symbol)
8. [Off chain: driver, lanes, responder, index](#8-off-chain-driver-lanes-responder-index)
9. [Trust tiers](#9-trust-tiers)
10. [What is not built](#10-what-is-not-built)

---

## 1. Coin topology of one pool spend

A pool is one singleton whose inner puzzle is the upstream **action layer**
curried with a **finalizer**, a **merkle root** over six leaves, and the
**state**. Reserves are separate coins that obey the pool through CHIP-0025
messages. Everything a spend creates is hinted with the launcher id.

```mermaid
flowchart TB
    subgraph spend["One pool spend, N reserves"]
        direction TB
        POOL["<b>Pool singleton</b><br/>singleton_top_layer_v1_1 ( action_layer )<br/><i>curried:</i> finalizer · merkle root · state"]
        POOL2["Successor singleton<br/>new state, same launcher, hinted"]
        LEAF["<b>Leaf</b> (one or more, merkle-proven)<br/>swap · add · remove · observe · collect · dao_fee<br/><i>curried:</i> PoolConfig"]
        FIN["<b>Multi-reserve finalizer</b><br/>forge_multi_reserve_finalizer.rue<br/><i>curried:</i> reserve full hashes, inner hashes,<br/>reserve amount program, hint,<br/><b>config hash · six leaf mod hashes · slot hash</b>"]
        R0["<b>Reserve 0</b> · asset 0<br/>p2_delegated_by_singleton, nonce = 0<br/>bare XCH or CAT-wrapped"]
        RN["<b>Reserve i</b> · asset i<br/>nonce = i"]
        R0B["Reserve 0 successor<br/>amount = reserve + fees owed + dao owed"]
        RNB["Reserve i successor"]
        PAY["Payout coin<br/>OFFER_MOD child of a shrinking reserve"]
        SET["Settlement coin<br/>OFFER_MOD, the trader's input<br/><b>named by parent + amount; id derived</b>"]
        EVE["<b>LP eve / melt coin</b><br/>LP CAT wrapping the pinned mint or melt inner"]
        TAIL["<b>LP CAT TAIL</b><br/>forge_lp_cat_tail.rue<br/><i>curried:</i> launcher_id, 15"]
    end

    POOL -->|"runs, with proofs"| LEAF
    LEAF -->|"conditions, some tagged (-42 i . cond)"| FIN
    FIN ==>|"asserts the root is this config's,<br/>then recreates"| POOL2
    FIN -->|"SEND_MESSAGE mode 23<br/>tree_hash((recreate, tagged_i))"| R0
    FIN -->|"one message per reserve"| RN
    R0 ==> R0B
    RN ==> RNB
    R0 -.->|"CREATE_COIN from a tagged condition"| PAY
    LEAF -.->|"ASSERT_CONCURRENT_SPEND coinid(parent, settle_ph, amount)<br/>+ the announcement (id . nil)"| SET
    LEAF -->|"SEND_MESSAGE mode 23<br/>lp message"| EVE
    EVE --> TAIL
    TAIL -->|"extra_delta = mint or -burn"| SUPPLY(["LP CAT supply"])
```

**What each piece is for.** The singleton decides; the action layer only runs
leaves it can prove against its root and hands their conditions to the
finalizer. The finalizer is the one place that recreates the singleton and
speaks to the reserves, so a leaf cannot move a reserve except through a tagged
condition the finalizer routes. A reserve holds one asset and does nothing but
obey the message its pool sent it in the same block. The LP eve and melt coins
exist so a supply change cannot be faked: their inner puzzles have no branch but
the TAIL, and the TAIL takes its authority from the pool's message.

**What V14 adds here.** Two things, both about *deriving* an identifier the puzzle
used to accept. A leaf that takes value now names its settlement by parent and
amount and derives the coin id, then asserts that exact coin was spent -- so the
amount is bound by the puzzle, not only by the CAT ring and bundle conservation
(section 4). And every reserve is born from a **reserve launcher** (section 6): a
small puzzle that creates the reserve and announces it, whose coin id `register`
derives from the launcher's own parent, so a registered pool's reserves exist by
construction. The finalizer is unchanged from V13.

**What V13 added here.** The finalizer carries the configuration's hash,
the six leaf module hashes and the observe leaf's slot hash, and asserts that the
action layer's merkle root is exactly the root those produce. The action layer
proves that each leaf it runs is *a member* of the root; it never proved that the
six leaves agreed with one another, and six leaves curried with six different
configurations form a perfectly valid root. A pool whose `remove` leaf alone named
a foreign LP TAIL therefore released its reserves against a worthless self-minted
asset. That is closed at the coin level now, and it has a second effect worth
stating: the leaf hashes sit in the finalizer's curry in the clear, so a verifier
reads them off the coin and compares them against the published set. The binding
does not stop an unrelated puzzle existing — nothing can — it makes the leaf set
legible.

---

## 2. Spend anatomy: action layer and finalizer

The solution the singleton receives, and the order things run in.

```mermaid
flowchart LR
    subgraph sol["Inner solution"]
        direction TB
        S1["puzzles<br/>each distinct leaf once, in order of first use"]
        S2["selectors_and_proofs<br/><b>reverse</b> execution order;<br/>a proof only on the last use of a selector"]
        S3["solutions<br/>one per action, execution order<br/>each begins [h, birth, …]; swap and add end with<br/>the settlement's (parent, amount), never its id"]
    end
    subgraph run["Action layer"]
        direction TB
        A1["verify leaf i against the merkle root"]
        A2["run leaf i on ((ephemeral . state) . solution_i)"]
        A3["leaf returns ((ephemeral' . state') . conditions)"]
        A4["prepend conditions; state' feeds leaf i+1"]
    end
    subgraph fin["Finalizer, once"]
        direction TB
        F0["<b>assert Merkle_Root == config_root(LEAVES, CONFIG_HASH, SLOT)</b>"]
        F1["walk conditions LAST action first"]
        F2["untagged → the singleton's own conditions"]
        F3["tagged (-42 i . c) → prepend to reserve i's list"]
        F4["CREATE_COIN successor singleton<br/>state' with reserve_parents rewritten"]
        F5["per reserve: SEND_MESSAGE mode 23 to<br/>coinid(parent_i from STATE, full_hash_i, amount(state, i))<br/>message = tree_hash((recreate_i, conditions_i))"]
    end
    sol --> run --> fin
```

Two rules the driver mirrors exactly. **Ephemeral state** is the height `h` named
by the first action; every later action in the same spend must name the same `h`,
and the prologue (height bind, birth bind, oracle credit, `prev_root`) runs once.
**Per-reserve order** is each action's tagged conditions reversed, concatenated in
execution order, because the action layer prepends lists and the finalizer walks
them last-first; a bundle built in any other order fails the message pairing
(error 147).

A reserve's delegated puzzle is `(1 . [recreate, *tagged_for_i])`: recreate itself
at `reserve_i + fees_owed_i + dao_owed_i` with the launcher as hint, then whatever
the leaves asked. The reserve's `p2_delegated_by_singleton` receives the pool's
message for exactly that delegated puzzle, so nothing else can be run against it.

**What changed since V11.** The solution no longer carries reserve parent ids.
They were moved into state in V12, because a solution-supplied parent let a decoy
coin at the reserve's puzzle hash and amount be substituted for the real reserve;
the finalizer reads them from the pre-spend state and writes the coins it has just
messaged as the next parents. V14 applies the same rule at genesis: the first
parents in state are derived by `register` from the launchers' grandparents, never
claimed (section 6). There is nothing left to name rather than a check that naming
must fail -- and every V13 finding, and two of V12's three, were one mistake in
different costumes: a puzzle taking an authorizing identifier from its own
solution.

---

## 3. State and config

```mermaid
flowchart LR
    subgraph cfg["PoolConfig · curried into every leaf · immutable"]
        direction TB
        C1["asset_ids (ZERO_32 for XCH)"]
        C2["weights (integer units)"]
        C3["fee_bps · protocol_fee_bps · protocol_puzzle_hash"]
        C4["lp_tail_hash (the LP asset id)"]
        C5["price_scale 2^64 · oracle_window 32 · both bounded"]
        C6["dao_puzzle_hash (immutable; the RATE lives in state)"]
    end
    subgraph st["ForgeState · curried into the singleton · changes every spend"]
        direction TB
        T1["reserves[i] · the tradable amount"]
        T2["total_lp"]
        T3["fees_owed[i] · protocol fee accrued, uncollected"]
        T4["oracle: last_height · cums[] · <b>last_spot[]</b>"]
        T5["prev_root · tree_hash of the state this spend started from"]
        T6["dao_fee_bps (may only fall) · dao_owed[i]"]
        T7["reserve_parents[i] · written by the finalizer, never solved;<br/>at genesis, derived by register from the launchers"]
    end
    cfg -->|"pool key = tree_hash([assets, weights, fee, protocol fee, dao ph])"| KEY(["registry slot key"])
    cfg -->|"tree_hash(config) → the finalizer's CONFIG_HASH"| BIND(["the leaf-set binding"])
    st -->|"reserve coin i = reserves[i] + fees_owed[i] + dao_owed[i]"| AMT(["what the reserve coin holds"])
```

The reserve coin holds more than the tradable reserve whenever fees are owed;
quotes read `reserves`, the finalizer sizes coins from all three. `prev_root`
chains the states so a history can be replayed from the spends alone, which is how
`forge_v14_resync.py` rebuilds a lagging snapshot.

**`last_spot` is V13's addition, and it exists for one reason.** A spend is signed
at one height and included at another, and the state it replaces stays in force
until the block that includes it. V12 credited the oracle only over `h - birth`
and wrote `last_height = h`, so the blocks between a spend's claimed height and
its inclusion were credited by nobody and could never be credited afterwards —
spending at `h = birth` every time held the accumulator still while real blocks
passed, and a pool spent honestly in every transaction block did the same by
accident. State now remembers the spot price it last credited, and the prologue
credits the previous state over `(last_height, birth]` at that spot before
crediting the current state from `birth` to `h`.

The registry **pins** `protocol_puzzle_hash`, `price_scale` and `oracle_window` to
its own constants. They are protocol parameters rather than market parameters, so
they neither belong in the key nor stay a registrant's free choice: were the fee
recipient free, a registrant could hold a market's key with a pool that paid the
protocol fee to themselves.

---

## 4. Authorization: what binds what

```mermaid
flowchart TB
    POOL["Pool singleton (finalizer)"]
    RES["Reserve i"]
    EVE["LP eve (mint) or melt coin"]
    TAIL["LP TAIL"]
    LAUNCH["Launcher"]
    SET["Settlement coin (OFFER_MOD)"]
    BURN["LP settlement · burn group"]
    LEAF["Leaf"]
    REG["Registry · register"]

    POOL -->|"SEND_MESSAGE mode 23 (sender puzzle, receiver coin)<br/>receiver id from STATE parent, full hash, amount(state)"| RES
    RES -->|"RECEIVE_MESSAGE mode 23 for the delegated puzzle it runs"| POOL
    LEAF -->|"SEND_MESSAGE mode 23<br/>tree_hash(['forge-lp-v14', lp_delta, new_total_lp, next_state_root])<br/>to coinid(lp_parent, LP eve or melt full hash, amount)"| EVE
    EVE -->|"RECEIVE_MESSAGE from the pool's full puzzle hash"| TAIL
    TAIL -->|"effective_delta == expected_delta<br/>melt requires a CAT parent"| SUPPLY(["supply change"])
    LAUNCH -->|"genesis: announcement of kv [total_lp, eve_coin_id]<br/>asserted with the eve's OWN id from CAT truths"| TAIL
    LEAF -->|"id = coinid(parent, settlement_ph, amount) · amount ≥ input<br/>ASSERT_CONCURRENT_SPEND id · ASSERT_PUZZLE_ANNOUNCEMENT (id . nil)"| SET
    REG -->|"ASSERT_PUZZLE_ANNOUNCEMENT<br/>LOCKED_BURN paid to the zero puzzle hash"| BURN
    REG -->|"P_i = coinid(grandparent_i, launcher hash_i, reserves[i])<br/>ASSERT_COIN_ANNOUNCEMENT from P_i: created inner_i for reserves[i], hinted"| RL["Reserve launcher i"]
    RL -->|"CREATE_COIN reserve i"| RES
```

Every binding is a CHIP-0025 message, an announcement, or a derived coin id; no
puzzle takes an authorizing identifier from its own solution. **The settlement
binding is V14's.** V13 asserted only the settlement's puzzle announcement, whose
nonce is whatever the solver wrote, so a leaf bound the asset and never the
amount; value was guarded by the CAT ring and bundle conservation alone, and a
swap naming a one-mojo settlement as its 250,000 input was accepted whenever
another coin in the bundle supplied the value. A V14 leaf is given the
settlement's parent and amount, derives its id, asserts that coin is spent, and
requires the amount to cover the input. The amount bound is the settlement's own,
because the router carves its fee out of the same coin; the inequality ties it to
the action. A payout coin still becomes the next pool's settlement inside one
bundle, and one entry coin still fans out to children (section 8) -- the composer
knows every parent and amount it creates.

**Two of these are later than V11.** The genesis announcement names the eve's own
coin id, read from the CAT truths rather than from a solution, so exactly one eve
can claim the genesis supply; under V11 any funded coin could assert the
launcher's single announcement and two eves each minted the whole supply. And
`register` asserts the LP settlement's burn group, so a registered pool has
put `LOCKED_BURN` -- one unit, in V14 -- beyond recovery in the transaction that
minted it. The pool
itself cannot check this — it sees a total supply and a burn, never who holds
which unit — but the registry can, because the genesis supply passes through a
settlement whose payments are announced. Since only the TAIL's genesis branch can
create that asset, the same assertion also proves the mint happened rather than
merely that it was authorized.

---

## 5. The six leaves

Every leaf runs the same prologue, then its own rule; the merkle root commits to
exactly these six, and the finalizer checks that it does.

```mermaid
flowchart TB
    PRO["<b>Prologue</b><br/>first action: valid config, state shape, total_lp ≥ LOCKED_BURN,<br/>h > last_height, birth > last_height, h ≥ birth,<br/>ASSERT_MY_BIRTH_HEIGHT birth, ASSERT_HEIGHT_ABSOLUTE h,<br/>ASSERT_BEFORE h + window, ASSERT_MY_AMOUNT 1,<br/>oracle credits (last_height, birth] at last_spot then [birth, h] at the pre-spend spot,<br/>prev_root = tree_hash(state)<br/>later actions: same h, no prologue"]
    PRO --> SWAP["<b>swap</b> [h, birth, i_in, i_out, gross, claimed, settle_parent, settle_amount]<br/>claimed = the invariant bracket, exactly; protocol and DAO slices accrue;<br/>reserve out pays claimed − fees to OFFER_MOD;<br/>settle_amount ≥ gross; asserts the derived settlement is spent"]
    PRO --> ADD["<b>add</b> [h, birth, deposits, lp_delta, lp_parent, settle_parents, settle_amounts]<br/>lp_delta = the invariant mint, exactly; per positive deposit,<br/>settle_amount ≥ deposit and the derived settlement is spent;<br/>lp message to the eve at coinid(lp_parent, mint full hash, 1)"]
    PRO --> REM["<b>remove</b> [h, birth, burn, lp_parent, payouts]<br/>burn ≤ total_lp − LOCKED_BURN; payouts = pro-rata, exactly;<br/>lp message to the melt coin; reserves pay OFFER_MOD"]
    PRO --> OBS["<b>observe</b> [h, birth]<br/>creates a slot valued (h . cums); announces ('forge-observe-v1', h, cums)"]
    PRO --> COL["<b>collect</b> [h, birth, indices]<br/>pays fees_owed[i] and dao_owed[i], one coin if the recipients match"]
    PRO --> DAO["<b>dao_fee</b> [h, birth, new_bps]<br/>0 ≤ new_bps < current, on a mode-23 message from the DAO recipient"]
    SWAP & ADD & REM & OBS & COL & DAO --> FIN(["finalizer"])
```

The brackets are exact: an `add` asking one LP less than the mirror's figure is
refused as surely as one asking one more, and a `remove` asking one mojo under
pro-rata as surely as one over (`_test_v14_review_corrections.py`). `burn ≤ total_lp
− LOCKED_BURN` is the one genuine bound. The swap prices on the state the pool is
actually in when the action runs, so two swaps in one spend price sequentially and
a stale second quote is refused. Observe
records the pre-spend price, so a manipulation in the same spend cannot poison the
oracle.

`collect` pays **one** coin when the protocol and DAO recipients are the same
address. Two identical `CreateCoin`s from one reserve are one coin id twice, which
consensus rejects as `DUPLICATE_OUTPUT` — and since `collect` is the only exit for
accrued fees, that bricked the exit for exactly the pools whose two recipients
coincided.

---

## 6. The registry and the creation bundle

```mermaid
flowchart TB
    subgraph reg["Registry singleton"]
        direction TB
        RS["state: initialized · pool_count"]
        RC["curried constants: leaf mod hashes, creation fee, treasury,<br/>protocol ph · price scale · oracle window,<br/><b>the reserve launcher's mod hash</b>"]
        L1["init: one-shot sentinels 0x00.. and 0xff.."]
        L2["register [launcher_parent, config, reserves, total_lp, dao_bps,<br/><b>reserve_grandparents</b>, eve_coin_id, left, right]<br/>P_i = coinid(grandparent_i, launcher hash_i, reserves[i]) → the eve state's parents<br/>key = tree_hash([assets, weights, fee, protocol fee, dao ph])<br/>left < key < right, both live slots"]
    end
    subgraph slots["Slots (sorted list on chain)"]
        direction LR
        SL["… ← slot(left) ← <b>slot(key, launcher)</b> → slot(right) → …"]
    end
    subgraph bundle["The creation bundle · one transaction"]
        direction TB
        F["Funding coin (the creator)<br/>creates launcher (memos: name, symbol), LP eve, <b>the XCH reserve launcher</b>,<br/>fee settlement, change"]
        LA["Launcher spend<br/>kv [total_lp, eve_coin_id] → eve singleton at the pool's puzzle hash"]
        CR["CAT coins (the creator)<br/>one ring per asset, each creating <b>a CAT-wrapped reserve launcher</b>"]
        RL["<b>Reserve launchers</b>, one per asset, spent in the same bundle<br/>each creates its reserve at the inner hash for its whole amount, hinted,<br/>and announces ('forge-reserve-v14', inner, amount, launcher)"]
        GM["Genesis LP mint<br/>eve → the LP settlement, TAIL genesis path on the launcher announcement"]
        SP["LP settlement splits:<br/><b>LOCKED_BURN → the zero puzzle hash</b>, the rest → the creator"]
        FEE["Fee settlement → treasury"]
        RG["Registry register spend<br/>derives each reserve parent from its grandparent and the launcher hash,<br/>rebuilds the pool's puzzle hash from launcher id + config + the binding + those parents,<br/>asserts the launcher announcement, the burn announcement<br/>AND one reserve-launcher announcement per asset,<br/>spends and recreates the neighbour slots,<br/>announces ('forge-registered-v14', key, launcher, hash, total_lp, eve)"]
    end
    F --> LA & RL & GM & FEE
    CR --> RL
    RL --> RG
    GM --> SP --> RG
    LA --> RG
    FEE --> RG
    RG --> SL
```

A pool exists only if it is registered, and it registers in the same transaction
that mints it; a second pool with the same key is refused because its slot already
exists. `valid_pool` requires `total_lp > LOCKED_BURN` — strictly above, because a
genesis of exactly the floor leaves the creator nothing redeemable, which is the
position the floor exists to prevent.

**The reserve launcher is V14's, and it is the answer to the fourth review.** V13's
`register` took `reserve_parents` in its solution and used it only to rebuild the
pool's puzzle hash; nothing in the bundle spent a reserve, so a registration naming
parents that created nothing was accepted, the slot was taken, and every later
spend by anyone failed message pairing forever. A coin id commits to its puzzle
hash, so `register` now derives each parent itself -- from the launcher's own
parent, the launcher puzzle's hash (CAT-wrapped for a CAT) and the reserve amount
-- and asserts the launcher's coin announcement that it created the reserve. The
only coin that can make that announcement runs the launcher puzzle, and the
launcher puzzle does nothing but create the reserve. Both halves at once: the id
fixes which coin, the puzzle fixes what it did. Every creation lane -- the deploy
script, the website's `forge_v14_create`, the simulator -- creates a launcher per
reserve, and `_test_v14_lanes_agree.py` requires the deploy and website lanes to
produce the same launcher coins, byte-identical announcements and byte-identical
`register` solutions, because S2 existed exactly where two lanes did different
things and one was checked.

**No deregistration or expiry, deliberately.** A registered pool cannot die,
because the floor keeps it alive, and a thinly funded pool is a live market anyone
may deepen, so "the key is taken" means "this market exists", which is the
permissionless answer. That claim is pinned by a test rather than argued: a pool
registered at the smallest thing the registry admits, reserves `[1, 1]` and 2 LP,
accepts a real deposit and mints billions of LP against the squatter's single
remaining unit -- and under V14 that squatter has at least funded `[1, 1]`. A minimum reserve would price small markets out without changing
what one creation fee buys.

---

## 7. Names: inception, name, symbol

A pool's identity is its launcher id and the puzzle hashes recomputed from it; its
**name** and **symbol** are metadata, set at mint and updatable by the deployer,
never inputs to any decision.

```mermaid
flowchart LR
    MINT["<b>Inception</b><br/>launcher creation memos: [name, symbol]<br/>written by the funding coin — the deployer's address"]
    RN["<b>Rename</b><br/>a coin the deployer's address creates for itself,<br/>hint = launcher id, memos [launcher, name, symbol]"]
    RES["<b>Resolution</b> (forge_v11_names, shared)<br/>get_coin_records_by_hint(launcher) → coins at the deployer's<br/>puzzle hash whose parent sat there → newest wins, else genesis"]
    UI["Frontend · Sage labels · index<br/>name = glyphs (TXCH by name) · symbol = tickers, weights"]
    PUZ["<b>Immutable</b>: assets, weights, LP fee, protocol fee, asset ids<br/>read from the puzzle through the snapshot"]

    MINT --> RES
    RN -->|"only the launcher parent's puzzle hash can sign one"| RES
    RES --> UI
    PUZ -->|"the truth; hash-verified"| UI
```

Forge is word-minimal: the emoji is the market. A memo can be wrong or malicious;
it can never change what a pool is, because everything the interface asserts about
a pool is recomputed from tier 1.

---

## 8. Off chain: driver, lanes, responder, index

```mermaid
flowchart TB
    subgraph trader["Trader"]
        W["Wallet (WalletConnect)<br/>make_offer: offered coins → OFFER_MOD settlements,<br/>requested notarized payments, network fee"]
    end
    subgraph server["Responder · api/_forgeResponder.js · no key"]
        direction TB
        PF["preflight: tip unspent, puzzle hash matches, not in mempool,<br/>birth corrected from the node's confirmed height"]
        HT["peak height from the node"]
        ST["forge_stdin.py → the V14 lane (protocol 15)"]
        PS["push_tx · persist successor snapshot"]
    end
    subgraph py["contracts/"]
        direction TB
        OF["forge_v14_offer.py<br/>swap · add · remove"]
        RT["forge_v14_route.py<br/>multi-hop · split · flow · vault route · routed deposit · zap · wrap"]
        DRV["forge_v14_driver.py<br/>run_leaf · spend_actions · rings · validate"]
        RS["forge_resync.py → forge_v14_resync.py<br/>replay spends to the tip"]
        IX["forge_v14_index.py + names<br/>snapshots, emoji names, symbols"]
    end
    subgraph idx["Deployment index"]
        DI["poolSnapshot (V14) · emojiName · poolSymbol · displayLabel"]
    end
    W -->|"offer"| PF --> HT --> ST
    ST --> OF & RT
    OF & RT --> DRV
    DRV -->|"bundle"| PS
    PS --> DI
    RS -.->|"when the index lags"| DI
    IX -->|"import-v14-pools.mjs"| DI
    DI --> UI["pools API · frontend"]
```

**Hubs** are the route composer's one idea: for each asset on a route, the coins
holding it (offer settlements, payouts, redemptions, LP mints) are producers and
the legs that drink it are consumers. One producer and one consumer bridge
directly; otherwise the producers are spent in one ring and the first pays each
consumer a child settlement, the trader's requested payments, and the remainder to
the surplus recipient.

**Two off-chain defects V14 found by pushing, 2026-09-16.** Both sit in this layer rather
than in a puzzle, and neither was visible from reading.

*The replay under-counted a repeated leaf.* `forge_v14_resync.replay_spend` rebuilt a
pool's state by zipping the action layer's `puzzles` against its `solutions`. `puzzles`
lists each **distinct** leaf once; `solutions` has one entry **per action**. A spend that
ran `swap` twice therefore replayed as one swap and produced a state that never existed.
That matters here specifically: the resync is what the browser reaches for when it finds a
pool behind the chain, so a wrong replay repairs a pool into a coin that does not exist.
Each action's leaf is now resolved through its selector, and a selector count that
disagrees with the solutions is refused rather than half-replayed
(`contracts/_test_v14_replay.py`, 9/9).

*The builders trusted a peak that can lag.* `push_and_wait` returns as soon as the
successor coin record exists; the node's reported peak can still be one block behind it. A
vault remove built straight after a confirmed add claimed a height one below the coin's
birth, and the prologue's `h >= birth` refused it locally. `v14_ops.claim_height` clamps
every builder to `max(peak, birth)` -- claiming the birth height is always legal, and
understating `h` only defers oracle credit to the next spend.

**Three off-chain things move with the protocol, and each has bitten us.** The
protocol constant lives in `api/_forgeVersion.js` *and* `src/lib/poolIndexer.ts`
*and* the compiled browser bundle; a bundle built before a bump silently drops
every pool while the API serves them correctly. `forge_resync.py` maps protocol to
a replay module and needs the new number or a lagging pool cannot be repaired —
which is the path the browser takes when it finds a pool behind the chain. And the
fee model in `src/lib/networkFee.ts` must be re-measured, because a fee below the
node's floor of 5 mojos per cost is refused outright and leaves a signed offer
resting in the trader's wallet (a zero fee, by contrast, is valid): V14 adds one
condition per settlement to `swap` and `add`, and a registration carries a launcher
spend per reserve -- measured at 287M cost for a two-asset registration and 336M
through the website lane, against V13's 220M.

---

## 9. Trust tiers

```mermaid
flowchart TB
    subgraph t1["Tier 1 — consensus"]
        H["coins, amounts, puzzle hashes; curried config and state;<br/>the merkle root, the finalizer's hashes and its leaf-set binding"]
    end
    subgraph t2["Tier 2 — index"]
        HINT["hints: launcher on every pool coin, recipient on LP,<br/>launcher on renames"]
    end
    subgraph t3["Tier 3 — metadata"]
        M["name and symbol memos; the deployment record"]
    end
    H -->|"every puzzle decision reads only this"| DEC(["puzzle decisions"])
    H -->|"snapshot_to_pool re-curries and checks the pool coin"| UI(["what the interface asserts"])
    HINT -->|"finds, never proves"| UI
    M -->|"displays; attributed to the deployer's address"| UI
    M -.->|"never"| DEC
```

V13 moved one thing up a tier: whether a pool's six leaves share a configuration
is readable off the coin, from the finalizer's curry. V14 moves another. Whether a
registered pool's reserves *exist* used to be an off-chain fact -- the record said
so, and a registration that lied was accepted. Now it is a consensus fact: the
parents in the eve state are launcher coins the registration bundle spent, and the
launchers created the reserves in that same transaction.

---

## 10. What is not built

Named so this document does not overstate: a vault crossed forward inside a route
(an add mid-route); a name or symbol inside the registry slot or launcher kv list;
an observation slot that any leaf spends (the slot is written for a consumer to
spend with its own message, and the announcement is the live path); a
deregistration or expiry path, which is a decision rather than an omission
(section 6); and the wallet-sdk second driver beyond the fixture it exports.

Above all: **no external audit.** Four independent reviews have been run against
this code and each found real defects. That is an argument for more review, not
for confidence.

## Where these diagrams come from

| Section | Source |
|---|---|
| 1, 2, 4, 5 | `contracts/v14/puzzles/*.rue`, `forge_v14_driver.py` (`spend_actions`, `assemble`, `run_leaf`, `reserve_launcher_spends`, `settlement_ref`), `_test_v14_actions.py`, `_test_v14_finalizer.py`, `_test_v14_settlement_amount.py`, `_test_v14_action_binding.py` |
| 3 | `forge_action_common.rue` (`PoolConfig`, `ForgeState`, `prologue`, `credit`, `spots`, `settlement_binding`), `forge_reserve_amount.rue`, `_test_v14_oracle.py` |
| 6 | `forge_reserve_launcher.rue`, `forge_registry_*.rue`, `scripts/deploy-v14-testnet.py create-pool`, `forge_v14_create.py`, `_test_v14_registry.py`, `_test_v14_reserves_proved.py`, `_test_v14_lanes_agree.py`, `_test_v14_before_after.py` |
| 7 | `forge_v11_names.py` (shared), `deploy-v14-testnet.py rename`, `forge_v14_index.py` |
| 8 | `forge_v14_offer.py`, `forge_v14_route.py`, `api/_forgeResponder.js`, the index importer (in the private repository), `forge_resync.py` |
| 9 | `forge_v14_offer.snapshot_to_pool`, `_test_v14_discoverability.py` |
