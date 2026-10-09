# Forge puzzle V15 -- the 2026-10-07 review revision

Testnet research only; unaudited. Protocol 16 on chain.

V14 (protocol 15) was reviewed a sixth time on 2026-10-07 (the chialisp.com rows, run
over forge-puzzles, forge-ui and the agent specs). Every finding above Informational was
in the composers and the services, not the puzzles, and was fixed on V14 without a
puzzle change (`FORGE_PUZZLE_V14.md` section 6 and the review log). Two puzzle findings
remained, both fail-closed on V14 and both worth taking only as a revision: F4, two coin
ids still derived by hashing solution-supplied bytes, and F7, hashed state that could
carry a non-canonical atom. The owner decided (2026-10-07) to cut the revision before the
first mainnet pair rather than after it, and to use the cut for one change the operator
needed: a creation fee that can be changed without a new registry.

**Why a major increment.** F4 rewrites `add`, `remove` and `register`; F7 touches the
prologue every leaf includes; the registry gains a leaf and its state a field. The six-leaf
root, the registry root and every new pool's puzzle hash move, and the V14 registry cannot
admit a V15 pool. That is V15, protocol 16. **V14 is not retired**: no V14 pool is drained,
its lane ships in every slice and stays registered in `forge_stdin` and `forge_resync`, its
records stay in the index, and its pools are listed, quoted and routed beside V15's -- a route
may cross the two revisions (the composer spends each pool with its own driver); only the
in-app settlement lane is V15-only and hands a mixed route to the router.
There is no mainnet V14 pool; the mainnet V14 registry (launched 2026-10-06) stays
unused.

Revision fingerprint (`scripts/revision-fingerprint.py --revision contracts/v15`):
`9931bcb1d4e3eaff5d3cf3ff0438a169389b9228b93a121cf166f0d20005f3ec`. V14's was
`6298777e...147ab4`.

## The changes, one line each

| # | Change | Where | Hex moves |
|---|---|---|---|
| F4 | `lp_action_coin_id` and `register`'s launcher id are derived with `coinid`, which refuses a parent that is not 32 bytes and an amount with a redundant leading zero in the leaf itself, instead of deriving an id no coin has and failing at message pairing or the TAIL binding | `forge_action_common.rue`, `forge_registry_register.rue` | add, remove, register |
| F7 | `h` (every leaf) and `new_bps` (`dao_fee`) are stored after `+ 0`, so the hashed state, the truth and `ASSERT_HEIGHT_ABSOLUTE` always carry canonical atoms; a leading-zero spelling is accepted as the canonical spend it is, where V14's state hash diverged from every mirror and consensus refused it | prologue, `forge_action_dao_fee.rue` | all six leaves |
| F5 | Comment: the reserve launcher's ASCII prefix is a namespace, not a 0xcb guard (CAT2 refuses only a 33-byte inner coin announcement beginning 0xcb, measured) | `forge_action_common.rue` | no |
| F6 | Stale comments: the finalizer reads reserve parents from state; the TAIL cites the V14 suites | finalizer, TAIL | no |
| fee | The creation fee is registry STATE `(initialized, pool_count, creation_fee)`: `init` seeds it from the launch constant, `register` charges what the state holds (zero = free, no settlement asserted), and the new `set_fee` leaf is the only thing that moves it, in either direction, authorized by a mode-23 message from a coin at the treasury's puzzle hash to this registry coin -- the DAO-fee handshake turned to the treasury | `forge_registry_common.rue`, `forge_registry_init.rue`, `forge_registry_register.rue`, `forge_registry_set_fee.rue` (new) | init, register, set_fee, registry root |
| tags | `forge-lp-v15`, `forge-reserve-v15`, `forge-registered-v15`, `forge-registry-fee-v15`; `PROTOCOL_VERSION = 16` in the TAIL and the registry | | TAIL, reserve launcher |

Unchanged throughout: the action layer, the multi-reserve finalizer, `registry_init`'s
logic, `reserve_amount`, the LP inners, every curve export and all five upstream pins.

## What F4 and F7 are, and are not

Neither closes an exploit. The coinid suite (`_test_v15_coinid_derivations.py`, run with
`--before contracts/v14/compiled`) measures the V14 column: a 31- or 33-byte
`lp_parent_id`, or a `burn` spelled with a leading zero, derived an id no coin has and
the spend failed at `MESSAGE_NOT_SENT_OR_RECEIVED` or the TAIL's binding; a leading-zero
`h` stored the raw atom and consensus refused the spend (code 14). No funds moved. What
V15 changes is where the refusal lives: in the leaf, so a validator that only runs the
puzzle -- the relay's in-app lane, the mirrors -- sees it without a mempool behind it,
and the hashed state is canonical by construction rather than by the solution's
spelling. Hardening worth taking when a revision is cut; not a reason to cut one.

## The creation fee

V14 curried the fee into `RegistryConstants`, so changing it was `registry --rollover`:
a successor registry for future pairs, the old one kept under `retired_registries` and
its pools listed from it. That path stays (it is still how the dev-fee RECIPIENT changes,
because every pool carries its recipient curried in) and `_test_registry_rollover.py`
still proves it. The fee alone no longer needs it.

* `RegistryState` is `(initialized, pool_count, creation_fee)`. `make_registry` starts at
  `(0, 0, 0)`; `init` writes `(1, 0, CONSTANTS.creation_fee)`. `Registry.current_fee`
  reads the state; `Registry.creation_fee` stays the launch constant that seeded it.
* `register` asserts the settlement announcement for `state.creation_fee`, carries the
  fee through, and asserts nothing when it is zero.
* `set_fee(new_fee)`: `initialized == 1`, `new_fee >= 0`, stored as `new_fee + 0` (F7),
  `ReceiveMessage { mode: SENDER_PUZZLE | RECEIVER_COIN, message:
  tree_hash(["forge-registry-fee-v15", fee]), sender: [CONSTANTS.treasury_puzzle_hash] }`.
  No slot is spent or created. The treasury proves control of its recipient by spending a
  coin at it; on testnet that is the deploy wallet (`deploy-v15-testnet.py set-fee
  --new-fee N`), on mainnet the treasury lock, whose own action emits the message.
* `_test_v15_registry_fee.py` (26 checks, real validator): init seeds; set_fee refused
  before init, without the message, from a non-treasury puzzle, with a message naming
  another fee, negative, and when the message is addressed to another registry coin;
  raise accepted and carried through `register`; the launch constant and one mojo under
  the raised fee refused; zero makes a registration with no settlement pass; raise from
  zero; the leading-zero spelling stores the canonical atom.
* Readers of the fee: the create lane (`forge_v15_create`, `api/forge-create.js
  currentCreationFee`) charge `state[2]`; the registry record keeps `creation_fee` (the
  constant, needed to rebuild the puzzle) and `state`; `forge_registry_record.verify`
  reports both. After a `set-fee` the operator re-uploads the record
  (`scripts/push-registry-record.mjs`), as after any registry spend.

## rCATs

Out of scope, by construction, as on V14: the TAIL and the reserve rules accept no
revocation layer, so no fake rCAT can reach a pool or brick it. The CHIP-0062 review
recommended the "Later" option (a follow-up CHIP if demand appears; a coin's hidden
puzzle cannot be known until it is spent), and the owner took it (2026-10-07).
`_test_v15_asset_scope.py` and `_sim_v15_rcat_imprint.py` carry the refusal unchanged.

## Everything else that moves, mechanically

* `PROTOCOL_VERSION` 15 -> 16 in the TAIL, the registry, `forge_v15_driver`,
  `api/_forgeVersion.js` and `src/lib/forgeVersion.ts`; `api/__checks__/revisionAgreement`
  holds the three together.
* `FORGE_KEPT_PROTOCOLS = [15, 16]`: the live revisions. The index keeps their records
  (`retireSupersededBatches`); the pool list, the responder, the reconciler, breadcrumbs,
  activity, supply and price history serve both (`isKeptProtocol`); a creation takes the
  newest lane; the relay's in-app lane is V15-only.
* `forge_stdin._lane_version` lets a route action span the live revisions and hands it to the
  newest composer; `forge_v15_route._drv(pool)` spends each pool with its own driver
  (`_test_v15_route_lane.py`, "a route across revisions").
* V14 and V15 share the action layer, so `pool_module_hash` alone no longer identifies a
  revision. Snapshots carry `revision_hash` (the LP TAIL mod hash); `api/_deploymentIndex.js`
  believes the `(action, tail)` pair from both manifests and a bare action hash only as a
  V14 record written before the field existed, never as the shipping revision.
* `forge_stdin._LANES = {16: V15, 15: V14}`, V14 imported unconditionally (argued in
  `_test_version_hygiene.py` as the live secondary lane); `forge_resync.REPLAY_LANES[16]`.
* `src/lib/relay/v15/` (modules.json regenerated by `gen-relay-v15-modules.py`),
  `verifyState.ts` pinned to the V15 leaves, TAIL and protocol; the relay fixtures are
  regenerated from V15 testnet pools.
* The matrix planner takes `--scale`: V15 launches beside the undrained V14 matrix, which
  still holds most of the wallet's T11 and T6, so the same fifty shapes run at 0.6x depth.
* The publish slice ships `contracts/v14` AND `contracts/v15`; the forge-ui prune list
  does not name v14.

## Verification

Offline, on the final tree with rue 0.8.4: `_test_v15_integrity.py` (source reproduces
every hex, the three registry leaves included), the whole `_test_v15_*` battery plus the
unversioned suites that now import V15, `_test_v15_coinid_derivations.py` in both
columns, `_test_v15_registry_fee.py`, `_test_v15_audit_qa.py` and `scripts/sim-v15.py`
on the in-process simulator, `scripts/mutate-v15.py` with every unreached line argued in
`contracts/v15/mutation-arguments.json`, `npx tsc`, `node scripts/run-checks.mjs`. On
testnet11: the registry, the fifty-pool matrix, the lifecycle matrix (adds, swaps,
collects, removes, multihop), a `set-fee` round trip, discoverability and provenance.
The record of each run is below this section as it happens.

### Record, 2026-10-07 (testnet11)

* Offline, final tree, rue 0.8.4: integrity 124/124; the `_test_v15_*` battery and the
  unversioned suites 41/42, the one being provenance, which skips until `contracts/v15` is
  in a git commit; coinid derivations both columns; registry fee 26/26; audit-QA 7/7;
  `sim-v15.py`, `sim-v15-review-derivations`, `sim-v15-chip0062`, `sim-v15-batch-security`,
  `sim-v15-rcat-imprint` all passed; `mutate-v15.py`: every line killed or argued
  (`set_fee`'s two asserts killed by the fee suite); `tsc` clean; `run-checks.mjs` 118/118
  with fixtures regenerated from the live pools.
* Registry `9d99ac47ff619a229173e73bdcfef200123b20184c2af108529aa5fc6d71d2fa`, genesis +
  init tx `a3950ab6…`, confirmed at 4,789,100, treasury and dev-fee recipient the deploy
  wallet (as V14's testnet registry), creation fee 1,000,000. The confirmation poll died on a
  coinset timeout after the push; the record was rebuilt from chain and verified (successor
  unspent, both sentinel slots present), and the deploy script's node reads now retry.
* The fifty-pool matrix at 0.6x depth (`plan-v15-matrix.py --scale 0.6`), registry at pool
  count 50. Two creations were refused at push with INVALID_SPEND_BUNDLE after passing local
  validation and succeeded unchanged on the resume: a signing-side flake, not a puzzle refusal.
  The create command had recorded the registry's state with two fields after the first pool;
  fixed, record repaired against the chain (the website's create lane takes the state from the
  leaf's output and never had it).
* V14 liquidity: half removed from A3 t6/t11, F1 t6 vault, G2 t8/t11 and T11/t14 (four
  confirmed removes), freeing about 247k T11 and 30k T6 for the V15 adds. V14 is undrained.
* Lifecycle on every V15 pool: adds at 40%, swaps at 5%, collects; removes at 10% on A1, A4,
  D2, K10; multihop A1 -> A3 (`7353862…`, confirmed 4,790,085). 122 confirmed transactions in
  the first pass; three pushes failed on the signing side (two INVALID_SPEND_BUNDLE, one
  upstream timeout whose spend had in fact confirmed) and were redone individually.
* `set-fee` round trip on the live registry: 1,000,000 -> 2,000,000 (`6e2a772…`, confirmed
  4,790,059) and back (`91b587d…`, 4,790,060); the record's state followed.
* Discoverability 563/563 against the live record; every record resyncs to its chain tip;
  the deployment index holds 50 V14 and 50 V15 batches, each snapshot carrying its
  `revision_hash`, labelled 15 and 16 respectively; the local responder lists both.
* **A swap across revisions, settled the way the site settles** (`scripts/v15-mixed-route-test.py`
  against the local responder): 1,000,000 TXCH mojos in, of which 30,927 paid to the router by the
  trader's own spend (the least the route required was 29,971), 969,073 into the V15 pool A1
  (TXCH/T6) releasing 446 T6, into the V14 pool A3 (T6/T11) releasing 9,914 T11 -- the preview's
  figure exactly; offer `e4260847…`, confirmed at 4,790,145. The offer-lane harness
  (`v15-offer-lane-test.py`) now lists both revisions; its own offers are built with Sage's
  plain make-offer and cannot carry the fee payment, which is why the new script builds
  through `/api/forge-offer-build` as the site and the agent API do.
* **The lock as the authority** (`scripts/v15-lock-fee-test.py`, through the board the site
  uses): the testnet registry was rolled over to `b3c4f216…` with the matrix lock
  (`txch1rdana3…`, a vault lock, 1 of 2) as treasury (confirmed 4,790,634; `9d99ac47…` kept
  under `retired_registries`). A proposal with no outputs and one condition
  `[66, 23, registry_fee_message(fee), <registry coin id>]`, proposed and signed by the
  owner key Sage holds, executed with the registry's `set_fee` spend ATTACHED
  (`attachedSpends` on `/api/multisig-execute`, `attached_spends` in `vault_tool.assemble`;
  keyless, paired by consensus): 1,000,000 -> 2,000,000 (`c611f651…`, 4,790,641) and back
  (`7cb0b104…`, 4,790,644). The same for a pool: I2's DAO fee 50 -> 20 bps with the pool's
  `dao_fee` spend attached, built at execution against the coin the message names
  (`f89fa71b…`, 4,790,656; record resynced through the replay). One push was refused with
  INVALID_FEE_TOO_CLOSE_TO_ZERO: a pool's spend re-creates every reserve (~300M cost), so the
  lock's network fee must clear 5 mojos per cost of the WHOLE bundle, attached spends
  included; the driver now sizes it per action and the stale proposal was marked by the board.
  On mainnet the treasury and DAO locks drive both leaves exactly this way.
* **A raise, for both.** Creation fee 1,000,000 -> 3,000,000 through the lock (`4fad8d66…`,
  4,790,682); a pair created at the raised fee (`L1 txch t14 fee-test v15`, 4,790,686) paid
  exactly 3,000,000 to the lock: its balance moved by -500,000,000 (the proposal's network fee)
  +3,000,000, and the registry counted the pool; then back to 1,000,000 (`89c81abd…`,
  4,790,690), state `(1, 1, 1,000,000)`. The first attempt had been refused locally with
  ASSERT_ANNOUNCE_CONSUMED_FAILED: the operator script's create path still paid the launch
  constant, not the state's fee (fixed: `creation_fee = reg.current_fee`; the website's lane
  already read the state). A DAO RAISE on I2 (20 -> 40 bps) is refused by the leaf
  (`new_bps < current`), as designed: the driver now runs the leaf before proposing, so a
  refused change never leaves a signed proposal on the board (one did, and the board marked
  it stale on its own). A DAO fee only ever falls; a creation fee moves either way.
