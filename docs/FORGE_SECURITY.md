# Security

How to report something, what has been checked, and what has not.

---

## Reporting

If you find a problem, please open an issue on this repository. If you would
rather not do that in public, say so in an issue with no detail and a contact
will be arranged.

There is no bug bounty yet. That is a gap, not a policy, and it is named as an
outstanding item in [`FORGE_AUDIT_TIBETSWAP.md`](FORGE_AUDIT_TIBETSWAP.md). A
report is worth more than a quiet fix either way.

## Scope

**In scope: the revisions in this repository.** `contracts/v16` (protocol 17) is
the shipping pool: the only revision on mainnet, and live on testnet11 beside
`contracts/v15` (protocol 16) and `contracts/v14` (protocol 15), which are kept
live, undrained. `contracts/vault_*.py` with `contracts/mips.py` is the lock.
Those are what an audit should look at. V16 changes one constant from V15 (the
burn address of the locked minimum) and V15 three things from V14; see
`FORGE_PUZZLE_V16.md` and `FORGE_PUZZLE_V15.md`.

**Out of scope: earlier revisions** (V13 and before). They are retired,
unmintable, and not deployed. Their sources and the detail of what was wrong with them are held in a
private findings log rather than published here — not to hide the history, but
because publishing working exploit mechanics for retired code helps an attacker
practising against the current one and helps a reviewer not at all. That log
will be published with the audit request.

What is kept here is the part that is useful to a reviewer: what was tested, how,
and what the tests found.

## External review

An external security review of the responder and these puzzles was received on
2026-09-26. It is described here as exactly that -- a review, with a date -- and
not as a guarantee: the reviewers asked that their work not be presented as an
endorsement or a certification, and this project does not describe its code as
"audited and found secure" by them or by anyone. Its findings were all in the
off-chain responder and router; it found no value-extraction path in the V14
puzzles, and that statement is theirs, about the commits they read, not a
standing property. It predates V15 and V16, so it does not cover what those two
revisions changed. The fixes are in this repository's history from that date.

## What has been checked

Every suite runs the compiled puzzles rather than hashing them. That discipline
was bought the hard way — a DID once confirmed on chain and could never be spent,
because a puzzle had been hashed but never executed.

| Suite | What it establishes |
|---|---|
| `_test_v16_actions.py` | Every leaf, accepted and refused. Each refusal case is a claim about what the puzzle will not do. |
| `_test_v16_reserves_proved.py` | A registered pool's reserves exist: every reserve parent is derived from a launcher the bundle spends, and every way of faking that is refused. |
| `_test_v16_settlement_amount.py`, `_test_v16_action_binding.py` | A leaf binds its settlement's asset **and amount** in the puzzle, with every attack bundle value-balanced so conservation cannot be the thing refusing it. |
| `_test_v16_before_after.py` | The changes since V13 run against the V13 build and the V16 build, so "fixed" is a measurement rather than a reading. |
| `_test_v16_coinid_derivations.py` | V15's `coinid` derivations and canonical atoms, with a `--before` column against the V14 build. |
| `_test_v16_registry.py`, `_test_v16_registry_fee.py` | Admission (with `valid_pool` pinned at the leaf), the locked minimum burned to `0x000...dead` and nowhere else, and the creation fee as registry state moved only by `set_fee` with the treasury's message. |
| `_test_v16_manipulation.py` | The flash-loan analog: chained actions in one spend, the actor's end position priced at pre-sequence spot, never above the start. |
| `_test_v16_curve_equivalence.py` | The curve against its mirror, thousands of cases. |
| `_test_v16_integrity.py` | **Every** puzzle recompiled from source in a scratch copy and compared byte for byte with the shipped hex — leaves, finalizer, TAIL, registry, launcher, upstream pins, curve exports. Exits 2, never 0, when the compiler is absent. |
| `_test_v16_provenance.py` | Every path a published document cites resolves; the manifest's source hashes match the committed tree. |
| `_test_v16_genesis.py` | The genesis mint is bound to one eve coin, so a launcher announcement cannot authorise a second. |
| `_test_v16_consensus_timelocks.py` | Birth heights judged by the mempool's own `check_time_locks`, so a claimed height is checked the way a node checks it. |
| `_test_v16_lp_receive_forgery.py` | An LP burn cannot be forged by emitting conditions that merely look like one; the pool's message is welded to the payout. |
| `_test_v16_dao_fee.py`, `_test_v16_multipool.py`, `_test_v16_lanes_agree.py` | The monotonic fee decrease, routes across pools, and the deploy and website creation lanes producing byte-identical registrations. |
| `_test_mips.py` | The lock's composition against vectors generated from `chia-wallet-sdk` itself — Python compared against Chia's Rust, not against itself. |
| `_test_vault_*.py` | The lock: policy, DIDs, offers, batches, NFTs. |

`docs/FORGE_V14_CLVM_PASS.md` sets out what each leaf asserts and emits, and which
test pins each refusal. It was written for V14; V15 and V16 keep those leaves except
for the lines `FORGE_PUZZLE_V15.md` and `FORGE_PUZZLE_V16.md` list.

## Mutation testing

`scripts/mutate-v16.py` deletes each assertion in turn, rebuilds, and re-runs
thirteen suites. A line no suite reaches is reported **UNREACHED** — never
"survived", and never "redundant". The fourth review found a line we had called
redundant on the strength of a "survived" verdict, and it was load-bearing. So the
run now fails unless every unreached line has either a bracket-level probe that
reaches it or a written argument in `contracts/v16/mutation-arguments.json`, and
that file is published with the code.

V16, measured 2026-10-18: 50 assertions across eleven files, **26 killed, 24
unreached, 0 unbuildable**, every unreached line argued, exit 0. The kills include
the settlement binding's `settlement_amount >= at_least`, the registry's
`valid_pool`, `remove`'s floor cap, the floor rules on `LOCKED_BURN`, the DAO fee's
bounds and the `set_fee` leaf's checks (by the registry-fee suite). The unreached
lines are the documented defence-in-depth twins, the structural list-length checks,
and the zero-id guards on values the puzzle derives. (V14's own run, for comparison:
48 assertions across ten files, 21 killed, 27 unreached.)

This is published because a reviewer should know which of our guards are
load-bearing and which are belt-and-braces; we would rather be told we have
miscounted than have it assumed we checked.

## Comparison against published failures

[`FORGE_AUDIT_TIBETSWAP.md`](FORGE_AUDIT_TIBETSWAP.md) tests this pool against
both publicly documented TibetSwap failures — the announcement that did not name
its singleton, and the swap that accepted negative input amounts. Forge is not
vulnerable to either, and the document shows the evidence rather than asserting
it, including where the defence turned out to be a different line than expected.

## What has not been checked

- **No commissioned audit.** Four rounds of external review — Chia Network's, a
  six-agent community audit, trgarrett's, and a review of the documents — have
  each found real defects, all on Chia-Network/chips#217, and each is reproduced
  and disposed of in the documents here. Nobody has been engaged to audit this
  code end to end. That is the reason this repository exists. Attribution:
  commissioned audit, none; external community review, CNI and trgarrett; automated,
  cursor[bot]; internal, ours.
- **Mainnet is live on V16.** Since 2026-10-09 the V16 pools on mainnet hold real
  funds (21 pools on 2026-10-18), on the strength of the checks above and the
  reviews named here, not of an audit.
- **The off-chain half is elsewhere.** The router, the quoting and the interface
  live in a separate repository. The router holds no key and cannot alter what a
  trader signed, but it has its own failure modes and they are not in scope here.

## Copyright

MIT.
