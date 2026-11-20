# Forge puzzle V20

Protocol 21 on chain. **Unaudited.** This is the published record of the V20 build: what it changes
from V19, how to rebuild it, and which suites run it. (The monorepo keeps a longer working record;
this file is the one that ships with the puzzles.) CHIP-0062 revision 18 describes it.

> **Status (2026-11-19): live on mainnet and testnet11; unaudited.** V20 is V19 plus one assert in
> the add's curve. On mainnet its registry launched on 2026-11-19 and every V18 market's liquidity
> was moved into a V20 pool of the same market by the burn-and-deposit bundles below; V18 is no
> longer served there. V19 never reached mainnet. On testnet11 the V19 pools' liquidity moved into
> V20 the same way. V16 pools stay live and listed beside V20 on both networks. Each pool is spent by
> its own revision's driver.

| Mainnet registry | |
|---|---|
| Launcher id | `8a60e08be1308b6a6a6fc19bd28deecaa034501f2e0e00573702d4a6e8ceefe9` |
| Creation fee | 1 XCH (registry state; moved only by `set_fee`) |
| Treasury puzzle hash | `0b027368186ed4f5cf0ae6fe55f0f96a3aa8318991512c78daaa9feba8c6b9c9` (a 2-of-4 vault) |
| Pools at launch | 50 |

| Testnet11 registry | |
|---|---|
| Launcher id | `4a61f1c3ccad49f38d27a26531dad49052faf22f1812054d91b1af38aca9760d` |
| Creation fee | 1 TXCH (registry state; moved only by `set_fee`) |
| Treasury puzzle hash | `1b7b3ec609bf2bb53b60238971b88fdbdee1384e3c18f4daa5c4bc99f772778d` |
| Pools | 18 |

Reference revision fingerprint (99 files): `946a9cdd8b7f6353dc2fb60d5e73499e0c09dc62fdf21e7baf0248226c816461`.
The fingerprint is over the reference sources. Some of the `.rue` files published here differ from
them in comments only, so this tree does not reproduce it; what it does reproduce is every compiled
output. `python scripts/build-v20.py` rebuilds `contracts/v20/compiled/`, and every tree hash in
`compiled/manifest.json` is the same as the reference build's and the deployed puzzles'. The
manifest's source hashes are those of the files published here.

## What V20 changes

Everything in [`FORGE_PUZZLE_V19.md`](FORGE_PUZZLE_V19.md) still holds. One change, stated as what
the code checks; the sources in `contracts/v20/puzzles/` are the reference.

### Every effective term of an add is positive (`forge_curve.rue`, `effective_product`)

An `add` mints LP against the product of each asset's effective term, the old reserve plus the
deposit net of the add's LP, dev and DAO fees on that asset:

```
effective_i = old_i + deposit_i - lp_fee_i - dev_i - dao_i
assert effective_i > 0                                       (V20, every asset)
product     = prod(effective_i ^ w_i)
```

V19's fees on an unbalanced add are charged on a grossed-up swapped share, so they grow faster than
the deposit, and V19 placed no bound on the term they are taken from. V20 refuses any add whose fees
on an asset exceed what it deposits of that asset plus the reserve, so no factor of the product is
zero or negative and no stored reserve is ever below zero. Every add V19 accepts with every effective
term positive, V20 accepts with the same mint and the same successor state. The off-chain mirror
(`contracts/forge_math.py`, math version 20) refuses the same adds before a quote is shown.

## Puzzle hashes (`contracts/v20/compiled/manifest.json`)

| Puzzle | Tree hash | Changed from V19 |
|---|---|---|
| `forge_action_swap` | `31f7e3d1...e11d1181` | |
| `forge_action_add` | `6c2517a6...2d36860e` | yes |
| `forge_action_remove` | `a53479b3...d17a8395` | yes |
| `forge_action_observe` | `1a0813f0...74fb095f` | |
| `forge_action_collect` | `19be84b3...080793a4` | |
| `forge_action_dao_fee` | `12b0e026...0b4ec7ef` | |
| `forge_action_rebase` | `5f06405a...ca4350d0` | yes |
| `forge_action_refund` | `aa140bf3...42b61312` | yes |
| `forge_multi_reserve_finalizer` | `88bdfedb...86b0d9c9` | |
| `forge_lp_cat_tail` | `7cf3835f...1bbb8d5d` | yes |
| `forge_registry_init` | `75a091c2...41da9909` | |
| `forge_registry_register` | `f1e1e985...741978be` | yes |
| `forge_registry_set_fee` | `83d78d03...b1166d32` | yes |
| `forge_reserve_launcher` | `c115d65b...d990365b` | yes |
| `forge_reserve_amount` | `1b831c90...63327c23` | |

The full hashes are in the manifest. Only `add`'s curve changes in substance; the other changed
hashes follow from the protocol version and the message tags: `forge-lp-v20`, `forge-reserve-v20`,
`forge-registered-v20`, `forge-registry-fee-v20`, `forge-dao-consent-v20`, `forge-rebase-v20`,
`forge-refund-v20`, `forge-rcat-issuer-v20`. The LP TAIL curries the protocol version, so every V20
pool hash differs from every earlier revision's.

## Off-chain lanes

* `contracts/forge_stdin.py` dispatches by protocol version; `_LANES[21]` is V20, built with
  `forge_v20_*`. The add lanes quote with math version 20, and the site's quote never exceeds the
  puzzle's.
* `contracts/forge_v20_route.py` spends V14, V15, V16, V18, V19 and V20 pools on one route, each with
  its own driver.
* `contracts/forge_v20_move.py` moves a V18 pool's (mainnet) or V19 pool's (testnet) liquidity into
  the V20 pool of the same market in one bundle: the owner's LP is removed through the source pool's
  own `remove`, nested Forge LP is unpaired to its underlying assets, and the released assets seed
  the V20 twin or are added to the V20 pool already registered for that market. A twin is created
  with its market's DAO consent, a lock vote of the treasury.
* `contracts/forge_pool_admin.py`, `contracts/forge_recover.py` and `contracts/forge_resync.py`
  serve V20.

## Building

Needs [`rue`](https://github.com/rigidity/rue) 0.8.4 on PATH.

```bash
python scripts/build-v20.py                                    # rebuild, re-check the upstream pins
python scripts/revision-fingerprint.py --revision contracts/v20   # over this tree; see the note above
```

## Running the suites

From the repository root, in a Python environment with a current `chia-blockchain`:

```bash
python contracts/_test_v20_integrity.py              # every puzzle recompiled; exit 2 without rue
python contracts/_test_v20_curve_mirror.py           # the puzzle against an independent mirror, the new refusal included
python contracts/_test_v20_add_output_fees.py        # the deposit fees, against a swap of the same net input
python contracts/_test_v20_lp_receive_forgery.py     # a forged LP receipt refused
python contracts/_test_v20_actions.py                # the leaves, accept and refuse
python contracts/_test_v20_registry.py               # admission
python contracts/_test_v20_route_lane.py             # cross-revision routes
```

and the rest of the `_test_v20_*` set.
