# DAO fee — V13, as built, with the threat model checked off

`FORGE_DAO_FEE_V11.md` was a design: what a DAO fee would be, and the seven
vectors a review would have to check. This is the same document after the feature
shipped, read against `contracts/v13`. Each vector says what closes it in the
built puzzle and which suite pins it, and the places where the design changed
between proposal and implementation are called out rather than quietly restated.

The short version: the feature is live on testnet11, six of the thirty-two V13
pools carry a DAO rate, and the multisig lock has been paid in all four test CATs
through the ordinary `collect` path.

---

## What changed between the design and the build

The design put both halves in `PoolConfig`:

```
dao_fee_bps: Int,          // proposed: config
dao_puzzle_hash: Bytes32,  // recipient
```

**As built, only the recipient is config. The rate lives in state.**

```
PoolConfig  { … dao_puzzle_hash: Bytes32 }      // immutable for the life of the pool
ForgeState  { … dao_fee_bps: Int, dao_owed: List<Int> }
```

That is a better answer to vector V1 than the one proposed, and it is worth being
explicit about why. The design's plan was to let the successor's config differ in
exactly one field and to check field-by-field equality everywhere else — a check
that is correct only if it is exhaustive, and that has to be rewritten every time
a config field is added. Putting the rate in state removes the question: the
config is curried into every leaf and is **byte-identical** across a decrease, so
there is no "successor config" to validate against its predecessor at all. The
pool's puzzle hash changes only because its state does, which is true of every
spend.

The recipient stays in config, so it cannot move. A recipient change remains out
of scope, as the design said.

The second change is V13's, from the second external review: **`collect` pays one
coin when the protocol and DAO recipients are the same address**. Two identical
`CreateCoin`s from one reserve are one coin id twice, which consensus rejects as
`DUPLICATE_OUTPUT`, and because `collect` is the only exit for accrued fees, that
bricked the exit for exactly the pools whose two recipients coincided. See V8
below.

---

## The feature, as built

The DAO fee is a second slice of a swap's output, taken beside the protocol fee,
each floored on its own rather than one floor on a summed rate:

```
protocol_fee = protocol_fee_owed(claimed_output, CONFIG.protocol_fee_bps)
dao_fee      = protocol_fee_owed(claimed_output, state.dao_fee_bps)
```

Both stay physically **inside** the out reserve, recorded as owed
(`fees_owed[out]`, `dao_owed[out]`), until `collect` pays them. The reserve coin
therefore holds `reserves[i] + fees_owed[i] + dao_owed[i]`, which is what
`forge_reserve_amount.rue` returns and what the finalizer sizes the recreated coin
from. No new payment mechanism: a second instance of the audited one.

The cap is `MAX_DAO_FEE_BPS = 100`, asserted in the prologue rather than in
`valid_config`, because the rate is state. Alongside it:

```
assert state.dao_fee_bps >= 0;
assert state.dao_fee_bps <= MAX_DAO_FEE_BPS;
assert state.dao_fee_bps == 0 || config.dao_puzzle_hash != zero_bytes32();
```

The last line is the design's zero-recipient guard, unchanged in intent: a fee with
no recipient would be burned rather than collected.

### Adjustability: monotonic decrease only

Unchanged from the design, and it is the `dao_fee` leaf:

```
assert new_bps >= 0;
assert new_bps < p.state.dao_fee_bps;
assert CONFIG.dao_puzzle_hash != zero_bytes32();
```

Irreversibility is topology, not a flag. A pool at `dao_fee_bps = 0` contains no
transition producing a nonzero successor, so there is nothing to forge and nothing
to reset. A raisable fee would be a key that reprices liquidity providers after
they commit capital; refusing it structurally rather than by policy was the right
call and survives unchanged.

---

## The vectors, checked off

### V1 — Successor substitution — **closed, by construction**

The design's concern was that allowing the successor's config to differ at all
invites swapping in arbitrary weights, asset ids or a different `lp_tail_hash`
under cover of a fee decrease. As built the config does not differ: the rate is
state, so the curried config is byte-identical across the decrease and there is no
equality check to get wrong or to forget to extend.

V13 adds a second, unrelated guarantee in the same area. The finalizer now asserts
that the action layer's merkle root is the six-leaf root of one configuration
hash, so a pool cannot be assembled from leaves that disagree about config in the
first place. That closes a genuine drain the second review demonstrated against
V12, and it means the "one config, all six leaves" property this vector assumed is
now checked on every spend rather than only at registration.

Pinned: `_test_v13_dao_fee.py`; `_test_v13_finalizer.py`;
`_test_v12_v13_review_findings.py` (the cross-leaf drain, accepted against V12 and
refused against V13).

### V2 — Authorization forgery — **closed**

The built binding is stronger than the announcement the design sketched. The
`dao_fee` leaf requires a CHIP-0025 `RECEIVE_MESSAGE` with mode
`SENDER_PUZZLE | RECEIVER_COIN` from a coin whose **puzzle hash is** the curried
`dao_puzzle_hash`, carrying `dao_fee_message(new_bps)`.

Mode 23 means consensus commits the sender by puzzle hash and the receiver by coin
id, so the message names only the rate: it cannot be replayed against another pool
or another spend of this one, because the receiving coin is this pool coin and
nothing else. The DAO proves control of the recipient by spending a coin at it; no
signature scheme enters the puzzle.

Pinned: "no message from the DAO: refused"; "a message from a coin at another
puzzle hash: refused"; "a message naming a different rate than the leaf: refused".
All three asserts are **killed** by the mutation run.

### V3 — Value movement smuggled into the transition — **closed**

The `dao_fee` leaf carries reserves, `total_lp`, `fees_owed`, `dao_owed`, the
oracle and `reserve_parents` through the prologue untouched and changes exactly one
field. It emits no `for_reserve` condition, so it tags nothing to any reserve, and
the finalizer therefore re-creates every reserve at its current amount. The leaf is
its own merkle leaf rather than a flag on an existing one, so the swap, add and
remove logic is unreachable from it.

Pinned: `_test_v13_dao_fee.py` checks every reserve is re-created unchanged.

### V4 — Replay / double-spend — **closed, and it is the puzzle's doing**

A second decrease against the same pool coin targets a coin that has been spent,
and the mode-23 receiver binding names that coin. The design asked that the test
prove the failure is the puzzle's rather than the mempool's, and it does: the
replay is refused for the message pairing, not as a double-spend.

### V5 — Interleaving with trades — **no assert, by design**

Unchanged and still correct. A swap and a fee decrease are serialized by the
singleton lineage. The only observable effect is quote staleness and it fails safe:
a quote computed at the old, higher rate executes against the new, lower one, so
the taker receives more than quoted and any minimum-output guard holds. The reverse
cannot occur because the rate never rises. Recorded here so nobody adds an assert
that breaks routing.

### V6 — Off-chain state assumptions — **closed**

The rate is read from live state everywhere, not from the creation record. The
snapshot carries `dao_fee_bps` out of `state[5]`, the index and the pools API serve
it, and quotes price from it. The launcher id remains the pool's identity
off-chain, so a decrease does not disturb anything that keys on it.

One consequence the design did not anticipate, worth recording because it cost a
day: **a builder must mirror every slice the puzzle takes.** The swap leaf
subtracts two, and `v13_ops.swap_bundle` subtracted only the protocol fee, so it
constructed a settlement for a coin the pool never created. That bundle passes
local validation, which has no coin store, and the node refuses it as
`UNKNOWN_UNSPENT` — on exactly the nine DAO-bearing pools of thirty-two and no
others. Fixed in the single-swap and multi-hop builders. The lesson generalises: a
fee that accrues rather than pays out is invisible to a builder that forgets it.

### V7 — Creation-time hazards — **closed**

The caps agree across the layers: `MAX_DAO_FEE_BPS = 100` in the puzzle, and the
front end and services read the same figure rather than carrying their own. The
bech32m decoder verifies checksums (`coinUtils.ts`, pinned by `daoFee.check.ts`),
so a mistyped DAO address cannot decode to a wrong but well-formed recipient. The
zero-recipient assert is in the prologue, above.

The testnet deployment exercises the hazard deliberately: `H5 txch t6 2-1 max-dao`
runs at the cap of 100 bps, and `I5 txch t14 2-1 dao-lock max` does the same with
the multisig lock as recipient.

### V8 — Coincident recipients — **new in V13, closed**

Not in the original seven, because it only appears once a pool has two fee
recipients that can be equal. When `protocol_puzzle_hash == dao_puzzle_hash` and
both owed amounts land on the same reserve, `collect` emitted two `CreateCoin`
conditions with the same puzzle hash and, when the amounts coincided, the same
value — one coin id twice, which consensus rejects as `DUPLICATE_OUTPUT`. Since
`collect` is the only exit for accrued fees, the exit was bricked for that pool.

V13 pays one coin of `fee + dao` when the recipients match:

```
conditions: if protocol_puzzle_hash == dao_puzzle_hash {
    payout(index, protocol_puzzle_hash, fee + dao, rest.conditions)
} else {
    payout(index, protocol_puzzle_hash, fee,
           payout(index, dao_puzzle_hash, dao, rest.conditions))
}
```

Pinned: `_test_v13_actions.py` checks exactly one coin of the summed amount, and
`_test_v12_v13_review_findings.py` reproduces the V12 `DUPLICATE_OUTPUT` beside
V13 paying the merged coin.

---

## What runs today

| Piece | Where | State |
|---|---|---|
| The rate, in state, lowerable only | `forge_action_common.rue`, `forge_action_dao_fee.rue` | live, protocol 14 |
| The recipient, in config, immutable | `PoolConfig.dao_puzzle_hash` | live |
| Accrual beside the protocol fee | `forge_action_swap.rue` | live |
| Payment, merged when recipients match | `forge_action_collect.rue` | live, V13 |
| Percent to bps, rounding not truncating | `src/lib/daoFee.ts` | live, 42 checks |
| Address to puzzle hash, checksum verified | `src/lib/coinUtils.ts` | live |
| Vectors V1 to V8 | `_test_v13_dao_fee.py` (37 checks) | green |

On testnet11: six of thirty-two pools carry a rate, from 10 bps to the 100 bps cap.
The DAO recipient on the lock-bearing pools is the multisig lock
`txch1rdana3s…`, and `collect` has paid it in T6, T11, t8 and t14. A DAO fee paid
to the operator's own address would prove nothing about the lock, which is why the
matrix points them at the lock instead.

## Still out of scope

- **Raising the fee**, under any authorization. Structurally refused.
- **Changing `dao_puzzle_hash`** after launch. A recipient-swap has its own threat
  model; it is not smuggled into this one.
- **Retrofitting earlier pools.** The recipient is curried into the puzzle hash, so
  a pool that launched without one can never gain it.
- **Timelocks or multi-step governance** on the decrease. Lower-only is already
  safe for depositors, and ceremony belongs in the DAO's own coin — which, on this
  deployment, is a multisig vault rather than a single key.
