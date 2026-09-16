# Forge — puzzles

The on-chain half of [Forge](https://forge.awizard.dev): a weighted N-asset AMM
on Chia, and the multisig lock that can own and operate one.

Everything here can move funds. That is the whole point of the split — the
interface lives in a separate repository, so what is in front of you *is* the
attack surface and nothing dilutes that claim.

> **Status: testnet research. Unaudited.** V14 (protocol 15) is live on testnet11
> with 32 pools. It replaces V13, which a fourth independent review retired on
> 2026-09-15: the registry admitted a pool whose reserves were never funded, and
> the slot it took could never be spent by anyone. V14 derives every reserve's
> parent from a reserve launcher the registration bundle must spend, and every
> leaf that takes value binds its settlement's amount in the puzzle. Four rounds
> of external review have found real defects in this code; there has been no
> commissioned audit. Every leaf carries this warning in its own header, and it
> is not decoration.

---

## What is here

```
contracts/
  v14/                 the shipping pool, a rue project
    puzzles/           Forge's leaves, the finalizer, the reserve launcher, the curve
    puzzles/upstream/  CNI's CHIP-0050 puzzles, vendored verbatim
    compiled/          built hex and tree hashes
    pins.json          upstream hashes, pinned byte-for-byte
  *.py                 the driver, the math mirrors, the lock, the tooling
  _test_*.py           the suites — every one runs the puzzles
docs/                  protocol documents
```

**Start at [`docs/FORGE_PUZZLE_V14.md`](docs/FORGE_PUZZLE_V14.md)**: what the
fourth and fifth reviews found, what V14 changes, and what was measured rather
than argued. Then [`docs/FORGE_V14_CLVM_PASS.md`](docs/FORGE_V14_CLVM_PASS.md)
for the leaf-by-leaf reading with the mutation verdict on every assert, and
[`docs/FORGE_PUZZLE_V14_SPEC.md`](docs/FORGE_PUZZLE_V14_SPEC.md) for the design
decisions and the five fix routes simulated against consensus before one was
chosen.

## The design in one paragraph

The pool is an action-layer singleton built on **CHIP-0050**. CNI's upstream
`action`, `finalizer`, `slot` and `p2_delegated_by_singleton` are vendored
verbatim and required to hash to the values in `contracts/v14/pins.json`; Forge
adds six leaves (`swap`, `add`, `remove`, `observe`, `collect`, `dao_fee`), a
multi-reserve finalizer written from the upstream reserve finalizer, and — in
V14 — a reserve launcher, the only puzzle a reserve may be born from. The rule
that runs through every revision since V12 is *derive the identifier, never
accept it*: reserve parents are written by the finalizer and, at genesis, derived
by the registry; settlements are named by parent and amount and their ids
derived by the leaf. Every finding of the last three reviews was a puzzle taking
an authorizing identifier from its own solution.

The **lock** is a Chia vault: a singleton whose inner puzzle is CNI's MIPS
`delegated_puzzle_feeder` over `m_of_n`, so it keeps its address while its owners
change by vote. Funds sit in `p2_singleton_via_delegated_puzzle` coins the
singleton authorises one at a time. See
[`docs/FORGE_MULTISIG.md`](docs/FORGE_MULTISIG.md).

## Building

Needs [`rue`](https://github.com/rigidity/rue) 0.8.4 on PATH.

```bash
python scripts/build-v14.py     # in the monorepo; rebuilds and re-checks the pins
rue build --hex --hash --all .  # in contracts/v14, to build in place
```

`pins.json` records every upstream puzzle's tree hash **and** its source's
sha256, so a stale hex can be told from a fresh one without rue installed.

## Running the suites

Every suite runs the compiled puzzles rather than hashing them. That discipline
was bought the hard way: a DID once confirmed on chain and could never be spent,
because a puzzle was hashed but never executed.

```bash
python contracts/_test_v14_actions.py            # the leaves, accept and refuse
python contracts/_test_v14_reserves_proved.py    # the reserve launcher and the derived parent (R-1)
python contracts/_test_v14_settlement_amount.py  # the settlement binding, attacks value-balanced
python contracts/_test_v14_before_after.py       # each change against the V13 AND V14 builds
python contracts/_test_v14_manipulation.py       # flash-loan-analog sequences
python contracts/_test_v14_curve_equivalence.py
python contracts/_test_v14_genesis.py            # the eve-bound genesis mint
python contracts/_test_v14_consensus_timelocks.py  # birth heights, judged offline
python contracts/_test_v14_integrity.py          # every puzzle recompiled; exit 2 without rue
python contracts/_test_mips.py               # against vectors from chia-wallet-sdk
python contracts/_test_vault_policy.py       # the lock
```

They need a Python environment with a current `chia-blockchain` — an older
`chia_rs` will fail to import.

## Security

Start at **[`docs/FORGE_SECURITY.md`](docs/FORGE_SECURITY.md)**: how to report,
what is in scope, what has been checked and what has not.

- [`docs/FORGE_AUDIT_TIBETSWAP.md`](docs/FORGE_AUDIT_TIBETSWAP.md) — both public
  TibetSwap failures tested against these puzzles, with the mutation test that
  established which layer is actually load-bearing.
- [`docs/FORGE_V14_CLVM_PASS.md`](docs/FORGE_V14_CLVM_PASS.md) — what each leaf
  asserts and emits, which test pins each refusal, and the mutation verdict on
  every line under the UNREACHED rule.

**Scope is the revision in this repository.** Retired revisions are not published
here and are not deployed; the detail of what was wrong with them sits in a
private findings log and will be published with the audit request. Publishing
working mechanics for dead code helps an attacker practising against the live one
and helps a reviewer not at all.

If you find something, please open an issue. Given what happened to TibetSwap in
August 2026 — a bug that two AI audits and several expert human reviews missed —
a report is worth more than a quiet fix.

## How this repository is maintained

Code is developed in a private monorepo and pushed here in one direction. Issues
are very welcome; pull requests cannot be merged directly, because the source of
truth is elsewhere — open one anyway if it is the clearest way to show a fix, and
it will be applied upstream with credit.

## Licence

MIT.
