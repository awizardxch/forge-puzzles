# Forge puzzle V18

Protocol 19 on chain. **Unaudited.** This is the published record of the V18 build: what it
checks, how to rebuild it, and which suites run it. (The monorepo keeps a longer working record;
this file is the one that ships with the puzzles.)

> **Status (2026-11-15): live on mainnet and testnet11, unaudited.** The V18 mainnet registry
> launched on 2026-11-15, before the independent audit, which will be requested on V18. V16
> pools stay live and listed on mainnet beside V18, and their liquidity providers migrate their
> own positions into V18 twins; V16, V15 and V14 stay live on testnet11. Each pool is spent by
> its own revision's driver.

| Mainnet registry | |
|---|---|
| Launcher id | `5fb01bf2dce18f5c4bb1ae99d965b58de88f2bb9a6b117d242baaf5565d15df9` |
| Launched | 2026-11-15, block 9,579,600 |
| Creation fee | 1 XCH (registry state; moved only by `set_fee`) |
| Treasury puzzle hash | `3d4e9dcf71e039f0814909d24f2a5c9c7e76359fe5b0b2ca8e49af1989750712` |
| Protocol fee recipient | `4844835b72f800e0a0c3eacb6575f90b4b637ca3bdc63ccde09d6b4f37185bae` |
| Oracle window | 32 |

Reference revision fingerprint (98 files): `91918853e17359bc14bf08349b4a5444c15febbe9dee3a31bcd211dc90a3e119`.
The fingerprint is over the reference sources. Sixteen of the `.rue` files published here differ
from them in comments only, so this tree does not reproduce it; what it does reproduce is every
compiled output. `python scripts/build-v18.py` rebuilds `contracts/v18/compiled/`, and every tree
hash in `compiled/manifest.json` is the same as the reference build's and the deployed puzzles'.
The manifest's source hashes are those of the files published here.

V17 (protocol 18) was an intermediate revision that ran on testnet11 only. It is frozen and is
not published here; V18 carries everything it added. The repository's Python lanes import V17
only when it is present, and the suites skip their V17 cases when it is not.

## What the V18 leaves check (summary)

Stated as what the code checks; the leaf sources in `contracts/v18/puzzles/` are the reference.

* **Canonical integers.** Every index, weight and fee rate a leaf or `register` compares is
  reduced to its minimal integer encoding (`x + 0`) before it is compared, hashed or used to
  route a payout. The multi-reserve finalizer stores each reserve tag's index canonically, so
  the range check and the per-reserve assignment read the same integer. `swap` compares its
  asset indices as integers.
* **Recipients are 32 bytes.** Fee and DAO recipients are checked as puzzle hashes.
* **The dev fee rate is pinned** as a registry constant at registry genesis; `register`
  requires every pool to carry exactly that rate, and the rate is no longer part of the
  market key.
* **One market, one key.** Weights must be in lowest terms and every key field canonical;
  `valid_config` requires the PoolConfig to have exactly its declared number of fields.
* **DAO consent.** A pool that names a DAO recipient registers only with a mode-23 message from
  a coin at that DAO address naming the pool's launcher, config and opening rate.
* **Genesis floor.** `register` requires a genesis supply of at least `MIN_GENESIS_LP`
  (10^6 LP units).
* **rCAT reserves (CHIP-0038).** PoolConfig carries one hidden puzzle hash `H` per asset (zero
  for a plain asset). A pool holding a revocable CAT keeps it under the revocation layer with
  that `H` on every coin it creates; the `rebase` and `refund` leaves serve the issuer, with an
  optional issuer attestation at registration. `H` is issuer trust: `register` cannot tell which
  `H` an asset was minted under, so the router checks the asset's mint before it lists, quotes or
  routes such a pool.
* **A pool may not list its own LP.** In `forge_registry_register.rue`, right after
  `valid_pool` has proved that `config.lp_tail_hash` is this launcher's own LP:

  ```
  assert !lists_asset(config.asset_ids, config.lp_tail_hash);
  ```

  Listing another pool's LP (nested pools, nested zaps) is unaffected.

## Puzzle hashes (`contracts/v18/compiled/manifest.json`)

| Puzzle | Tree hash |
|---|---|
| `forge_action_swap` | `31f7e3d1...e11d1181` |
| `forge_action_add` | `44bbab0c...7581dfa` |
| `forge_action_remove` | `ea478701...5929a6f` |
| `forge_action_observe` | `1a0813f0...74fb095f` |
| `forge_action_collect` | `19be84b3...080793a4` |
| `forge_action_dao_fee` | `12b0e026...0b4ec7ef` |
| `forge_action_rebase` | `2479dfab...fccfff09` |
| `forge_action_refund` | `a8097459...7b152ea` |
| `forge_multi_reserve_finalizer` | `88bdfedb...86b0d9c9` |
| `forge_lp_cat_tail` | `de05823e...39142c8f` |
| `forge_registry_init` | `75a091c2...41da9909` |
| `forge_registry_register` | `84a712b2...c9f59e03` |
| `forge_registry_set_fee` | `6862bd38...9764bb9f5` |
| `forge_reserve_launcher` | `bf1aa22d...fab0a7f06` |
| `forge_reserve_amount` | `1b831c90...63327c23` |

The full hashes are in the manifest. Message tags: `forge-lp-v18`, `forge-reserve-v18`,
`forge-registered-v18`, `forge-registry-fee-v18`, `forge-dao-consent-v18`, `forge-rebase-v18`,
`forge-refund-v18`, `forge-rcat-issuer-v18`. The LP TAIL curries the protocol version, so every
V18 pool hash differs from every earlier revision's.

## Off-chain lanes

* `contracts/forge_stdin.py` dispatches by protocol version; `_LANES[19]` is V18. Every
  single-pool lane (swap, add, remove, the rCAT `rebase`/`refund` lanes) and every route lane
  (multi-hop, split, flow balance, routed deposit, vault route, zaps) builds V18 pools with
  `forge_v18_*`.
* `contracts/forge_v18_route.py` spends V14, V15, V16 and V18 pools on one route, each with its
  own driver; the LP mint and melt inners are asserted byte-equal across the builds.
* `migrate-v16-to-v18` (`forge_v18_route.migrate_v16_to_v18`): V16 LP removed through the V16
  pool's own `remove` and deposited into its V18 mirror (`forge_v18_create.mirror_config_from_v16`)
  in one bundle.
* `contracts/forge_pool_admin.py` (collect, dao_fee), `contracts/forge_recover.py` (rebuild a
  registry and its pools from chain alone) and `contracts/forge_resync.py` serve V18.

## Building

Needs [`rue`](https://github.com/rigidity/rue) 0.8.4 on PATH.

```bash
python scripts/build-v18.py                                    # rebuild, re-check the upstream pins
python scripts/revision-fingerprint.py --revision contracts/v18   # the fingerprint above
```

## Running the suites

From the repository root, in a Python environment with a current `chia-blockchain`:

```bash
python contracts/_test_v18_integrity.py          # every puzzle recompiled; exit 2 without rue
python contracts/_test_v18_actions.py            # the leaves, accept and refuse
python contracts/_test_v18_registry.py           # admission and the genesis floor
python contracts/_test_v18_reserves_proved.py    # the reserve launcher and the derived parent
python contracts/_test_v18_settlement_amount.py  # the settlement binding
python contracts/_test_v18_rcat_attacks.py       # rCAT: rebase, refund, the adopt branch
python contracts/_test_v18_route_lane.py         # cross-revision routes
python contracts/_test_v18_migrate.py            # V16 -> V18 migration
```

and the rest of the `_test_v18_*` set.
