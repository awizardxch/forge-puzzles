# Forge V14 audit run — 2026-09-24: revocable CATs and the imprint

A targeted run of the runbook in `skills/n-asset-pool-audit/SKILL.md` against the shipping
revision, on one question raised at a public AMA the same day:

> A plain CAT can carry the rCAT (CHIP-0038) pattern without being an rCAT. Could a pool
> read such a coin as revocable, alone or combined with a genuine one in a swap or an add,
> and have its LP bricked by whoever imprinted it?

**Verdict: no finding against the puzzles.** The imprint is real and costs nothing. V14
refuses every layered coin, whatever its hidden puzzle hash, on every lane a stranger can
reach, on a simulator and on testnet11, and a fake riding in the same bundle as a genuine
coin changes nothing about a reserve. Two findings against this run's own probes, both
fixed in the run and turned into runbook rule 11. The design constraints for a future
revision that accepts revocable liquidity are recorded below as constraints, not findings.

**Who ran this.** Claude Opus 5.5, from the runbook, on a question the runbook listed only
as a roadmap ("rCAT roadmap": two sentences on transfer hooks). A question asked from
outside is a good test of a runbook, because nobody chose it to fit the method. What the
run needed and the runbook did not say is now in it: the rCAT and layered-asset section,
the wallet-free refusal pair under the Testnet11 protocol, and rule 11. The earlier passes
(`FORGE_AUDIT_RUN_V14_2026-09-19.md`, `FORGE_AUDIT_RUN_V14_2026-09-20.md`) are not
re-litigated here.

## Revision under audit

| | |
|---|---|
| Revision | V14, protocol 15 |
| Artifacts | `contracts/v14`, 89 files |
| Fingerprint | `6298777ef1ac2ecf835b2323d9f6a8805c7ced1c9a8764d195f5b9960e147ab4` |
| Recompute it | `python scripts/revision-fingerprint.py` |
| Toolchain | Python 3.11, chia-blockchain 2.5.5, chia_rs 0.27.0 |
| Chain | testnet11, peaks 4,732,797 to 4,733,650 |
| Revocation layer | `REVOCATION_LAYER` from `chia_puzzles_py` 0.20.3, the puzzle chia's own rCAT wallet uses; hash `00848115…131f51` |

The fingerprint matches the 2026-09-20 run's, and `_test_v14_integrity.py` recompiles every
source and compares it to the shipped hex: 122/122. Nothing in the puzzles moved.

## The layer, as the probes use it

Read off the disassembly, not the specification: curried `(MOD_HASH HIDDEN_PUZZLE_HASH
INNER_PUZZLE_HASH)`, solved `(hidden puzzle solution)`. The **inner path** requires the
puzzle to hash to `INNER_PUZZLE_HASH` and rewrites every `CREATE_COIN` to
`revocation(H, ph)`. The **hidden path** requires the puzzle to hash to `H` and passes its
conditions through untouched. The TAIL runs only at issuance and melt, so nothing ties a
layer to the issuer. Three hidden hashes are probed throughout: an issuer-shaped key-gated
H, an imprinter's H, and `sha256tree(1)`, which anyone can satisfy.

## Lanes

| Lane | What ran | Result |
|---|---|---|
| Offline | `contracts/_sim_v14_rcat_imprint.py`, the mempool's own validator | 43/43 |
| Offline | `_test_v14_asset_scope.py`, `_test_v14_integrity.py`, `_test_version_hygiene.py` | 8/8, 122/122, every crossing argued |
| Provenance | `_test_v14_provenance.py`, `FORGE_REPO` at a tracking clone | 86/86, exit 0 |
| Publication gate | `scripts/check-doc-links.py` against the real slice | every citation in a published document is published |
| Simulator | `scripts/sim-v14-rcat-imprint.py`, real coin store, pool created through the registry | 20/20 |
| Testnet11, unsigned | `scripts/v14-rcat-imprint-live-probe.py --pools A1,D2` | 16/16 |
| Testnet11, unsigned | `scripts/v14-rcat-imprint-live-probe.py --pools all` | **244/244 across all 32 pools** at peak 4,733,650: 29 pools on both lanes, the 3 one-asset vaults on the reserve lane (a vault has no swap); none skipped |
| Testnet11, signed | `scripts/v14-rcat-imprint-testnet.py`, five stages | as below |

### What the chain lane proved

**Unsigned refusal pairs.** Forge's pool spends need no signature, and a node checks
conditions before it looks coins up. So each probe is a pair built from a pool's real tip:
the attack, and the same bundle in its honest shape, both naming a coin that does not
exist. The control must clear every guard and die only at `UNKNOWN_UNSPENT`; that is what
makes the attack's code meaningful. Nothing can be included.

| Lane | Control | Issuer-shaped H | Imprinted H | Keyless H |
|---|---|---|---|---|
| swap or add, paid with a layered settlement | `UNKNOWN_UNSPENT` | `ASSERT_CONCURRENT_SPEND_FAILED` | `ASSERT_CONCURRENT_SPEND_FAILED` | `ASSERT_CONCURRENT_SPEND_FAILED` |
| a layered coin substituted for a live reserve | `UNKNOWN_UNSPENT` | `MESSAGE_NOT_SENT_OR_RECEIVED` | `MESSAGE_NOT_SENT_OR_RECEIVED` | `MESSAGE_NOT_SENT_OR_RECEIVED` |

`add` passes each deposit through the same `settlement_binding` as a swap input
(`forge_action_add.rue:39`, `forge_action_swap.rue:93`), so a mixed add is checked per coin.

**Signed, with real coins**, from a testnet wallet of ours holding the test token T6. Every
hidden hash was our own key, except one deliberately keyless coin, 1 T6 each:

| Stage | Height | Result |
|---|---|---|
| imprint two T6 coins from an ordinary transfer, no issuer involved | 4,733,607 | confirmed |
| how the wallet (Sage) shows them | — | T6 stays one plain token with no revocation address; both coins neither counted nor listed |
| the real imprinted coin, signed by its owner, pays a swap on pool A1 | — | refused `ASSERT_CONCURRENT_SPEND_FAILED`; offline validator refused first; coin untouched |
| take the keyless coin with an empty signature | 4,733,634 | confirmed |
| strip the other coin's layer through our hidden key | 4,733,639 | confirmed, a plain T6 coin again |

The swap stage is the strongest evidence in the run: every coin in that bundle existed with
true lineage and a valid signature, so the settlement binding is the only thing that could
have refused it. The probe pushes only after the local validator has already refused, so a
guard failure would have been reported without trading against a live pool.

### What the simulator proved

On an in-process node, with a V14 pool created through the real registry:

- the imprint, the keyless theft and a second revoker nested inside a genuine rCAT coin
  all confirm; chia's classifier reports only the outer H;
- a swap paid by the imprinted settlement is refused `ASSERT_CONCURRENT_SPEND_FAILED`, and
  the identical plain swap confirms;
- **the combination**: a swap whose ring carries the real settlement *and* an imprinted
  coin confirms, the new reserve is plain, and the fake's value leaves under its own layer;
  a twin planted at the reserve's exact puzzle hash is ignored, and its planter's attempt
  to spend it back is refused `MESSAGE_NOT_SENT_OR_RECEIVED`; the pool's next spend
  confirms;
- walking each coin back to the spend that revealed its TAIL separates the imprint (the
  issuance made plain coins) from the genuine coin (the issuance made `revocation(H)`
  coins). It does not see a nested revoker; only an exact-hash check does.

## Findings against this run's probes

### A-1 — a put-back probe passed for the wrong reason (fixed in the run)

- **Risk:** none to the puzzles; the probe would have reported a guard it never reached.
- The offline "revoke and put an identical coin back" probe gave the replacement coin its
  grandparent's lineage proof. The CAT layer died (`GENERATOR_RUNTIME_ERROR`, 117) before
  the finalizer ran, and a check written as "was it refused?" passed. **Fixed:** the coin
  carries its own lineage, a control spends it standalone to prove the lineage sound, and
  the check pins `MESSAGE_NOT_SENT_OR_RECEIVED` (147).

### A-2 — a helper hard-coded the fixture's asset id (fixed in the run)

- **Risk:** none to the puzzles; every offline pool used the one asset, so it could not show.
- The helper that builds a layered reserve computed its puzzle hash with the fixture asset
  id. The first live run, against pools holding other assets, came back
  `WRONG_PUZZLE_HASH` where `MESSAGE_NOT_SENT_OR_RECEIVED` was expected, and only the pinned
  code made that a failure. **Fixed:** the hash is built from the reserve's own asset id.

Both became rule 11: pin the exact refusal code, and run a probe away from the fixture it
was written on.

## Design constraints for a revision that accepts revocable liquidity

Not findings: V14 accepts no layered coin. Measured offline against the compiled V14
puzzles, with the reserve's curried hash changed and nothing else:

- **Acceptance is one curried hash away.** A finalizer curried with
  `CAT(asset, revocation(H, p2))` carries observe and swap without complaint; the inner path
  re-wraps its own recreate. `_test_v14_asset_scope.py` pins the refusal so this cannot
  arrive by accident.
- **H must be configuration, never read from a coin.** With H fixed at creation, an
  imprinted coin, and an issuer-H twin of the same amount, are both refused as the reserve
  (147), and a mixed ring leaves the reserve under the configured H only.
- **A revocation bricks without stealing.** After any hidden-path spend, including one that
  puts an identical coin straight back, the pool refuses the replacement (147) and every
  asset in it is frozen. Fixing H defeats the fake; it does not constrain the issuer.
- **Splits and merges must go through the pool.** At 100 XCH / 200,000 X through the real
  curve, a 2:1 split the pool does not take part in costs LPs 29.3% (10:1: 68.3%); a merge
  leaves the pool holding supply the merge meant to retire.

A future revision must therefore: fix H in the pool configuration and derive every reserve
and settlement from it; decide nothing about revocability at run time from a coin it is
shown; check H against the issuance at creation and refuse keyless hidden puzzles; route
splits, merges and revokes through an issuer lane that keeps LP supply and rescales the
oracle; let a revoked reserve freeze only itself; and hold at most one revocable asset per
pool. Each is a test to write before that revision ships.

## Residuals

- Wallets other than Sage are untested. chia's reference rCAT wallet reads H from the coin;
  how it treats an imprinted coin was read from its source, not run.
- Nothing of the future design is built. The constraints above are measured against V14's
  puzzles with one curried value changed, which is evidence for the constraint, not for any
  design that meets it.
- The three residuals carried by the 2026-09-20 run are unchanged.
