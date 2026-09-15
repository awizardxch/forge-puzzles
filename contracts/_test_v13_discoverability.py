#!/usr/bin/env python3
"""Discoverability, against testnet11: everything V11 creates can be found cold.

This is the lane whose absence cost 49,879 LP in V10. It reads the live
deployment record (.awizard/v13-testnet.json) and asks the node the questions a
wallet, an indexer, or an incentive engine would ask, with no knowledge beyond a
launcher id or a puzzle hash:

  * get_coin_records_by_hint(registry launcher) returns the registry singleton's
    generations and every slot -- the sorted list is enumerable from one hint;
  * get_coin_records_by_hint(pool launcher) returns the pool's singleton and its
    reserve coins, every generation the finalizer re-created;
  * get_coin_records_by_hint(LP recipient) returns every LP coin the pool minted
    to that holder -- genesis and add -- as CAT coins of the pool's LP asset id;
  * the registry spends carry each pool's config in their solutions, so a slot's
    launcher id leads to the pool's assets, weights and fees without a server.

Exit 0 all pass, 1 a failure, 2 nothing exercised (no deployment record or no node).
"""
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia_rs.sized_bytes import bytes32

RECORD = pathlib.Path(__file__).resolve().parent.parent / ".awizard" / "v13-testnet.json"
NODE = "https://testnet11.api.coinset.org"
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def rpc(endpoint, body):
    req = urllib.request.Request(f"{NODE}/{endpoint}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "forge-v11-discover/1.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def by_hint(hint_hex: str):
    return rpc("get_coin_records_by_hint", {"hint": "0x" + hint_hex, "include_spent_coins": True}).get("coin_records", [])


def strip(v):
    return str(v or "").removeprefix("0x")


def _router_ph():
    """The router's fee address as a puzzle hash, or None when none is configured."""
    import pathlib as _pl
    from chia.util.bech32m import decode_puzzle_hash as _decode
    env = _pl.Path("../.env") if _pl.Path("../.env").is_file() else _pl.Path(".env")
    if not env.is_file():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        for key in ("VITE_CFMM_DEV_FEE_PUZZLE_HASH", "VITE_CFMM_DEV_FEE_ADDRESS",
                    "VITE_CFMM_PLATFORM_FEE_PUZZLE_HASH"):
            if line.strip().startswith(key + "="):
                raw = line.split("=", 1)[1].strip()
                if not raw:
                    continue
                return bytes32(_decode(raw)) if raw.startswith(("xch1", "txch1")) else bytes32.fromhex(raw.removeprefix("0x"))
    return None


def main() -> int:
    if not RECORD.is_file():
        print("  [skip] no deployment record; run scripts/deploy-v13-testnet.py first")
        return 2
    state = json.loads(RECORD.read_text(encoding="utf-8"))
    try:
        rpc("get_blockchain_state", {})
    except Exception as exc:
        print(f"  [skip] node unreachable: {exc}")
        return 2

    reg = state["registry"]
    print("registry:")
    recs = by_hint(reg["launcher_id"])
    slots = [r for r in recs if int(r["coin"]["amount"]) == 0]
    gens = [r for r in recs if int(r["coin"]["amount"]) == 1]
    check("one hint on the registry launcher returns its slots", len(slots) >= 2, f"{len(slots)} slots")
    check("...and its singleton generations", len(gens) >= 1, f"{len(gens)} generations")
    live_slots = [r for r in slots if not r["spent"]]
    check("live slots equal the recorded list", len(live_slots) == len(reg["slots"]), f"{len(live_slots)} vs {len(reg['slots'])}")
    check("exactly one unspent registry singleton", sum(1 for r in gens if not r["spent"]) == 1)
    # the recorded live slot puzzle hashes are among the unspent slots the node returns
    import forge_v13_driver as drv
    regobj = drv.make_registry(creation_fee=reg["creation_fee"], treasury_ph=bytes32.fromhex(reg["treasury_ph"]),
                               launcher_parent=bytes32.fromhex(reg["launcher_parent"]), state=reg["state"])
    live_phs = {strip(r["coin"]["puzzle_hash"]) for r in live_slots}
    for key, s in reg["slots"].items():
        value = drv.slot_value(bytes32.fromhex(s["key"]), bytes32.fromhex(s["launcher_id"]), bytes32.fromhex(s["left"]), bytes32.fromhex(s["right"]))
        check(f"  slot {key[:8]}… is unspent on chain at its recorded value", regobj.slot_puzzle(value).get_tree_hash().hex() in live_phs)

    for pool in state["pools"]:
        print(f"pool {pool['label']}:")
        recs = by_hint(pool["launcher_id"])
        # The deployer's rename coins are hinted with the launcher too (that is how a name is found),
        # sit at the deployer's own puzzle hash and may hold 1 mojo: they are not pool coins.
        deployer = pool.get("deployer_ph")
        recs = [r for r in recs if strip(r["coin"]["puzzle_hash"]) != deployer]
        singles = [r for r in recs if int(r["coin"]["amount"]) == 1]
        others = [r for r in recs if int(r["coin"]["amount"]) != 1]
        # The eve singleton is created by the standard launcher, which attaches no memo, so it is
        # found through the launcher (its parent), not by hint; every generation the finalizer
        # re-creates is hinted. A pool that has never spent therefore has zero hinted generations.
        # every recorded step after creation is one singleton spend (offer-*, multihop and resync included)
        # a rename is the deployer's own coin, not a pool spend; it sits in the history for the record
        spends = len([e for e in pool["history"] if e.get("step") and not str(e["step"]).startswith("rename")])
        eve = rpc("get_coin_records_by_parent_ids", {"parent_ids": ["0x" + pool["launcher_id"]], "include_spent_coins": True}).get("coin_records", [])
        check("  the eve singleton is the launcher's child", any(int(r["coin"]["amount"]) == 1 for r in eve))
        # The deploy record and the website's responder are separate stores: a trade settled
        # through the offer lane spends the pool and never touches this record's history, so
        # counting generations against it stopped being true the moment the site settled a
        # trade. Counting is not the property worth checking anyway. What matters is that
        # the hinted generations form an UNBROKEN lineage -- each one spent in the block the
        # next was created in -- because a finalizer that dropped a hint leaves a gap, and a
        # gap is exactly what this check exists to catch. That holds however many spends
        # came from the website rather than from this record.
        chain = sorted(singles, key=lambda r: int(r["confirmed_block_index"]))
        gaps = [(int(a["spent_block_index"]), int(b["confirmed_block_index"]))
                for a, b in zip(chain, chain[1:])
                if int(a["spent_block_index"]) != int(b["confirmed_block_index"])]
        check("  the hinted generations form an unbroken lineage (no hint was dropped)",
              not gaps, f"breaks at {gaps}")
        # The same filter the spend count uses: the creation entry carries a height but no
        # step, and the eve it made is the launcher's child, deliberately unhinted. A rename
        # spends the deployer's own coin, not the pool.
        recorded = {int(e["height"]) for e in pool["history"]
                    if e.get("height") and e.get("step") and not str(e["step"]).startswith("rename")}
        on_chain = {int(r["confirmed_block_index"]) for r in chain}
        missing = sorted(recorded - on_chain)
        check("  every recorded step is one of those generations", not missing,
              f"recorded but not on chain: {missing}")
        check("  ...and there are at least as many generations as recorded spends",
              len(singles) >= spends,
              f"{len(singles)} hinted for {spends} recorded"
              + (f"; {len(singles) - spends} settled through the offer lane" if len(singles) > spends else ""))
        check("  ...and the reserve coins", len(others) >= len(pool["asset_ids"]), f"{len(others)} reserve coins")
        live_reserves = {(strip(r["coin"]["puzzle_hash"]), int(r["coin"]["amount"])) for r in others if not r["spent"]}
        for res in pool["reserves"]:
            check(f"  recorded reserve {res['coin']['puzzle_hash'][:8]}… ({res['coin']['amount']}) is a live hinted coin",
                  (res["coin"]["puzzle_hash"], int(res["coin"]["amount"])) in live_reserves)
        # LP coins: hinted with the recipient's puzzle hash, at the LP CAT puzzle hash for that recipient
        recipient = bytes32.fromhex(pool["lp_recipient_ph"])
        lp_ph = construct_cat_puzzle(CAT_MOD, bytes32.fromhex(pool["lp_asset_id"]), Program.to(recipient)).get_tree_hash_precalc(recipient).hex()
        lp_coins = [r for r in by_hint(pool["lp_recipient_ph"]) if strip(r["coin"]["puzzle_hash"]) == lp_ph]
        check("  the LP recipient's hint returns this pool's LP coins", len(lp_coins) >= 1, f"{len(lp_coins)} coins")
        # Supply reconciles: the operator's unspent LP (found by hint) plus any of this LP held as
        # another registered pool's reserve (found by that pool's record) equals total_lp. A
        # reserve coin holds the tradable reserve plus the protocol fees owed out of it, so both
        # count: a swap of LP out of B2 leaves its protocol fee in the reserve coin as fees owed.
        held = sum(int(r["coin"]["amount"]) for r in lp_coins if not r["spent"])
        in_reserves = sum(other["state"][0][i] + other["state"][2][i] for other in state["pools"]
                          for i, a in enumerate(other["asset_ids"]) if a == pool["lp_asset_id"])
        # A pool created after the genesis burn landed sends MIN_LOCKED_LP to the burn
        # address in the same transaction that mints the supply, so that slice answers to
        # nobody's hint and has to be counted separately or the supply looks short by
        # exactly a thousand mojos. It is still LP and still part of total_lp; it simply
        # sits where no one can spend it, which is the point of sending it there.
        burn_ph = bytes32(bytes(32))
        burn_lp_ph = construct_cat_puzzle(CAT_MOD, bytes32.fromhex(pool["lp_asset_id"]),
                                          Program.to(burn_ph)).get_tree_hash_precalc(burn_ph).hex()
        burned = sum(int(r["coin"]["amount"]) for r in rpc(
            "get_coin_records_by_puzzle_hashes",
            {"puzzle_hashes": ["0x" + burn_lp_ph], "include_spent_coins": False}).get("coin_records", []))
        # The router address is a real LP holder on a few pools, from deposits that
        # settled before the overage on a deposit was refunded to the depositor rather
        # than kept. Those coins exist on chain and cannot be unmade, so they are counted
        # as what they are. Nothing NEW should ever land here: a deposit's overage now
        # goes back to the depositor, so a growing balance is a regression, not a holder.
        router_ph = _router_ph()
        router_held = 0
        if router_ph:
            router_lp_ph = construct_cat_puzzle(CAT_MOD, bytes32.fromhex(pool["lp_asset_id"]),
                                                Program.to(router_ph)).get_tree_hash_precalc(router_ph).hex()
            router_held = sum(int(r["coin"]["amount"]) for r in rpc(
                "get_coin_records_by_puzzle_hashes",
                {"puzzle_hashes": ["0x" + router_lp_ph], "include_spent_coins": False}).get("coin_records", []))
        check("  LP by hint, inside other pools, burned at genesis and at the router, equals total_lp",
              held + in_reserves + burned + router_held == pool["state"][1],
              f"{held} + {in_reserves} + {burned} burned + {router_held} router vs {pool['state'][1]}")
        if burned:
            check("  the genesis burn is unspendable and covers the pool's locked floor",
                  burned >= drv.MIN_LOCKED_LP, f"{burned} at the burn address")

    print("registry spends reveal each pool's config:")
    reg_gens = sorted(gens, key=lambda r: int(r["confirmed_block_index"]))
    configs = 0
    for r in reg_gens:
        if not r["spent"]:
            continue
        cs = rpc("get_puzzle_and_solution", {"coin_id": "0x" + coin_name(r), "height": int(r["spent_block_index"])})["coin_solution"]
        sol = Program.from_bytes(bytes.fromhex(strip(cs["solution"])))
        inner_sol = list(sol.as_iter())[2]
        solutions = list(list(inner_sol.as_iter())[2].as_iter())
        leaf_solution = list(solutions[0].as_iter())
        if len(leaf_solution) >= 4:   # register: launcher_parent, config, reserves, total_lp, ...
            configs += 1
    check("every registration's config is readable from the registry's spends", configs == len(state["pools"]), f"{configs}")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} discoverability checks passed")
    return 0 if passed == len(results) else 1


def coin_name(rec) -> str:
    from chia_rs import Coin
    from chia_rs.sized_ints import uint64
    c = rec["coin"]
    return bytes(Coin(bytes32.fromhex(strip(c["parent_coin_info"])), bytes32.fromhex(strip(c["puzzle_hash"])), uint64(int(c["amount"]))).name()).hex()


if __name__ == "__main__":
    raise SystemExit(main())
