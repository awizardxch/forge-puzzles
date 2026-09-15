# Forge — puzzles

The on-chain half of [Forge](https://forge.awizard.dev): a weighted N-asset AMM
on Chia, and the multisig lock that can own and operate one.

Everything here can move funds. That is the whole point of the split — the
interface lives in a separate repository, so what is in front of you *is* the
attack surface and nothing dilutes that claim.

> **Status: testnet research. Unaudited.** V13 is live on testnet11 with 32
> pools, and every action a user can take — swap in either direction, deposit,
> withdrawal, multi-hop route and split route — has settled on chain through the
> same keyless responder the interface uses. It has still been reviewed by nobody
> outside the team that wrote it. Every leaf carries this warning in its own
> header, and it is not decoration.

---

## What is here

```
contracts/
  v13/                 the shipping pool, a rue project
    puzzles/           Forge's leaves, the finalizer, the curve
    puzzles/upstream/  CNI's CHIP-0050 puzzles, vendored verbatim
    compiled/          built hex and tree hashes
    pins.json          upstream hashes, pinned byte-for-byte
  *.py                 the driver, the math mirrors, the lock, the tooling
  _test_*.py           the suites — every one runs the puzzles
docs/                  protocol documents
```

**Start at [`docs/FORGE_PUZZLE_V13.md`](docs/FORGE_PUZZLE_V13.md)**: coin layout,
config and state, the prologue, the leaves, the finalizer, authorization, fees,
the registry, and the change each of the CHIP-0062 review's findings asked for.

## The design in one paragraph

The pool is an action-layer singleton built on **CHIP-0050**. CNI's upstream
`action`, `finalizer`, `slot` and `p2_delegated_by_singleton` are vendored
verbatim and required to hash to the values in `contracts/v13/pins.json`; Forge
adds five leaves (`swap`, `add`, `remove`, `collect`, `observe`, plus a DAO fee
leaf) and a multi-reserve finalizer written from the upstream reserve finalizer.
Reserves are bound by the finalizer rather than by hand-rolled announcements,
which makes a whole class of binding mistake structurally impossible rather than
merely fixed.

The **lock** is a Chia vault: a singleton whose inner puzzle is CNI's MIPS
`delegated_puzzle_feeder` over `m_of_n`, so it keeps its address while its owners
change by vote. Funds sit in `p2_singleton_via_delegated_puzzle` coins the
singleton authorises one at a time. See
[`docs/FORGE_MULTISIG.md`](docs/FORGE_MULTISIG.md).

## Building

Needs [`rue`](https://github.com/rigidity/rue) 0.8.4 on PATH.

```bash
python scripts/build-v13.py     # in the monorepo; rebuilds and re-checks the pins
rue build --hex --hash --all .  # in contracts/v13, to build in place
```

`pins.json` records every upstream puzzle's tree hash **and** its source's
sha256, so a stale hex can be told from a fresh one without rue installed.

## Running the suites

Every suite runs the compiled puzzles rather than hashing them. That discipline
was bought the hard way: a DID once confirmed on chain and could never be spent,
because a puzzle was hashed but never executed.

```bash
python contracts/_test_v13_actions.py        # the leaves, accept and refuse
python contracts/_test_v13_manipulation.py   # flash-loan-analog sequences
python contracts/_test_v13_curve_equivalence.py
python contracts/_test_v13_genesis.py        # the eve-bound genesis mint
python contracts/_test_v13_consensus_timelocks.py  # birth heights, judged offline
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
- [`docs/FORGE_PUZZLE_V13.md`](docs/FORGE_PUZZLE_V13.md) — what each leaf
  asserts and emits, and which test pins each refusal.

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
