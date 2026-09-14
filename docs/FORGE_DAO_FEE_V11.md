# DAO fee — V11 design and threat model

Written before the build, on purpose. The vectors below are not review items to
check afterward; they are the constraints the feature is built *from*. When V11
work starts, this document is the specification, and any implementation choice
that cannot be traced to a line here needs a line added here first.

Status: **built, live and verified on testnet11 (2026-09-05, re-verified 2026-09-06).**

The three questions a reviewer asks of this design, and what answers them:

**Is the fee actually paid?** Yes, on chain and in the suite. `swap` accrues the
DAO's slice into `dao_owed` beside the protocol's, the reserve coin is re-created
holding `reserves + fees_owed + dao_owed`, and `collect` pays both recipients
from that reserve and zeroes both balances. Live on pool A1: both slices accrued
on a swap at height 4,650,884 and were paid at 4,650,887; the pool then accrued
7,830 mojos to the DAO across the deepening trades and paid them out at
4,653,347, leaving `fees_owed [0, 0]` and `dao_owed [0, 0]`. The suite checks the
two recipients separately, with distinct puzzle hashes — worth saying plainly,
because on the live pool the DAO recipient and the protocol recipient are the
same test wallet, so the chain run alone does not distinguish them.

**Does the reduction work?** Yes, live: 25 bps to 10 bps at height 4,650,872 on
the DAO coin's mode-23 message, and the suite's own 50 to 20 case checks that the
rate is the only field that moved and that every reserve is re-created at its
current amount.

**Can it be reversed?** No, and by two independent arguments. The leaf asserts
`0 <= new_bps < dao_fee_bps`, and that assert is compiled CLVM: an attempted
raise fails with a `clvm raise` out of the leaf itself, not out of a driver
guard. Above that, the rate is curried into the singleton's state, so a pool at
a different rate hashes to a different puzzle and is simply a different coin —
there is no spend that raises the rate, only a coin that does not exist. The
suite pins both, including that a pool at zero contains no transition at all.
No leaf but `dao_fee` writes the field: `swap`, `add`, `remove` and `collect`
each copy `dao_fee_bps` through from the prior state, and `observe` returns the
prior state whole.

 On pool A1 (TXCH/T6, 25 bps to the deployer's own puzzle hash) the DAO coin lowered the rate to 10 at height 4,650,872, a swap accrued both slices at 4,650,884, and `collect` paid both recipients at 4,650,887; `verify-v11-pool.py` walked the generations cold. The implementation
follows this document with one structural translation for the action layer:
the recipient is curried config, the RATE lives in state (`dao_fee_bps`) with
its owed balance (`dao_owed`), and the decrease is a sixth leaf,
`forge_action_dao_fee`, authorized by a mode-23 message from a coin at the
recipient's puzzle hash (sender by puzzle, receiver by the pool coin — replay
committed by consensus, V2 and V4). Config is unchanged by the decrease, so V1
holds by construction rather than by field-wise comparison. The registry key
adds the recipient. `_test_v11_dao_fee.py` checks V1–V5 and V7 one by one;
`FORGE_V11_CLVM_PASS.md` section 7 reads the leaf against them. V10 pools
carry no DAO fee and cannot gain one; neither can protocol-11 pools.

---

## Why V10 has no DAO fee

The V10 `PoolConfig` is: `protocol_version`, `pool_module_hash`, `asset_ids`,
`weights`, `fee_bps`, `protocol_fee_bps`, `protocol_puzzle_hash`,
`lp_tail_hash`, `reserve_inner_puzzle_hash`. There is no DAO field. The
frontend historically carried `daoFeeBps` and `daoPuzzleHash` anyway, and the
Stage-2 builder folded the fee into the curve:

```ts
// spendBundles.ts — buildCanonicalStage2BootstrapTarget
params.swapFeeBps + (params.daoFeeBps ?? 0),   // curried as ONE fee_bps
```

Two consequences, both bad:

1. **The money never reached a DAO.** A folded fee stays inside the curve and
   accrues to LPs, like any liquidity fee. `daoPuzzleHash` was validated and
   recorded but never curried — the chain never saw it. A UI that says
   "paid to an address you choose" over this plumbing is lying.
2. **The folded sum is subject to `MAX_FEE_BPS`.** `validate_config` asserts
   `fee_bps <= 200` at the top of `main()` — on *every* spend, not just
   creation. A combined fee over 200 mints a pool whose swaps, adds and
   removes all fail: the reserves are stranded permanently. The frontend's
   old caps (500 in `poolCreationService`, 2000 in
   `validateFixedFieldStage2Inputs`) both allowed this.

So V10 is locked instead: the creation UI offers no DAO control,
`buildConfig` pins `daoFeeBps: 0` / `daoPuzzleHash: undefined`, and
`poolCreationService.validate` refuses a nonzero value from any caller. The
conversion helpers live on in `src/lib/daoFee.ts` (dormant, tested) because
V11 needs exactly them.

---

## The V11 feature

Add to `PoolConfig`, mirroring the protocol-fee pair that already exists:

```
dao_fee_bps: Int,          // charged on swap output, like protocol_fee_bps
dao_puzzle_hash: Bytes32,  // recipient; fees leave the pool, never fold in
```

With the same guard the protocol fee already has:

```
// A fee with no recipient would be burned rather than collected.
assert config.dao_fee_bps == 0 || config.dao_puzzle_hash != zero_bytes32();
```

And its own explicit cap (`MAX_DAO_FEE_BPS`), asserted in `validate_config`,
sized so that `fee_bps + protocol_fee_bps + dao_fee_bps` at their maxima is
still a fee a trader could sanely pay. The fee is charged and paid exactly the
way `valid_protocol_fees` and `ReservePlan.protocol_fee_amount` already work —
derived by the pool, bound by announcement, unskippable and uninflatable by
the reserve. No new payment mechanism; a second instance of the audited one.

### Adjustability: monotonic decrease only

The DAO may lower its fee after launch — including to zero — and may never
raise it. Rationale:

- A raisable fee is a key that reprices LPs *after* they commit capital: the
  classic rug vector. Refuse it structurally, not by policy.
- Lower-only preserves every promise made to a depositor: the fee they priced
  in is the worst case forever.
- "Revoke to zero" is the special case; monotonic decrease is the same
  security argument (`next < current` instead of `next == 0`) and strictly
  more useful. Build the general form.

Mechanically this is a new action mode. The singleton already recomputes its
successor puzzle hash from config on every spend (`curry_tree_hash` at the end
of `main`); today the config half is byte-identical by construction. The
fee-decrease action produces a successor whose config differs in exactly one
field. Irreversibility is then topology, not a flag: a pool at
`dao_fee_bps = 0` contains no transition producing a nonzero successor, so
there is nothing to forge and nothing to reset.

---

## The vectors, and the assert that closes each

These are numbered so V11 review can check them off one by one.

### V1 — Successor substitution (the critical one)

The moment the successor config may differ from the current config *at all*,
the check must be equality-with-one-exception:

> next config == current config in every field, **except** `dao_fee_bps`,
> which must satisfy `0 <= next.dao_fee_bps < current.dao_fee_bps`.

Anything looser — "next config is well-formed", "next config passes
validate_config" — lets a spender swap in arbitrary weights, fees, asset ids,
or a different `lp_tail_hash` under cover of a fee decrease. This is the same
shape as the V4–V9 unbound-reserves bug: the fatal pattern is validating the
successor for plausibility instead of validating it *against the
predecessor*. Field-by-field equality, spelled out, no structural shortcuts.

Note `dao_puzzle_hash` is NOT the exception: it stays fixed even as the fee
decreases. A recipient change is a different feature with a different threat
model; do not smuggle it into this one.

### V2 — Authorization forgery

Without proof the DAO initiated the decrease, anyone can zero the fee — pure
griefing against the treasury. Bind the action the way the LP melt/mint path
already binds its action coin (`lp_parent_id` + announcement):

- The decrease spend must assert an announcement from a coin whose puzzle
  hash equals the curried `dao_puzzle_hash`.
- The announcement message must commit to **this pool coin's id** and the
  **new fee value**, so it cannot be replayed against another pool, another
  spend of the same pool, or a different target value.

The DAO's coin proves control of the recipient key by being spendable at all;
no signature scheme enters the puzzle.

### V3 — Value movement smuggled into the transition

The fee-decrease action must be a pure config transition:

- every `ReservePlan` asserts `successor_amount == current_amount`;
- `total_lp` is carried through unchanged;
- the swap/add/remove logic is unreachable from this mode (`MODE_DAO_FEE`
  is its own branch in `validate_action`, not a flag on an existing one).

Otherwise the decrease branch becomes a second, less-audited door into the
reserves — and it would be the branch with the least test traffic.

### V4 — Replay / double-spend of the decrease

Free, given V1: a second decrease attempt targets a puzzle hash that no
longer exists (the pool moved to the lower-fee curry). The announcement
binding in V2 already commits to a specific pool coin id, which is spent.
Verify in tests anyway — assert the replayed bundle fails with the puzzle
error, not with a mempool double-spend error, so the guarantee is the
puzzle's and not the mempool's.

### V5 — Interleaving with trades

A swap and a fee decrease in the same block are serialized by the singleton
lineage — each spends a specific pool coin. The only observable effect is
quote staleness, and it fails safe: a quote computed at the old (higher) fee
executes against the new (lower) one, so the taker receives *more* than
quoted and min-output guards hold. The reverse direction cannot occur —
the fee never rises. No assert needed; record it so nobody adds one that
breaks routing.

### V6 — Off-chain state assumptions

The indexer and router currently treat pool config as spend-invariant: fee is
read once from the creation record. After V11 that assumption is false for
`dao_fee_bps` specifically:

- the indexer must re-derive the current fee from the pool's current curried
  state (it already re-reads reserves every spend; fee joins that read);
- quotes must price from the live fee, or every post-decrease quote
  overprices the pool;
- `isLegacyPool` / version gating is untouched — the launcher id, which is
  the pool's identity everywhere off-chain, survives the transition.

### V7 — Creation-time hazards (inherited from this audit)

- Frontend, service, and stage-2 caps must all equal the puzzle's
  `MAX_DAO_FEE_BPS` — the V10 incident was three layers with three different
  caps (200 / 500 / 2000), and the loosest one reached the chain.
- The bech32m decoder must verify checksums. Fixed in `coinUtils.ts` during
  this audit (it previously sliced the checksum off undecoded — a mistyped
  DAO address would have decoded to a wrong, unowned recipient hash).
  `daoFee.check.ts` pins it.
- Zero-recipient assert as above; a fee with no recipient is a burn.

---

## What exists today, ready for V11

| Piece | Where | State |
|---|---|---|
| Percent ⇄ bps conversion, rounding not truncating | `src/lib/daoFee.ts` | dormant, 42 checks |
| Address ⇄ puzzle-hash with checksum verification | `src/lib/coinUtils.ts` | live (used by treasury fee config) |
| Combined-cap guard as executable documentation | `daoFee.check.ts` | pins the V10 folding hazard |
| Store fields + plumbing (`daoFeeBps`, `daoPuzzleHash`) | store → config → stage2 | present, forced to 0/unset |
| Payment mechanism to copy | `valid_protocol_fees`, `ReservePlan.protocol_fee_amount` | audited, live in V10 |
| Authorization pattern to copy | LP melt/mint binding (`lp_parent_id`) | audited, live in V10 |

## Explicitly out of scope for V11

- **Raising the fee**, under any authorization. Structurally refused.
- **Changing `dao_puzzle_hash`** after launch. Separate feature, separate
  threat model (recipient-swap attacks), if ever.
- **Retrofitting V10 pools.** Config is curried into the puzzle hash; a V10
  pool can never gain a DAO fee or a decrease path. V11 means new pools.
- **Timelocks / multi-step governance** on the decrease. Lower-only is
  already safe for LPs; ceremony can live in the DAO's own coin.
