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

**In scope: the revision in this repository.** `contracts/v14` is the shipping
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
| `_test_v14_actions.py` | Every leaf, accepted and refused. Each refusal case is a claim about what the puzzle will not do. |
| `_test_v14_reserves_proved.py` | A registered pool's reserves exist: every reserve parent is derived from a launcher the bundle spends, and every way of faking that is refused. |
| `_test_v14_settlement_amount.py`, `_test_v14_action_binding.py` | A leaf binds its settlement's asset **and amount** in the puzzle, with every attack bundle value-balanced so conservation cannot be the thing refusing it. |
| `_test_v14_before_after.py` | Each V14 change run against the V13 build and the V14 build, so "fixed" is a measurement rather than a reading. |
| `_test_v14_manipulation.py` | The flash-loan analog: chained actions in one spend, the actor's end position priced at pre-sequence spot, never above the start. |
| `_test_v14_curve_equivalence.py` | The curve against its mirror, thousands of cases. |
| `_test_v14_integrity.py` | **Every** puzzle recompiled from source in a scratch copy and compared byte for byte with the shipped hex — leaves, finalizer, TAIL, registry, launcher, upstream pins, curve exports. Exits 2, never 0, when the compiler is absent. |
| `_test_v14_provenance.py` | Every path a published document cites resolves; the manifest's source hashes match the committed tree. |
| `_test_v14_genesis.py` | The genesis mint is bound to one eve coin, so a launcher announcement cannot authorise a second. |
| `_test_v14_consensus_timelocks.py` | Birth heights judged by the mempool's own `check_time_locks`, so a claimed height is checked the way a node checks it. |
| `_test_v14_lp_receive_forgery.py` | An LP burn cannot be forged by emitting conditions that merely look like one; the pool's message is welded to the payout. |
| `_test_v14_registry.py`, `_test_v14_dao_fee.py`, `_test_v14_multipool.py`, `_test_v14_lanes_agree.py` | The registry (with `valid_pool` pinned at the leaf), the monotonic fee decrease, routes across pools, and the deploy and website creation lanes producing byte-identical registrations. |
| `_test_mips.py` | The lock's composition against vectors generated from `chia-wallet-sdk` itself — Python compared against Chia's Rust, not against itself. |
| `_test_vault_*.py` | The lock: policy, DIDs, offers, batches, NFTs. |

`docs/FORGE_V14_CLVM_PASS.md` sets out what each leaf asserts and emits, and which
test pins each refusal.

## Mutation testing

`scripts/mutate-v14.py` deletes each assertion in turn, rebuilds, and re-runs
eleven suites. A line no suite reaches is reported **UNREACHED** — never
"survived", and never "redundant". The fourth review found a line we had called
redundant on the strength of a "survived" verdict, and it was load-bearing. So the
run now fails unless every unreached line has either a bracket-level probe that
reaches it or a written argument in `contracts/v14/mutation-arguments.json`, and
that file is published with the code.

48 assertions across ten files: **21 killed, 27 unreached, 0 unbuildable**, every unreached line argued, exit 0. Every assertion V14 introduced or corrected is killed: the settlement binding's `settlement_amount >= at_least`, the registry's `valid_pool` (unreached in the first run because the suite handed its probes another pool's neighbours; killed once each pool was bracketed by its own), `remove`'s floor cap (unreached in the first run because the Python mirror's own guard raised first; killed with payouts sized by hand), the prologue's `dao_fee_bps >= 0` (killed by a pool minted with a negative rate in state) and `add`'s `deposit >= 0` (R-2). The unreached lines are the documented defence-in-depth twins in `swap` and `add`, the structural list-length checks, and the zero-id guards on values the puzzle now derives.

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
- **Testnet only.** Nothing here has held mainnet funds.
- **The off-chain half is elsewhere.** The router, the quoting and the interface
  live in a separate repository. The router holds no key and cannot alter what a
  trader signed, but it has its own failure modes and they are not in scope here.

## Copyright

MIT.
