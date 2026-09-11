# Forge V11 — Foundations (phases 1 to 4, and 7)

Record of what landed on 2026-09-05 across six sessions, written so the next
one can start from evidence rather than from the plan. V11 is live on testnet11:
a registry, 16 registered pools covering the launch matrix, and one pool
that has run every leaf. The
[Build Spec](https://claude.ai/code/artifact/c517e2cd-225f-436e-aafc-9bf93e147607)
remains the source of truth; its decision ledger and section 13 carry the
settled items, and this file carries the mechanics: where things are, how they
are built, and what each green run proves.

## What landed

| Item | Outcome |
|---|---|
| CHIP-0025 on testnet11 | **Live.** A mode-23 `SEND_MESSAGE`/`RECEIVE_MESSAGE` pair confirmed in block 4,647,109. |
| Upstream pins | All five reproduce from vendored source under rue 0.8.4; `_test_v11_integrity.py` 75/75. |
| `forge_curve.rue` | Ported. Identical to V10 on 81 real spends and 300 randomized pools; `_test_v11_curve_equivalence.py` 6,043/6,043. |
| Open items (8) | Settled with reasons, written into the Build Spec ledger. |
| CHIP-0040 | Evaluated and **declined** for the LP TAIL — see below. |
| `lpRatio` | Clarified: a vault (N = 1) genesis parameter, fixed at mint and enforced by the puzzle's proportional math; a cap outside the puzzle is cosmetic. No bound proposed. |
| Multi-reserve finalizer | **Written and probed.** `forge_multi_reserve_finalizer.rue` + `forge_reserve_amount.rue`; `_test_v11_finalizer.py` 42/42 through the consensus validator at N = 2 and N = 10. |
| Reserve coins | Upstream `p2_delegated_by_singleton`, nonce = asset index, bare for XCH and CAT-wrapped otherwise; exercised by the same suite. |
| Five leaves, prologue, V11 TAIL, hinted mint inner | **Written and probed** (phase 2). `_test_v11_actions.py` 54/54; merkle root over exactly five leaves in the wallet-sdk shape. |
| Registry singleton | **Written and probed** (phase 3). `init` + `register` leaves; `_test_v11_registry.py` 31/31; recomputes a real pool's puzzle hash from launcher id and config. |
| Python driver, testnet deployment | **Done** (phase 4). `forge_v11_driver.py`, `scripts/deploy-v11-testnet.py`; registry + pool live on testnet11, all five leaves confirmed. |
| Offer flow | **Live** (phase 4). `scripts/v11_offer_router.py`: Sage-built trader offers for swap, add and remove, settled by a keyless router with the surplus as its fee. |
| Route lanes | **Live** (phase 6). `forge_v11_route.py`: multi-hop, split, flow, vault route and routed deposit through one composer; each settled a Sage offer via the responder (4,649,690 to 4,649,707). |
| Off-chain stack | **Live** (phase 5). Responder, pool listing and frontend gate on V11; `forge_v11_offer` settles offers keylessly; a Sage swap settled through the responder at 4,649,587; V11 resync replays spends. |
| Lifecycle across the matrix | **Live** (phase 7). Adds on every pool, swaps on every multi-asset pool, collects, removes, observes, a two-pool multi-hop; 71 confirmed transactions. |
| Launch matrix under V11 | **Live** (phase 7). 16 registered pools on testnet11; `_test_v11_discoverability.py` green against the live record. |
| Multi-action spends, sandwich, same-spend observe | **Probed** (phase 5). `_test_v11_actions.py` 66/66. |
| DAO fee (V11.1, protocol 12) | **Built and live 2026-09-05** (A1: lowered 4,650,872, swap 4,650,884, collect 4,650,887). Recipient in config, rate and owed balance in state, a sixth leaf lowers the rate on the DAO coin's mode-23 message; `_test_v11_dao_fee.py` 31/31. The written CLVM pass over every leaf is `FORGE_V11_CLVM_PASS.md`. |
| Multi-pool bundles | **Probed** (phase 5, 2026-09-05). `_test_v11_multipool.py` 26/26: every lane's touched pools keep their value per LP; 300 random cross-pool sequences the same. |
| Intra-bundle manipulation, hostile router, stale-height window | **Probed** (phase 5, 2026-09-05). `_test_v11_manipulation.py` 14/14 (swap/add/swap and swap/remove/swap as one spend, 400 random mirror sequences, the actor never richer); `_test_v11_payout_audit.py` 23/23 (the trader's floor and the exact protocol fee at any router rate); live: a bundle 40 blocks behind the tip refused, 40 ahead held pending. |
| V10 | **Closed** 2026-09-05. Roadmap frozen with dispositions; the parity gaps are phase 8 below. |

## Layout

V11 is a rue **project**, not a set of single files, because the actions import
the curve module and the finalizer imports upstream types.

```
contracts/v11/
  Rue.toml                     entrypoint = "puzzles", dist_dir = "compiled"
  pins.json                    upstream commit, five tree hashes, vendored-source sha256s,
                               the functions that compile byte-identical to V10
  puzzles/
    common_types.rue           upstream, verbatim (slot.rue imports super::common_types)
    upstream/                  upstream, verbatim, never edited:
      action.rue  finalizer.rue  reserve_finalizer.rue
      p2_delegated_by_singleton.rue  slot.rue  merkle_utils.rue
    forge_curve.rue            the port; export fn per curve function
    forge_multi_reserve_finalizer.rue   N reserves, (-42 index . condition), one mode-23 message each
    forge_reserve_amount.rue   RESERVE_AMOUNT_PROGRAM: (state, i) -> reserves[i] + fees_owed[i]
    testing/passthrough_action.rue      TEST leaf: returns the state and conditions its solution names
    forge_registry_common.rue  constants, key, slot value, pool_full_puzzle_hash rebuild, five_leaf_root
    forge_registry_init.rue    one-shot sentinels
    forge_registry_register.rue  the insert, gated by the fee settlement
    upstream/slot_helpers.rue  upstream's rue-puzzles/slot.rue (create_slot, spend_slot), renamed
  compiled/
    <upstream>.rue.hex/.hash   one per upstream module with a main()
    forge_curve.<fn>.rue.hex   each export compiled standalone, for the suites
    v10_probe.<fn>.rue.hex     the same functions lifted out of pool_singleton_FORGE.rue
    manifest.json              tree hash + source sha256 per output
```

Every `.rue` under `puzzles/` is a module named by its file stem. Actions will
`import forge_curve::*;` the way `reserve_finalizer.rue` does
`import finalizer::*;`. `super::` walks up one directory.

Upstream is Yakuhito/slot-machine at `2d37ba1014e14220c26a0541b5d3199f9d15ec21`,
which is what chia-sdk-types 0.36.0 ships. The pinned hashes are the **rue**
builds (`rue-puzzles/singleton/*.rue.hex`); the clsp builds of the same puzzles
hash differently. Upstream's own manifest pins rue 0.9.0; the installed 0.8.4
reproduces every hash, which the integrity lane proves on each run rather than
assumes.

## Commands

From `projects/chia-cfmm`, with `../../.venv/Scripts/python.exe` and
`PYTHONIOENCODING=utf-8`:

```bash
python scripts/build-v11.py                 # recompile upstream to pins, export curve fns, lift V10 reference
python scripts/record-v10-corpus.py         # walk every V10 launcher on testnet11 -> contracts/compiled/v10-spend-corpus.json
python scripts/probe-chip0025-testnet11.py  # push a mode-23 message pair; --dry-run signs without pushing
cd contracts && python _test_v11_integrity.py && python _test_v11_curve_equivalence.py && python _test_v11_finalizer.py && python _test_v11_actions.py && python _test_v11_registry.py && python _test_v11_offer_lane.py && python _test_v11_route_lane.py && python _test_v11_create.py && python _test_v11_manipulation.py && python _test_v11_payout_audit.py && python _test_v11_dao_fee.py && python _test_v11_multipool.py
# 2026-09-06 after the deepening: 13 suites, 6,771 checks, all green (discoverability grew to 170 across the 20 pools)
cd .. && python scripts/wallet-sdk/export-fixture.py > scripts/wallet-sdk/fixtures.json && node scripts/wallet-sdk/second-driver.mjs   # the second driver, byte-equal
python _test_v11_discoverability.py      # live chain, needs the deployment record
```

`contracts/_v11_testkit.py` is the shared harness: it builds a V11 pool from the
compiled puzzles, assembles the singleton and reserve spends, and validates the
bundle with `chia_rs.get_conditions_from_spendbundle` — the mempool's own code,
which enforces message pairing. It does not check that coins exist or that
lineage is real, so a green run proves the puzzles agree with each other and
with consensus rules; the chain is still the last word.

Exit codes follow the lifecycle skill: 0 pass, 1 fail, 2 nothing exercised
(build outputs or corpus absent). A skip is never a pass.

## The integrity lane — what green proves

`_test_v11_integrity.py`:

- the seven vendored sources still hash to the sha256s in `pins.json` (never edited);
- each upstream build's tree hash equals its pin, and the `.rue.hash` file agrees;
- every manifest entry matches the shipped hex **and** its source's current digest,
  so a source edit without a rebuild fails as "stale source";
- the curve module exports its whole surface (eleven functions);
- `exact_swap_output`, `pow_int`, `sum_weights`, `vault_fee_bps` are byte-identical
  to the V10 functions;
- with rue on PATH, a fresh recompile in a scratch copy hits every pin and every
  shipped curve hex, so the check cannot pass by reading back the last build.

## The curve port — what "moved, not rewritten" means here

`forge_curve.rue` carries the V10 bodies. The only mechanical differences, each
named in the file header:

- reserves are `List<Int>` amounts, not `List<ReserveState>` — V11 state holds
  amounts and the finalizer derives coin ids — so `x.first.amount` became `x.first`;
- `valid_protocol_fees` no longer walks reserve plans (V11 has none). The arithmetic
  core is `protocol_fee_owed(released, bps)`, with the V10 name kept as its predicate;
- the structural checks around the curve (pairwise swap shape, non-decreasing
  adds, mode dispatch, coin-id derivation) belong to the actions, not the module.

`_test_v11_curve_equivalence.py` holds it to that two ways:

1. **The corpus.** `contracts/compiled/v10-spend-corpus.json` is every V10 pool
   spend that confirmed on testnet11: puzzle reveal, solution, decoded config,
   state and action, per generation, per launcher from the deployment index.
   Each row is first replayed through the real V10 puzzle (so the record is what
   consensus ran), then fed to the V11 functions, which must accept the recorded
   figure and refuse one unit either side of it. `forge_math` is checked on the
   same rows, so three implementations agree on every real spend.
2. **The V10 functions themselves.** `build-v11.py` compiles the curve functions
   straight out of `pool_singleton_FORGE.rue`, bodies untouched. Both sets are
   driven with randomized pools — one to ten assets, weights to the cap, every
   fee edge — at the honest value, one unit either side, and at random. Every
   output, including a refusal by exception, must match.

Corpus as recorded on 2026-09-05:

| pool | spends | swap | add | remove |
|---|---|---|---|---|
| txch t6 v10 | 9 | 1 | 5 | 3 |
| txch t11 v10 | 5 | 0 | 4 | 1 |
| t6 t11 v10 | 5 | 0 | 4 | 1 |
| txch t8 v10 | 2 | 0 | 1 | 1 |
| t8 v10 (vault) | 20 | 0 | 5 | 15 |
| txch FLP - t8 v10 | 6 | 1 | 3 | 2 |
| t8 FLP - t8 v10 | 1 | 0 | 0 | 1 |
| txch t6 80-20 v10 | 4 | 2 | 1 | 1 |
| txch t8 75-25 v10 | 5 | 2 | 2 | 1 |
| txch t6 t11 v10 | 3 | 1 | 1 | 1 |
| txch t6 t11 4-1-1 v10 | 3 | 0 | 2 | 1 |
| txch t11 t14 t8 t6 v10 | 3 | 2 | 0 | 1 |
| txch t14 zero-fee v10 | 3 | 1 | 1 | 1 |
| txch t8 max-fee v10 | 1 | 0 | 0 | 1 |
| t14 chest 100x | 7 | 0 | 1 | 6 |
| txch FLP - t14 chest | 4 | 1 | 2 | 1 |
| **total** | **81** | **11** | **32** | **38** |

Every launch-matrix shape is represented: native XCH reserves, all-CAT pairs,
vaults, nested LP, 80/20 and 75/25 weights, three and five assets, weighted
three-asset, zero fee, both caps, and the 100x chest.

## Phase 2 — the five leaves

Landed 2026-09-05, the same day as phase 1. Every leaf is a rue module under
`contracts/v11/puzzles/`, curried with `PoolConfig` (the observe leaf also with
the slot's first-curry hash), and sits behind a merkle root in chia-wallet-sdk's
tree shape (`contracts/forge_v11_merkle.py`; `compiled/merkle.json` records the
leaves, root and proofs). `_test_v11_actions.py` drives each leaf alone through
the consensus validator: **54/54**.

| leaf | solution (a proper list) | what it checks | conditions |
|---|---|---|---|
| `forge_action_swap` | `h, asset_in, asset_out, gross_input, claimed_output, settlement_coin_id` | indices distinct and in range; `exact_swap_output` bracket; `protocol_fee_owed` | assert the input settlement's announcement (V10 shape: OFFER_MOD spent under its own id, no payments); `(-42 out . CREATE_COIN(OFFER_MOD, claimed − fee))`; fee accrues into `fees_owed[out]` |
| `forge_action_add` | `h, deposits[N], lp_delta, lp_parent_id, settlement_coin_ids[N]` | `lp_delta > 0`, a deposit somewhere, `exact_invariant_lp_mint`; one settlement assert per positive deposit | mode-23 `SEND_MESSAGE` to the derived mint eve id (pinned mint inner, amount 1) |
| `forge_action_remove` | `h, burn, lp_parent_id, payouts[N]` | `0 < burn < total_lp`; `exact_withdrawal` with `vault_fee_bps` | `(-42 i . CREATE_COIN(OFFER_MOD, payout_i))` per positive payout; mode-23 message to the derived melt coin id (pinned melt inner, amount `burn`) |
| `forge_action_observe` | `h` | prologue only | slot coin (nonce 1, double-curried upstream `slot.rue`, value `(h . cums)`, hinted with the slot's first-curry hash) and a puzzle announcement of `("forge-observe-v1", h, cums)` |
| `forge_action_collect` | `h, indices` (non-empty) | each named `fees_owed[i] > 0`; the same index twice fails on the second visit | `(-42 i . CREATE_COIN(protocol_puzzle_hash, fee, [protocol_puzzle_hash]))`; `fees_owed[i] = 0` |

The **prologue** (`forge_action_common.rue`) runs in the first action of a spend:
V10's `validate_config` (carried, so the weight caps that bound every
multiplication still hold), state-shape checks, `h > last_height`,
`ASSERT_HEIGHT_ABSOLUTE(h)`, `ASSERT_BEFORE_HEIGHT_ABSOLUTE(h + oracle_window)`,
`ASSERT_MY_AMOUNT(1)`, oracle accumulation on the pre-spend price
(`(r0·w_i·price_scale) / (r_i·w0)` per block elapsed), and
`prev_root = tree_hash(state before this spend)`. Ephemeral state is the height
itself; a later action in the same spend asserts it names the same `h`.

**Config**, curried into every leaf in this order: `asset_ids, weights,
fee_bps, protocol_fee_bps, protocol_puzzle_hash, lp_tail_hash, price_scale,
oracle_window`. The LP asset id is the V11 TAIL curried with
`(launcher_id, 11)`.

**The LP TAIL** (`forge_lp_cat_tail.rue`) is V10's with the coin announcement
replaced by one mode-23 `RECEIVE_MESSAGE` against the pool's full singleton
puzzle hash, rebuilt from the curried launcher and the solution-supplied inner
hash. `AssertMyCoinId` and the action coin id in the message are gone: consensus
commits the receiving coin. Its solution is `(expected_delta, new_total_lp,
next_state_root, pool_inner_puzzle_hash)`; the message both sides compute is
`tree_hash(["forge-lp-v11", lp_delta, new_total_lp, next_state_root])`. Both
V10 locks stay: `parent_is_cat || delta > 0` and `effective_delta == expected`.

Three refinements to the Build Spec, recorded rather than silent:

- **The LP hint is structural, not asserted.** The V11 mint inner
  (`contracts/v11/clsp/forge_lp_mint_inner.clsp`) emits
  `CREATE_COIN(settlement_hash, amount, [settlement_hash])`, so the hint IS the
  coin's inner puzzle hash by construction. No `lp_hint` solution field exists to
  assert. The melt inner is V10's byte for byte (`8b5b948a…`); the mint inner is
  `66811aed…`; both are pinned and baked into `forge_action_common.rue`.
- **Payouts go to `OFFER_MOD`**, as V10, not to a solution-supplied puzzle hash.
  One fewer free field; the offer's notarized payment protects the destination.
- **Observe creates and announces; it does not yet read a past slot back.** The
  slot exists for a later consumer to spend by message.

Costs from the validator (real leaves, settlements included): swap 159.7M on a
two-asset pool, add 237.5M, three-asset remove 224.1M. V10's two-asset swap
bundle was modelled at about 370M.

What `_test_v11_actions.py` proves, 54 checks:

| lane | cases |
|---|---|
| swap | XCH→CAT, CAT→XCH, 80/20 weighted, 3-asset CAT→CAT with the XCH reserve untouched; reserves, fee accrual, trader payout, successor singleton, `prev_root`, oracle, height pins all checked; claimed +1 and −1 refused; same asset refused; missing input settlement refused |
| add | balanced and off-ratio adds mint exactly `forge_math`'s figure through the real eve; the minted LP lands at the recipient's LP CAT puzzle hash **with the CHIP-0020 hint**; `lp_delta` +1 refused; an eve minting a different amount refused by message (147); missing deposit settlement refused; no deposit refused |
| remove | 3-asset pro-rata payouts to `OFFER_MOD` and `CAT(OFFER_MOD)`; vault withholds 30 bps on the crossing; `burn = total_lp` refused; payout +1 refused; **a melt coin fabricated from ordinary mojos with a balanced ring is refused inside the TAIL** (finding 4 stays closed, and it is the TAIL's lock that closes it, not the ring builder) |
| observe | slot coin at the double-curried slot puzzle hash for `(h . cums)`, amount 0; state unchanged beyond the prologue; announcement present |
| collect | after a swap, pays the accrued fee CAT-wrapped to the recipient with its hint, zeroes `fees_owed`, chains `prev_root`, re-creates the reserve holding the curve reserve alone; zero fee, duplicate index and empty list refused |
| tree | `h ≤ last_height` refused; the passthrough leaf under a valid-looking proof refused (117); the right leaf under the wrong proof refused |

Harness facts a driver needs (`_v11_testkit.py`): `puzzles` is a list and the
first puzzle's selector is `2` (then 5, 11, … in order of first use, as the
wallet-sdk driver numbers them); a proof is `(path . hashes)`; the reserve's
delegated puzzle is `(q . (recreate . tagged))` with this reserve's tagged
conditions in **reverse** emission order, because the finalizer prepends; the
mint eve is funded by a coin carrying the whole `lp_delta` in mojos, because
every CAT mojo is an XCH mojo.

## Phase 8 — V10 parity, the next phase

V10 is closed (2026-09-05): nothing is built on it, its roadmap is frozen with
each item's disposition recorded in `FORGE_ROADMAP.md`, and V11 is the only
revision listed, acted on or created. What V10 could do that V11 cannot yet is
the next phase, in dependency order. Each item names its acceptance.

| # | Item | What V10 had | What V11 needs | Done when |
|---|---|---|---|---|
| 8.1 | **Offer-driven pool creation** | The Deploy Pool tab: a creator signs one offer; `prepare-create` / `finalize-create` / `create` and the taker mint the pool | A registration bundle built from a creator's coins with no key: launcher, reserves, genesis LP mint to the creator, registry fee settlement and the slot spend | **Library, builder and API done 2026-09-05.** `contracts/forge_v11_create.py`: the creator's XCH coin is the launcher's parent, so the launcher id and the LP asset id are known before signing and the creator's own spend asserts the genesis LP payment to their address (a router paying itself fails the creator's spend); the creator signs only their coins (Sage `sign_coin_spends`), the router builds launcher, eve mint to OFFER_MOD, LP payment, fee settlement, slots and `register`. `forge_stdin.py` actions `prepare-create` / `create` / `commit-create`; `api/forge-v11-create.js`; `_test_v11_create.py` 20/20 (hostile router refused, duplicates refused, a 10:1 one-asset pool). Live: 🛸⚡ (T11/t14) created keylessly at 4,650,421 by `scripts/v11-create-pool-keyless.py`, the wallet playing the frontend. **Deploy Pool tab wired 2026-09-05:** `src/lib/forgeV11Create.ts` turns the tab's config into a V11 request (weights in basis points become the puzzle's small integer units: 80/20 is 4:1), `handleCreatePool` takes the V11 path unless `VITE_FORGE_POOL_LAUNCH_PRIMARY=legacy-v10`; `api/forge-v11-create.js` picks the wallet's coins and signs them through Sage (`contracts/v11_create_bridge.py`), the builder does the rest keylessly, pushes, commits and re-imports. Exercised end to end through the running host: ⚡💵 (t14/t8) created at 4,650,445. The V10 create lane's refusal stays as the guard for a forced legacy mode. **Remaining in 8.1:** none; a browser-driven creation is 8.2's first row |
| 8.2 | **The interface end to end** | Every tab settled V10 pools through Sage in the browser | Nothing in the frontend was changed beyond the gate, so this is verification: Swap and Aggregator (single, multi-hop, split), Liquidity add and remove, Deposit at market, Balancer flows, vault routes, the Dexie poller's auto-fill, all against V11 pools | **Done 2026-09-06.** Every lane the tabs can reach has been driven against V11.1 pools and confirmed on chain: split swap 4,651,405 and add 4,651,546 through the browser and the wallet; then, at the user's call to accelerate, remove 4,651,567, vault route 4,651,570, balancer flow 4,651,597, routed deposit 4,651,628, multi-hop 4,651,648 and zap 4,651,656 through the same quote → offer → responder path with the offers built over Sage's RPC instead of the wallet popup, since the WalletConnect create-offer lane was already proven on V10 and again on the first two rows here. The pools were verified cold afterwards (A1 through eight generations). **What the browser rows taught:** the wallet round trip is slow and sometimes silent — a request can take minutes to surface in Sage, and a reply can be lost on the way back when a cleared cache leaves WalletConnect residue. `sweepOrphanedRelayState` clears that residue before the client starts, `assertSessionAlive` pings the relay so a dead session is known in seconds rather than after the five-minute request timeout, every wallet action checks the session before awaiting the client, and a lost reply costs nothing because the offer is still in Sage for `scripts/v11-respond-offer.mjs` to settle |
| 8.3 | **Vault crossed forward inside a route** | `_build_vault_wrap_leg` (Roadmap 2d): an add minting a vault's LP mid-route, settled on chain at 4,624,203 | An add leg in `forge_v11_route.py` whose eve is parented by the route's XCH hub and whose backing is carved out of the entry (the trader offers it) | **Landed 2026-09-05.** A `wrap` leg: the incoming asset is deposited into the vault, the vault mints its LP, and the LP settlement coin is the next leg's input. The kind is read off the path (`_kind`: a vault crossed with its LP out wraps, LP in redeems), so the multihop and flow lanes take it with no new payload. The eve and the backing come from the offer's XCH: when XCH is the entry, `wrap_backing` finds the split by fixed point (entry gross + mint = offered) and the composer hands the entry hub's remainder to the wrap; when the entry is a CAT the offer carries the backing as a second asset and XCH beyond the mint returns with the output. Route suite 94/94 (mid-route and entry wraps, wrong asset and missing backing refused, reserves and LP supply of all three pools checked). **Live:** TXCH → t8 on A4, wrapped into B1 (25,485 LP minted), the LP sold for TXCH on B2, one bundle at 4,650,659; the offered 0.003 TXCH came back as 0.01298 because B2 prices the vault LP six times above A4, the same gap the Balancer lists. Cost 604M, fee 0.004 TXCH. Unblocks 8.11 |
| 8.4 | **Markets tab** | Roadmap §3, open on V10 too | Pool rows priced from constituents, the spread column reading the observed cross-pool delta, volume in native units; V11 adds the oracle cumulatives as a TWAP source | **Three fixes landed 2026-09-05.** Pool rows were already priced from their second slot; the Spread column now reads `buildPriceSpreads` (dearest quote over cheapest across the pools quoting an asset against XCH, vault-via-LP quotes included) and shows a dash when one pool quotes alone, since a single pool has fees but no spread to observe; volume is repeated in each asset's own units (`PoolStat.volumeNative`, `TokenStat.volumeNative`, input side, only the pools in view). `poolAnalytics.check.ts` 45/45. Found on the way: the index merge had unioned `emojiSymbols` when TXCH lost its glyph, so every pool's first slots wore old names; `import-v11-pools.mjs` now replaces the batch name keys outright. **TWAP landed 2026-09-05:** the pools API exposes the V11 oracle (`oracle`) and the reading the first import kept (`oracleAnchor`, seeded from the earliest index backup so the span is real); `buildTwaps` gives (Δcum / blocks) / 2^64 in the base slot's units, pinned to a live A4 reading pair (110.6 mojos TXCH per t8 mojo over 1,549 blocks); the Markets pool row shows it when the base slot is XCH. **Top view landed 2026-09-05:** a Top filter shows the five deepest pools and the five most-held assets by liquidity (the set the split planner's variants and the balancer's cycles come from), and every asset row carries a `held` line: the asset's total across the pools that hold it, and how many. 8.4 closed. Note: the offer index only sees trades that go through the API lanes, so the V11 responder runs driven from `scripts/v11-respond-offer.mjs` show no volume; browser-driven trades (8.2) will |
| 8.5 | **Fee sizing by pool count** | Roadmap §4, partial on V10 | `feeFor(kind, poolCount)` at every call site; V11 bundles cost more per pool (finalizer plus reserves), so the tiers are re-measured from built bundles | **Landed 2026-09-05** with 8.13. `networkFee.ts` re-measured from V11 bundles the composer built (swap 198M, add 250M, 2-hop 408M, 3-pool split 606M, 3-pool wrap 596M, zap 357M; live 376M and 604M agreed): 205M per pool crossed, 50M per mint, 100M per extra action in one pool, a 10% margin. The Standard tier is five mojos per cost, the mempool's floor for any nonzero fee (seen live: 50M on a 376M bundle refused); Fast is ten. `feeFor(kind, poolCount, shape)` everywhere: the swap counts its hops plus wrap mints, the routed deposit its sale pools plus a second action when a sale runs through the target. 44/44 fee checks against the measured table |
| 8.6 | **Wallet-sdk second driver** | n/a (V11 phase 4 item) | Byte-equal bundles from a second implementation, catching driver-specific assumptions | **Done 2026-09-05, at the user's scope (single-pool swap, add, remove).** `scripts/wallet-sdk/export-fixture.py` writes each action's pool, every mod as hex, the solution and the coin spends the Python driver built; `scripts/wallet-sdk/second-driver.mjs` rebuilds the singleton spend, every reserve spend and the LP eve and melt rings on chia-wallet-sdk 0.36's CLVM from the protocol description alone and compares puzzle reveal and solution byte for byte: **29/29 equal**. Found on the way: the SDK's `Clvm.int(bigint)` folds values past 64 bits (the pool's 2^64 price scale encoded as 1), so the driver encodes CLVM integers itself; recorded as observation O-3 in the written pass. Routes and creation stay Python-only, as scoped |
| 8.7 | **Documentation parity** | `FORGE_PUZZLE_V10.md`, `forgePuzzleV10.md`, a V10 protocol status page | `FORGE_PUZZLE_V11.md` and a `forgePuzzleV11.md` skill; the workspace protocol status on V11; the Liquidity and Swap surface docs | **Done 2026-09-05.** `FORGE_PUZZLE_V11.md`, the `forgePuzzleV11.md` skill, the workspace protocol status on V11, `FORGE_V11_ARCHITECTURE.md` as built, the Workflow Atlas, and the surface docs: `FORGE_LIQUIDITY.md`, `FORGE_SWAP.md`, `FORGE_MARKETS.md` (Markets, Holdings, Offers). Every tab is documented; the docs index says so |
| 8.8 | **DAO fee field** (Roadmap 5a) | Designed, never built; V10 has no field | A `PoolConfig` field with the monotonic-decrease rule. Config is curried, so this is a revision of the leaves and a new registry pool key: decide whether it ships as V11.1 before the internal audit or after | **Built 2026-09-05 as V11.1 (protocol 12), by the user's call.** The recipient (`dao_puzzle_hash`) is curried config; the rate (`dao_fee_bps`) and the owed balance (`dao_owed`) are state, so the rate can only fall: a sixth leaf, `forge_action_dao_fee`, lowers it on a mode-23 message from a coin at the DAO's puzzle hash (sender by puzzle, receiver by this pool coin: replay-proof by consensus). `swap` takes the DAO slice off the same output as the protocol slice, `collect` pays both recipients from the same reserve, the reserve amount rule adds `dao_owed`, the registry key adds the recipient (two DAOs may list one pair), `register` takes the opening rate, and the LP TAIL asserts protocol 12 so no protocol-11 LP is ever authorized by a protocol-12 pool. **Live 2026-09-05 on A1 (TXCH/T6, opened with a 25 bps DAO fee to the wallet's own puzzle hash):** `dao-fee --new-bps 10` lowered the rate on the DAO coin's mode-23 message at 4,650,872; a Sage swap offer settled through the responder at 4,650,884 accrued the protocol slice (1) and the DAO slice (2) side by side; `collect` paid both at 4,650,887 and `verify-v11-pool.py` walked the three generations cold from chain, all green. The Markets fees column shows the DAO slice; `make-swap` quotes it. `_test_v11_dao_fee.py` 31/31 checks the design's vectors V1–V5 and V7 one by one; every other suite is green on the new shape (integrity 101, actions, finalizer, registry 23, offer 98, route 106, create 20, manipulation 14, payout 23, curve 6,043). Off chain: `PROTOCOL_VERSION`/`FORGE_PROTOCOL_VERSION` 12 everywhere, quotes take the DAO slice from live state, the Deploy Pool form has a DAO control (recipient address or hash, rate up to 100 bps), the deploy script has `--dao-ph --dao-fee-bps` and a `dao-fee` command. The protocol-11 registry and its 20 pools are retired like V10 was; the matrix is re-created under V11.1 by `scripts/v11-1-redeploy.sh` (record kept as `.awizard/v11-testnet.protocol11.json`) |
| 8.9 | **Creation gate, off chain first** (Roadmap 1c) | Not started | The wallet is already connected: verify the connected wallet holds an NFT of a collection id, in the frontend for the interaction and in the router by its own scan of the creator's puzzle hash. The puzzle stays as it is: the router and frontend change continuously while testing, and a puzzle rule is immutable and widens the attack surface. Bypassing the router lets a creator pick custom values, but never past the fees the puzzle enforces. Later, when the rule has settled, a varying-fee build inside the puzzle: one lane for holders of the asset, one for everyone else | **Landed 2026-09-05, off chain.** `FORGE_CREATION_GATE_COLLECTION` (server) names an NFT collection; `api/forge-v11-create.js` probes Sage (`v11_create_bridge.py gate` → `get_nfts` filtered by collection) and refuses `prepare-create`/`create` with `CREATE_GATED` for a non-holder; `GET ?probe=gate` reports the rule; the Deploy Pool flow asks the router first and, when the connected wallet can list its NFTs, checks that list too (`holdsCollection`, any field). Unset, creation is open. **Allowlist added 2026-09-05 (the user's call: approve addresses, no NFT to hold):** `FORGE_CREATION_GATE_ALLOWLIST` names approved creators by address or puzzle hash; the router passes its signing wallet by address and refuses a connected address that is not listed (`probe=gate&address=`, `address` on `prepare-create`/`create`); either rule passes when both are set. Verified: the router wallet (Hearts on Fire, listed) passes, a foreign connected address is refused, the collection path still refuses a non-holder. Documented in `FORGE_DEPLOY_POOL.md` |
| 8.10 | **Split candidates from the smallest pool up** (Roadmap 2c follow-up) | Open on V10 | Candidate selection prices each path at full size and keeps one route per path, so a shallow-but-cheap pool never enters the split. Keep the top few pool variants per path as candidates (the Markets tab will track holdings and pricing for the top pools and CATs anyway) and fill from the smallest route up, pouring the rest into the larger routes; the winner is whichever allocation nets the most after fees and post-trade price impact | **Landed 2026-09-05.** `getSplitRouteQuotes` keeps the top three pool variants per path (deduplicated by pool set), so two pools of one pair can both be branches; pairwise splits refine to the mojo as before, and a greedy allocation hands the order out in 40 chunks to whichever pool-disjoint branch pays the most for the next chunk, then tidies with slice transfers: water-filling, the shallow pool first. `splitGreedy.check.ts` 9/9: three depths of one pair, a three-way split beating every two-way, shares ordered by depth, marginal rates level within 5%. Wrap routes stay out of splits until the backing can be apportioned across branches |
| 8.11 | **Wrap-direction routes in ordinary swap quotes** (Roadmap 2d open) | Excluded by `forgeAdapter` | Open the route whenever it improves the swap's price, settling through the flow lane (which needs 8.3). A swap quote and a balancer quote are the same graph | **Landed 2026-09-05.** `forgeAdapter` quotes wrap-direction paths: the entry is sized by the same fixed point as the composer when the trader pays XCH (entry + mint = offered), and a CAT entry carries the XCH backing as a second offered asset; the two-leg swap-then-redeem shape keeps the vault-route lane, every other vault arrangement rides multihop. `wrapRoute.check.ts` 13/13. Redeem-first is still refused. The live proof is 8.3's route at 4,650,659 and 8.12's at 4,650,704 |
| 8.12 | **Asset-revisiting cycles, per leg** (Roadmap 2b open) | Marked non-executable | An asset passed twice (TXCH → t8 → wrap → LP → t8 → TXCH) has a new price the second time; the planner must quote each leg on the state its pool is in after the earlier legs, with the revisited asset's price already moved, and the builder wire per leg, not per asset pot. `forge_v11_route.py` already re-derives per leg against threaded pool state; the planner and the toposort need the same | **Landed 2026-09-05.** The composer wires a chain leg to the one leg it follows (`Leg.feed_from`, `_chain` links them, hubs keyed by source), so a revisited asset is two coins instead of one pot; a flow keeps per-asset pots, where netting makes that correct. `arbCycle.ts` marks revisiting cycles executable again. Route suite 106/106 including a four-pool revisit. **Live:** TXCH → t8 on A4, wrapped into B1, the LP sold for TXCH on B2, that TXCH buying t8 again on C2, one bundle at 4,650,704; TXCH and t8 each passed twice. Found on the way: the record file lags the chain after responder runs, so `v11-lifecycle-matrix.py resync` first, and the importer now keeps the newer of the record's and the index's snapshot |
| 8.13 | **Fee sizing per leg** (with 8.5) | Bundle-level floor | Each leg's cost is measured from its own spends (finalizer, reserves, hub coins) and the bundle fee is their sum, so a long route never under-pays for a leg | **Landed 2026-09-05** with 8.5: `BundleShape {mints, extraActions}` on `estimateBundleCost`, and the call sites pass the legs they build |
| 8.14 | **Emoji names** | V10 pools carried emoji asset symbols from the create tab | Forge is word-minimal: the emoji is the market (the lock is the multisig, the hammer and pick the forge). Every pool has an emoji name (assets' glyphs heaviest first, weight ratio when unequal, fee when it is an edge) and asset symbols in the V10 style; new pools carry the name as a memo on the launcher's creation, readable from the launcher's parent spend without any puzzle change | **Done 2026-09-05** for the 16 pools (`forge_v11_index.py` `emoji_name`, `create-pool --emoji`, the record's `emoji` field); new emoji-named pairs: ⚡🍀 (t14/T6, height 4,650,233) and 🌀🌊 (t8/T11, height 4,650,236), each carrying its name as a memo on the launcher's creation. A pool's **name** is its glyphs only (TXCH 🍕, 🛸💵, 💵 for the one-asset pool, which gets no glyph or word of its own since it is just a pool; its deployer may rename it to anything, a new token name included); everything else is its **label**: tickers, weight ratio and fee (`TXCH/T6 4:1 · 0.30%`, `t8 · 0.30%`), so two pools of one pair stay distinguishable. In Sage the LP's name is the forge glyph plus the pool name and its ticker line is `LP ` plus the label. TXCH is the base asset and is named, never a glyph (TXCH 🍕, TXCH 🛸⚡💵🍕). Glyphs: 🍕 T6, 🛸 T11, 💵 t8, ⚡ t14, from `src/lib/tokenRegistry.json` (`emoji`), the single source for the frontend, the Sage label sync and the index. **Renames** (`contracts/forge_v11_names.py`, `deploy-v11-testnet.py rename`): a coin the deployer's address (the launcher parent's puzzle hash) creates for itself, hinted with the launcher id, second memo the name, third the symbol; authenticated by that address's signature, so the router cannot rename; newest by height wins, else the genesis memo. The index resolves names from chain on import. All 18 names were written to chain by their deployers on 2026-09-05 (heights 4,650,371 and 4,650,376), superseding the two genesis memos. Relaunching a pair is not possible (the registry refuses an identical configuration), so the rename is how a genesis memo is superseded. Open: a label inside the registry slot or launcher kv list would be a leaf change, deferred with the DAO fee decision |
| 8.15 | **Zap add** (workspace roadmap item 9) | V4-era `_zapPlanner.js` quoted a two-asset split for a router that no longer exists; the V10 routed deposit refused a sale through the target | One asset in, LP out, in one signature: the composer accepts a sale through the pool being deposited into, so the swap and the add are two actions of the same pool spend and the add is priced on the state the swap leaves; the planner races three runners (as typed, balanced at market, balanced inside the pool) and the panel says which won and why | **Landed 2026-09-05.** `routed_deposit` no longer refuses the target, `_forgeResponder.js` follows; route suite 74/74 with the zap case (reserves, owed protocol fee, mint on the post-swap state). `planInside` in `depositRouter.ts`, `via: 'market' | 'pool'` on the plan, 18/18 and 9/9 deposit checks. **Live:** TXCH only into A0 txch t14, half swapped inside the pool, 1,658 LP minted at 4,650,545 (fee 0.002 TXCH: a nonzero fee below five mojos per cost is refused, and a two-action pool spend costs 376M). **The finding:** on the invariant join the one-sided add already *is* a zap, priced by the puzzle on the same curve, so balancing inside the pool can only lose the protocol fee to it (8,016 vs 8,019 LP on the check's pool). The race therefore keeps the deposit as typed unless another pool prices the asset, where the market route wins outright. Seen in the Liquidity tab on TXCH ⚡ (0.004 TXCH, one side): 468.274 LP as typed against 467.861 LP zapped inside, with the swap shown and the reason stated. What the roadmap item still lists: versioned LP announcements are V11's message path (done by construction); the range-LP strategy layer is not started |

**Naming principle (decided 2026-09-05).** Inception is tracked: the launcher's
creation memos carry the name and the symbol, both defined at mint. The deployer
may update that slot later (a rename carries `[launcher, name, symbol]`). What is
immutable stays in the puzzle and is never a memo: assets, weights, LP fee,
protocol fee, asset ids. Name and symbol exist for the frontend and wallets; the
on-chain asset id with hash verification is the truth to follow. A one-asset
pool needs no special marking, and a deployer may name its LP as a new token.

**Intra-bundle manipulation (the flash-loan analog), for the audit.** Chia has no
loans, but one bundle can chain spends: swap, then add, then swap back against the
same pool, or sandwich a trader's leg. V11 prices every action on the state the
pool is actually in (a stale quote is refused, `_test_v11_actions.py`), the
observe leaf records the pre-spend price so a same-spend manipulation cannot
poison the oracle, and a trader's own offer bounds what they receive. What is
not yet pinned: that no swap/add/swap or swap/remove/swap sequence in one bundle
ends with the attacker holding more than they started with, less fees. That
probe is phase 5's first adversarial lane.

After parity: phase 5 (internal audit: the written CLVM pass over every leaf,
the stale-height window live, the hostile-router and hostile-route probes, the
intra-bundle manipulation sequences above),
phase 6 (external audit), then mainnet. Deferred and still open from earlier
phases: observe reading a past slot; a creation gate beyond the registry fee
(NFT or allowlist, Roadmap 1c). The current functions and what is left are
kept as a page: https://claude.ai/code/artifact/2ca0d498-976f-44c9-b3f9-66b83568c9d6

## Phase 6 — the route lanes on V11

Landed 2026-09-05. Multi-hop, split, flow balance, vault route and routed
deposit all run on V11 through one composer, `contracts/forge_v11_route.py`,
behind the same payload contracts the V10 lanes had, and each shape settled a
Sage-built offer through the responder on testnet11:

| lane | what ran | height |
|---|---|---|
| multi-hop | TXCH → T6 → T11 across A1 and A3 | 4,649,690 |
| vault route | TXCH → B1's LP on B2, redeemed in the B1 vault for t8 | 4,649,690 |
| split | TXCH → t14, 50/50 across E1 (zero fee) and E2 (max fee) | 4,649,704 |
| flow | the triangle TXCH → T6 → T11 → TXCH over A1, A3, A2 | 4,649,707 |
| routed deposit | TXCH + T6 into D3 (4/1/1), part of the T6 sold through C1 first | 4,649,707 |
| zap (in-pool) | TXCH only into A0 (txch t14): half swapped into t14 inside the pool, then the add, two actions of one spend | 4,650,545 |
| wrap | TXCH → t8 on A4, the t8 deposited into the B1 vault mid-route (its LP minted), the LP sold for TXCH on B2 | 4,650,659 |
| revisit | TXCH → t8 on A4, wrapped into B1, the LP sold for TXCH on B2, that TXCH buying t8 again on C2: two passes of TXCH and of t8, four pools | 4,650,704 |
| V11.1 registry and matrix | the registry re-minted on protocol 12 and the 20 pools re-opened at their on-market reserves; A1 with a 25 bps DAO fee, D2 with 10 | 4,650,811 to 4,650,861 |
| DAO fee lowered | A1's DAO coin sends `["forge-dao-fee-v1", 10]` by mode-23 message; the sixth leaf writes the rate 25 → 10 | 4,650,872 |
| DAO slice on a swap | a Sage swap offer on A1 settled by the responder; `fees_owed [0, 1]`, `dao_owed [0, 2]` | 4,650,884 |
| collect, both recipients | protocol and DAO slices paid from the same reserve; both owed lists back to zero; verified cold | 4,650,887 |
| **browser-driven split swap** (8.2) | quoted in the Swap tab, signed in Sage, settled by the responder: 1,000,000 mojos TXCH for 7,564 t14 (asked 6,701), two branches over six pools — A2 → D2 → t14/t8 at 75%, A4 → G2 → T11/t14 at 25%, 31 coin spends in one bundle | 4,651,405 |
| **browser-driven add** (8.2) | the Liquidity tab on A1 (which carries the 10 bps DAO fee): deposits 1,429,522 mojos TXCH + 686 T6 with 737 mojos of XCH backing, minting exactly the 737 LP quoted, a 1.96% share; approved in Sage and settled by the responder without a copy-paste | 4,651,546 |
| remove (8.2) | 700 LP burned on A1 for [1,356,132 mojos TXCH, 651 T6] | 4,651,567 |
| vault route (8.2) | 400,000 mojos TXCH → B1's LP on B2, redeemed inside the B1 vault for t8 | 4,651,570 |
| balancer flow (8.2) | the triangle TXCH → T6 on C1 → T11 on A3 → TXCH on A2; quoted a loss on these thin pools and settled anyway, which is the honest read of a cycle the Balancer would decline | 4,651,597 |
| routed deposit (8.2) | into D3 (4/1/1) offering TXCH only, with two sales first — TXCH → T11 on A2 and TXCH → T6 on C1 — then the add, one bundle | 4,651,628 |
| multi-hop, an arbitrage (8.2) | TXCH → t8 on A4 → TXCH on C2: 500,000 mojos in, 606,428 back, the disagreement between two pools collected | 4,651,648 |
| zap on V11.1 (8.2) | 1,500,000 mojos TXCH into A1 with 700,000 sold through A1 itself: the swap and the add are two actions of one pool spend, priced on the state the swap leaves | 4,651,656 |
| **converge, three arbitrage cycles** | the matrix priced one CAT up to 30x apart across pools, which is what made routes lossy. Buy where cheap, sell where dear, back to TXCH: T6 and T11 through D1 into D3, and T6 through D2 into A1 | 4,652,673 to 4,652,714 |
| **deepen, 19 adds at one price** | every pool reseeded to a single price per CAT and a common depth: T6 550,745, T11 422,972, t8 3,179, t14 370 mojos a unit. Combined pool XCH 0.0028 → 1.0000 TXCH, cross-pool spread 2,782% → 0.00% | 4,652,748 to 4,652,868 |
| DAO fee paid again | A1 accrued 7,830 mojos to the DAO across the deepening trades; `collect` paid both recipients and left `fees_owed [0, 0]`, `dao_owed [0, 0]`, the rate still 10 | 4,653,347 |

How the composer works, which is the settlement rule applied as a graph:

- A leaf names ONE settlement coin per input and asserts its `(id . nil)`
  announcement; it binds the coin, not an amount, and the mojos balance through
  the CAT ring the coin is spent in (bundle balance for XCH). So a pool's payout
  coin, the OFFER_MOD child its reserve creates, is the next pool's settlement.
- Every asset on the route has a **hub**: its producers (offer settlements,
  payouts, redemptions, LP mints) and consumers (legs, and the trader's request).
  One producer and one pool consumer bridge directly. Otherwise the producers
  are spent in one ring and the first pays each consumer a child settlement of
  its amount plus the trader's requested groups and the remainder to the surplus
  recipient; the other producers carry an empty group. Children's ids are known
  before their consumers run, so the leaves can name them.
- Entry legs drink from the offer; every other leg from earlier legs. Amounts
  are re-derived in topological order against the current snapshots: a hub's
  consumers get their declared shares of what its producers actually hold.
  Equal shares are nudged apart by a mojo, since two children of one parent
  with the same amount would be the same coin (a 50/50 split is exactly that,
  and the first live split was refused for it until this landed).
- A pool crossed more than once in a flow runs several actions in one spend
  (the action layer's multi-action form); the later action prices on the
  earlier one's state. Every local run goes through `run_leaf`, so the leaf's
  own asserts are what refuse a bad plan.
- A redeem leg spends the LP coin paying the pinned melt inner, so the melt has
  a CAT parent (finding 4 stays closed inside routes). The routed deposit's add
  takes every target asset still in the bundle; the first XCH coin parents the
  eve, the backing stays unpaid and reaches the mint through bundle balance.
- The trader's offer carries the network fee. Not ported: a vault crossed
  forward (an add minting LP mid-route), refused with a reason.

`_test_v11_route_lane.py` drives all five through `forge_stdin.build` with
fabricated wallet offers (2- and 3-hop, XCH and CAT splits, an equal split, a
triangle flow, a merge with a pool crossed twice, a vault route, a routed
deposit, greedy traders, mixed revisions): 63/63. The responder's route
builders read the peak from the node for V11 snapshots. The driver's `resync`
replayed every route spend to the exact persisted snapshots.

Found on the way: a local test host started before the version bump was still
listening on 4184 with `FORGE_PROTOCOL_VERSION = 10` in memory, and rewrote the
index from the browser's cached V10 copy (the normalizer runs on the way in).
Restart the host after bumping the version; the workspace `.claude/launch.json`
entry `chia-cfmm-dev` does it.

## Phase 5 — the off-chain stack on V11

Landed 2026-09-05. The responder, the pool listing and the frontend's version
gate now speak V11, and the responder settled a Sage-built offer against a live
V11 pool on testnet11 (swap on `txch t14 v11`, successor confirmed at height
4,649,587; build plus push took 1.4 s).

What changed, and why it is small:

- `contracts/forge_v11_offer.py` is `scripts/v11_offer_router.py` made into a
  library the builder can call, minus the router's own funding coin. Everything
  comes out of the offer: the settlement coins are the leaf's inputs; the payout
  coin pays the requested notarized payments plus a surplus group to the router
  recipient (the configured dev-fee puzzle hash, else the pool's protocol
  recipient); on an add the offered XCH is the LP eve's parent (a second payment
  group on the XCH settlement creates the one-mojo eve, and returns any XCH
  above the backing to the router) and carries the mint's backing; on a remove
  the offered LP settlement pays the pinned melt inner. The network fee is the
  trader's: whatever their offer leaves unspent. `_test_v11_offer_lane.py`
  drives it through `forge_stdin.build` with fabricated wallet offers on a pair,
  an all-CAT pair, a weighted triple and a vault: 98/98, including greedy
  traders refused and the route lanes refused with a reason.
- The V11 snapshot (`pool_to_snapshot` / `snapshot_to_pool`) keeps every key the
  JS side read from V10 snapshots and adds the state; large integers are strings
  because the snapshot passes through `JSON.parse` in node. Rebuilding a
  snapshot re-curries the inner and checks it against the recorded pool coin,
  which is V11's module-hash check: an altered state is refused.
- `contracts/forge_stdin.py` dispatches any request carrying a V11 snapshot to
  the V11 lane (swap, add, remove; multi-pool lanes refuse with a reason until
  ported) and requires `current_height`, since the leaves bind a spend to the
  chain height. `api/_forgeResponder.js` reads the node's peak for V11 requests,
  routes them to `forge_stdin.py`, passes the dev-fee recipient on every action,
  and persists the successor with the tradable reserves (state, not coin
  amounts, which include fees owed). `api/forge-pool.js` supplies the height too.
- `contracts/forge_v11_resync.py` rebuilds a lagging snapshot by replaying the
  on-chain spends between it and the tip (the action layer's solution carries
  the leaves and their solutions); `forge_resync.py` dispatches to it. Probed
  live: the registry driver's record, one swap behind the responder, replayed
  to the exact snapshot the responder had persisted.
- `FORGE_PROTOCOL_VERSION` is 11 in `api/_forgeVersion.js` and
  `src/lib/poolIndexer.ts`. The index normalizer therefore retires the V10
  batches (backup `.awizard/deployment-index.backup-pre-v11-*.json`).
  `contracts/forge_v11_index.py` + `scripts/import-v11-pools.mjs` import the
  registry driver's record into the deployment index, one plan per pool with
  the V11 snapshot, so `api/pools.js` lists the 16 pools unchanged (slots from
  the state, protocol fee from the snapshot). `scripts/v11-respond-offer.mjs`
  runs the responder on an offer from the shell.
- The offer-based create lane (`api/router-create-pool-taker.js`) refuses to
  execute: it would build a V10 pool the index retires. V11 pools register
  through the registry driver until registration is offer-driven.
- The frontend's quoting is untouched: V11's swap maths is byte-identical to
  V10's and its mint is V10's, the fee logic keys on `>= 8`, and the snapshot
  declares the same join rule so the router fee stays in output mode.

Two records now describe the same pools: the registry driver's
`.awizard/v11-testnet.json` and the deployment index. The responder writes only
the index; the driver's `resync` (or the import) brings the record back to the
tip. The route lanes followed in phase 6; offer-driven pool registration is
not yet ported.

## Phase 4 continued — the offer flow, settled by a router

Landed 2026-09-05. `scripts/v11_offer_router.py` is the product path: the
trader's wallet builds and signs an Offer (Sage `make_offer`), and the router,
holding no key, settles it against a live V11 pool. All three shapes ran on
testnet11 against `txch t14 v11`:

| shape | trader offered | trader asked | pool paid | router surplus | height |
|---|---|---|---|---|---|
| swap | 5,000,000 TXCH mojos | 4,004 t14 | 4,045 t14 | 41 | 4,649,497 |
| add | deposits [5125947, 3958] plus the LP backing in XCH | 4,457 LP | 4,503 LP minted | 46 | 4,649,500 |
| remove | 3,000 LP | [3380513, 2609] | [3414660, 2636] | the difference | 4,649,508 |

How it is shaped, which is V10's `forge_offer.py` on V11's leaves:

- The offer's settlement coins (OFFER_MOD, found by `find_offer_settlements`)
  are the pool's inputs, spent under their own coin id with no payments; the
  leaf asserts that announcement.
- The pool's payout coin (the reserve's OFFER_MOD child, or the eve's LP mint to
  OFFER_MOD on an add) is spent with the offer's **requested** notarized
  payments — the nonces and memos the trader's spends assert — plus one more
  group paying the surplus above the request to the router. The trader is paid
  at least what they signed for, at any state; the surplus is the router fee.
- On a remove, the offered LP settlement is spent paying the pinned melt inner,
  so the melt coin is its child with a real CAT parent; finding 4 stays closed
  in the offer flow too.
- An add's LP backing (every CAT mojo is an XCH mojo) rides in the trader's
  offered XCH: the offer carries deposit plus mint, and the router computes the
  deposit as the settlement minus the mint, iterating once since the mint
  depends on the deposit.
- Signatures: the offer keeps the trader's aggregate; Sage signs only the
  router's fee coin; the two are aggregated. The router excludes the offer's
  coins from its own funding selection.

The responder now calls this lane through `contracts/forge_v11_offer.py`; see
phase 5 above.

## Phase 7 continued — the lifecycle across the matrix, through Sage

Landed 2026-09-05. `scripts/v11_ops.py` builds any operation for any registered
pool shape — any asset in or out (XCH, a CAT, or another pool's LP), vault adds
and removes, a two-pool multi-hop in one bundle — and
`scripts/v11-lifecycle-matrix.py` runs them across the record, confirming each
before the next and advancing the record so a stopped run resumes. Coins come
from Sage; every bundle is validated with the mempool's rules before Sage signs.
71 transactions are confirmed in the log, heights 4,647,591 to 4,649,288.

| pool | assets | total_lp now | steps on chain |
|---|---|---|---|
| txch t14 v11 | 2 | 90,070 | swap observe collect add remove |
| A1 txch t6 v11 | 2 | 49,501 | add swap multihop collect remove |
| A2 txch t11 v11 | 2 | 55,000 | add swap collect |
| A3 t6 t11 v11 | 2 | 49,501 | add swap multihop collect remove |
| A4 txch t8 v11 | 2 | 550,000 | add swap collect |
| B1 t8 vault v11 | 1 | 208,946 | add remove observe |
| B2 txch FLP-B1 v11 | 2 | 99,001 | add swap collect remove |
| C1 txch t6 80-20 v11 | 2 | 19,801 | add swap collect remove |
| C2 txch t8 75-25 v11 | 2 | 220,000 | add swap collect |
| D1 txch t6 t11 v11 | 3 | 33,000 | add swap |
| D2 five assets v11 | 5 | 19,801 | add swap observe remove |
| D3 txch t6 t11 4-1-1 v11 | 3 | 16,500 | add swap collect |
| E1 txch t14 zero-fee v11 | 2 | 55,000 | add swap |
| E2 txch t14 max-fee v11 | 2 | 55,000 | add swap collect |
| F1 t6 vault v11 | 1 | 104,473 | add remove |
| F2 txch FLP-F1 v11 | 2 | 55,000 | add swap collect |

**Adds** on all 17 pools at about a tenth of each reserve: the vaults minted
19,940 and 9,970 LP for 20,000 and 10,000 deposited, the 30 bps crossing fee
withheld; the pools whose reserve is a vault's LP took that LP as an ordinary CAT
deposit. **Swaps** on every multi-asset pool at about a twentieth of the in
reserve, including the all-CAT pair, buying vault LP through the pool that
holds it, 4/1 and 3/1 weights, three and five reserves, zero-fee (0 accrued) and
max-fee (25 accrued). **Multi-hop:** TXCH 5,000,000 mojos into A1 for 4,162 T6 mojos; that T6 settlement, spent as the input of A3 in the same bundle, bought 4,235 T11 mojos; height 4,649,136, cost 413,048,958. **Collects** on every pool that
had accrued a fee, including both reserves of the all-CAT pair after the
multi-hop. **Removes** on representative shapes including both vaults and the
nested pool, payouts settled back to the wallet. **Observes** on the five-asset
pool and a vault.

`resync` rebuilds a pool record from the chain: for each spent singleton coin it
decodes the spend's leaves and solutions, runs them on the recorded state, and
advances — a driver that lost a confirmation, or an indexer following every
pool, does exactly this.

## Phase 7 — the launch matrix under V11, registered

Landed 2026-09-05, the same day. Every row of the V10 launch matrix that the
wallet's assets could fund was created, genesis-minted and registered on
testnet11 in one bundle each, through `scripts/deploy-v11-testnet.py create-pool`
(now N-asset: `--assets`, `--reserves`, `--weights`, tickers or `@<pool label>`
for a pool's LP). The registry holds 16 keys in its sorted list and
refused none of them, each inserted between the right neighbours. Where the V10
matrix used A1, A4 and A5, t14 stands in; the shapes are the same.

| pool | shape | fee / protocol bps | height | launcher |
|---|---|---|---|---|
| txch t14 v11 | TXCH 50% / t14 50% | 30 / 5 | 4,647,596 | `be4c270006a4…` |
| A1 txch t6 v11 | TXCH 50% / T6 50% | 30 / 5 | 4,647,687 | `23d5b0f60b73…` |
| A2 txch t11 v11 | TXCH 50% / T11 50% | 30 / 5 | 4,647,691 | `ca7ad0f3e98a…` |
| A3 t6 t11 v11 | T11 50% / T6 50% | 30 / 5 | 4,647,693 | `40246776b294…` |
| A4 txch t8 v11 | TXCH 50% / t8 50% | 30 / 5 | 4,647,698 | `5465252fccbf…` |
| B1 t8 vault v11 | t8 100% | 30 / 0 | 4,647,703 | `67009143f620…` |
| B2 txch FLP-B1 v11 | TXCH 50% / @B1 t8 vault v11 50% | 30 / 5 | 4,647,709 | `ef90bbdfcf89…` |
| C1 txch t6 80-20 v11 | TXCH 80% / T6 20% | 30 / 5 | 4,647,713 | `6cbfdd0efa46…` |
| C2 txch t8 75-25 v11 | TXCH 75% / t8 25% | 30 / 5 | 4,647,718 | `3859e62c8df1…` |
| D1 txch t6 t11 v11 | TXCH 33% / T11 33% / T6 33% | 30 / 5 | 4,647,723 | `af8a8dbcecc3…` |
| D2 five assets v11 | TXCH 20% / T11 20% / t14 20% / t8 20% / T6 20% | 30 / 5 | 4,647,724 | `c2291c05789f…` |
| D3 txch t6 t11 4-1-1 v11 | TXCH 66% / T11 16% / T6 16% | 30 / 5 | 4,647,732 | `062c69dcb397…` |
| E1 txch t14 zero-fee v11 | TXCH 50% / t14 50% | 0 / 0 | 4,647,734 | `d1808b2b5604…` |
| E2 txch t14 max-fee v11 | TXCH 50% / t14 50% | 200 / 100 | 4,647,740 | `827a466d168b…` |
| F1 t6 vault v11 | T6 100% | 30 / 0 | 4,647,744 | `d0754bc0c7ad…` |
| F2 txch FLP-F1 v11 | TXCH 50% / @F1 t6 vault v11 50% | 30 / 5 | 4,647,745 | `381e1cbfba07…` |

What the matrix proves under V11 that the first pool did not: an all-CAT pair
with no XCH reserve (A3), single-asset vaults (B1, F1), a pool whose reserve is
another pool's LP (B2, F2 — the LP asset id resolved from the registry record),
integer weights 4/1 and 3/1, three and five reserves under one finalizer, a
weighted three-asset pool, and both fee edges (0/0 and 200/100) as distinct
registry keys against the same pair.

`_test_v11_discoverability.py` runs against the live record and asks the node
what a wallet or indexer would: one hint on the registry launcher returns every
slot and generation; one hint on a pool launcher returns its reserves and every
re-created generation (the eve is found as the launcher's child, since the
standard launcher attaches no memo); the LP recipient's hint returns the LP
coins and their unspent sum equals `total_lp` for the sole holder; every
registration's config is readable from the registry's spends.
`scripts/v11-lp-holders.py <launcher>` rebuilds a pool's full LP coin history
from its eves and reports holder, amount, from height, to height — the interval
an incentive engine needs — and every coin but the transient melt carries its
holder's hint.

### Phase 5 progress — adversarial cases closed this session

Multi-action spends now exist in the driver (`spend_actions`, wallet-sdk
selector and proof conventions) and in `_test_v11_actions.py` (66 checks):
swap then collect in one spend sharing `h`, swap then observe, two swaps with
one proof for the shared leaf. Closed: **sandwich** — a second swap in the same
spend cannot claim its pre-spend quote, so a trader's settlement minimum is
enforced by the offer against the post-sandwich price; **observe cannot record
a same-spend price** — the slot holds the prologue's pre-spend accumulator even
after a swap in the same spend; two actions naming different heights refused; a
proof omitted for a selector never verified refused. Found on the way: the
finalizer hashes each action's tagged conditions **reversed, in execution
order**, because the action layer prepends condition lists; the driver mirrors
that and the harness caught the difference.

### Phase 5 progress — the adversarial lanes, 2026-09-05

Three more lanes, all green, and one live probe:

- **Intra-bundle manipulation** (`_test_v11_manipulation.py`, 14 checks): a
  swap/add/swap and a swap/remove/swap each as ONE pool spend with three
  actions, validated by the mempool's validator, the puzzle's end state equal
  to the mirrors' and the actor's end position — priced at the pre-sequence
  spot — below their start in both (the swap/add/swap lost 86,567
  mojo-equivalents of 3,000,000 to fees); the swap back cannot claim the
  pre-sequence quote, the add cannot mint one LP above the moved-state mirror,
  the remove cannot pay one mojo above the moved-state share; and 400 random
  sequences of swaps, adds and removes on the mirrors (which the puzzle pins)
  with the actor never richer at pre-sequence prices and the pool's value per
  LP never falling across any action. The flash-loan analog is closed for
  single-pool sequences.
- **Hostile router** (`_test_v11_payout_audit.py`, 23 checks): at 0, 30, 500,
  10,000 and 100,000 bps the trader is paid exactly what they notarised, the
  router's take stops exactly at the surplus, the protocol fee is owed
  exactly, and trader + router + protocol equals what the curve released;
  asking one mojo above the release refuses the bundle; with no router
  recipient the surplus goes to the pool's protocol address, never nowhere.
- **Hostile route plan**: covered by the actions suite's curve bracket (over-
  and under-sized outputs refused), the sandwich refusal, and the route
  suite's guards (a chain leg fed the wrong asset, a wrap of the wrong asset,
  a wrap with no backing, over-asking).
- **Stale-height window, live** (testnet11, tip 4,650,761): a swap pinned 40
  blocks behind the tip was refused by the node with
  `ASSERT_BEFORE_HEIGHT_ABSOLUTE_FAILED`, as the prologue intends. A swap
  pinned 40 blocks AHEAD was accepted into the mempool as PENDING and held for
  its height. **Observation for the written pass:** the prologue binds `h` from
  below (`ASSERT_HEIGHT_ABSOLUTE h`) and above (`h + oracle_window`), but a
  bundle may name a future `h` and wait; it then accumulates the oracle with
  `h − last_height` blocks while it lands anywhere in `[h, h + 32)`, so a
  builder can skew one observation's weight by up to the window. Bounded by
  `oracle_window` and by the pool coin staying unspent until then; noted as a
  low-severity design observation, not a fund-loss path.

**The written CLVM pass** is `FORGE_V11_CLVM_PASS.md` (2026-09-05): every leaf
of V11.1 read top to bottom, each assert with the suite check that pins it, two
observations (the future-dated height, the zero-recipient symmetry) and no
assert without a pin. The minimal wallet-sdk driver is done (29/29 byte-equal, 8.6). **The
multi-pool lane** is `_test_v11_multipool.py` (26 checks): every composer lane
(multihop, split, the arbitrage triangle, wrap, revisit, routed deposit, zap)
built and consensus-validated, and for every pool a bundle touches the value
behind one LP unit never falls; then 300 random cross-pool sequences (1,616
actions) on the mirrors with the same invariant after every action. The pin is
on the pools, not the actor: across pools a route may collect a price
disagreement, which is arbitrage and what the Balancer exists to collect
first; no pool is ever drained below its own invariant. The internal audit's
lanes are all green. Remaining: the browser-driven 8.2 rows once the wallet
is paired, and the external audit.

## Phase 4 — the Python driver, and V11 live on testnet11

Landed 2026-09-05. `contracts/forge_v11_driver.py` is the production driver: it
is the harness's construction code moved into a module with real coins pluggable
(`make_pool(..., launcher_parent=, reserve_coins=, state=)`,
`make_registry(..., launcher_parent=, state=)`), and `_v11_testkit.py` is now a
thin layer over it — fake-coin factories and the validator. One implementation;
the five simulator lanes prove the same code that signs on testnet.

`scripts/deploy-v11-testnet.py` is the operator flow: coins from Sage (picked
from `get_coins`, puzzles rebuilt from `get_derivations`, CAT lineage read from
the parent's spend on chain), every bundle validated locally with the mempool's
rules before signing, pushed to the node, confirmed by watching the successor
coin. `.awizard/v11-testnet.json` carries the singletons' state; a snapshot is
in `contracts/development/artifacts/v11_testnet_deploy_2026-09-05.json`.

### What is live

| step | height | tx | cost |
|---|---|---|---|
| registry | 4,647,591 | `80e9abebbcc6…` | 68,252,284 |
| create-pool | 4,647,596 | `2db0e2dd99bd…` | 238,645,994 |
| swap | 4,647,600 | `5a0aaf0014e6…` | 202,245,160 |
| observe | 4,647,610 | `c68f3c97d922…` | 139,674,468 |
| collect | 4,647,622 | `d49a3837a1fe…` | 142,057,462 |
| add | 4,647,630 | `955fd0779f5a…` | 286,284,421 |
| remove | 4,647,640 | `67bd8c8cf5d3…` | 272,463,721 |

- **Registry** launcher `31ab7ead7b06d698dd8a2f2126025418a7093e041cb3adeca6b7fe4b17a9570f` (protocol 11; retired 2026-09-05 when V11.1 re-opened the matrix on registry `599ba997bc5ec16d185f2bfde20f5e3e8ac70cee42ef94fc0ebeacc9c8110f24`, heights 4,650,811 to 4,650,861): minted and `init` run in one bundle; both sentinel slots and every generation are returned by `get_coin_records_by_hint` on the launcher id.
- **Pool** `txch t14 v11` launcher `be4c270006a43e3f4e3ec451de02685507913fe77d6301a4f58e24d583c3e5ca`, LP asset `cc74fba00f456d32d340926fdba72c2851607f8f4e8e485a35fbdd155583dec6`: TXCH 100,000,000 mojos / t14 100,000, `total_lp` 100,000, fee 30 bps, protocol 5 bps. Created, genesis-minted and **registered in one bundle**.
- **Swap**: 10,000,000 TXCH mojos → 9066 t14 mojos, protocol fee 4 accrued into `fees_owed`.
- **Observe**: slot created for `(h . cums)` and announced.
- **Collect**: the accrued fee paid CAT-wrapped and hinted to the recipient.
- **Add**: deposits [20000000, 20000] minted 20070 LP by message.
- **Remove**: burned 30000 LP for payouts [32481052, 27717], both settled back to the wallet in the bundle.
- **Sage sees the LP.** The genesis coin of 100,000 LP appeared in the wallet's CAT list unaided — the hint by construction closes the failure that hid 49,879 LP in V10.

Pool state after the run: reserves `[97518948, 83217]`, `total_lp` 90,070, `fees_owed` `[0, 0]`, oracle at height 4,647,636.

### Deepening the matrix (2026-09-06)

The launch matrix was seeded pool by pool at whatever reserves each pool
happened to open with, and the result was a market that disagreed with itself:
one CAT priced up to **30x apart** across pools, a Markets spread reading
2,782%, and a 0.000001 TXCH trade showing 21% price impact. Depth was 0.0028
TXCH across twenty pools. The disagreement, not the thinness, was doing most of
the damage — a route that crosses two pools pays their gap.

**The constraint.** The test CATs are single-issuance: T6's entire supply is
768,581 units and we hold all of it. Supply cannot grow, so the XCH a lane can
hold is supply times price, and real depth means repricing the CATs upward. That
is free for a test asset but it is a deliberate choice of scale, so the user
made it: about 1 TXCH across the matrix.

**Converge first.** Three two-pool cycles took the obvious arbitrage and pulled
the pools together — the Balancer's own lane, run against itself (4,652,673 to
4,652,714). The t8 spread went 45% → 19% and t14 40% → 21%; D3's 30x collapsed
to 8x, and one cycle exhausted itself, which is the mechanism reporting that it
had converged.

**Then reseed.** With prices chosen so each CAT's whole deployable budget is
used, every pool got a single large add at the target ratio. No removes were
needed: a deposit that dwarfs the current reserves lands the pool on the
deposit's own ratio, so one transaction per pool both reprices and deepens it.
The vaults went first, because B2 and F2 hold vault LP that has to be minted by
depositing into B1 and F1 before it can be deposited anywhere.

| | before | after |
|---|---|---|
| combined pool XCH | 0.0028 TXCH | **1.0000 TXCH** |
| cross-pool spread, T6 | 2,782% | **0.00%** |
| cross-pool spread, T11 | 3,600% | **0.00%** |
| price impact at 0.000001 TXCH | 21.47% | far below a basis point |
| price impact at 0.0100 TXCH | not tradeable | 21.31% |

A trade ten thousand times larger now costs what the old matrix charged for
dust. Cost: 1.0 TXCH deposited and about 0.095 TXCH in fees, from 85.68 TXCH.
T6 is the asset that runs out first — 58,805 units are left in the wallet, so
the matrix is now near the deepest a fixed T6 supply allows at this price.

**Two operational notes.** The mempool sat at 99.9% of its cost ceiling
throughout, and refuses a fee that cannot displace what is queued; the driver
escalates 5B → 15B → 40B mojos rather than assuming a rate. And a Sage offer
that fails to settle stays open and keeps its coins reserved, so a retry cannot
select the same funds until the stale offer is released with `delete_offer` —
`cancel_offer` only hands back unsigned spends.

### The genesis mint — a gap found and closed in this phase

At creation no singleton of the pool's launcher has spent, so the TAIL's
message path cannot authorize the first mint; V10 let a creator coin vouch. V11
uses the launcher: `singleton_launcher` announces `sha256tree([full_puzzle_hash,
1, kv_list])` exactly once, so the TAIL's **genesis branch** (solution names the
pool's full puzzle hash) asserts that announcement with the list fixed to
`(expected_delta)`, requires no CAT parent, and `new_total_lp == expected_delta`.
The registry's `register` asserts the same announcement with the list fixed to
`(total_lp)`, so a registered pool's genesis supply equals its eve state's
`total_lp`. Probed: a genesis mint one LP above the launcher's figure, a launcher
naming a supply other than the eve state's, and a genesis eve with a CAT parent
are all refused. Recorded in the Build Spec ledger.

### Operational facts

- **Fee rate.** With the mempool at 97% a 100M-mojo fee on a 286M-cost bundle was refused as `INVALID_FEE_TOO_CLOSE_TO_ZERO`; the deploy script now pays 2,000M mojos (about 6 per cost). Nothing moved on the refusal.
- **Every CAT mojo is an XCH mojo.** A mint of `n` LP needs `n − 1` mojos left unspent in the bundle by the funding coin, exactly as V10's creation offer sized it.
- **Sage coin selection prefers small coins**, so `send_xch` previews rarely hand back a single coin large enough for a fee; the script picks its own coin from `get_coins` and rebuilds the puzzle from the derivation's public key (`puzzle_for_synthetic_public_key`).
- **Verifier.** `scripts/verify-v11-pool.py <launcher>` walks the pool's generations on chain and checks the `prev_root` chain, the oracle height, and that `total_lp` moves only by the LP each leaf authorized.

### Not done in phase 4

The wallet-sdk second driver, offer-driven registration, a vault crossed forward
inside a route, and the Markets tab (responder, frontend gates and route lanes
are done, see phases 5 and 6).

## Phase 3 — the registry

Landed 2026-09-05. A second CHIP-0050 singleton with upstream's default
finalizer and two leaves, `forge_registry_init` and `forge_registry_register`,
sharing `forge_registry_common.rue`. `_test_v11_registry.py`: **27/27**.

**What `register` proves before it lists a pool.** It derives the pool's
launcher id from the launcher's parent (so the named coin necessarily runs the
standard launcher puzzle), rebuilds the pool's **full puzzle hash** from that
launcher id and the claimed config and genesis — reserve puzzle hashes,
multi-reserve finalizer, the five leaves, the five-leaf root in the wallet-sdk
shape, the eve state — and asserts the launcher's announcement
`sha256tree([full_puzzle_hash, 1, kv_list])`. The suite's registration only
passes because the rue recomputation equals the Python harness's real pool
puzzle hash bit for bit, which is the revision gate: only V11 hashes are
computable, so no other revision can register. It also carries V10's
`validate_config`, requires every genesis reserve funded and `total_lp > 0`,
and ties `lp_tail_hash` to this launcher's TAIL. It does **not** bound
`lpRatio`.

**Gate.** A settlement (OFFER_MOD) coin pays `CREATION_FEE` to
`TREASURY_PUZZLE_HASH` under the pool's launcher id as nonce; the leaf asserts
that announcement, so one payment registers one pool. `CREATION_FEE = 0`
disables the gate at registry mint.

**Uniqueness.** Key = `tree_hash([asset_ids, weights, fee_bps,
protocol_fee_bps])`. Slots (nonce 0) hold
`(key . (launcher_id . (left_key . right_key)))` in a sorted doubly-linked list
between sentinels `0x00…00` and `0xff…ff`; `register` spends the two
neighbours by message and re-creates them around the new key. Adjacency is
structural (CATalog's pattern): both neighbour hashes are built from shared
variables, so non-adjacent slots cannot both be spent, and `left < key < right`
admits a key exactly once. The same economic config under a new launcher is
refused on either side of its twin.

**Init.** The registry is minted with state `(0 . 0)`; `init` creates the two
sentinels once and sets `initialized = 1`. A second init is refused, as is any
register before it.

**Discovery.** Every slot and the singleton are hinted with the registry's
launcher id, so `get_coin_records_by_hint(registry_launcher)` enumerates the
list; a slot's pool config is in the registry spend that created it. The
register leaf also announces `("forge-registered-v11", key, launcher_id)`.

Costs from the validator: two-asset registration 141.1M, ten-asset 145.8M
(the launcher and fee spends included, the pool's reserves not).

| lane | cases |
|---|---|
| init | register before init refused; sentinels created at their slot hashes, amount 0, hinted; state `(1 . 0)`; init twice refused |
| register | two-asset pool between the sentinels: launcher created the pool at the recomputed hash, treasury paid, three slots re-created, `pool_count` 1; weighted three-asset pool next to it; ten-asset pool |
| adversarial | duplicate economic config under a new launcher refused on both sides; non-bracketing neighbours refused; fee one mojo short, no fee coin, no launcher spend, launcher minting a different config, foreign LP asset id, unfunded genesis reserve, slot spent by a non-registry sender: all refused |

Deferred to the driver phase: the registry's own genesis flow (mint the
singleton, spend `init`), and the creation bundle that mints a pool's reserves
and LP in the same bundle as `register`.

## The multi-reserve finalizer

`forge_multi_reserve_finalizer.rue` is upstream's `reserve_finalizer.rue`
generalized in two places and nowhere else. The marker carries an index —
`(-42 index . condition)` — and an index outside `0..N-1` fails the spend rather
than falling into the base bucket. One `SEND_MESSAGE` goes to every reserve on
every spend, touched or not, mode 23 (sender by puzzle hash, receiver by coin
id), with the receiver derived from the reserve's parent id (solution), its
curried full puzzle hash, and its **pre-spend** amount read from the truth —
never from anything an action returned. The message is the tree hash of the
`(q . conditions)` the reserve's `p2_delegated_by_singleton` runs: the recreate
`CREATE_COIN` first, then that reserve's tagged conditions.

The amount program is curried, as upstream's is; Forge's reads
`reserves[i] + fees_owed[i]` and assumes nothing about the LP:reserve ratio.
Conservation — payouts plus the recreated reserve equal to what the reserve held
— is enforced by the reserve coin itself, so an action whose new state does not
match its payouts asks the reserve to mint and the bundle dies there. The
reverse, a state that shrinks a reserve with no payout, burns the difference as
fee and is legal at consensus; the curve actions are what make it impossible.

What `_test_v11_finalizer.py` proves, 42 checks:

| lane | cases |
|---|---|
| honest | N = 2 (XCH + CAT) and N = 10 (XCH + 9 CATs): one reserve pays out, one grows from a settlement coin, the rest untouched; successor singleton at the action layer curried with the new state; every reserve re-created at its own puzzle hash holding reserve + fees; exactly N messages, all mode 23 |
| adversarial | tag index = N and tag index = -1; missing, extra, wrong, and swapped parent ids; a reserve run with a delegated puzzle the singleton did not send; a reserve naming a different sender inner hash; an action whose state omits its payout (reserve would mint); a growing CAT or XCH reserve with no settlement, and accepted once one is added; a curried state that misstates the pre-spend amount (fails on the receiver id, 147); an untagged payout from the singleton; pool B curried with pool A's reserve hashes, with and without naming B's inner hash as sender |
| quiet | a spend with no tagged conditions still re-creates every reserve |

Costs from the validator, passthrough leaf, no offer settlements: N = 2 spend
120,072,938; N = 10 spend 403,642,162. A real action leaf and the LP handshake
add to these; the V10 two-asset swap bundle was modelled at about 370M.

## The CHIP-0025 probe

`scripts/probe-chip0025-testnet11.py` builds one wallet coin A that creates a
throwaway coin B and sends it a message, and B whose puzzle does nothing but
receive that message — mode 23, `SENDER_PUZZLE | RECEIVER_COIN`, exactly what
the upstream reserve finalizer uses and the Forge one will. A is signed through
Sage (key "Hearts on Fire"), the bundle goes to the node's `push_tx`, and the
script waits for A to be spent.

| | |
|---|---|
| pushed at peak | 4,647,108, mempool at 98% of cost capacity |
| fee | 100,000,000 mojos |
| verdict | `SUCCESS`, confirmed at **4,647,109** |
| tx | `dc97bc876fc4164cbedeeee276ececf0b646552284d4d50aa9580c1907434932` |
| coin A | `fa0eca5fb915a531cabdcc4b7fa8c68e3fae96bec2ccdea812821d747df7a68a` |
| record | `contracts/development/artifacts/chip0025_probe_testnet11.json` |

Message conditions are consensus on testnet11 at our working height. The
finalizer design is no longer gated on this.

## Settled open items

All eight, with reasons, are in the Build Spec's decision ledger (rows marked
"settled 09-05") and summarized in its section 13. In one line each:

| item | settled |
|---|---|
| tag encoding | `(-42 index . condition)`: one strip rule, one failure branch |
| LP TAIL handshake | CHIP-0025 message, mode 23; Forge TAIL kept |
| config placement | curried into actions, committed by the merkle root |
| oracle constants | `PRICE_SCALE` 2^64, `ORACLE_WINDOW` 32 blocks, both per-pool config |
| observe output | puzzle announcement; a message with no receiver fails the spend |
| registry gate | small fee, amount fixed at registry mint |
| hint cost | +36 B, +432,000 cost per payout; +4.32M at N=10 (about 1%); hint unconditionally |
| `prev_root` | in state; 32 B, 384,000 cost per spend |

## CHIP-0040 evaluated and declined

`everything_with_singleton` is the standard singleton-controlled TAIL. Its
whole body is one condition:

```
(RECEIVE_MESSAGE 23 delta <singleton full puzzle hash>)
```

Two of Forge's three LP locks do not survive the move:

- **No `parent_is_cat` check.** The TAIL receives `parent_is_cat` from CAT2 and
  ignores it. A plain coin fabricated at the LP CAT's melt puzzle hash, holding
  `burn` ordinary mojos, would run this TAIL with `delta = -burn`, match the
  pool's message exactly, and balance the ring while destroying no LP — the pool
  releases reserves against nothing. That is security finding 4 (fabricated melt,
  High, V9), which Forge's TAIL closes with `assert parent_is_cat || delta > 0`.
- **The message commits to `delta` only.** Forge's message commits to the
  effective delta, `new_total_lp`, and the successor state root, so the TAIL
  cannot be satisfied by a different accounting than the one the pool applied.

What does carry over is the mechanism: CHIP-0025 messages with the receiver
committed by coin id, which is how V11's handshake is specified. Under the
adoption rule this is case 1 — tested against the standard, ours won — and the
cited test is the fabricated-melt probe in `_test_forge_audit.py`, to be re-run
against the V11 TAIL when it is bumped.

## `lpRatio` — a vault genesis parameter, not a cap

Corrected 2026-09-05 after review. The earlier "bound at 1,000, enforced at
registration" proposal is withdrawn: a cap anywhere outside the puzzle is a
front-end value, because anyone can mint their own pool at any ratio.

- **It exists because of the vault, N = 1.** With one reserve the mint is exactly
  proportional — `mint = deposit × total_lp / reserve` — so the only free
  parameter a vault has is the LP it opens at per mojo of reserve. `lpRatio` in
  `forge_create_pool.py` sets `initial_lp = lpRatio × reserve` and is **refused
  on any multi-asset pool**, where LP is a share of a basket and a ratio has no
  referent (`_test_lp_ratio.py`).
- **Fixed at mint, enforced by the puzzle thereafter.** The mint bracket is
  homogeneous in `total_lp`, so the ratio set at genesis is preserved by every
  mint and burn; the claim and the mint simply multiply by it. Nothing on chain
  can change it and there is no field to correct it.
- **What it changes is granularity and rounding, not ownership.** A CAT displays
  three decimals, 1,000 mojos per unit; the ratio decides how finely a vault's
  claim divides. Each exit's per-asset floor is worth up to `ratio` LP of claim,
  which is the whole of the measured drift (the 100x chest at 5.19x against the
  1x vault at 1.20x). The LP:reserve *rate* moves; the ratio does not.
- **A cap outside the puzzle binds nothing.** The registry can decline to list
  a pool and the interface can default to 1 and warn, but neither stops a pool
  existing at any ratio. If a bound is ever wanted it has to be a puzzle rule,
  and the pool has no genesis-time check today. Not proposed; recorded for the
  owner.

## Attribution cases touched this sprint

| row (draft §11) | was | now |
|---|---|---|
| §10 authorization · CHIP-0025 | ② never evaluated | ① evaluated: live on testnet11, adopted for reserves and the LP handshake |
| §10 authorization · CHIP-0040 | ② never evaluated | ① evaluated and declined; finding 4 is the test |
| §5 discoverability · CHIP-0020 | ③ gap found by failure | adopted; cost measured |

## Next session

Phases 1 to 4 are complete and V11 runs on testnet11. What remains, in the
Build Spec's order: the wallet-sdk second driver (bundles byte-identical to the
Python driver), the responder gates and offer flow, the frontend on the
registry, then the internal audit routine over the whole set before any external
review. Also queued from phase 2: reading a past observation back inside `observe`; a multi-action spend test
(two leaves in one spend sharing `h`); the driver in `forge_transition.py`'s
lineage using the harness conventions above. `lpRatio` remains a vault genesis
parameter that no action assumes anything about.

### The plan phase 2 started from, for the trail

Phase 1 is complete: pins, curve, finalizer, reserve coins, open items. Phase 2
writes the five leaves against `_v11_testkit.py` (swap them in for the
passthrough leaf), in this order: the shared prologue (height pin, amount 1,
oracle accumulation on the pre-spend price, `prev_root`), then `swap`, `add` and
`remove` importing `forge_curve::*`, then `observe` and `collect`. The merkle
root over exactly five leaves must match whatever tree shape the wallet-sdk
second driver builds, so pick that shape before the first proof is written. The
integrity lane gains "root equals exactly five Forge leaves, none of them
`passthrough_action`". `lpRatio` remains a vault genesis parameter no action may
assume anything about.

For the trail, the finalizer session's plan as written before it ran: the multi-reserve finalizer, from `reserve_finalizer.rue`, with
`clvmPuzzleAudit.md` loaded. It imports `upstream::action::StateTruth` and
`upstream::finalizer::*` from the vendored modules, extends `parse_conditions` to
bucket `(-42 index . condition)` with a hard failure for an index outside the
asset count, and sends one mode-23 message per reserve with the receiver derived
from the **pre-spend** amount in the truth. Then reserve coins on
`p2_delegated_by_singleton` with nonce equal to asset index. The integrity lane
gains "merkle root equals exactly five Forge leaves" once leaves exist.
