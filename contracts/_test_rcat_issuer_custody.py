#!/usr/bin/env python3
"""rCAT issuer custody (contracts/rcat_issuer_custody.py), offline.

  * the guard: an H that is the standard puzzle of an unhardened derivation of the TRADING key is
    refused; a listed hardened one too; the explicit override works on a test network only and is
    refused on mainnet; an H of a separate issuer key passes with no override;
  * derivation from the master public key equals derivation from the secret key (what Sage does);
  * issuer_message_spends: the issuer's own coin creates a 1-mojo coin AT H and that coin sends the
    message; the fee is stated; a funding coin AT H is refused; the bundle a V17 pool's `rebase` leaf
    is given with these spends validates (nothing sits at H before or after);
  * issuer_fee_spend states the fee and returns the change.
Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from chia.types.blockchain_format.program import Program
from chia.wallet.derive_keys import master_sk_to_wallet_sk_unhardened
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_for_pk
from chia_rs import AugSchemeMPL, Coin
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

import rcat_issuer_custody as custody

results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def refused(thunk, needle=""):
    try:
        thunk()
        return False, "accepted"
    except custody.IssuerCustodyError as exc:
        return (needle in str(exc)), str(exc)[:110]


def conditions(spend) -> list:
    out = Program.from_bytes(bytes(spend.puzzle_reveal)).run(Program.from_bytes(bytes(spend.solution)))
    return [[c.first().as_int(), *[x for x in c.rest().as_iter()]] for c in out.as_iter()]


def main() -> int:
    trading_sk = AugSchemeMPL.key_gen(b"\x11" * 32)
    issuer_sk = AugSchemeMPL.key_gen(b"\x22" * 32)
    trading_pk, issuer_pk = trading_sk.get_g1(), issuer_sk.get_g1()
    h_trading = bytes32(custody.standard_puzzle(trading_pk, 70).get_tree_hash())       # the legacy shape: H1
    h_issuer = bytes32(custody.standard_puzzle(issuer_pk, 3).get_tree_hash())

    print("derivation from the master public key:")
    check("equals the standard puzzle of the secret-key derivation (what Sage hands out)",
          custody.standard_puzzle(trading_pk, 70).get_tree_hash()
          == puzzle_for_pk(master_sk_to_wallet_sk_unhardened(trading_sk, 70).get_g1()).get_tree_hash())
    check("trading_derivation_of finds derivation 70", custody.trading_derivation_of(h_trading, trading_pk, 200) == 70)
    check("...and nothing for the separate issuer's H", custody.trading_derivation_of(h_issuer, trading_pk, 200) is None)

    print("\nthe guard:")
    ok, why = refused(lambda: custody.check_issuer_h(h_trading, trading_pk, "testnet11", scan=200), "TRADING key's derivation 70")
    check("an H that is a trading-key derivation is refused without the override", ok, why)
    v = custody.check_issuer_h(h_trading, trading_pk, "testnet11", allow_trading_issuer=True, scan=200)
    check("with the override on testnet11 it proceeds, marked overridden", v.ok and v.trading and v.overridden and v.derivation == 70)
    ok, why = refused(lambda: custody.check_issuer_h(h_trading, trading_pk, "mainnet", allow_trading_issuer=True, scan=200),
                      "refused on mainnet")
    check("the override is refused on mainnet", ok, why)
    ok, why = refused(lambda: custody.check_issuer_h(h_trading, trading_pk, "some-other-net", allow_trading_issuer=True, scan=200),
                      "refused on")
    check("...and on any network not known to be a test network", ok, why)
    v = custody.check_issuer_h(h_issuer, trading_pk, "mainnet", scan=200)
    check("a separate issuer key's H passes with no override, even on mainnet", v.ok and not v.trading and not v.overridden)
    ok, why = refused(lambda: custody.check_issuer_h(h_issuer, trading_pk, "testnet11", scan=200, hardened_phs=[h_issuer]),
                      "derivation hardened")
    check("an H among the trading key's listed hardened derivations is refused", ok, why)
    deep = bytes32(custody.standard_puzzle(trading_pk, 150).get_tree_hash())
    ok, why = refused(lambda: custody.check_issuer_h(deep, trading_pk, "testnet11", scan=200), "derivation 150")
    check("a deep derivation (150) inside the scan is refused", ok, why)

    print("\nan issuer action funded from the issuer's own coin:")
    fund_puzzle = custody.standard_puzzle(issuer_pk, 1)
    h_puzzle = custody.standard_puzzle(issuer_pk, 3)
    fund = Coin(bytes32(b"\x31" * 32), bytes32(fund_puzzle.get_tree_hash()), uint64(10_000_000))
    message = bytes32(b"\x6d" * 32)
    receiver = bytes32(b"\x72" * 32)
    spends, eph = custody.issuer_message_spends(fund, fund_puzzle, h_puzzle, [[66, 0b010111, message, receiver]],
                                                 5_000, bytes32(fund.puzzle_hash))
    fc, ec = conditions(spends[0]), conditions(spends[1])
    creates = [(bytes32(c[1].as_atom()), c[2].as_int()) for c in fc if c[0] == 51]
    check("the issuer's coin creates exactly 1 mojo AT H and returns the change to itself",
          (h_issuer, 1) in creates and (bytes32(fund.puzzle_hash), 10_000_000 - 5_001) in creates and len(creates) == 2, f"{creates}")
    check("the fee is stated (RESERVE_FEE fee + the ephemeral mojo)", any(c[0] == 52 and c[1].as_int() == 5_001 for c in fc))
    check("the ephemeral coin is the issuer coin's child at H", eph == Coin(fund.name(), h_issuer, uint64(1))
          and spends[1].coin == eph)
    check("it sends the mode-23 message and creates nothing (no coin left at H)",
          any(c[0] == 66 and c[1].as_int() == 0b010111 and bytes(c[2].as_atom()) == bytes(message) for c in ec)
          and not any(c[0] == 51 for c in ec))
    check("both spends are AGG_SIG_ME by the issuer's keys", any(c[0] == 50 for c in fc) and any(c[0] == 50 for c in ec))
    at_h = Coin(bytes32(b"\x32" * 32), h_issuer, uint64(10_000_000))
    ok, why = refused(lambda: custody.issuer_message_spends(at_h, h_puzzle, h_puzzle, [], 5_000, h_issuer), "NOT at H")
    check("a funding coin AT H is refused (nothing should sit at H)", ok, why)
    ok, why = refused(lambda: custody.issuer_message_spends(Coin(fund.parent_coin_info, fund.puzzle_hash, uint64(10)),
                                                            fund_puzzle, h_puzzle, [], 5_000, fund.puzzle_hash), "holds 10")
    check("a funding coin that cannot cover the fee is refused", ok, why)
    fee = custody.issuer_fee_spend(fund, fund_puzzle, 5_000, bytes32(fund.puzzle_hash))
    fcs = conditions(fee)
    check("issuer_fee_spend states the fee and returns the change",
          any(c[0] == 52 and c[1].as_int() == 5_000 for c in fcs)
          and any(c[0] == 51 and c[2].as_int() == 10_000_000 - 5_000 for c in fcs))

    print("\nV17 rebase with the message funded the new way (validated; signatures are not run here):")
    try:
        import forge_v17_driver as drv
        if not drv.v17_available():
            raise ImportError("V17 build absent")
        plain, r_asset = bytes32(b"\xd0" * 32), bytes32(b"\xd5" * 32)
        pool = drv.make_pool([None, plain, r_asset], [10_000_000, 20_000_000, 30_000_000], total_lp=5_000_000, leaves="forge",
                             salt=0x5A, protocol_ph=bytes32(b"\x55" * 32), hidden_puzzle_hashes=[drv.ZERO_32, drv.ZERO_32, h_issuer],
                             last_height=6_999_980)
        msg = bytes32(Program.to([b"forge-rebase-v17", 2, 29_000_000]).get_tree_hash())
        spends, _ = custody.issuer_message_spends(fund, fund_puzzle, h_puzzle, [[66, 0b010111, msg, pool.coin.name()]],
                                                  5_000, bytes32(fund.puzzle_hash))
        bundle, _st = drv.spend_action(pool, "forge_action_rebase", [6_999_990, 2, 29_000_000], extra_spends=spends)
        conds, adds = drv.validate(bundle)
        check("rebase down 30,000,000 -> 29,000,000 validates with the issuer-funded ephemeral message coin", True,
              f"cost {conds.cost:,}")
        spent = {cs.coin.name() for cs in bundle.coin_spends}
        at_h = [c for c in bundle.additions() if bytes32(c.puzzle_hash) == h_issuer]
        check("the only coin at H is the ephemeral one, and the bundle spends it (nothing is left at H)",
              len(at_h) == 1 and at_h[0].name() in spent and int(at_h[0].amount) == 1, f"{len(at_h)} at H")
        bad_msg = bytes32(Program.to([b"forge-rebase-v17", 2, 28_000_000]).get_tree_hash())
        spends_bad, _ = custody.issuer_message_spends(fund, fund_puzzle, h_puzzle, [[66, 0b010111, bad_msg, pool.coin.name()]],
                                                      5_000, bytes32(fund.puzzle_hash))
        try:
            drv.validate(drv.spend_action(pool, "forge_action_rebase", [6_999_990, 2, 29_000_000], extra_spends=spends_bad)[0])
            check("control: a message for another amount is refused", False, "accepted")
        except Exception as exc:  # noqa: BLE001
            check("control: a message for another amount is refused", True, str(exc)[:80])
    except ImportError as exc:
        print(f"  [skip] {exc}")

    print(f"\n{sum(results)}/{len(results)} issuer-custody checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
