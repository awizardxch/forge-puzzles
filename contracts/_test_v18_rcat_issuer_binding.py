#!/usr/bin/env python3
"""V18 rCAT: only the asset's own issuer can name its H (owner decision 2026-11-12).

The attack this closes: anyone can wrap a PLAIN CAT in a revocation layer with an H they hold.
A pool naming (asset, H_attacker) would hand every LP's deposited reserve to the attacker's
hidden path. Attestation is OPTIONAL (owner decision 2026-11-12): an rCAT with a nil proof
registers with no issuer condition, and the fake-H defence for it is the router/site gate
(H checked against the asset's mint on chain). An rCAT WITH a proof is enforced in full:

  1. the asset is an rCAT of CHIP-0038's reference TAIL, everything_with_singleton (EWS): the
     registrant names the issuer's launcher id L and TAIL nonce N, and the asset id must equal
     curry(EWS, [SINGLETON_MOD_HASH, struct(L), N]) -- the singleton created at the asset's origin;
  2. that issuer singleton sends a mode-23 message to the registry coin naming (asset, H, pool).

Every case goes through full consensus with the REAL singleton top layer v1.1 spending. Every
refusal has an accepted control built the same way. Refusals: a leaf raise (the registry leaf
runs locally while the bundle is built -- the same CLVM as on chain) or consensus Rejected;
anything else is a harness error. Exit 0 all pass, 1 a failure.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_v18_driver as kit  # noqa: E402
import _test_v18_registry as rt  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

results: list[bool] = []
LEFT = (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY)
RIGHT = (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY)
H = bytes32(b"\x4b" * 32)            # the issuer's revocation hash
H_ATTACKER = bytes32(b"\xa7" * 32)
NONCE = b"\x01"


class HarnessError(Exception):
    pass


def check(label, good, detail=""):
    detail = detail if isinstance(detail, str) else "ACCEPTED"
    results.append(bool(good))
    print(f"  [{'PASS' if good else 'FAIL'}] {label}{'  -- ' + detail if detail else ''}")


def attempt(thunk):
    try:
        bundle = thunk()
        if not isinstance(bundle, kit.SpendBundle):
            raise HarnessError(f"not a SpendBundle: {type(bundle).__name__}")
        kit.validate(bundle)
        return "accepted", ""
    except kit.Rejected as exc:
        return "refused", f"consensus {str(exc)[:55]}"
    except ValueError as exc:
        return "refused", f"leaf {str(exc)[:55]}"


def fresh():
    reg0 = kit.make_registry(salt=0x21, protocol_fee_bps=5)
    kit.validate(kit.registry_spend(reg0, "forge_registry_init", [])[0])
    reg1 = reg0.advance([1, 0, kit.DEFAULT_CREATION_FEE])
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    return reg1, slots


ISSUER = kit.issuer_singleton(bytes32(b"\x5a" * 32))            # the real issuer of R
OTHER = kit.issuer_singleton(bytes32(b"\x5b" * 32))             # someone else's singleton
R = kit.ews_asset_id(ISSUER[0], NONCE)                          # an EWS rCAT minted by ISSUER
PLAIN = bytes32(b"\x14" * 32)                                   # a plain CAT (not an EWS curry)


def pool(asset, hidden, salt):
    return kit.make_pool([None, asset], [10_000_000, 20_000_000], total_lp=5_000_000, leaves="forge", salt=salt,
                         hidden_puzzle_hashes=[kit.ZERO_32, hidden])


def register(p, proofs, issuer_spends=()):
    reg, slots = fresh()
    p.extra["issuer_proofs"] = proofs
    spends = [s(reg, p) for s in issuer_spends]
    return lambda: rt.registration(reg, p, LEFT, RIGHT, slots, more_spends=spends)[0]


def attest(issuer, asset, hidden, message=None):
    return lambda reg, p: kit.issuer_attest_spend(reg, p, asset, hidden, issuer, message=message)


def proof_for(issuer, nonce=NONCE):
    return kit.issuer_proof(issuer[0], nonce, issuer[3].get_tree_hash())


def main() -> int:
    if not (kit.v18_available() and kit.registry_available()):
        print("  [skip] V18 build outputs are absent")
        return 2
    print(f"R = EWS(issuer {ISSUER[0].hex()[:12]}, nonce {NONCE.hex()}) = {R.hex()[:16]}...")

    print("\nCONTROLS:")
    v, why = attempt(register(pool(R, H, 0x61), [None, proof_for(ISSUER)], [attest(ISSUER, R, H)]))
    check("the issuer's EWS rCAT, attested by the issuer singleton: registers", v == "accepted", why)
    v, why = attempt(register(pool(PLAIN, kit.ZERO_32, 0x62), [None, None]))
    check("a plain pool needs no attestation: registers", v == "accepted", why)

    print("\nTHE ATTACK: a plain CAT wrapped in the attacker's own H:")
    v, why = attempt(register(pool(PLAIN, H_ATTACKER, 0x63), [None, proof_for(OTHER)], [attest(OTHER, PLAIN, H_ATTACKER)]))
    check("plain CAT + attacker's H, vouched by the attacker's own singleton: refused", v == "refused", why)
    v, why = attempt(register(pool(PLAIN, H_ATTACKER, 0x64), [None, None]))
    check("plain CAT + attacker's H with no proof at all: ACCEPTED on chain (unattested; the gate's mint-H check refuses it)",
          v == "accepted", why)

    print("\nTHE NEW RULE (2026-11-12): attestation is optional:")
    v, why = attempt(register(pool(R, H, 0x6C), [None, None]))
    check("the issuer's rCAT with no proof and no issuer spend: ACCEPTED (unattested)", v == "accepted", why)
    from chia.types.blockchain_format.program import Program
    p_un = pool(R, H, 0x6D)
    gps = [bytes32(b"\x00" * 32)] * 2
    sol_un = Program.to(kit.register_solution(p_un, LEFT, RIGHT, grandparents=gps, issuer_proofs=[None, None]))
    sol_at = Program.to(kit.register_solution(p_un, LEFT, RIGHT, grandparents=gps, issuer_proofs=[None, proof_for(ISSUER)]))
    hid = [kit.ZERO_32, H]
    last = lambda sol: list(sol.as_iter())[11]
    check("issuer_attested reads the register solution: unattested -> [None, False], attested -> [None, True]",
          kit.issuer_attested(hid, last(sol_un)) == [None, False] and kit.issuer_attested(hid, last(sol_at)) == [None, True])

    print("\nTHE REAL rCAT, a SUPPLIED attestation that is wrong or incomplete:")
    cases = [
        ("a proof but no issuer message", [None, proof_for(ISSUER)], []),
        ("a proof naming the right issuer, message from ANOTHER singleton", [None, proof_for(ISSUER)], [attest(OTHER, R, H)]),
        ("a proof naming another singleton as issuer (asset id mismatch)", [None, proof_for(OTHER)], [attest(OTHER, R, H)]),
        ("the right issuer and message, the wrong TAIL nonce", [None, proof_for(ISSUER, nonce=b"\x02")], [attest(ISSUER, R, H)]),
    ]
    for label, proofs, spends in cases:
        v, why = attempt(register(pool(R, H, 0x65), proofs, spends))
        check(f"{label}: refused", v == "refused", why)

    v, why = attempt(register(pool(R, H_ATTACKER, 0x66), [None, proof_for(ISSUER)], [attest(ISSUER, R, H)]))
    check("the issuer attested H, the pool names another H: refused", v == "refused", why)

    other_pool = pool(R, H, 0x67)
    v, why = attempt(register(pool(R, H, 0x68), [None, proof_for(ISSUER)],
                              [lambda reg, p: kit.issuer_attest_spend(reg, p, R, H, ISSUER,
                                                                      message=kit.rcat_issuer_message(R, H, other_pool.launcher_id))]))
    check("the issuer's attestation for ANOTHER pool, replayed: refused", v == "refused", why)

    print("\nSHAPE:")
    v, why = attempt(register(pool(PLAIN, kit.ZERO_32, 0x69), [None, proof_for(ISSUER)]))
    check("a proof on a plain asset: refused", v == "refused", why)
    v, why = attempt(register(pool(R, H, 0x6A), [proof_for(ISSUER)], [attest(ISSUER, R, H)]))
    check("a proof list one short: refused", v == "refused", why)
    v, why = attempt(register(pool(R, H, 0x6B), [None, proof_for(ISSUER), None], [attest(ISSUER, R, H)]))
    check("a proof list one long: refused", v == "refused", why)

    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} rCAT issuer-binding checks passed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
