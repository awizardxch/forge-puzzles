#!/usr/bin/env python3
"""contracts/offer_take.py: the Sage app takes a fixed Dexie offer with its own
counter-offer (owner, 2026-10-11: "the app is its own router for settlement").

Two standard wallets sign real CAT offers (the builder the app's counter-offer comes
from, forge_offer_build); the router merges them with Chia's Offer and the bundle must
pass validate_clvm_and_signature with both signatures. A counter-offer that is not the
exact mirror is refused, and so is a bundle checked against the wrong network.
Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import json
import subprocess
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import offer_take  # noqa: E402
from _test_v16_dexie_split import std_offer, wallet  # noqa: E402
from _test_v16_offer_lane import T_A, T_B, paid_to  # noqa: E402

results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def run(payload: dict) -> dict:
    out = subprocess.run([sys.executable, "offer_take.py"], input=json.dumps(payload), capture_output=True, text=True, check=False)
    return json.loads(out.stdout)


def main() -> int:
    m_sk, m_inner, m_ph = wallet(0x41)   # the Dexie maker
    t_sk, t_inner, t_ph = wallet(0x42)   # the Sage app's wallet
    b_offered, a_requested = 2_436, 1_999_193
    maker = std_offer(m_sk, m_inner, m_ph, T_B, b_offered, T_A, a_requested, 0xC1)

    print("plan: what the maker offers and requests")
    planned = run({"action": "plan", "maker_offer": maker.to_bech32()})
    check("the plan reads the maker's offer", planned.get("success") is True, planned.get("error", ""))
    check("  it offers B and requests A, in mojos",
          planned.get("offered") == {T_B.hex(): b_offered} and planned.get("requested") == {T_A.hex(): a_requested},
          json.dumps({k: planned.get(k) for k in ("offered", "requested")}))

    print("assemble: the exact mirror, signed by the app's wallet")
    taker = std_offer(t_sk, t_inner, t_ph, T_A, a_requested, T_B, b_offered, 0xC2)
    out = run({"action": "assemble", "maker_offer": maker.to_bech32(), "taker_offer": taker.to_bech32(), "network": "testnet11"})
    check("the merged bundle passes validate_clvm_and_signature (both signatures)", out.get("success") is True, out.get("error", "")[:160])
    if out.get("success"):
        from chia_rs import SpendBundle
        bundle = SpendBundle.from_json_dict(out["bundle"])
        check("  the maker is paid exactly what it asked", paid_to(bundle, m_ph, T_A) == a_requested, str(paid_to(bundle, m_ph, T_A)))
        check("  the app's wallet gets exactly what the maker offered", paid_to(bundle, t_ph, T_B) == b_offered, str(paid_to(bundle, t_ph, T_B)))
        check("  nothing is left over as a fee or arbitrage", out.get("fee") == 0, str(out.get("fee")))
        check("  the transaction id is the bundle's", out.get("transaction_id") == bundle.name().hex())

    print("refusals")
    greedy = std_offer(t_sk, t_inner, t_ph, T_A, a_requested, T_B, b_offered + 1, 0xC3)
    out = run({"action": "assemble", "maker_offer": maker.to_bech32(), "taker_offer": greedy.to_bech32(), "network": "testnet11"})
    check("a counter-offer asking one mojo more than the maker offers is refused",
          out.get("success") is False and "must offer" in out.get("error", ""), out.get("error", "")[:120])
    short = std_offer(t_sk, t_inner, t_ph, T_A, a_requested - 1, T_B, b_offered, 0xC4)
    out = run({"action": "assemble", "maker_offer": maker.to_bech32(), "taker_offer": short.to_bech32(), "network": "testnet11"})
    check("a counter-offer paying one mojo less than the maker asks is refused", out.get("success") is False, out.get("error", "")[:120])
    out = run({"action": "assemble", "maker_offer": maker.to_bech32(), "taker_offer": taker.to_bech32(), "network": "mainnet"})
    check("signatures made for testnet11 do not validate as mainnet (the network is checked)",
          out.get("success") is False, out.get("error", "")[:120])
    out = run({"action": "plan", "maker_offer": "not an offer"})
    check("something that is not an offer is refused", out.get("success") is False)
    check("the constants differ by network", offer_take.constants_for("mainnet") is not offer_take.constants_for("testnet11"))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} offer-take checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
