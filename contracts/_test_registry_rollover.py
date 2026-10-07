#!/usr/bin/env python3
"""Rolling a registry over to change the dev fee address for FUTURE pairs.

Owner, 2026-10-05: the dev (protocol) fee address "should be able to change for future
launches but can't be updated for a pair already launched", by a new registry per dev
address. No puzzle changes: the reviewed registry pins one protocol recipient forever
(forge_registry_common.rue: config.protocol_puzzle_hash == c.protocol_puzzle_hash).

Proved here, through consensus where it is the chain that decides:
  - a successor registry with dev address B admits a pool carrying B, and refuses one
    carrying A, the outgoing address -- so the change really takes effect;
  - a pool keeps the DAO recipient and fee its creator chose at mint;
  - the record keeps the retired registry, stamps each pool with the registry that
    admitted it, and the index reports each pool under its own registry.
Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

from chia_rs import G2Element  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

import forge_v15_create as create  # noqa: E402
import forge_v15_driver as drv  # noqa: E402
import forge_v15_index as index  # noqa: E402
from _test_v15_create import CREATOR_PH, T_A, creator_cat, creator_xch, paid_to  # noqa: E402
from forge_offer import ZERO_32  # noqa: E402

DEV_A = bytes32(b"\xaa" * 32)
DEV_B = bytes32(b"\xbb" * 32)
TREASURY = bytes32(b"\x55" * 32)
DAO = bytes32(b"\xda" * 32)
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))


def registry(protocol_ph: bytes32, salt: int):
    reg0 = drv.make_registry(salt=salt, creation_fee=1_000_000, treasury_ph=TREASURY, protocol_ph=protocol_ph)
    _bundle, new_state = drv.registry_spend(reg0, "forge_registry_init", [])
    reg1 = reg0.advance([x.as_int() for x in new_state.as_iter()])
    parent = {"parent_coin_info": reg0.coin.parent_coin_info.hex(), "puzzle_hash": reg0.coin.puzzle_hash.hex(), "amount": 1}
    sentinel = lambda: {"launcher_id": ZERO_32.hex(), "left": drv.MIN_KEY.hex(), "right": drv.MAX_KEY.hex(),
                        "parent": parent, "parent_inner_hash": reg0.inner_hash.hex()}
    slots = {drv.MIN_KEY.hex(): {"key": drv.MIN_KEY.hex(), **sentinel()}, drv.MAX_KEY.hex(): {"key": drv.MAX_KEY.hex(), **sentinel()}}
    return reg1, slots


def plan(reg, slots, protocol_ph: bytes32, salt: int, dao_ph=ZERO_32, dao_fee_bps=0):
    cfg = create.CreationConfig([None, T_A], [100_000_000, 50_000], [1, 1], 30, 5, protocol_ph, 50_000, "TXCH 🍕", "TXCH/A",
                                dao_ph=dao_ph, dao_fee_bps=dao_fee_bps)
    fee = 5_000_000
    return create.plan(reg, slots, cfg, creator_xch(2 + 100_000_000 + 1_000_000 + 49_999 + fee, salt),
                       {T_A: creator_cat(T_A, 50_000, salt + 1)}, CREATOR_PH, fee)


def load_deploy_script():
    spec = importlib.util.spec_from_file_location("deploy_v14", HERE.parent / "scripts" / "deploy-v14-testnet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if not (drv.v15_available() and drv.registry_available()):
        print("  [skip] V14 build outputs are absent"); return 2

    print("the outgoing registry, dev address A")
    reg_a, slots_a = registry(DEV_A, 0x21)
    p_a = plan(reg_a, slots_a, DEV_A, 0x41)
    drv.validate(create.finalize(p_a, G2Element()))
    check("  admits a pool carrying A", p_a.pool.protocol_ph == DEV_A)

    print("its successor, dev address B")
    reg_b, slots_b = registry(DEV_B, 0x22)
    check("  is a different registry", reg_b.launcher_id != reg_a.launcher_id)
    p_b = plan(reg_b, slots_b, DEV_B, 0x43)
    drv.validate(create.finalize(p_b, G2Element()))
    check("  admits a pool carrying B", p_b.pool.protocol_ph == DEV_B)
    try:
        drv.validate(create.finalize(plan(reg_b, slots_b, DEV_A, 0x45), G2Element()))
        check("  refuses a pool still carrying A, the outgoing address", False)
    except ValueError as exc:   # the registry puzzle raises while its spend runs
        check("  refuses a pool still carrying A, the outgoing address", "clvm raise" in str(exc), str(exc)[:60])
    same_coins = plan(reg_b, slots_b, DEV_B, 0x45)
    drv.validate(create.finalize(same_coins, G2Element()))
    check("  and admits the identical plan, same coins, carrying B: the address is the only difference", True)
    check("  the pool under A keeps A, curried in: a rollover never touches it", p_a.pool.protocol_ph == DEV_A)

    print("the DAO recipient is the creator's, set at mint")
    p_dao = plan(reg_b, slots_b, DEV_B, 0x47, dao_ph=DAO, dao_fee_bps=25)
    bundle = create.finalize(p_dao, G2Element())
    drv.validate(bundle)
    check("  a pool with a 25 bps DAO fee to the creator's DAO address registers", p_dao.pool.dao_ph == DAO and p_dao.pool.dao_fee_bps == 25,
          f"{p_dao.pool.dao_ph.hex()[:8]} {p_dao.pool.dao_fee_bps}")
    check("  and still carries the registry's dev address", p_dao.pool.protocol_ph == DEV_B)

    print("the record")
    record_a = {"launcher_id": reg_a.launcher_id.hex(), "coin": {"parent_coin_info": reg_a.coin.parent_coin_info.hex(),
                "puzzle_hash": reg_a.coin.puzzle_hash.hex(), "amount": 1}, "slots": slots_a, "protocol_ph": DEV_A.hex()}
    patch = create.record_patch(p_a, record_a)
    check("  a created pool's record names the registry that admitted it", patch["pool"]["registry_launcher_id"] == reg_a.launcher_id.hex())

    deploy = load_deploy_script()
    legacy = {"launcher_id": p_a.pool.launcher_id.hex()}          # written before records named their registry
    state = {"registry": {**record_a, "slots": patch["registry"]["slots"]}, "pools": [legacy], "log": []}
    deploy.retire_registry(state, reg_b.launcher_id.hex())
    retired = state.get("retired_registries") or []
    check("  rolling over keeps the outgoing registry", len(retired) == 1 and retired[0]["launcher_id"] == reg_a.launcher_id.hex())
    check("  and says what succeeded it, and when", retired[0].get("succeeded_by") == reg_b.launcher_id.hex() and bool(retired[0].get("retired_at")))
    check("  every existing pool is stamped with the outgoing registry", legacy.get("registry_launcher_id") == reg_a.launcher_id.hex())

    state["registry"] = {"launcher_id": reg_b.launcher_id.hex(), "slots": slots_b}
    unstamped = {"launcher_id": p_a.pool.launcher_id.hex()}
    stamped_b = {"launcher_id": p_b.pool.launcher_id.hex(), "registry_launcher_id": reg_b.launcher_id.hex()}
    stray = {"launcher_id": "ee" * 32}
    check("  the index reports a pool under the registry its record names", index.pool_registry_id(state, stamped_b) == reg_b.launcher_id.hex())
    check("  an unstamped pool is found in the retired registry's slots", index.pool_registry_id(state, unstamped) == reg_a.launcher_id.hex())
    check("  one in no slot list falls back to the active registry", index.pool_registry_id(state, stray) == reg_b.launcher_id.hex())

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} registry rollover checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
