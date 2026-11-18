#!/usr/bin/env python3
"""V19 (protocol 20; external review 2026-11-16, finding 08): an unbalanced add pays the dev and
DAO fees on its swapped share.

Fee names (owner's): the DEV fee is the in-puzzle fee booked in `fees_owed` (config
`protocol_fee_bps`); the DAO fee is the per-pool in-puzzle fee booked in `dao_owed` (the rate in
STATE); the PROTOCOL fee is the router's, off chain, and not the puzzle's business.

V19's `add` takes, per asset i, the (K - k_i)/K share of the excess over the balanced deposit --
the share the LP fee was already charged on -- and books floor(swapped_i * dev_bps / W) in
fees_owed and floor(swapped_i * dao_bps / W) in dao_owed, in the DEPOSITED asset. The curve
reserve is old + deposit - dev - dao; the reserve coin still grows by the full deposit. LP is
minted against old + deposit - lp_fee - dev - dao. So an add followed by a remove of the LP it
minted pays the trader NO MORE than a swap of the same net input. Every fee of the add is
charged on a grossed-up base: the swapped share times W / (W - lp - dev - dao) (a swap of the
route's net input is charged on an input that already carries the add's fees) and times 1 + the
largest deposit ratio (the depositor's own claim on the LP fee it leaves behind, and the curve at
size), rounded in the pool's favor. Measured here on 2-asset (both sides, LP 0 to 200 bps),
weighted 4/1 and 3/2, 3-asset and rCAT pools, from 10^-6 to 10% of the reserve: every edge <= 0
(V18: +10,474,488 mojos on the review's case).

Shapes: 2-asset XCH/CAT, weighted 4/1 (both sides), 3-asset, rCAT, a vault. Exits 0 when every
check passes, 1 otherwise, 2 when the V19 build is absent.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_math  # noqa: E402
import _v19_testkit as kit  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle  # noqa: E402
from chia_rs import Coin  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

results: list[bool] = []
H0 = 6_999_990
CAT, CAT2 = bytes32(b"\xdb" * 32), bytes32(b"\xdc" * 32)
R = bytes32(b"\xdd" * 32)                       # the rCAT asset
H = bytes32(b"\x4d" * 32)                       # its hidden puzzle hash
DAO_PUZZLE = Program.to(1)
DAO_PH = bytes32(DAO_PUZZLE.get_tree_hash())
PROTOCOL_PH = bytes32(b"\x55" * 32)
RECIPIENT = bytes32(b"\x56" * 32)
SEND_MESSAGE, MODE_PUZZLE_TO_COIN = 66, 0b010111
W = 10_000
ZERO = bytes32(b"\x00" * 32)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def refuses(label, thunk):
    try:
        thunk()
    except Exception as exc:  # noqa: BLE001 -- kit.Rejected, a clvm raise, or a construction that cannot exist
        return check(label, True, f"{type(exc).__name__}: {str(exc)[:60]}")
    return check(label, False, "ACCEPTED")


def rates(pool, dao_bps=None):
    r = kit.add_fee_rates(pool)
    if dao_bps is not None:
        r["dao_fee_bps"] = dao_bps
    return r


def v19_mint(pool, deposits, state=None, dao_bps=None):
    st = state if state is not None else pool.state
    return forge_math.invariant_lp_mint(st[0], deposits, st[1], pool.fee_bps, pool.weights, version=19,
                                        **rates(pool, dao_bps))


def settlements(pool, deposits, salt):
    """(parents, amounts, extra spends, extra cats) funding each positive deposit."""
    parents, amounts, spends, cats = [], [], [], {}
    for i, (asset, d) in enumerate(zip(pool.asset_ids, deposits)):
        if d <= 0:
            parents.append(bytes32(b"\x03" * 32)); amounts.append(0)
            continue
        if asset is None:
            coin, spend = kit.offer_settlement_xch(d, salt=salt + i)
            spends.append(spend)
        else:
            h = pool.hiddens[i]
            coin, spend = kit.offer_settlement_cat(asset, d, salt=salt + i, hidden=None if h == ZERO else h)
            cats.setdefault(asset, []).append(spend)
        parents.append(coin.parent_coin_info); amounts.append(d)
    return parents, amounts, spends, cats


def add_bundle(pool, deposits, salt, lp_delta=None, root=None):
    """One add: (bundle, end state, minted, probe state). `root` overrides the state root the LP
    handshake names (the adversarial lane)."""
    minted = v19_mint(pool, deposits) if lp_delta is None else lp_delta
    parents, amounts, spends, cats = settlements(pool, deposits, salt)
    probe, _, _, _ = kit.run_leaf(pool, "forge_action_add", [H0, deposits, minted, bytes32(b"\x01" * 32), parents, amounts])
    root = probe.get_tree_hash() if root is None else root
    mint_parent, mint_spends = kit.lp_mint_spends(pool, minted, pool.state[1] + minted, root, RECIPIENT, salt=salt + 9)
    bundle, end = kit.spend_actions(pool, [("forge_action_add", [H0, deposits, minted, mint_parent, parents, amounts])],
                                    extra_spends=[*spends, *mint_spends], extra_cats=cats)
    return bundle, kit.state_to_list(end), minted, probe


def add_remove(pool, deposits, salt):
    """Add `deposits`, then remove exactly the LP minted, in ONE pool spend.
    Returns (validated output, end state, payouts, state after the add, minted)."""
    minted = v19_mint(pool, deposits)
    parents, amounts, spends, cats = settlements(pool, deposits, salt)
    st1, _, _, eph1 = kit.run_leaf(pool, "forge_action_add", [H0, deposits, minted, bytes32(b"\x01" * 32), parents, amounts])
    s1 = kit.state_to_list(st1)
    vf = forge_math.vault_fee_bps(len(s1[0]), 19, pool.fee_bps)
    payouts = forge_math.withdrawal_amounts(s1[0], minted, s1[1], vf)
    st2, _, _, _ = kit.run_leaf(pool, "forge_action_remove", [H0, minted, bytes32(b"\x01" * 32), payouts],
                                ephemeral=eph1, state=s1)
    mint_parent, mint_spends = kit.lp_mint_spends(pool, minted, pool.state[1] + minted, st1.get_tree_hash(), RECIPIENT, salt=salt + 7)
    melt_parent, melt_spends = kit.lp_melt_spend(pool, minted, pool.state[1], st2.get_tree_hash(), salt=salt + 8)
    steps = [("forge_action_add", [H0, deposits, minted, mint_parent, parents, amounts]),
             ("forge_action_remove", [H0, minted, melt_parent, payouts])]
    bundle, end = kit.spend_actions(pool, steps, extra_spends=[*spends, *mint_spends, *melt_spends], extra_cats=cats)
    out = kit.validate(bundle)
    return out, kit.state_to_list(end), payouts, s1, minted


def swap_net(pool, state, i, j, gross, salt, h=H0):
    """A real swap i -> j of `gross` on `state`; returns (trader payout, new state, dev, dao)."""
    p = pool if state is pool.state else pool.advance(state)
    r, w = p.state[0], p.weights
    honest = forge_math.swap_output(r[i], r[j], gross, p.fee_bps, w[i], w[j])
    dev, dao = honest * p.protocol_fee_bps // W, honest * int(p.state[5]) // W
    if p.asset_ids[i] is None:
        coin, spend = kit.offer_settlement_xch(gross, salt=salt)
        bundle, st = kit.spend_action(p, "forge_action_swap", [h, i, j, gross, honest, *kit.settlement_ref(coin)],
                                      extra_spends=[spend])
    else:
        hid = p.hiddens[i]
        coin, cat = kit.offer_settlement_cat(p.asset_ids[i], gross, salt=salt, hidden=None if hid == ZERO else hid)
        bundle, st = kit.spend_action(p, "forge_action_swap", [h, i, j, gross, honest, *kit.settlement_ref(coin)],
                                      extra_cats={p.asset_ids[i]: [cat]})
    kit.validate(bundle)
    return honest - dev - dao, kit.state_to_list(st), dev, dao


EDGES: list[tuple[str, int]] = []      # (case, route payout - swap payout): every one must be <= 0
SIZES = (10 ** -6, 10 ** -4, 10 ** -3, 10 ** -2, 5 * 10 ** -2, 10 ** -1)   # of the deposited reserve


def v18_edge(pool, deposits, i, j):
    """The same route on V18's arithmetic (the frozen V18 leaf's exact mirror): route - swap."""
    r, w, supply = pool.state[0], pool.weights, pool.state[1]
    m18 = forge_math.invariant_lp_mint(r, deposits, supply, pool.fee_bps, w, version=10)
    vf = forge_math.vault_fee_bps(len(r), 10, pool.fee_bps)
    p18 = forge_math.withdrawal_amounts([a + d for a, d in zip(r, deposits)], m18, supply + m18, vf)
    net18 = deposits[i] - p18[i]
    h18 = forge_math.swap_output(r[i], r[j], net18, pool.fee_bps, w[i], w[j])
    return p18[j] - deposits[j] - (h18 - h18 * pool.protocol_fee_bps // W - h18 * int(pool.state[5]) // W)


def compare_two(label, pool, deposits, salt, quiet=False):
    """2-asset route vs swap: add `deposits` then remove the LP minted, in one validated spend,
    against a validated swap of the route's net input. The route must pay no more."""
    out, end, payouts, s1, minted = add_remove(pool, deposits, salt)
    dev, dao = forge_math.add_output_fees(pool.state[0], deposits, pool.weights, pool.fee_bps, pool.protocol_fee_bps,
                                          int(pool.state[5]))
    net = [d - x for d, x in zip(deposits, payouts)]
    i = 0 if net[0] > 0 else 1
    j = 1 - i
    got = -net[j]                       # what the trader nets of the output asset
    booked = (s1[2] == [o + f for o, f in zip(pool.state[2], dev)] and s1[6] == [o + g for o, g in zip(pool.state[6], dao)]
              and s1[0] == [r + d - f - g for r, d, f, g in zip(pool.state[0], deposits, dev, dao)]
              and end[2] == s1[2] and end[6] == s1[6]
              and kit.reserve_amounts(s1) == [r + f + g for r, f, g in zip(s1[0], s1[2], s1[6])])
    swap_out, _, sdev, sdao = swap_net(pool, pool.state, i, j, net[i], salt + 0x20)
    gap = got - swap_out
    EDGES.append((f"{label} {deposits}", gap))
    if not quiet:
        check(f"{label}: add {deposits} then remove validates as ONE pool spend", out is not None)
        check(f"  the add books dev {dev} and DAO {dao} (the mirror's), out of the curve, owed in the deposited asset", booked)
    check(f"  {'' if quiet else 'the route '}{label} {deposits}: route pays no more than the swap of its net input",
          out is not None and booked and gap <= 0,
          f"route {got:,} vs swap {swap_out:,} ({gap:+,}; V18 {v18_edge(pool, deposits, i, j):+,})")
    return dev, dao, got, swap_out, sdev, sdao, gap


def sweep_two(label, pool, side, salt):
    """Single-sided deposits of `side` from 10^-6 to 10% of its reserve, and one two-sided mix."""
    for k, frac in enumerate(SIZES):
        d = [0, 0]
        d[side] = max(1, int(pool.state[0][side] * frac))
        compare_two(label, pool, d, (salt + 0x08 * k) % 0xD0, quiet=True)
    mix = [0, 0]
    mix[side] = pool.state[0][side] // 20
    mix[1 - side] = pool.state[0][1 - side] // 100
    compare_two(label + " (two-sided, unbalanced)", pool, mix, (salt + 0x37) % 0xD0, quiet=True)


def compare_three(pool, frac, salt, h=H0):
    """3-asset single-sided XCH add + remove vs two swaps of the same net XCH: the first matched
    (bisected) to the route's CAT output, the rest into CAT2; the route's CAT2 must be no more."""
    deposits = [int(pool.state[0][0] * frac), 0, 0]
    out, end, payouts, s1, minted = add_remove(pool, deposits, salt)
    dev, dao = forge_math.add_output_fees(pool.state[0], deposits, pool.weights, pool.fee_bps, pool.protocol_fee_bps,
                                          int(pool.state[5]))
    net_in = deposits[0] - payouts[0]
    r = pool.state[0]
    dao_bps = int(pool.state[5])
    lo, hi = 1, net_in
    while lo < hi:
        mid = (lo + hi) // 2
        honest = forge_math.swap_output(r[0], r[1], mid, pool.fee_bps, pool.weights[0], pool.weights[1])
        if honest - honest * pool.protocol_fee_bps // W - honest * dao_bps // W >= payouts[1]:
            hi = mid
        else:
            lo = mid + 1
    o1, st_a, _, _ = swap_net(pool, pool.state, 0, 1, lo, (salt + 0x10) % 0xD0)
    o2, _, _, _ = swap_net(pool, st_a, 0, 2, net_in - lo, (salt + 0x11) % 0xD0, h=H0 + 1)
    gap = payouts[2] - o2
    EDGES.append((f"3-asset {deposits}", gap))
    check(f"  3-asset add {deposits} + remove: books the mirror's fees, pays no more than two swaps of its net XCH",
          out is not None and s1[2] == dev and s1[6] == dao and o1 >= payouts[1] and gap <= 0,
          f"CAT2 route {payouts[2]:,} vs swaps {o2:,} ({gap:+,}; CAT {payouts[1]:,} vs {o1:,})")


def main() -> int:
    if not kit.v19_available():
        print("  [skip] V19 build outputs are absent; run scripts/build-v19.py")
        return 2

    print("the measured case (2-asset XCH/CAT, 10^12 each, LP 30 / dev 5 / DAO 100 bps):")
    pool = kit.make_pool([None, CAT], [10 ** 12, 10 ** 12], total_lp=10 ** 12, leaves="forge", salt=0x81,
                         protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=100)
    dev, dao, route_out, swap_out, sdev, sdao, gap = compare_two("measured", pool, [2 * 10 ** 9, 0], 0xA0)
    check("  fees_owed booked is [507857, 0] (the grossed-up base)", dev == [507_857, 0], str(dev))
    check("  dao_owed booked is [10157122, 0]", dao == [10_157_122, 0], str(dao))
    g18 = v18_edge(pool, [2 * 10 ** 9, 0], 0, 1)
    check("  V18's edge on this route is the measured 10,474,488 mojos; V19's is at or below zero",
          g18 == 10_474_488 and gap <= 0, f"V18 {g18:+,} -> V19 {gap:+,}")
    print(f"    (V19 route {route_out:,} CAT vs swap {swap_out:,}: {gap:+,}. "
          f"The swap books dev {sdev:,} and DAO {sdao:,} in CAT)")

    print("2-asset sweep, 10^-6 to 10% of the reserve, both sides:")
    sweep_two("2-asset XCH side", pool, 0, 0x10)
    sweep_two("2-asset CAT side", pool, 1, 0x50)
    hi_fee = kit.make_pool([None, CAT], [10 ** 12, 3 * 10 ** 12], total_lp=10 ** 12, fee_bps=200, leaves="forge",
                           salt=0x8A, protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=100)
    sweep_two("2-asset LP 200 / DAO 100", hi_fee, 0, 0x90)
    zero_lp = kit.make_pool([None, CAT], [10 ** 12, 10 ** 12], total_lp=10 ** 12, fee_bps=0, protocol_fee_bps=15,
                            leaves="forge", salt=0x8B, protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=100)
    sweep_two("2-asset LP 0 / dev 15 / DAO 100", zero_lp, 1, 0x20)

    print("weighted 4/1 (XCH weight 4), both sides:")
    w41 = kit.make_pool([None, CAT], [4 * 10 ** 11, 10 ** 11], total_lp=10 ** 11, weights=[4, 1], leaves="forge",
                        salt=0x82, protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=50)
    sweep_two("4/1 heavy XCH", w41, 0, 0x30)
    sweep_two("4/1 light CAT", w41, 1, 0x70)
    w32 = kit.make_pool([None, CAT], [4 * 10 ** 11, 8 * 10 ** 12], total_lp=10 ** 12, weights=[3, 2], fee_bps=0,
                        protocol_fee_bps=0, leaves="forge", salt=0x8C, protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=100)
    sweep_two("3/2 DAO-only (the case the growth factor covers)", w32, 0, 0xA0)

    print("rCAT (XCH / R under its H), both sides:")
    rc = kit.make_pool([None, R], [5 * 10 ** 11, 8 * 10 ** 11], total_lp=10 ** 11, leaves="forge", salt=0x83,
                       protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=30, hidden_puzzle_hashes=[ZERO, H])
    sweep_two("rCAT XCH side", rc, 0, 0x40)
    sweep_two("rCAT R side", rc, 1, 0x80)

    print("3-asset XCH / CAT / CAT2 (single-sided XCH), 10^-6 to 10%:")
    tri = kit.make_pool([None, CAT, CAT2], [10 ** 12, 2 * 10 ** 12, 3 * 10 ** 12], total_lp=10 ** 12, leaves="forge",
                        salt=0x84, protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=100)
    for k, frac in enumerate(SIZES):
        compare_three(tri, frac, (0x18 + 0x0C * k) % 0xD0)

    worst = max(EDGES, key=lambda x: x[1])
    check(f"every route of the {len(EDGES)} measured pays the trader no more than its swap (worst {worst[1]:+,})",
          worst[1] <= 0, worst[0])

    print("a balanced add books nothing (rounding at most a mojo):")
    bal = kit.make_pool([None, CAT], [3 * 10 ** 12 + 7, 10 ** 12 + 3], total_lp=10 ** 12, leaves="forge", salt=0x85,
                        protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=100)
    for k, deposits in enumerate(([3 * 10 ** 9, 10 ** 9], [(3 * 10 ** 12 + 7) // 977, (10 ** 12 + 3) // 977],
                                  [(3 * 10 ** 12 + 7) * 3 // 1000 + 1, (10 ** 12 + 3) * 3 // 1000])):
        b, st, _, _ = add_bundle(bal, deposits, 0x60 + 0x10 * k)
        kit.validate(b)
        # every V19 division rounds toward the pool, so the rounding excess of a proportional add
        # books at most one mojo of each fee
        check(f"  add {deposits} books at most 1 mojo of each fee per asset",
              all(x <= 1 for x in [*st[2], *st[6]]), f"dev {st[2]} dao {st[6]}")
    b, st, _, _ = add_bundle(bal, [3 * 10 ** 12 + 7, 10 ** 12 + 3], 0x90)   # exactly the reserves: ratio 1, excess 0
    kit.validate(b)
    check("  an exactly proportional add books exactly nothing", st[2] == [0, 0] and st[6] == [0, 0], f"{st[2]} {st[6]}")

    print("a vault books nothing beyond its LP fee:")
    vault = kit.make_pool([CAT], [10 ** 12], total_lp=10 ** 12, leaves="forge", salt=0x86,
                          protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=100)
    b, st, vminted, _ = add_bundle(vault, [10 ** 9], 0x98)
    kit.validate(b)
    check("  a vault add validates and books neither fee", st[2] == [0] and st[6] == [0], f"{st[2]} {st[6]}")
    check("  its reserve rises by the whole deposit (the LP fee stays with the holders)", st[0] == [10 ** 12 + 10 ** 9])
    check("  and its mint is the V18 mint (vault fee only)", vminted == forge_math.invariant_lp_mint(
        vault.state[0], [10 ** 9], vault.state[1], vault.fee_bps, vault.weights, version=10))

    print("the DAO rate is the one in STATE:")
    nodao = kit.make_pool([None, CAT], [10 ** 12, 10 ** 12], total_lp=10 ** 12, leaves="forge", salt=0x87,
                          protocol_ph=PROTOCOL_PH)
    b, st, _, _ = add_bundle(nodao, [2 * 10 ** 9, 0], 0xA8)
    kit.validate(b)
    nd, _ = forge_math.add_output_fees(nodao.state[0], [2 * 10 ** 9, 0], nodao.weights, nodao.fee_bps, nodao.protocol_fee_bps, 0)
    check("  dao_fee_bps = 0 books only the dev slice", st[2] == nd and nd[0] > 0 and st[6] == [0, 0], f"{st[2]} {st[6]}")
    # A `dao_fee` lowering to 20 bps, then the add, in ONE spend: the add charges 20, not 100.
    from _test_v19_dao_fee import dao_coin_spend  # noqa: E402
    dpool = kit.make_pool([None, CAT], [10 ** 12, 10 ** 12], total_lp=10 ** 12, leaves="forge", salt=0x88,
                          protocol_ph=PROTOCOL_PH, dao_ph=DAO_PH, dao_fee_bps=100)
    st_d, _, _, eph_d = kit.run_leaf(dpool, "forge_action_dao_fee", [H0, 20])
    sd = kit.state_to_list(st_d)
    deposits = [2 * 10 ** 9, 0]
    minted = v19_mint(dpool, deposits, state=sd, dao_bps=20)
    parents, amounts, spends, cats = settlements(dpool, deposits, 0xB0)
    probe, _, _, _ = kit.run_leaf(dpool, "forge_action_add", [H0, deposits, minted, bytes32(b"\x01" * 32), parents, amounts],
                                  ephemeral=eph_d, state=sd)
    mp, ms = kit.lp_mint_spends(dpool, minted, sd[1] + minted, probe.get_tree_hash(), RECIPIENT, salt=0xB9)
    bundle, end = kit.spend_actions(dpool, [("forge_action_dao_fee", [H0, 20]),
                                            ("forge_action_add", [H0, deposits, minted, mp, parents, amounts])],
                                    extra_spends=[dao_coin_spend(dpool, 20), *spends, *ms])
    try:
        kit.validate(bundle)
        e = kit.state_to_list(end)
        check("  dao_fee 100 -> 20 then add in one spend: the add books DAO at 20 bps",
              (e[2], e[6]) == tuple(forge_math.add_output_fees(sd[0], deposits, dpool.weights, dpool.fee_bps,
                                                               dpool.protocol_fee_bps, 20)) and e[5] == 20,
              f"dev {e[2]} dao {e[6]} rate {e[5]}")
    except Exception as exc:  # noqa: BLE001
        check("  dao_fee 100 -> 20 then add in one spend: the add books DAO at 20 bps", False, str(exc)[:90])
    m100 = v19_mint(dpool, deposits, state=sd, dao_bps=100)
    check("  the mint at the pre-lowering 100 bps differs (so the probe below can tell them apart)", m100 != minted,
          f"{m100:,} vs {minted:,}")
    refuses("  ...and a mint priced at the pre-lowering 100 bps is refused by the add after the lowering",
            lambda: kit.run_leaf(dpool, "forge_action_add", [H0, deposits, m100, bytes32(b"\x01" * 32), parents, amounts],
                                 ephemeral=eph_d, state=sd))

    print("adversarial:")
    v18_mint = forge_math.invariant_lp_mint(pool.state[0], [2 * 10 ** 9, 0], pool.state[1], pool.fee_bps, pool.weights, version=10)
    honest = v19_mint(pool, [2 * 10 ** 9, 0])
    check("  the V18 (fee-free) mint exceeds the V19 mint", v18_mint > honest, f"{v18_mint:,} vs {honest:,}")
    refuses("  lp_delta computed against the V18 effective product is refused",
            lambda: kit.run_leaf(pool, "forge_action_add", [H0, [2 * 10 ** 9, 0], v18_mint, bytes32(b"\x01" * 32),
                                                             [bytes32(b"\x02" * 32)] * 2, [2 * 10 ** 9, 0]]))
    refuses("  ...and so is the V19 mint plus one LP",
            lambda: kit.run_leaf(pool, "forge_action_add", [H0, [2 * 10 ** 9, 0], honest + 1, bytes32(b"\x01" * 32),
                                                             [bytes32(b"\x02" * 32)] * 2, [2 * 10 ** 9, 0]]))
    b_ok, st_ok, _, _ = add_bundle(pool, [2 * 10 ** 9, 0], 0xE0)
    check("  the honest V19 add validates", bool(kit.validate(b_ok)))
    # The successor the V18 leaf would have produced: reserves + deposits, owed ledgers unchanged.
    no_owed = list(st_ok); no_owed[0] = [pool.state[0][0] + 2 * 10 ** 9, pool.state[0][1]]
    no_owed[2] = list(pool.state[2]); no_owed[6] = list(pool.state[6])
    refuses("  an add whose LP handshake names a successor without the owed increase is refused",
            lambda: kit.validate(add_bundle(pool, [2 * 10 ** 9, 0], 0xE8, root=Program.to(no_owed).get_tree_hash())[0]))
    wrong = list(st_ok); wrong[2] = [0, st_ok[2][0]]; wrong[0] = [st_ok[0][0] + st_ok[2][0], st_ok[0][1] - st_ok[2][0]]
    refuses("  an add whose successor books the dev fee on the wrong index is refused",
            lambda: kit.validate(add_bundle(pool, [2 * 10 ** 9, 0], 0xF0, root=Program.to(wrong).get_tree_hash())[0]))

    print("collect after such an add pays both recipients from the deposited asset's reserve:")
    after = pool.advance(st_ok)
    b_c, st_c = kit.spend_action(after, "forge_action_collect", [H0 + 1, [0]])
    try:
        _, adds = kit.validate(b_c)
        c = kit.state_to_list(st_c)
        check(f"  the dev-fee recipient is paid {st_ok[2][0]:,} XCH mojos", (PROTOCOL_PH, st_ok[2][0]) in adds)
        check(f"  the DAO recipient is paid {st_ok[6][0]:,} XCH mojos", (DAO_PH, st_ok[6][0]) in adds)
        check("  both owed ledgers are zeroed and the reserve is the curve reserve alone",
              c[2] == [0, 0] and c[6] == [0, 0] and kit.reserve_amounts(c) == c[0])
    except Exception as exc:  # noqa: BLE001
        check("  collect validates", False, str(exc)[:90])

    print("the off-chain mirror agrees with the puzzle's add_output_fees export:")
    fees_prog = kit.Program.from_bytes(bytes.fromhex(
        (kit.V19_COMPILED / "forge_curve.add_output_fees.rue.hex").read_text().strip()))
    import random
    rng = random.Random(0x08)
    agree = 0
    for _ in range(300):
        n = rng.randint(1, 4)
        old = [rng.randint(1, 10 ** 13) for _ in range(n)]
        w = [rng.randint(1, 4) for _ in range(n)]
        dep = [rng.choice([0, rng.randint(0, 10 ** 11)]) for _ in range(n)]
        if not any(dep):
            dep[0] = 1
        pf, df = rng.choice([0, 5, 15]), rng.choice([0, 30, 100])
        nxt = [o + d for o, d in zip(old, dep)]
        fee = rng.choice([0, 30, 200])
        got = fees_prog.run([old, nxt, w, fee, pf, df])
        want = forge_math.add_output_fees(old, dep, w, fee, pf, df)
        got_l = ([x.as_int() for x in got.first().as_iter()], [x.as_int() for x in got.rest().as_iter()])
        agree += got_l == (list(want[0]), list(want[1]))
    check("  forge_math.add_output_fees == forge_curve.add_output_fees on 300 random adds", agree == 300, f"{agree}/300")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} V19 add-output-fee checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
