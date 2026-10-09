# Forge puzzle V16 -- the burn-address revision

Testnet research only; unaudited. Protocol 17 on chain.

V15 (protocol 16) burned every pool's locked minimum, `LOCKED_BURN` (1 LP unit), to the
all-zero puzzle hash, and `register` asserted that payment. Chia's own documentation names
a different hash as the network's burn address: all zeros ending in `dead` (the FAQ, "What
is Chia's burn address?"), which is `xch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqm6ks6e8mvy`
on mainnet and `txch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqm6ksh7qddh` on testnet11.
Sage reports the same address as its own `burn_address`. Both hashes have no known
preimage, so V15's burn is as unspendable as V16's; the difference is recognition. An
all-zero hash is also what a cleared or unset field looks like, so a burn there reads the
same as a bug, while `0x...dead` is the one wallets and explorers display as a burn.
The owner decided (2026-10-09) to move the floor there before the first mainnet pair. The
mainnet V15 registry (`e78d35be...`, launched the same day) had no pool, so nothing was
stranded.

Revision fingerprint (`scripts/revision-fingerprint.py --revision contracts/v16`):
`a9614baee66cca7644b1b2a9b2f166f003747e0e95458fc4d3935ed0e3e7cbc9`. V15's is unchanged at
`9931bcb1...05f3ec`.

## The change

| What | Where | Hex moves |
|---|---|---|
| `BURN_PUZZLE_HASH = 0x000...dead` | `forge_action_common.rue` (a constant; only `register` reads it) | no leaf by itself |
| `register` asserts the genesis settlement paid `LOCKED_BURN` to `BURN_PUZZLE_HASH`, where V15 named `zero_bytes32()` | `forge_registry_register.rue` | register, registry root |
| The driver's genesis settlement pays the floor there | `forge_v16_driver.genesis_lp_settlement_spends` | -- |
| Tags `forge-lp-v16`, `forge-reserve-v16`, `forge-registered-v16`, `forge-registry-fee-v16`; `PROTOCOL_VERSION = 17` in the TAIL and the registry | | LP TAIL, `add`, `remove`, reserve launcher, registry |

Unchanged: `LOCKED_BURN = 1`, every rule that uses it (`register` requires `total_lp >
LOCKED_BURN`, every leaf requires `total_lp >= LOCKED_BURN`, `remove` caps a burn at
`total_lp - LOCKED_BURN`), the action layer, the finalizer, `swap`, `observe`, `collect`,
`dao_fee`, every curve export and all five upstream pins. Measured against V15's build: the
action layer, finalizer, slot, `p2_delegated`, reserve amount and four of the six leaves
hash identically; the LP TAIL and `add` and `remove` move only by their message tags.

## What proves it

* `_test_v16_registry.py` (49): the floor paid to `0x...dead` registers; paid to the
  all-zero hash (V15's target), to the creator, to `...deac` (one bit off) or as two units
  instead of one, `register` refuses (`ASSERT_ANNOUNCE_CONSUMED_FAILED`). The creation
  paid nothing to the zero hash.
* `_test_v16_genesis.py`, `_test_v16_create.py` (30): the creator receives `total_lp - 1`,
  `0x...dead` receives 1, the zero hash nothing.
* `_test_v16_integrity.py` (126): the source names the address and `register` asserts it,
  not `zero_bytes32()`; the registry pins protocol 17.
* `_test_v16_actions.py`: burning everything above `LOCKED_BURN` is accepted, one unit into
  it refused, the whole supply refused -- the floor rules are untouched by the move.
* Targeted mutants, built outside `contracts/v16/compiled` and run against the registry
  suite: `register` asserting the zero hash again, `BURN_PUZZLE_HASH` spelled `...deac`,
  and `LOCKED_BURN = 2` -- all three killed (six checks fail each).
* `_test_v16_discoverability.py` (live, testnet11): for every V16 pool, the genesis burn
  sits unspent at `0x...dead`, nothing of the pool's LP was ever paid to the zero hash, and
  LP held + burned + in other pools equals `total_lp`.

## Testnet record (2026-10-09)

Registry `f483a16a854e469f7aa9b2908d63daab47f27b328534857a39d2cd6090996b69` (height
4,797,913; treasury the matrix lock `txch1rdana3...`, dev fee `txch15jhx...`, creation fee
1,000,000 mojos). Pools, each confirmed with its floor at `0x...dead`:

| Pool | Assets | Notes |
|---|---|---|
| A1 | TXCH / t14 | add, swap, observe, collect, then every creator LP unit removed: `total_lp` is 1, the burned unit, and the pool lives on; a further remove is refused |
| A2 | TXCH / T6 | |
| A3 | TXCH / t8 | |
| A4 | TXCH / t14 / t8 | |
| B1 | TXCH / T6 | 20 TXCH deep, at the market ratio, for the relay fixtures |
| B2 | t14 / T6 | CAT/CAT at the market ratio |
| B3 | t14 / t8 / T6 | |
| B4 | T6 vault | |

## Everything else that moves, mechanically

* `PROTOCOL_VERSION` 16 -> 17 in the TAIL, the registry, `forge_v16_driver`,
  `api/_forgeVersion.js` and `src/lib/forgeVersion.ts`; `FORGE_KEPT_PROTOCOLS = [15, 16, 17]`.
* `forge_stdin._LANES = {17: V16, 16: V15, 15: V14}`; V15 imported unconditionally beside
  V14 (argued in `_test_version_hygiene.py`); `forge_resync.REPLAY_LANES[17]`;
  `forge_v16_route._drv` spends V14, V15 and V16 pools on one route.
* `api/_deploymentIndex.js` believes the V16 `(action, tail)` pair; the create lane spawns
  the importer named by the shipping revision tag instead of a literal.
* The in-app settlement lane moves to `src/lib/relay/v16/` (V15 pools hand off to the
  router, as V14's did); `verifyState.ts` carries the V16 LP TAIL, `add` and `remove`
  hashes and protocol 17; `modules.json` and the relay fixtures are regenerated from V16.
* Off-chain, the site's "Burn LP Coin" option and `api/v1/offers/create-pool.js` use the
  same address, and Markets counts LP or tokens at either hash as burned
  (`contracts/cat_supply.py`).
* CHIP-0062 revision 12 names the address; the rule ("a puzzle hash with no preimage") is
  unchanged.
