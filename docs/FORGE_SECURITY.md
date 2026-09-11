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

**In scope: the revision in this repository.** `contracts/v11` is the shipping
pool, live on testnet11, and `contracts/vault_*.py` with `contracts/mips.py` is
the lock. Those are what an audit should look at.

**Out of scope: earlier revisions.** They are retired, unmintable, and not
deployed. Their sources and the detail of what was wrong with them are held in a
private findings log rather than published here — not to hide the history, but
because publishing working exploit mechanics for retired code helps an attacker
practising against the current one and helps a reviewer not at all. That log
will be published with the audit request.

What is kept here is the part that is useful to a reviewer: what was tested, how,
and what the tests found.

## What has been checked

Every suite runs the compiled puzzles rather than hashing them. That discipline
was bought the hard way — a DID once confirmed on chain and could never be spent,
because a puzzle had been hashed but never executed.

| Suite | What it establishes |
|---|---|
| `_test_v11_actions.py` | Every leaf, accepted and refused. Each refusal case is a claim about what the puzzle will not do. |
| `_test_v11_manipulation.py` | The flash-loan analog: chained actions in one spend, the actor's end position priced at pre-sequence spot, never above the start. |
| `_test_v11_curve_equivalence.py` | The curve against its mirror, thousands of cases. |
| `_test_v11_integrity.py` | Built hex matches source; upstream pins hold byte-for-byte. |
| `_test_v11_registry.py`, `_test_v11_dao_fee.py`, `_test_v11_multipool.py` | The registry, the monotonic fee decrease, routes across pools. |
| `_test_mips.py` | The lock's composition against vectors generated from `chia-wallet-sdk` itself — Python compared against Chia's Rust, not against itself. |
| `_test_vault_*.py` | The lock: policy, DIDs, offers, batches, NFTs. |

`docs/FORGE_V11_CLVM_PASS.md` is the written CLVM pass over every leaf: what each
asserts, what it emits, and which test pins each refusal.

## Mutation testing

`scripts/mutate-v11.py` deletes each assertion in turn, rebuilds, and re-runs the
suites. An assertion whose deletion changes nothing is either redundant or
untested, and the two are worth telling apart.

The current sweep is **10 killed, 22 survived of 32**. Most survivors are
structurally unreachable — the curve refuses first — and the leaves say so where
that is the case. This is published because a reviewer should know which of our
guards are load-bearing and which are belt-and-braces; we would rather be told we
have miscounted than have it assumed we checked.

## Comparison against published failures

[`FORGE_AUDIT_TIBETSWAP.md`](FORGE_AUDIT_TIBETSWAP.md) tests this pool against
both publicly documented TibetSwap failures — the announcement that did not name
its singleton, and the swap that accepted negative input amounts. Forge is not
vulnerable to either, and the document shows the evidence rather than asserting
it, including where the defence turned out to be a different line than expected.

## What has not been checked

- **No external audit.** No revision has been reviewed by anyone outside the team
  that wrote it. That is the reason this repository exists.
- **Testnet only.** Nothing here has held mainnet funds.
- **The off-chain half is elsewhere.** The router, the quoting and the interface
  live in a separate repository. The router holds no key and cannot alter what a
  trader signed, but it has its own failure modes and they are not in scope here.

## Copyright

MIT.
