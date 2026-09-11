# Forge V11 — architecture, as built

**Updated 2026-09-05.** This describes the V11 that runs on testnet11: the
CHIP-0050 action layer under `contracts/v11`, the registry, the LP CAT, the
keyless responder and its lanes, and the naming layer. The design-phase version
of this document (V10 panels beside V11 targets) is superseded; where a target
was met the picture below is the built shape, and where it was not, the
difference is stated. The workflow charts that pair with this document are the
Workflow Atlas artifact (linked from `FORGE_V11_FOUNDATIONS.md`, phase 8).

Contents:

1. [Coin topology of one pool spend](#1-coin-topology-of-one-pool-spend)
2. [Spend anatomy: action layer and finalizer](#2-spend-anatomy-action-layer-and-finalizer)
3. [State and config](#3-state-and-config)
4. [Authorization: what binds what](#4-authorization-what-binds-what)
5. [The five leaves](#5-the-five-leaves)
6. [The registry and the creation bundle](#6-the-registry-and-the-creation-bundle)
7. [Names: inception, name, symbol](#7-names-inception-name-symbol)
8. [Off chain: driver, lanes, responder, index](#8-off-chain-driver-lanes-responder-index)
9. [Trust tiers](#9-trust-tiers)
10. [What is not built](#10-what-is-not-built)

---

## 1. Coin topology of one pool spend

A pool is one singleton whose inner puzzle is the upstream **action layer**
curried with a **finalizer**, a **merkle root** over five leaves, and the
**state**. Reserves are separate coins that obey the pool through CHIP-0025
messages. Everything a spend creates is hinted with the launcher id.

```mermaid
flowchart TB
    subgraph spend["One pool spend, N reserves"]
        direction TB
        POOL["<b>Pool singleton</b><br/>singleton_top_layer_v1_1 ( action_layer )<br/><i>curried:</i> finalizer · merkle root · state"]
        POOL2["Successor singleton<br/>new state, same launcher, hinted"]
        LEAF["<b>Leaf</b> (one or more, merkle-proven)<br/>swap · add · remove · observe · collect<br/><i>curried:</i> PoolConfig"]
        FIN["<b>Multi-reserve finalizer</b><br/>forge_multi_reserve_finalizer.rue<br/><i>curried:</i> reserve full hashes, inner hashes,<br/>reserve amount program, hint"]
        R0["<b>Reserve 0</b> · asset 0<br/>p2_delegated_by_singleton, nonce = 0<br/>bare XCH or CAT-wrapped"]
        RN["<b>Reserve i</b> · asset i<br/>nonce = i"]
        R0B["Reserve 0 successor<br/>amount = reserve + fees owed"]
        RNB["Reserve i successor"]
        PAY["Payout coin<br/>OFFER_MOD child of a shrinking reserve"]
        SET["Settlement coin<br/>OFFER_MOD, the trader's input"]
        EVE["<b>LP eve / melt coin</b><br/>LP CAT wrapping the pinned mint or melt inner"]
        TAIL["<b>LP CAT TAIL</b><br/>forge_lp_cat_tail.rue<br/><i>curried:</i> launcher_id, 11"]
    end

    POOL -->|"runs, with proofs"| LEAF
    LEAF -->|"conditions, some tagged (-42 i . cond)"| FIN
    FIN ==>|"recreates"| POOL2
    FIN -->|"SEND_MESSAGE mode 23<br/>tree_hash((recreate, tagged_i))"| R0
    FIN -->|"one message per reserve"| RN
    R0 ==> R0B
    RN ==> RNB
    R0 -.->|"CREATE_COIN from a tagged condition"| PAY
    LEAF -.->|"asserts (coin_id . nil)"| SET
    LEAF -->|"SEND_MESSAGE mode 23<br/>lp message"| EVE
    EVE --> TAIL
    TAIL -->|"extra_delta = mint or -burn"| SUPPLY(["LP CAT supply"])
```

**What each piece is for.** The singleton decides; the action layer only runs
leaves it can prove against its root and hands their conditions to the
finalizer. The finalizer is the one place that recreates the singleton and
speaks to the reserves, so a leaf cannot move a reserve except through a
tagged condition the finalizer routes. A reserve holds one asset and does
nothing but obey the message its pool sent it in the same block. The LP eve and
melt coins exist so a supply change cannot be faked: their inner puzzles have
no branch but the TAIL, and the TAIL takes its authority from the pool's
message.

---

## 2. Spend anatomy: action layer and finalizer

The solution the singleton receives, and the order things run in.

```mermaid
flowchart LR
    subgraph sol["Inner solution"]
        direction TB
        S1["puzzles<br/>each distinct leaf once, in order of first use"]
        S2["selectors_and_proofs<br/><b>reverse</b> execution order;<br/>a proof only on the last use of a selector"]
        S3["solutions<br/>one per action, execution order"]
        S4["reserve parent ids<br/>one per reserve"]
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
        F1["walk conditions LAST action first"]
        F2["untagged → the singleton's own conditions"]
        F3["tagged (-42 i . c) → prepend to reserve i's list"]
        F4["CREATE_COIN successor singleton<br/>ASSERT_MY_PUZZLEHASH"]
        F5["per reserve: SEND_MESSAGE mode 23 to<br/>coinid(parent_i, full_hash_i, amount(state', i))<br/>message = tree_hash((recreate_i, conditions_i))"]
    end
    sol --> run --> fin
```

Two rules the driver mirrors exactly. **Ephemeral state** is the height `h`
named by the first action; every later action in the same spend must name the
same `h`, and the prologue (height bind, oracle accumulate, `prev_root`) runs
once. **Per-reserve order** is each action's tagged conditions reversed,
concatenated in execution order, because the action layer prepends lists and
the finalizer walks them last-first; a bundle built in any other order fails
the message pairing (error 147).

A reserve's delegated puzzle is `(1 . [recreate, *tagged_for_i])`: recreate
itself at `reserve_i + fees_owed_i` with the launcher as hint, then whatever the
leaves asked (a payout coin, an announcement). The reserve's
`p2_delegated_by_singleton` receives the pool's message for exactly that
delegated puzzle, so nothing else can be run against it.

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
        C5["price_scale 2^64 · oracle_window 32"]
    end
    subgraph st["ForgeState · curried into the singleton · changes every spend"]
        direction TB
        T1["reserves[i] · the tradable amount"]
        T2["total_lp"]
        T3["fees_owed[i] · protocol fee accrued, uncollected"]
        T4["oracle: last_height · cums[] (price-time accumulators)"]
        T5["prev_root · tree_hash of the state this spend started from"]
    end
    cfg -->|"pool key = tree_hash([assets, weights, fee, protocol fee])"| KEY(["registry slot key"])
    st -->|"reserve coin amount i = reserves[i] + fees_owed[i]"| AMT(["what the reserve coin holds"])
```

The reserve coin holds more than the tradable reserve whenever fees are owed;
quotes read `reserves`, the finalizer sizes coins from both. `prev_root` chains
the states so a history can be replayed from the spends alone, which is how
`forge_v11_resync.py` rebuilds a lagging snapshot.

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
    LEAF["Leaf"]

    POOL -->|"SEND_MESSAGE mode 23 (sender puzzle, receiver coin)<br/>receiver id from parent, full hash, amount(state')"| RES
    RES -->|"RECEIVE_MESSAGE mode 23 for the delegated puzzle it runs"| POOL
    LEAF -->|"SEND_MESSAGE mode 23<br/>tree_hash(['forge-lp-v11', lp_delta, new_total_lp, next_state_root])<br/>to coinid(lp_parent, LP eve or melt full hash, amount)"| EVE
    EVE -->|"RECEIVE_MESSAGE from the pool's full puzzle hash"| TAIL
    TAIL -->|"effective_delta == expected_delta<br/>melt requires a CAT parent"| SUPPLY(["supply change"])
    LAUNCH -->|"genesis: announcement of kv [expected_delta]<br/>from launcher id, parent not a CAT"| TAIL
    LEAF -->|"ASSERT_PUZZLE_ANNOUNCEMENT<br/>sha256(settlement_ph, tree_hash((coin_id . nil)))"| SET
```

Every binding is a CHIP-0025 message or a launcher announcement; no puzzle
takes an authorizing coin id from its own solution. The settlement assertion
binds a coin, not an amount, which is what lets a payout coin become the next
pool's settlement inside one bundle and lets one entry coin fan out to
children (section 8).

---

## 5. The five leaves

Every leaf runs the same prologue, then its own rule; the merkle root commits
to exactly these five.

```mermaid
flowchart TB
    PRO["<b>Prologue</b><br/>first action: valid config, state shape, h > last_height,<br/>ASSERT_HEIGHT_ABSOLUTE h, ASSERT_BEFORE h + 32, ASSERT_MY_AMOUNT 1,<br/>oracle accumulates the PRE-spend price, prev_root = tree_hash(state)<br/>later actions: same h, no prologue"]
    PRO --> SWAP["<b>swap</b> [h, i_in, i_out, gross, claimed, settlement]<br/>claimed brackets the invariant; protocol fee to fees_owed;<br/>reserve out pays claimed - fee to OFFER_MOD; asserts the settlement"]
    PRO --> ADD["<b>add</b> [h, deposits, lp_delta, lp_parent, settlements]<br/>lp_delta ≤ invariant mint; one settlement per deposited asset;<br/>lp message to the eve at coinid(lp_parent, mint full hash, 1)"]
    PRO --> REM["<b>remove</b> [h, burn, lp_parent, payouts]<br/>payouts ≤ pro-rata (vault fee for N=1); lp message to the melt coin;<br/>reserves pay OFFER_MOD"]
    PRO --> OBS["<b>observe</b> [h]<br/>creates a slot valued (h . cums); announces ('forge-observe-v1', h, cums)"]
    PRO --> COL["<b>collect</b> [h, indices]<br/>pays fees_owed[i] to protocol_puzzle_hash, zeroes them"]
    SWAP & ADD & REM & OBS & COL --> FIN(["finalizer"])
```

The swap prices on the state the pool is actually in when the action runs, so
two swaps in one spend price sequentially and a stale second quote is refused.
Observe records the pre-spend price, so a manipulation in the same spend cannot
poison the oracle.

---

## 6. The registry and the creation bundle

```mermaid
flowchart TB
    subgraph reg["Registry singleton"]
        direction TB
        RS["state: initialized · pool_count"]
        L1["init: one-shot sentinels 0x00.. and 0xff.."]
        L2["register [launcher_parent, config, reserves, total_lp, left, right]<br/>key = tree_hash([assets, weights, fee, protocol fee])<br/>left < key < right, both live slots"]
    end
    subgraph slots["Slots (sorted list on chain)"]
        direction LR
        SL["… ← slot(left) ← <b>slot(key, launcher)</b> → slot(right) → …"]
    end
    subgraph bundle["The creation bundle · one transaction"]
        direction TB
        F["Funding coin (the deployer)<br/>creates launcher (memos: name, symbol), LP eve, XCH reserve,<br/>fee settlement, change"]
        LA["Launcher spend<br/>kv [total_lp] → eve singleton at the pool's puzzle hash"]
        CR["CAT reserves<br/>one ring per asset, hinted with the launcher"]
        GM["Genesis LP mint<br/>eve → deployer, TAIL genesis path on the launcher announcement"]
        FEE["Fee settlement → treasury"]
        RG["Registry register spend<br/>recomputes the pool's puzzle hash from launcher id + config,<br/>asserts the launcher announcement, spends and recreates the neighbour slots,<br/>announces ('forge-registered-v11', key, launcher)"]
    end
    F --> LA & CR & GM & FEE
    LA --> RG
    FEE --> RG
    RG --> SL
```

A pool exists only if it is registered, and it registers in the same
transaction that mints it; a second pool with the same key is refused because
its slot already exists. The creation fee is the gate today; an NFT or
allowlist check runs off chain first (phase 8.9).

---

## 7. Names: inception, name, symbol

A pool's identity is its launcher id and the puzzle hashes recomputed from it;
its **name** and **symbol** are metadata, set at mint and updatable by the
deployer, never inputs to any decision.

```mermaid
flowchart LR
    MINT["<b>Inception</b><br/>launcher creation memos: [name, symbol]<br/>written by the funding coin — the deployer's address"]
    RN["<b>Rename</b><br/>a coin the deployer's address creates for itself,<br/>hint = launcher id, memos [launcher, name, symbol]"]
    RES["<b>Resolution</b> (forge_v11_names)<br/>get_coin_records_by_hint(launcher) → coins at the deployer's<br/>puzzle hash whose parent sat there → newest wins, else genesis"]
    UI["Frontend · Sage labels · index<br/>name = glyphs (TXCH by name) · symbol = tickers, weights"]
    PUZ["<b>Immutable</b>: assets, weights, LP fee, protocol fee, asset ids<br/>read from the puzzle through the snapshot"]

    MINT --> RES
    RN -->|"only the launcher parent's puzzle hash can sign one"| RES
    RES --> UI
    PUZ -->|"the truth; hash-verified"| UI
```

Forge is word-minimal: the emoji is the market. The name is the assets'
glyphs (TXCH stays a word: the base asset must be readable); the symbol is the
tickers and weight ratio; the fee is shown from the puzzle. A one-asset pool is
just a pool and its deployer may name its LP as a new token. A memo can be
wrong or malicious; it can never change what a pool is, because everything the
interface asserts about a pool is recomputed from tier 1.

---

## 8. Off chain: driver, lanes, responder, index

```mermaid
flowchart TB
    subgraph trader["Trader"]
        W["Sage wallet<br/>make_offer: offered coins → OFFER_MOD settlements,<br/>requested notarized payments, network fee"]
    end
    subgraph server["Responder · api/_forgeResponder.js · no key"]
        direction TB
        PF["preflight: tip unspent, puzzle hash matches, not in mempool"]
        HT["peak height from the node"]
        ST["forge_stdin.py → V11 lane"]
        PS["push_tx · persist successor snapshot"]
    end
    subgraph py["contracts/"]
        direction TB
        OF["forge_v11_offer.py<br/>swap · add · remove"]
        RT["forge_v11_route.py<br/>multi-hop · split · flow · vault route · routed deposit · zap · wrap<br/>hubs: producers → children → consumers"]
        DRV["forge_v11_driver.py<br/>run_leaf · spend_actions · rings · validate"]
        RS["forge_v11_resync.py<br/>replay spends to the tip"]
        IX["forge_v11_index.py + names<br/>snapshots, emoji names, symbols"]
    end
    subgraph idx["Deployment index"]
        DI["poolSnapshot (V11) · emojiName · poolSymbol · displayLabel"]
    end
    W -->|"offer"| PF --> HT --> ST
    ST --> OF & RT
    OF & RT --> DRV
    DRV -->|"bundle"| PS
    PS --> DI
    RS -.->|"when the index lags"| DI
    IX -->|"import-v11-pools.mjs"| DI
    DI --> UI["pools API · frontend"]
```

**Hubs** are the route composer's one idea: for each asset on a route, the
coins holding it (offer settlements, payouts, redemptions, LP mints) are
producers and the legs that drink it are consumers. One producer and one
consumer bridge directly; otherwise the producers are spent in one ring and the
first pays each consumer a child settlement, the trader's requested payments,
and the remainder to the surplus recipient. Amounts are re-derived per leg on
the pool's current state, and a pool crossed twice runs two actions in one
spend.

---

## 9. Trust tiers

```mermaid
flowchart TB
    subgraph t1["Tier 1 — consensus"]
        H["coins, amounts, puzzle hashes; curried config and state;<br/>the merkle root and the finalizer's hashes"]
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

---

## 10. What is not built

Named so this document does not overstate: offer-driven pool creation through
the registry (the deployer's own script creates today); a vault crossed forward
inside a route (an add mid-route); the DAO fee field (a revision, decided with
V11.1); a name or symbol inside the registry slot or launcher kv list (a leaf
change, deferred with the same decision); the wallet-sdk second driver; the
written CLVM audit pass. The build order is phase 8 in
`FORGE_V11_FOUNDATIONS.md`.

## Where these diagrams come from

| Section | Source |
|---|---|
| 1, 2, 4, 5 | `contracts/v11/puzzles/*.rue`, `forge_v11_driver.py` (`spend_actions`, `assemble`, `run_leaf`), `_test_v11_actions.py`, `_test_v11_finalizer.py` |
| 3 | `forge_action_common.rue` (`PoolConfig`, `ForgeState`, `prologue`), `forge_reserve_amount.rue` |
| 6 | `forge_registry_*.rue`, `scripts/deploy-v11-testnet.py create-pool`, `_test_v11_registry.py` |
| 7 | `forge_v11_names.py`, `deploy-v11-testnet.py rename`, `forge_v11_index.py` |
| 8 | `forge_v11_offer.py`, `forge_v11_route.py`, `api/_forgeResponder.js`, `scripts/import-v11-pools.mjs`, `forge_v11_resync.py` |
| 9 | `forge_v11_offer.snapshot_to_pool`, `_test_v11_discoverability.py` |
