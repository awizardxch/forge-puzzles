#!/usr/bin/env python3
"""The pool's maintenance spends for a connected wallet (contracts/forge_pool_admin.py).

Owner, 2026-10-10: "if the dao connects or the dev connects, there should be a way to
trigger the collect on the site and a way to lower the [DAO] fee on the site only for
the dao wallet". Each row pins one rule the site relies on, with a real standard wallet
signing and the signed bundle validated as a mempool would (validate_clvm_and_signature):

  - collect pays both owed fees to the pool's own recipients and zeroes them, from any
    wallet, or with no wallet at all when no network fee is paid;
  - the DAO fee goes down only, and only a coin AT the DAO recipient can move it;
  - the payer's change goes back to it, and the network fee is exactly what was asked.
Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import _v16_testkit as kit  # noqa: E402
import forge_pool_admin as admin  # noqa: E402
import forge_v16_offer as off  # noqa: E402
from chia.consensus.default_constants import DEFAULT_CONSTANTS  # noqa: E402
from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_synthetic_public_key  # noqa: E402
from chia_rs import AugSchemeMPL, Coin, CoinSpend, G2Element, PrivateKey, SpendBundle, validate_clvm_and_signature  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402

GENESIS = bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615")   # testnet11
CAT = bytes32(b"\xda" * 32)
PROTOCOL_PH = bytes32(b"\x55" * 32)
H = 7_000_100
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))


def wallet(seed: int):
    sk = PrivateKey.from_bytes(bytes([seed]) * 32)
    puzzle = puzzle_for_synthetic_public_key(sk.get_g1())
    return sk, puzzle, bytes32(puzzle.get_tree_hash())


def coin_at(puzzle, amount: int, salt: int) -> dict:
    coin = Coin(bytes32(bytes([salt]) * 32), bytes32(puzzle.get_tree_hash()), uint64(amount))
    return {"coin": {"parent_coin_info": coin.parent_coin_info.hex(), "puzzle_hash": coin.puzzle_hash.hex(), "amount": amount},
            "puzzle_reveal": bytes(puzzle).hex()}


def sign(sk, spends_json) -> str:
    sigs = []
    for raw in spends_json:
        cs = CoinSpend.from_json_dict(raw)
        conds = Program.from_bytes(bytes(cs.puzzle_reveal)).run(Program.from_bytes(bytes(cs.solution)))
        for cond in conds.as_iter():
            items = list(cond.as_iter())
            if items[0].as_int() == 50:
                sigs.append(AugSchemeMPL.sign(sk, bytes(items[2].as_atom()) + bytes(cs.coin.name()) + GENESIS))
    return bytes(AugSchemeMPL.aggregate(sigs)).hex()


def mempool_valid(bundle_json) -> tuple[bool, str]:
    extra = {"AGG_SIG_ME_ADDITIONAL_DATA": GENESIS}
    for name, op in (("AGG_SIG_PARENT_ADDITIONAL_DATA", 43), ("AGG_SIG_PUZZLE_ADDITIONAL_DATA", 44),
                     ("AGG_SIG_AMOUNT_ADDITIONAL_DATA", 45), ("AGG_SIG_PUZZLE_AMOUNT_ADDITIONAL_DATA", 46),
                     ("AGG_SIG_PARENT_AMOUNT_ADDITIONAL_DATA", 47), ("AGG_SIG_PARENT_PUZZLE_ADDITIONAL_DATA", 48)):
        extra[name] = hashlib.sha256(GENESIS + bytes([op])).digest()
    constants = DEFAULT_CONSTANTS.replace(GENESIS_CHALLENGE=bytes32(GENESIS), **{k: bytes32(v) for k, v in extra.items()})
    try:
        validate_clvm_and_signature(SpendBundle.from_json_dict(bundle_json), int(constants.MAX_BLOCK_COST_CLVM), constants, 10**8)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:140]


def refused(label, payload, code):
    try:
        admin.build(payload)
        check(label, False, "built")
    except admin.Refused as exc:
        check(label, exc.code == code, f"{exc.code}: {exc}"[:90])


def outputs(bundle_json) -> list[tuple[str, int]]:
    out = []
    for raw in bundle_json["coin_spends"]:
        cs = CoinSpend.from_json_dict(raw)
        for cond in Program.from_bytes(bytes(cs.puzzle_reveal)).run(Program.from_bytes(bytes(cs.solution))).as_iter():
            items = list(cond.as_iter())
            if items[0].as_int() == 51:
                out.append((bytes(items[1].as_atom()).hex(), items[2].as_int()))
    return out


def main() -> int:
    dao_sk, dao_puzzle, dao_ph = wallet(0x41)
    dev_sk, dev_puzzle, _dev_ph = wallet(0x42)
    reserves, fees, dao_owed = [10**12, 9_000_000], [309_292_598, 2_112], [6_185_851_976, 42_256]
    state = kit.forge_state(reserves, 9_000_000, fees, last_height=H - 10, dao_fee_bps=100, dao_owed=dao_owed)
    pool = kit.make_pool([None, CAT], reserves, total_lp=9_000_000, fees=fees, leaves="forge", salt=0x71,
                         protocol_ph=PROTOCOL_PH, dao_ph=dao_ph, state=state)
    snapshot = off.pool_to_snapshot(pool)

    print("collect: both owed fees to the pool's own recipients")
    free = admin.build({"kind": "collect", "pool": snapshot, "height": H})
    ok, why = mempool_valid(free["bundle"])
    check("with no network fee, no wallet is needed and the bundle is whole", not free["payer_spends"] and ok, why)
    succ = free["successor"]["state"]
    check("the successor owes nothing", succ["fees_owed"] == ["0", "0"] and succ["dao_owed"] == ["0", "0"], f"{succ['fees_owed']} {succ['dao_owed']}")
    paid = outputs(free["bundle"])
    check("the dev fee owed is paid to the protocol recipient", any(ph == PROTOCOL_PH.hex() and a == fees[0] for ph, a in paid))
    check("the DAO fee owed is paid to the DAO recipient", any(ph == dao_ph.hex() and a == dao_owed[0] for ph, a in paid))

    fee = 1_500_000_000
    payer = coin_at(dev_puzzle, 4_000_000_000, 0x91)
    prepared = admin.build({"kind": "collect", "pool": snapshot, "height": H, "payer": payer, "network_fee": fee})
    check("any wallet may pay a collect's network fee: one spend to sign", len(prepared["payer_spends"]) == 1 and "bundle" not in prepared)
    signed = admin.build({"kind": "collect", "pool": snapshot, "height": H, "payer": payer, "network_fee": fee,
                          "aggregated_signature": sign(dev_sk, prepared["payer_spends"])})
    ok, why = mempool_valid(signed["bundle"])
    check("signed by that wallet, it passes validate_clvm_and_signature", ok, why)
    check("the payer's change goes back to it, less exactly the fee",
          (payer["coin"]["puzzle_hash"], 4_000_000_000 - fee) in outputs(signed["bundle"]))
    check("prepare and submit build the same transaction", signed["successor_coin_id"] == prepared["successor_coin_id"])
    refused("a pool that owes nothing is refused before a bundle is built", {"kind": "collect", "pool": off.pool_to_snapshot(
        kit.make_pool([None, CAT], reserves, total_lp=9_000_000, leaves="forge", salt=0x72, protocol_ph=PROTOCOL_PH, dao_ph=dao_ph,
                      state=kit.forge_state(reserves, 9_000_000, [0, 0], last_height=H - 10, dao_fee_bps=100, dao_owed=[0, 0]))),
        "height": H}, "NOTHING_OWED")

    print("dao-fee: down only, and only from the DAO recipient")
    dao_payer = coin_at(dao_puzzle, 2_000_000_000, 0x92)
    lowered = admin.build({"kind": "dao-fee", "pool": snapshot, "height": H, "new_bps": 50, "payer": dao_payer, "network_fee": fee})
    signed = admin.build({"kind": "dao-fee", "pool": snapshot, "height": H, "new_bps": 50, "payer": dao_payer, "network_fee": fee,
                          "aggregated_signature": sign(dao_sk, lowered["payer_spends"])})
    ok, why = mempool_valid(signed["bundle"])
    check("the DAO's own coin lowers 100 -> 50 bps, signed, mempool-valid", ok and signed["successor"]["state"]["dao_fee_bps"] == 50, why)
    refused("a coin anywhere else cannot lower it", {"kind": "dao-fee", "pool": snapshot, "height": H, "new_bps": 50,
                                                     "payer": coin_at(dev_puzzle, 2_000_000_000, 0x93), "network_fee": fee}, "NOT_DAO")
    refused("nor can a request with no paying coin", {"kind": "dao-fee", "pool": snapshot, "height": H, "new_bps": 50}, "NOT_DAO")
    refused("the rate never goes up", {"kind": "dao-fee", "pool": snapshot, "height": H, "new_bps": 150, "payer": dao_payer, "network_fee": fee}, "BAD_REQUEST")
    refused("nor stays the same", {"kind": "dao-fee", "pool": snapshot, "height": H, "new_bps": 100, "payer": dao_payer, "network_fee": fee}, "BAD_REQUEST")
    # A bundle built honestly, then signed by a wallet that is NOT the DAO: the pool still asks for the
    # DAO's message, so the forged signature leaves it invalid -- the chain, not the router, decides.
    ok, _ = mempool_valid(admin.build({"kind": "dao-fee", "pool": snapshot, "height": H, "new_bps": 50, "payer": dao_payer,
                                       "network_fee": fee, "aggregated_signature": sign(dev_sk, lowered["payer_spends"])})["bundle"])
    check("the DAO's coin signed by another key is refused by consensus", not ok)
    refused("a payer smaller than the fee is refused", {"kind": "collect", "pool": snapshot, "height": H,
                                                        "payer": coin_at(dev_puzzle, 10, 0x94), "network_fee": fee}, "PAYER_TOO_SMALL")

    print("dao-fee from a lock: the router builds the pool's half, the lock's proposal sends the message")
    # A stand-in for the lock: a puzzle that emits its solution as conditions, so the test
    # can play the lock's half exactly as its signed proposal would (one SEND_MESSAGE).
    lock_puzzle = Program.to(1)
    lock_ph = bytes32(lock_puzzle.get_tree_hash())
    lock_pool = kit.make_pool([None, CAT], reserves, total_lp=9_000_000, fees=fees, leaves="forge", salt=0x73,
                              protocol_ph=PROTOCOL_PH, dao_ph=lock_ph, state=state)
    lock_snapshot = off.pool_to_snapshot(lock_pool)
    half = admin.build({"kind": "dao-fee", "pool": lock_snapshot, "height": H, "new_bps": 25, "receiver_only": True,
                        "sender_puzzle_hash": lock_ph.hex()})
    check("the pool's half is built with no payer and no signature", half["pool_spends"] and "bundle" not in half
          and half["successor"]["state"]["dao_fee_bps"] == 25)
    check("the condition the lock signs is [66, 23, message, pool coin]",
          half["condition"] == [66, 23, "0x" + admin.dao_fee_message(25).hex(), "0x" + lock_pool.coin.name().hex()])
    lock_coin = Coin(bytes32(bytes([0x95]) * 32), lock_ph, uint64(3_000_000_000))
    network_fee = 2_000_000_000
    lock_conditions = [[66, 23, admin.dao_fee_message(25), lock_pool.coin.name()], [51, lock_ph, 3_000_000_000 - network_fee], [52, network_fee]]
    lock_spend = make_spend(lock_coin, lock_puzzle, Program.to(lock_conditions))
    whole = SpendBundle([CoinSpend.from_json_dict(raw) for raw in half["pool_spends"]] + [lock_spend], G2Element())
    ok, why = mempool_valid(whole.to_json_dict())
    check("with the lock's half beside it, the bundle is mempool-valid", ok, why)
    alone = SpendBundle([CoinSpend.from_json_dict(raw) for raw in half["pool_spends"]], G2Element())
    ok, _ = mempool_valid(alone.to_json_dict())
    check("the pool's half alone is refused (the message has no sender)", not ok)
    import forge_v16_driver as drv
    real, _ = drv.validate(whole)
    lock_only = admin.estimate_cost([lock_spend])
    estimate = half["cost"]
    check("the pool half's cost estimate is within 10% of the measured cost",
          abs(estimate - (int(real.cost) - lock_only)) <= 0.1 * (int(real.cost) - lock_only), f"{estimate} vs {int(real.cost) - lock_only}")
    refused("a lock that is not the DAO recipient is refused", {"kind": "dao-fee", "pool": lock_snapshot, "height": H, "new_bps": 25,
                                                               "receiver_only": True, "sender_puzzle_hash": dao_ph.hex()}, "NOT_DAO")
    # A proposal that lost its record of the action: the change is read back from the
    # delegated puzzle the owners signed ((q . conditions)), never from the caller.
    signed_puzzle = bytes(Program.to((1, lock_conditions))).hex()
    other_puzzle = bytes(Program.to((1, [[51, lock_ph, 1]]))).hex()
    found = admin.build({"pool": lock_snapshot, "recover": True, "delegated_puzzles": [other_puzzle, signed_puzzle]})
    check("recovery reads 25 bps for this pool coin back from the signed conditions",
          found["found"] and found["new_bps"] == 25 and found["pool_coin_id"] == lock_pool.coin.name().hex(), str(found))
    elsewhere = admin.build({"pool": snapshot, "recover": True, "delegated_puzzles": [signed_puzzle]})
    check("a message to another pool's coin is no change for this pool", elsewhere["found"] is False)
    refused("a lock cannot raise it either", {"kind": "dao-fee", "pool": lock_snapshot, "height": H, "new_bps": 150, "receiver_only": True}, "BAD_REQUEST")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} pool admin checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
