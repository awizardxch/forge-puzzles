#!/usr/bin/env python3
"""V19 liquidity-creation matrix: register, then add to and remove from, pools of every shape.

Shapes: a 1-asset vault; 2 assets (XCH/CAT, CAT/CAT); 3 and 4 assets; unequal weights in lowest
terms; with and without a DAO (named with consent: kit.DAO_CONSENT_PH + dao_consent=True); and
with one rCAT asset (hidden_puzzle_hashes = [ZERO, ..., H]).

For every shape, through the REAL registry (make_registry(protocol_fee_bps=5), init, registration):
  * a genesis supply exactly at kit.MIN_GENESIS_LP registers;
  * a genesis supply one under it is refused;
  * the first add mints exactly the mirror's LP (forge_math.invariant_lp_mint(..., version=10));
  * a remove pays each asset pro rata (forge_math.withdrawal_amounts, vault fee on a vault);
  * every accepted bundle passes the per-reserve leak oracle read from consensus output:
    reserve coin amount minus its CREATE_COINs is exact (-deposit on add, 0 on remove), never positive.

Exit 0 all pass, 1 a failure, 2 build outputs absent.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_math  # noqa: E402
import forge_v19_driver as kit  # noqa: E402
import _test_v19_registry as rt  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle  # noqa: E402
from chia.wallet.trading.offer import OFFER_MOD_HASH  # noqa: E402
from chia.wallet.vc_wallet.vc_drivers import REVOCATION_LAYER, REVOCATION_LAYER_HASH  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

Program = kit.Program
CATS = [bytes32(bytes([0xD0 + i]) * 32) for i in range(4)]
ISSUER_PUZZLE = Program.to([5, [4, 1, []]])
H = bytes32(ISSUER_PUZZLE.get_tree_hash())
FLOOR = kit.MIN_GENESIS_LP
H0 = 6_999_990
RECIPIENT = bytes32(b"\x55" * 32)
# V19 (rCAT issuer binding): `register` accepts an rCAT only when its asset id is CHIP-0038's
# everything_with_singleton TAIL of an issuer singleton that attests (asset, H, pool) to the
# registry coin. The rCAT shapes use R, a real EWS asset, and its issuer's attestation.
# Attestation is OPTIONAL since 2026-11-12: the "unattested" shapes carry nil proofs and no issuer
# spend (R itself, and a non-EWS CAT under H), and must register and trade exactly as well.
ISSUER = kit.issuer_singleton(bytes32(b"\x5c" * 32))
ISSUER_NONCE = b"\x01"
R = kit.ews_asset_id(ISSUER[0], ISSUER_NONCE)
assert R > max(CATS), "the rCAT shapes keep XCH first and ascending ids with R last"
results: list[bool] = []
_salt = [0x60]


def check(label, good, detail=""):
    results.append(bool(good))
    print(f"  [{'PASS' if good else 'FAIL'}] {label}{'  -- ' + str(detail) if detail else ''}")


def attempt(thunk):
    try:
        return "accepted", thunk()
    except kit.Rejected as exc:
        return "refused", f"consensus {str(exc)[:70]}"
    except ValueError as exc:
        return "refused", f"leaf {str(exc)[:70]}"


def cat_ph(asset, inner_ph):
    return bytes32(construct_cat_puzzle(CAT_MOD, asset, Program.to(inner_ph)).get_tree_hash_precalc(inner_ph))


def layered_ph(asset, hidden, inner_ph):
    rev = REVOCATION_LAYER.curry(REVOCATION_LAYER_HASH, hidden, inner_ph).get_tree_hash()
    return cat_ph(asset, rev)


def payout_ph(pool, i):
    """Where the pool pays asset i to a settlement (offer) recipient."""
    asset, h = pool.asset_ids[i], pool.hiddens[i]
    if asset is None:
        return bytes32(OFFER_MOD_HASH)
    return cat_ph(asset, OFFER_MOD_HASH) if h == kit.ZERO_32 else layered_ph(asset, h, OFFER_MOD_HASH)


def leaks(pool, conds):
    ids = {bytes(r.coin.name()): r.index for r in pool.reserves}
    return {ids[bytes(s.coin_id)]: int(s.coin_amount) - sum(int(c[1]) for c in s.create_coin)
            for s in conds.spends if bytes(s.coin_id) in ids}


def fresh_registry():
    reg0 = kit.make_registry(salt=0x21, protocol_fee_bps=5)
    kit.validate(kit.registry_spend(reg0, "forge_registry_init", [])[0])
    reg1 = reg0.advance([1, 0, kit.DEFAULT_CREATION_FEE])
    slots = {kit.MIN_KEY: (reg0.coin, reg0.inner_hash), kit.MAX_KEY: (reg0.coin, reg0.inner_hash)}
    return reg1, slots


def issuer_spends(reg, pool):
    """The issuer's attestation spend for each rCAT asset of the pool (all R, issued by ISSUER);
    none for an unattested pool."""
    if not pool.extra.get("attested", True):
        return []
    return [kit.issuer_attest_spend(reg, pool, a, h, ISSUER)
            for a, h in zip(pool.asset_ids, pool.hiddens) if h != kit.ZERO_32]


def register(pool, dao):
    reg, slots = fresh_registry()
    return attempt(lambda: kit.validate(rt.registration(
        reg, pool, (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY), slots,
        dao_consent=True if dao else None, more_spends=issuer_spends(reg, pool))[0]))


def build(assets, reserves, weights, total_lp, dao, hidden, attested=True):
    _salt[0] += 1
    kw = dict(dao_ph=kit.DAO_CONSENT_PH, dao_fee_bps=50) if dao else {}
    if hidden is not None:
        kw["hidden_puzzle_hashes"] = hidden
    pool = kit.make_pool(assets, reserves, total_lp=total_lp, leaves="forge", salt=_salt[0], weights=weights,
                         protocol_ph=bytes32(b"\x7a" * 32), **kw)
    pool.extra["attested"] = attested
    if hidden is not None and not attested:
        pool.extra["issuer_proofs"] = [None] * len(hidden)     # unattested: nil proof on every rCAT
    elif hidden is not None:
        assert all(a == R for a, h in zip(assets, hidden) if h != kit.ZERO_32), "every rCAT here is ISSUER's R"
        pool.extra["issuer_proofs"] = [None if h == kit.ZERO_32 else
                                       kit.issuer_proof(ISSUER[0], ISSUER_NONCE, ISSUER[3].get_tree_hash())
                                       for h in hidden]
    return pool


def settlement(pool, i, amount, salt):
    asset = pool.asset_ids[i]
    if asset is None:
        coin, spend = kit.offer_settlement_xch(amount, salt=salt)
        return coin, [spend], {}
    coin, spendable = kit.offer_settlement_cat(asset, amount, salt=salt, hidden=pool.hiddens[i])
    return coin, [], {asset: [spendable]}


def add_bundle(pool, deposits):
    n = len(deposits)
    mint = forge_math.invariant_lp_mint(pool.state[0], deposits, pool.state[1], pool.fee_bps, pool.weights, version=19, **kit.add_fee_rates(pool))
    probe, _, _, _ = kit.run_leaf(pool, "forge_action_add", [H0, deposits, mint, bytes32(b"\x01" * 32),
                                                             [bytes32(b"\x02" * 32)] * n, list(deposits)])
    lp_parent, lp_spends = kit.lp_mint_spends(pool, mint, pool.state[1] + mint, probe.get_tree_hash(), RECIPIENT, salt=0xA0)
    es, ec, parents, amounts = list(lp_spends), {}, [], []
    for i, dep in enumerate(deposits):
        coin, s, c = settlement(pool, i, dep, 0xA1 + i)
        es += s
        for a, sp in c.items():
            ec.setdefault(a, []).extend(sp)
        parents.append(coin.parent_coin_info)
        amounts.append(int(coin.amount))
    bundle, new_state = kit.spend_action(pool, "forge_action_add", [H0, deposits, mint, lp_parent, parents, amounts],
                                         extra_spends=es, extra_cats=ec)
    return bundle, new_state, mint


def remove_bundle(pool, burn, h=H0 + 1):
    vf = forge_math.vault_fee_bps(len(pool.state[0]), 10, pool.fee_bps)
    shares = forge_math.withdrawal_amounts(pool.state[0], burn, pool.state[1], vf)
    probe, _, _, _ = kit.run_leaf(pool, "forge_action_remove", [h, burn, bytes32(b"\x01" * 32), shares])
    lp_parent, lp_spends = kit.lp_melt_spend(pool, burn, pool.state[1] - burn, probe.get_tree_hash(), salt=0xB0)
    bundle, new_state = kit.spend_action(pool, "forge_action_remove", [h, burn, lp_parent, shares], extra_spends=lp_spends)
    return bundle, new_state, shares


def scenario(name, assets, reserves, weights, dao=False, hidden=None, attested=True):
    print(f"\n{name}:")
    try:
        _scenario(assets, reserves, weights, dao, hidden, attested)
    except Exception as exc:  # a harness fault is a FAIL, never a silent skip
        check(f"{name}: harness ran to the end", False, f"{type(exc).__name__}: {str(exc)[:160]}")


def _scenario(assets, reserves, weights, dao, hidden, attested=True):
    n = len(assets)
    low = build(assets, reserves, weights, FLOOR - 1, dao, hidden, attested)
    v, why = register(low, dao)
    check("genesis supply one under the floor is refused", v == "refused", why if v == "refused" else "ACCEPTED")
    pool = build(assets, reserves, weights, FLOOR, dao, hidden, attested)
    v, why = register(pool, dao)
    check("genesis supply exactly at the floor registers", v == "accepted", why if v != "accepted" else "")
    if dao:
        reg, slots = fresh_registry()
        try:
            nodao = attempt(lambda: kit.validate(rt.registration(
                reg, pool, (kit.MIN_KEY, kit.ZERO_32, kit.MIN_KEY), (kit.MAX_KEY, kit.ZERO_32, kit.MAX_KEY), slots,
                more_spends=issuer_spends(reg, pool))[0]))
        except Exception as exc:
            nodao = ("refused", f"driver {type(exc).__name__}")
        check("  the same DAO pool without consent is refused", nodao[0] == "refused")
    if v != "accepted":
        return
    deposits = [max(r // 10, 1) for r in reserves]
    bundle, new_state, mint = add_bundle(pool, deposits)
    v, res = attempt(lambda: kit.validate(bundle))
    check("first add accepted", v == "accepted", res if v != "accepted" else "")
    if v != "accepted":
        return
    conds, adds = res
    st = kit.state_to_list(new_state)
    mirror = forge_math.invariant_lp_mint(pool.state[0], deposits, pool.state[1], pool.fee_bps, pool.weights, version=19, **kit.add_fee_rates(pool))
    check("first add mints the mirror's LP", mint == mirror and st[1] == FLOOR + mirror and mint > 0, f"mint {mint}")
    check("  reserves grew by exactly the deposits", st[0] == [r + d for r, d in zip(pool.state[0], deposits)])
    lp_ph = cat_ph(pool.lp_asset_id, RECIPIENT)
    check("  LP lands at the recipient as a plain CAT", any(ph == lp_ph and a == mint for ph, a in adds))
    lk = leaks(pool, conds)
    check("  leak oracle: every reserve exactly -deposit, never positive",
          lk == {i: -d for i, d in enumerate(deposits)}, f"leak {lk}")
    after = pool.advance(st)
    burn = after.state[1] // 10
    bundle, new_state, shares = remove_bundle(after, burn)
    v, res = attempt(lambda: kit.validate(bundle))
    check("remove accepted", v == "accepted", res if v != "accepted" else "")
    if v != "accepted":
        return
    conds, adds = res
    st2 = kit.state_to_list(new_state)
    check("  total LP fell by exactly the burn", st2[1] == after.state[1] - burn)
    check("  every reserve fell by exactly its share", st2[0] == [a - s for a, s in zip(after.state[0], shares)])
    if n > 1:
        exact = [r * burn // after.state[1] for r in after.state[0]]
        check("  shares are pro rata (floor of reserve x burn / total)", shares == exact, f"{shares} vs {exact}")
    else:
        vf = forge_math.vault_fee_bps(1, 10, after.fee_bps)
        check("  the vault pays pro rata less its crossing fee",
              vf > 0 and 0 < shares[0] < after.state[0][0] * burn // after.state[1])
    check("  each asset's payout coin exists at its (layered if rCAT) settlement hash",
          all(s == 0 or (payout_ph(after, i), s) in adds for i, s in enumerate(shares)))
    lk = leaks(after, conds)
    check("  leak oracle: 0 on every reserve", len(lk) == n and all(x == 0 for x in lk.values()), f"leak {lk}")
    if any(h != kit.ZERO_32 for h in after.hiddens):
        plain = {cat_ph(after.asset_ids[i], OFFER_MOD_HASH) for i, h in enumerate(after.hiddens) if h != kit.ZERO_32}
        check("  rCAT payout is layered: no plain coin of the rCAT asset is created",
              not any(ph in plain for ph, _ in adds))


def main() -> int:
    if not (kit.v19_available() and kit.registry_available()):
        print("  [skip] V19 build outputs are absent")
        return 2
    X, c, Z = None, CATS, kit.ZERO_32
    scenario("1 asset (vault)", [c[0]], [20_000_000], [1])
    scenario("2 assets XCH/CAT", [X, c[0]], [10_000_000, 20_000_000], [1, 1])
    scenario("2 assets CAT/CAT", [c[0], c[1]], [10_000_000, 20_000_000], [1, 1])
    scenario("3 assets XCH/CAT/CAT", [X, c[0], c[1]], [10_000_000, 20_000_000, 30_000_000], [1, 1, 1])
    scenario("4 assets XCH+3 CAT", [X, c[0], c[1], c[2]], [10_000_000, 20_000_000, 30_000_000, 40_000_000], [1, 1, 1, 1])
    scenario("unequal weights 2/3 (lowest terms)", [X, c[0]], [10_000_000, 20_000_000], [2, 3])
    scenario("unequal weights 1/4 CAT/CAT", [c[0], c[1]], [10_000_000, 20_000_000], [1, 4])
    scenario("unequal weights 1/2/5 (3 assets)", [X, c[0], c[1]], [10_000_000, 20_000_000, 30_000_000], [1, 2, 5])
    scenario("2 assets with a DAO (consent)", [X, c[0]], [10_000_000, 20_000_000], [1, 1], dao=True)
    scenario("3 assets, unequal weights, with a DAO", [X, c[0], c[1]], [10_000_000, 20_000_000, 30_000_000], [1, 2, 3], dao=True)
    scenario("rCAT: XCH/rCAT", [X, R], [10_000_000, 20_000_000], [1, 1], hidden=[Z, H])
    scenario("rCAT: XCH/CAT/rCAT", [X, c[0], R], [10_000_000, 20_000_000, 30_000_000], [1, 1, 1], hidden=[Z, Z, H])
    scenario("rCAT: CAT/rCAT, weights 1/3, with a DAO", [c[0], R], [10_000_000, 20_000_000], [1, 3], dao=True, hidden=[Z, H])
    # Attestation optional (owner decision 2026-11-12): no proof, no issuer spend -- registers.
    scenario("rCAT unattested: XCH/rCAT (EWS R, no proof)", [X, R], [10_000_000, 20_000_000], [1, 1], hidden=[Z, H], attested=False)
    scenario("rCAT unattested: XCH/CAT/rCAT (non-EWS CAT under H)", [X, c[0], c[1]], [10_000_000, 20_000_000, 30_000_000],
             [1, 1, 1], hidden=[Z, Z, H], attested=False)
    scenario("rCAT unattested: CAT/rCAT, weights 1/3, with a DAO", [c[0], R], [10_000_000, 20_000_000], [1, 3], dao=True,
             hidden=[Z, H], attested=False)

    print("\nrefusals the shapes bring:")
    for label, assets, reserves, weights in (
            ("weights not in lowest terms [2,4]", [X, c[0]], [10_000_000, 20_000_000], [2, 4]),
            ("a vault weight other than 1 ([2])", [c[0]], [20_000_000], [2])):
        try:
            v, why = register(build(assets, reserves, weights, FLOOR, False, None), False)
        except Exception as exc:
            v, why = "harness", f"{type(exc).__name__}: {str(exc)[:80]}"
        check(f"{label} is refused at registration", v == "refused", why if v == "refused" else "got " + v)

    print(f"\n{sum(results)}/{len(results)} creation-matrix checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
