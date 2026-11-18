# Forge puzzle V19

Protocol 20 on chain. **Unaudited.** This is the published record of the V19 build: what it changes
from V18, how to rebuild it, and which suites run it. (The monorepo keeps a longer working record;
this file is the one that ships with the puzzles.) CHIP-0062 revision 17 describes it.

> **Status (2026-11-17): live on testnet11, to follow V18 on mainnet; unaudited.** V19 is V18 plus
> two changes from an external review of V18 on 2026-11-16. On testnet11 every V18 market's
> liquidity has been moved into V19 by the same burn-and-deposit bundles the mainnet move will use.
> On mainnet, V18 pools stay spendable and their liquidity moves into V19 pools of the same markets;
> V16 pools stay live and listed beside V19. Each pool is spent by its own revision's driver.

| Testnet11 registry | |
|---|---|
| Launcher id | `e85622d4257a94240719bc674aad1c37b34db8b171696fa3cc7cfb2dedf63610` |
| Creation fee | 1 TXCH (registry state; moved only by `set_fee`) |
| Treasury puzzle hash | `1b7b3ec609bf2bb53b60238971b88fdbdee1384e3c18f4daa5c4bc99f772778d` |
| Pools | 19 |

Reference revision fingerprint (99 files): `85c0fd1ece88d40db887f1816c019f4ce022b4ead98cfe0a4424722430330e77`.
The fingerprint is over the reference sources. Some of the `.rue` files published here differ from
them in comments only, so this tree does not reproduce it; what it does reproduce is every compiled
output. `python scripts/build-v19.py` rebuilds `contracts/v19/compiled/`, and every tree hash in
`compiled/manifest.json` is the same as the reference build's and the deployed puzzles'. The
manifest's source hashes are those of the files published here.

## What V19 changes

Everything in [`FORGE_PUZZLE_V18.md`](FORGE_PUZZLE_V18.md) still holds. Two changes, stated as what
the code checks; the sources in `contracts/v19/puzzles/` are the reference.

### An unbalanced deposit pays the fees a swap would (`forge_curve.rue`, `forge_action_add.rue`)

An `add` whose deposits are not in the pool's current proportions is in part a swap: of each asset's
excess over the balanced deposit, the `(K - k_i) / K` share is exchanged for a claim on the other
reserves. Through V18 that share paid the LP fee but neither the dev fee (`fees_owed`) nor the DAO
fee (`dao_owed`), so an `add` followed by a `remove` of what it minted moved value like a swap while
accruing neither.

V19 charges all three fees of an unbalanced add on a grossed-up swapped share, rounded up toward the
pool:

```
F         = fee_bps + protocol_fee_bps + dao_fee_bps
scale     = 10000 - F
growth    = RATIO_SCALE + the largest deposit ratio
swap_base = ceil(floor(excess * (K - k) / K) * 10000 * growth / (scale * RATIO_SCALE))
lp_fee    = ceil(excess * fee_bps * (K - k) * growth / (K * RATIO_SCALE * scale))
dev, dao  = ceil(swap_base * rate / 10000)      each booked in the deposited asset
```

`10000 / scale` lifts the share to what a swap of the route's net input is charged on; `growth`
covers the depositor's own pro-rata claim on the fee it leaves in the pool and the curve's departure
from a linear share at size. LP is minted against the reserves net of all three fees, and the add's
successor state carries the dev and DAO fees in `fees_owed` and `dao_owed`. A balanced add pays
nothing; a vault add keeps V8's LP fee on its whole deposit and books no dev or DAO fee; `remove` is
unchanged (a proportional withdrawal swaps nothing). The rule the sizing meets: an add followed by a
remove of what it minted never returns more than a swap of the same net input.

### The genesis supply is bounded by the reserves (`forge_registry_common.rue`)

`valid_pool` keeps V17's floor and adds two upper bounds:

```
total_lp >= MIN_GENESIS_LP                                    (10^6, as in V17 and V18)
total_lp <= MAX_GENESIS_LP_PER_MOJO * min(reserves)           (10^6 LP per mojo of the scarcest reserve)
xch_backed(asset_ids, reserves, total_lp)                     (total_lp <= reserve_xch when XCH is an asset)
```

A balanced deposit keeps `total_lp / reserve` constant, so a genesis supply far above the reserves
would make every later deposit need LP backing many times its size, or past `2^64 - 1` impossible to
mint at all, and the pool would hold its market's key while nobody could usefully join it. A vault's
`lpRatio` is at most 10^6, and an XCH vault's at most 1. The bound is checked once, at registration.

### Stated rather than enforced

Registration refuses a pool's own LP (from V18) but cannot tell which revision another LP asset
belongs to, so the chain admits a V19 pool holding a pre-V19 Forge pool's LP. The reference router,
site and creation lanes refuse to create, list, quote or route one.

## Puzzle hashes (`contracts/v19/compiled/manifest.json`)

| Puzzle | Tree hash | Changed from V18 |
|---|---|---|
| `forge_action_swap` | `31f7e3d1...e11d1181` | |
| `forge_action_add` | `d37df7fb...9c0b09eb` | yes |
| `forge_action_remove` | `9b62cb1b...7e1edb060` | yes |
| `forge_action_observe` | `1a0813f0...74fb095f` | |
| `forge_action_collect` | `19be84b3...080793a4` | |
| `forge_action_dao_fee` | `12b0e026...0b4ec7ef` | |
| `forge_action_rebase` | `7d995041...5b54135048` | yes |
| `forge_action_refund` | `c914b5c5...afaeab7cca` | yes |
| `forge_multi_reserve_finalizer` | `88bdfedb...86b0d9c9` | |
| `forge_lp_cat_tail` | `6954729a...16fc0d6a39` | yes |
| `forge_registry_init` | `75a091c2...41da9909` | |
| `forge_registry_register` | `cf0aa00e...ab92c2ea1` | yes |
| `forge_registry_set_fee` | `b0868e23...b53486167a` | yes |
| `forge_reserve_launcher` | `4d60a57f...dbc4f91646` | yes |
| `forge_reserve_amount` | `1b831c90...63327c23` | |

The full hashes are in the manifest. Message tags: `forge-lp-v19`, `forge-reserve-v19`,
`forge-registered-v19`, `forge-registry-fee-v19`, `forge-dao-consent-v19`, `forge-rebase-v19`,
`forge-refund-v19`, `forge-rcat-issuer-v19`. The LP TAIL curries the protocol version, so every V19
pool hash differs from every earlier revision's.

## Off-chain lanes

* `contracts/forge_stdin.py` dispatches by protocol version; `_LANES[20]` is V19, built with
  `forge_v19_*`. The add lanes quote with the V19 fee rule (`forge_math`, version 19), and the site's
  quote never exceeds the puzzle's.
* `contracts/forge_v19_route.py` spends V14, V15, V16, V18 and V19 pools on one route, each with its
  own driver.
* `contracts/forge_v19_move.py` moves a V18 pool's liquidity into the V19 pool of the same market in
  one bundle: the owner's V18 LP is removed through the V18 pool's own `remove`, nested Forge LP is
  unpaired to its underlying assets, and the released assets seed the V19 twin or are added to the
  V19 pool already registered for that market.
* `contracts/forge_pool_admin.py`, `contracts/forge_recover.py` and `contracts/forge_resync.py`
  serve V19.

## Building

Needs [`rue`](https://github.com/rigidity/rue) 0.8.4 on PATH.

```bash
python scripts/build-v19.py                                    # rebuild, re-check the upstream pins
python scripts/revision-fingerprint.py --revision contracts/v19   # over this tree; see the note above
```

## Running the suites

From the repository root, in a Python environment with a current `chia-blockchain`:

```bash
python contracts/_test_v19_integrity.py              # every puzzle recompiled; exit 2 without rue
python contracts/_test_v19_add_output_fees.py        # the deposit fees, against a swap of the same net input
python contracts/_test_v19_registry_genesis_bounds.py   # the genesis bounds, refused and accepted at the boundary
python contracts/_test_v19_actions.py                # the leaves, accept and refuse
python contracts/_test_v19_registry.py               # admission
python contracts/_test_v19_curve_mirror.py           # the puzzle against an independent mirror
python contracts/_test_v19_route_lane.py             # cross-revision routes
```

and the rest of the `_test_v19_*` set.
