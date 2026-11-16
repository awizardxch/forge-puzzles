#!/usr/bin/env python3
"""V16 and V17 LP into the V18 mirror in one bundle (forge_v18_route.migrate_v16_to_v18 and
migrate_v17_to_v18), through forge_stdin's `migrate-v16-to-v18` and `migrate-v17-to-v18`
actions, keyless, offline. The V17 cases run every check the V16 ones do (V17 frozen
2026-11-13; V18 = V17 plus one register rule, so its holders move one for one).

Owner, 2026-11-12: "1 for 1 equivalent LP in V18" -- a V18 pool mirroring each V16 one
(forge_v18_create.mirror_config_from_v16) and a route that moves a holder across: N of their
V16 LP burned through the V16 pool's own `remove` (V16 driver, exact pro-rata), the payouts
deposited into the V18 mirror at ITS ratio (V18 driver), the V18 LP the V18 mint gives paid to
the holder with whatever dust the proportional deposit leaves, all in signed exact amounts.

What it proves, per case:
  * the bundle passes consensus validation;
  * every V16 reserve falls by EXACTLY the burned share (per-reserve leak oracle, as in
    _test_v18_fail_closed.py: coin amount minus what the reserve spend creates);
  * every V18 reserve rises by EXACTLY what was deposited (leak = -deposit), and the
    successor snapshots say the same;
  * the holder is paid the V18 LP the V18 mint math gives for those deposits, and the dust;
  * nothing else leaves: no other payee, no fee;
  * a holder cannot take more than their share (lane refusals, and the V16 remove leaf
    itself refusing an inflated payout).
Exit 0 all pass, 1 otherwise, 2 build absent.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs import SpendBundle
from chia_rs.sized_bytes import bytes32

import forge_math
import forge_stdin
import forge_v16_driver as drv16
import forge_v16_offer as v16
try:   # optional: the public repository ships V18 and not the frozen V17; its cases skip there
    import forge_v17_driver as drv17
    import forge_v17_offer as v17
except ImportError:
    drv17 = v17 = None
import forge_v18_create as cre
import forge_v18_driver as drv
import forge_v18_offer as v18
from _test_v18_offer_lane import ROUTER_PH, T_A, T_B, TRADER_PH, H, fabricate_offer, paid_to
from forge_offer import ZERO_32

DAO_PH = bytes32(b"\xda" * 32)
PROTOCOL_PH = bytes32(b"\x55" * 32)
results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def run(payload):
    return forge_stdin.build(json.loads(json.dumps(payload)))


def hx(asset):
    return (ZERO_32 if asset is None else asset).hex()


def pool16(assets, reserves, weights, salt, total_lp=1_000_000, fee_bps=30, dao_ph=None, dao_fee_bps=0):
    return drv16.make_pool(assets, reserves, total_lp=total_lp, weights=weights, fee_bps=fee_bps, protocol_fee_bps=5,
                           protocol_ph=PROTOCOL_PH, salt=salt, leaves="forge", last_height=H - 10,
                           dao_ph=dao_ph, dao_fee_bps=dao_fee_bps)


def pool17(assets, reserves, weights, salt, total_lp=1_000_000, fee_bps=30, dao_ph=None, dao_fee_bps=0):
    return drv.make_pool(assets, reserves, total_lp=total_lp, weights=weights, fee_bps=fee_bps, protocol_fee_bps=5,
                         protocol_ph=PROTOCOL_PH, salt=salt, leaves="forge", last_height=H - 10,
                         dao_ph=dao_ph, dao_fee_bps=dao_fee_bps)


def leaks(pool, conds) -> dict[int, int]:
    """coin_amount - sum(CREATE_COINs) for every reserve coin of `pool` spent (consensus output)."""
    ids = {bytes(r.coin.name()): r.index for r in pool.reserves}
    out: dict[int, int] = {}
    for s in conds.spends:
        cid = bytes(s.coin_id)
        if cid in ids:
            out[ids[cid]] = int(s.coin_amount) - sum(int(cc[1]) for cc in s.create_coin)
    return out


def flows(pool, conds) -> dict[int, tuple[int, int]]:
    """For every reserve coin of `pool` spent: (what it creates OTHER than its successor at its
    own puzzle hash, the successor's amount)."""
    ids = {bytes(r.coin.name()): r for r in pool.reserves}
    out: dict[int, tuple[int, int]] = {}
    for s in conds.spends:
        r = ids.get(bytes(s.coin_id))
        if r is None:
            continue
        succ = sum(int(cc[1]) for cc in s.create_coin if bytes(cc[0]) == bytes(r.full_hash))
        other = sum(int(cc[1]) for cc in s.create_coin if bytes(cc[0]) != bytes(r.full_hash))
        out[r.index] = (other, succ)
    return out


def lane_of(pool):
    """The source pool's own lane and migrate action (V16 or V17)."""
    return (v17, "migrate-v17-to-v18") if drv17 is not None and type(pool).__module__ == drv17.__name__ else (v16, "migrate-v16-to-v18")


def base(src, dst):
    lane, action = lane_of(src)
    return {"action": action, "current_height": H,
            "sourcePool": lane.pool_to_snapshot(src), "pool": v18.pool_to_snapshot(dst)}


def pool17src(assets, reserves, weights, salt, total_lp=1_000_000, fee_bps=30, dao_ph=None, dao_fee_bps=0, hidden=None):
    """A V17 (protocol 18) source pool."""
    return drv17.make_pool(assets, reserves, total_lp=total_lp, weights=weights, fee_bps=fee_bps, protocol_fee_bps=5,
                           protocol_ph=PROTOCOL_PH, salt=salt, leaves="forge", last_height=H - 10,
                           dao_ph=dao_ph, dao_fee_bps=dao_fee_bps, hidden_puzzle_hashes=hidden)


def asks_from(releases: dict, lp17) -> dict:
    """The exact request a preview names: every released asset at TRADER_PH."""
    out = {}
    for key, amount in releases.items():
        if int(amount) <= 0:
            continue
        asset = None if key == ZERO_32.hex() else bytes32.fromhex(key)
        out[asset] = int(amount)
    return out


def migrate(label, src, dst, burn, salt, payload=None):
    payload = payload or base(src, dst)
    probe = fabricate_offer({src.lp_asset_id: burn}, {dst.lp_asset_id: 1}, salt=salt)
    try:
        pv = run({**payload, "offer": probe.to_bech32(), "preview": True})
        asks = asks_from(pv["forge"]["releases"], dst.lp_asset_id)
        offer = fabricate_offer({src.lp_asset_id: burn}, asks, salt=salt)
        out = run({**payload, "offer": offer.to_bech32()})
    except Exception as exc:  # noqa: BLE001
        check(f"{label}: builder accepted", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return None
    check(f"{label}: builder accepted", True)
    bundle = SpendBundle.from_json_dict(out["bundle"])
    try:
        conds, _adds = drv.validate(bundle)
    except Exception as exc:  # noqa: BLE001
        check("  the bundle validates under consensus", False, str(exc)[:160])
        return None
    check("  the bundle validates under consensus", True)

    removed = [int(a) for a in out["removed"]]
    vf = forge_math.vault_fee_bps(len(src.state[0]), 10, src.fee_bps)
    share = [int(p) for p in forge_math.withdrawal_amounts(src.state[0], burn, src.state[1], vf)]
    check("  the V16 remove is the exact pro-rata share", removed == share, f"{removed} vs {share}")
    # A remove's reserve creates its own payout, so its leak is 0 (nothing destroyed); what it
    # RELEASES is everything it creates but its successor, and that must be the share exactly.
    lk16 = leaks(src, conds)
    released = flows(src, conds)
    check("  no V16 reserve destroys value (leak oracle: leak 0 on every reserve)",
          len(lk16) == len(share) and all(v == 0 for v in lk16.values()), f"leak {lk16}")
    check("  every V16 reserve falls by exactly the burned share (released = share, successor = coin - share)",
          released == {i: (share[i], int(src.reserves[i].coin.amount) - share[i]) for i in range(len(share))},
          f"{released} vs {share}")
    deposits = [int(out["deposits"][hx(a)]) for a in dst.asset_ids]
    lk17 = leaks(dst, conds)
    check("  every V18 reserve rises by exactly what was deposited (leak = -deposit)",
          all(lk17.get(i, 0) == -deposits[i] for i in range(len(deposits))), f"leak {lk17} vs {deposits}")
    src_after = next(s for s in out["pools"] if s["launcher_id"] == src.launcher_id.hex())
    dst_after = next(s for s in out["pools"] if s["launcher_id"] == dst.launcher_id.hex())
    check("  V16 successor: reserves less the share, supply less the burn",
          [int(x) for x in src_after["state"]["reserves"]] == [r - p for r, p in zip(src.state[0], share)]
          and int(src_after["state"]["total_lp"]) == src.state[1] - burn)
    minted = int(out["minted"])
    want = forge_math.invariant_lp_mint(dst.state[0], deposits, dst.state[1], dst.fee_bps, dst.weights, version=10)
    check("  V18 successor: reserves plus the deposits, supply plus the mint",
          [int(x) for x in dst_after["state"]["reserves"]] == [r + d for r, d in zip(dst.state[0], deposits)]
          and int(dst_after["state"]["total_lp"]) == dst.state[1] + minted)
    check("  the holder gets the V18 LP the V18 mint math gives", paid_to(bundle, TRADER_PH, dst.lp_asset_id) == minted == want,
          f"{paid_to(bundle, TRADER_PH, dst.lp_asset_id)} / {minted} / {want}")
    xi = dst.asset_ids.index(None)
    check("  the XCH payout is the deposit plus the mint's backing plus the dust",
          share[xi] == deposits[xi] + minted + int(out["returned"][hx(None)]))
    returned = {a: int(out["returned"][hx(a)]) for a in dst.asset_ids}
    check("  the dust of each asset goes back to the holder, exactly",
          all(paid_to(bundle, TRADER_PH, a) == returned[a] for a in dst.asset_ids)
          and all(share[i] == deposits[i] + returned[a] for i, a in enumerate(dst.asset_ids) if a is not None),
          f"{returned}")
    check("  nothing is paid to the router or of the V16 LP",
          paid_to(bundle, ROUTER_PH, None) == 0 and all(paid_to(bundle, ROUTER_PH, a) == 0 for a in dst.asset_ids if a)
          and paid_to(bundle, ROUTER_PH, dst.lp_asset_id) == 0 and paid_to(bundle, TRADER_PH, src.lp_asset_id) == 0)
    check("  both successor snapshots rebuild",
          lane_of(src)[0].snapshot_to_pool(src_after).coin.puzzle_hash.hex() == src_after["pool_coin"]["puzzle_hash"]
          and v18.snapshot_to_pool(dst_after).coin.puzzle_hash.hex() == dst_after["pool_coin"]["puzzle_hash"])
    return out


def refused(label, payload, offered, requested, salt, needle=""):
    offer = fabricate_offer(offered, requested, salt=salt)
    try:
        run({**payload, "offer": offer.to_bech32()})
    except Exception as exc:  # noqa: BLE001
        return check(f"refused: {label}", needle in str(exc), str(exc)[:150])
    return check(f"refused: {label}", False, "ACCEPTED")


def main() -> int:
    if not drv.v18_available():
        print("  [skip] V18 puzzles not built")
        return 2

    print("mirror_config_from_v16:")
    src = pool16([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x61, total_lp=2_000_000, dao_ph=DAO_PH, dao_fee_bps=40)
    reg = drv.make_registry(protocol_fee_bps=7)
    cfg = cre.mirror_config_from_v16(json.loads(json.dumps(v16.pool_to_snapshot(src))), registry=reg)
    check("same assets, weights, LP fee, DAO recipient and rate",
          cfg.asset_ids == src.asset_ids and cfg.weights == src.weights and cfg.fee_bps == src.fee_bps
          and cfg.dao_ph == src.dao_ph and cfg.dao_fee_bps == 40)
    check("every hidden puzzle hash zero", cfg.hidden_puzzle_hashes == [ZERO_32, ZERO_32])
    check("the dev fee is the V18 registry's pinned rate", cfg.protocol_fee_bps == 7 and cfg.protocol_ph == reg.protocol_ph)
    check("genesis supply at or above MIN_GENESIS_LP", cfg.total_lp >= drv.MIN_GENESIS_LP, str(cfg.total_lp))
    check("opens at the V16 LP value (never below it)",
          all(r * src.state[1] >= r0 * cfg.total_lp for r, r0 in zip(cfg.reserves, src.state[0])), f"{cfg.reserves}")
    small = pool16([None, T_A], [3_000_000, 900], [1, 1], salt=0x62, total_lp=1_000)
    cfg2 = cre.mirror_config_from_v16(v16.pool_to_snapshot(small))
    check("a small V16 supply is scaled up to MIN_GENESIS_LP", cfg2.total_lp == drv.MIN_GENESIS_LP
          and cfg2.reserves == [3_000_000_000, 900_000], f"{cfg2.total_lp} {cfg2.reserves}")
    mirror = pool17([None, T_A], cfg.reserves, [1, 1], salt=0x63, total_lp=cfg.total_lp, dao_ph=DAO_PH, dao_fee_bps=40)
    try:
        cre.mirror_config_from_v16(v16.pool_to_snapshot(src), existing_v18=[v18.pool_to_snapshot(mirror)])
        check("refused when the V18 mirror already exists", False, "accepted")
    except Exception as exc:  # noqa: BLE001
        check("refused when the V18 mirror already exists", "already exists" in str(exc), str(exc)[:100])
    other_fee = pool17([None, T_A], cfg.reserves, [1, 1], salt=0x64, total_lp=cfg.total_lp, fee_bps=100, dao_ph=DAO_PH)
    try:
        cre.mirror_config_from_v16(v16.pool_to_snapshot(src), existing_v18=[other_fee])
        check("a V18 pool with another LP fee is not the mirror", True)
    except Exception as exc:  # noqa: BLE001
        check("a V18 pool with another LP fee is not the mirror", False, str(exc)[:100])

    print("\nmigrate XCH/A (DAO fee) into its mirror at the same price:")
    for burn, salt in ((100_000, 0x11), (33_333, 0x12), (1_999_000, 0x13)):
        migrate(f"migrate {burn} V16 LP", src, mirror, burn, salt)

    print("\nmigrate into a mirror whose price moved (A dearer on V18): the A dust goes back")
    moved = pool17([None, T_A], [cfg.reserves[0], cfg.reserves[1] * 9 // 10], [1, 1], salt=0x65,
                   total_lp=cfg.total_lp, dao_ph=DAO_PH, dao_fee_bps=40)
    out = migrate("migrate 250000 V16 LP into a moved mirror", src, moved, 250_000, 0x14)
    if out:
        check("  the dust is real (more than a mojo of A returned)", int(out["returned"][hx(T_A)]) > 1, out["returned"][hx(T_A)])

    print("\nmigrate a weighted three-asset pool (2:1:1):")
    tri16 = pool16([None, T_A, T_B], [20_000_000_000, 400_000, 300_000], [2, 1, 1], salt=0x66, total_lp=5_000_000)
    tcfg = cre.mirror_config_from_v16(v16.pool_to_snapshot(tri16))
    tri17 = pool17([None, T_A, T_B], tcfg.reserves, [2, 1, 1], salt=0x67, total_lp=tcfg.total_lp)
    migrate("migrate 1234567 V16 LP of XCH/A/B", tri16, tri17, 1_234_567, 0x15)

    print("\nthe mirror found among `pools` (no `pool` named):")
    unrelated = pool17([None, T_B], [5_000_000_000, 100_000], [1, 1], salt=0x68)
    payload = {"action": "migrate-v16-to-v18", "current_height": H, "sourcePool": v16.pool_to_snapshot(src),
               "pools": [v18.pool_to_snapshot(unrelated), v18.pool_to_snapshot(mirror)]}
    migrate("migrate 50000 V16 LP, mirror looked up", src, mirror, 50_000, 0x16, payload=payload)

    print("\nrefusals -- a holder cannot take more than their share:")
    pv = run({**base(src, mirror), "offer": fabricate_offer({src.lp_asset_id: 100_000}, {mirror.lp_asset_id: 1}, salt=0x21).to_bech32(),
              "preview": True})
    asks = asks_from(pv["forge"]["releases"], mirror.lp_asset_id)
    more_lp = {**asks, mirror.lp_asset_id: asks[mirror.lp_asset_id] + 1}
    refused("asking one V18 LP mojo more than the mint", base(src, mirror), {src.lp_asset_id: 100_000}, more_lp, 0x22, "quote again")
    more_xch = {**asks, None: asks.get(None, 0) + 1}
    refused("asking one XCH mojo more than the dust", base(src, mirror), {src.lp_asset_id: 100_000}, more_xch, 0x23, "")
    more_a = {**asks, T_A: asks.get(T_A, 0) + 1}
    refused("asking one A mojo more than the dust", base(src, mirror), {src.lp_asset_id: 100_000}, more_a, 0x24, "")
    less_lp = {**asks, mirror.lp_asset_id: asks[mirror.lp_asset_id] - 1}
    refused("asking one V18 LP mojo less (the mojo would be unsigned)", base(src, mirror), {src.lp_asset_id: 100_000}, less_lp, 0x25, "")
    refused("burning more than the V16 supply", base(src, mirror), {src.lp_asset_id: src.state[1] + 1}, {mirror.lp_asset_id: 1}, 0x26,
            "at most the V16 LP supply")
    refused("into a V18 pool that is not the mirror (other LP fee)", base(src, other_fee), {src.lp_asset_id: 100_000},
            {other_fee.lp_asset_id: 1}, 0x27, "mirror_config_from_v16")
    refused("no mirror among `pools`", {**payload, "pools": [v18.pool_to_snapshot(unrelated)]}, {src.lp_asset_id: 100_000},
            {mirror.lp_asset_id: 1}, 0x28, "no V18 pool mirrors")
    src_b = pool16([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x69, total_lp=2_000_000, dao_ph=DAO_PH, dao_fee_bps=40)
    refused("a V16 'mirror' as the target", {**base(src, mirror), "pool": v16.pool_to_snapshot(src_b)},
            {src.lp_asset_id: 100_000}, {src_b.lp_asset_id: 1}, 0x29, "")
    refused("offering XCH beside the V16 LP", base(src, mirror), {src.lp_asset_id: 100_000, None: 1_000}, asks, 0x2A, "LP alone")

    print("\nthe V16 remove leaf itself refuses a payout above the pro-rata share:")
    vf = forge_math.vault_fee_bps(2, 10, src.fee_bps)
    share = [int(p) for p in forge_math.withdrawal_amounts(src.state[0], 100_000, src.state[1], vf)]
    try:
        drv16.run_leaf(src, "forge_action_remove", [H, 100_000, bytes32(b"\x31" * 32), share])
        check("CONTROL: the exact share runs in the same leaf", True)
    except Exception as exc:  # noqa: BLE001
        check("CONTROL: the exact share runs in the same leaf", False, str(exc)[:80])
    for i in range(2):
        greedy = list(share)
        greedy[i] += 1
        try:
            drv16.run_leaf(src, "forge_action_remove", [H, 100_000, bytes32(b"\x31" * 32), greedy])
            check(f"payout[{i}] + 1 is refused by the leaf", False, "ACCEPTED")
        except Exception as exc:  # noqa: BLE001
            check(f"payout[{i}] + 1 is refused by the leaf", True, str(exc)[:80])

    if drv17 is not None:
        v17_cases()
    else:
        print("\n=== V17 -> V18: skipped (no V17 in this tree) ===")
    print(f"\n{sum(results)}/{len(results)} migration checks passed")
    return 0 if all(results) else 1


def v17_cases() -> None:
    print("\n=== V17 -> V18 (migrate-v17-to-v18) ===")
    print("mirror_config_from_v17:")
    src = pool17src([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x71, total_lp=2_000_000, dao_ph=DAO_PH, dao_fee_bps=40)
    check("the V17 source is protocol 18", int(v17.pool_to_snapshot(src)["protocol_version"]) == 18)
    reg = drv.make_registry(protocol_fee_bps=7)
    cfg = cre.mirror_config_from_v17(json.loads(json.dumps(v17.pool_to_snapshot(src))), registry=reg)
    check("same assets, weights, LP fee, DAO recipient and rate",
          cfg.asset_ids == src.asset_ids and cfg.weights == src.weights and cfg.fee_bps == src.fee_bps
          and cfg.dao_ph == src.dao_ph and cfg.dao_fee_bps == 40)
    check("a plain V17 pool's mirror is plain", cfg.hidden_puzzle_hashes == [ZERO_32, ZERO_32])
    check("the dev fee is the V18 registry's pinned rate", cfg.protocol_fee_bps == 7 and cfg.protocol_ph == reg.protocol_ph)
    check("opens at the V17 LP value (never below it)",
          all(r * src.state[1] >= r0 * cfg.total_lp for r, r0 in zip(cfg.reserves, src.state[0])), f"{cfg.reserves}")
    h_r = bytes32(b"\x4e" * 32)
    rsrc = pool17src([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x72, total_lp=2_000_000, hidden=[ZERO_32, h_r])
    rcfg = cre.mirror_config_from_v17(v17.pool_to_snapshot(rsrc))
    check("an rCAT V17 pool's mirror carries the same H per asset", rcfg.hidden_puzzle_hashes == [ZERO_32, h_r])
    for label, config, source, ok, needle in (
            ("check_mirror accepts the rCAT mirror of the rCAT V17 pool", rcfg, rsrc, True, ""),
            ("check_mirror refuses a plain config claiming to mirror an rCAT V17 pool", replace(rcfg, hidden_puzzle_hashes=[ZERO_32, ZERO_32]), rsrc, False, "hidden puzzle hashes"),
            ("check_mirror refuses an rCAT config claiming to mirror a plain V17 pool", replace(cfg, hidden_puzzle_hashes=[ZERO_32, h_r]), src, False, "hidden puzzle hashes"),
            ("check_mirror accepts the plain mirror of the plain V17 pool", cfg, src, True, "")):
        try:
            cre.check_mirror(config, source)
            check(label, ok, "" if ok else "accepted")
        except Exception as exc:  # noqa: BLE001
            check(label, not ok and needle in str(exc), str(exc)[:100])

    mirror = pool17([None, T_A], cfg.reserves, [1, 1], salt=0x73, total_lp=cfg.total_lp, dao_ph=DAO_PH, dao_fee_bps=40)
    print("\nmigrate V17 XCH/A (DAO fee) into its V18 mirror:")
    for burn, salt in ((100_000, 0x31), (1_999_000, 0x32)):
        migrate(f"migrate {burn} V17 LP", src, mirror, burn, salt)
    print("\nmigrate a weighted three-asset V17 pool (2:1:1):")
    tri = pool17src([None, T_A, T_B], [20_000_000_000, 400_000, 300_000], [2, 1, 1], salt=0x74, total_lp=5_000_000)
    tcfg = cre.mirror_config_from_v17(v17.pool_to_snapshot(tri))
    tri18 = pool17([None, T_A, T_B], tcfg.reserves, [2, 1, 1], salt=0x75, total_lp=tcfg.total_lp)
    migrate("migrate 1234567 V17 LP of XCH/A/B", tri, tri18, 1_234_567, 0x33)
    print("\nthe V18 mirror found among `pools`:")
    unrelated = pool17([None, T_B], [5_000_000_000, 100_000], [1, 1], salt=0x76)
    payload = {"action": "migrate-v17-to-v18", "current_height": H, "sourcePool": v17.pool_to_snapshot(src),
               "pools": [v18.pool_to_snapshot(unrelated), v18.pool_to_snapshot(mirror)]}
    migrate("migrate 50000 V17 LP, mirror looked up", src, mirror, 50_000, 0x34, payload=payload)

    print("\nV17 refusals:")
    refused("burning more than the V17 supply", base(src, mirror), {src.lp_asset_id: src.state[1] + 1}, {mirror.lp_asset_id: 1}, 0x41,
            "at most the V17 LP supply")
    refused("no mirror among `pools`", {**payload, "pools": [v18.pool_to_snapshot(unrelated)]}, {src.lp_asset_id: 100_000},
            {mirror.lp_asset_id: 1}, 0x42, "no V18 pool mirrors")
    refused("a V17 source named under the V16 action", {**base(src, mirror), "action": "migrate-v16-to-v18"},
            {src.lp_asset_id: 100_000}, {mirror.lp_asset_id: 1}, 0x43, "V16 (protocol 17)")
    v16src = pool16([None, T_A], [10_000_000_000, 500_000], [1, 1], salt=0x77, total_lp=2_000_000, dao_ph=DAO_PH, dao_fee_bps=40)
    refused("a V16 source named under the V17 action", {**base(v16src, mirror), "action": "migrate-v17-to-v18"},
            {v16src.lp_asset_id: 100_000}, {mirror.lp_asset_id: 1}, 0x44, "V17 (protocol 18)")
    refused("a plain V18 pool is not an rCAT V17 pool's mirror", base(rsrc, mirror), {rsrc.lp_asset_id: 100_000},
            {mirror.lp_asset_id: 1}, 0x45, "")
    twin17 = pool17src([None, T_A], cfg.reserves, [1, 1], salt=0x79, total_lp=cfg.total_lp, dao_ph=DAO_PH, dao_fee_bps=40)
    refused("a V17 'mirror' as the target (the target must be V18)", {**base(src, mirror), "pool": v17.pool_to_snapshot(twin17)},
            {src.lp_asset_id: 100_000}, {twin17.lp_asset_id: 1}, 0x46, "")
    print("\nV17 frozen: its own migrate-v16-to-v17 lane still resolves to the V17 lane (protocol 18)")
    check("migrate-v16-to-v17 still resolves to the V17 lane",
          forge_stdin._lane_version({"action": "migrate-v16-to-v17", "sourcePool": v16.pool_to_snapshot(v16src),
                                     "pool": v17.pool_to_snapshot(src)}) == 18)


if __name__ == "__main__":
    raise SystemExit(main())
