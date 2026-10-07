#!/usr/bin/env python3
"""The registry record a router keeps (contracts/forge_registry_record.py).

Before the mainnet launch (owner, 2026-10-05: "we are working towards getting a registry
and launcher for mainnet"). A router creates pools from the record the deploy script
writes; on mainnet it arrives by upload, so the router checks it first. Offline here
(the chain checks are exercised by --live against testnet11's own record):

  - the creation lane rebuilds the registry with its protocol (dev fee) recipient. It
    used to drop it and fall back to the treasury, which is only the right registry while
    the two are one address -- launched with separate addresses, every site creation
    would have rebuilt a registry that does not exist;
  - a consistent record passes; one whose treasury, protocol recipient, fee, state or
    launcher parent was changed is refused, as is a record for another network or one
    missing a field.
Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import forge_registry_record as rr  # noqa: E402
import forge_v14_driver as drv  # noqa: E402
from forge_stdin import _lane_registry  # noqa: E402

TREASURY = bytes(b"\x55" * 32)
DEV = bytes(b"\xbb" * 32)
results = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))


def record_for(protocol_ph: bytes, network: str = "mainnet") -> dict:
    reg0 = drv.make_registry(salt=0x21, creation_fee=1_000_000_000_000, treasury_ph=drv.bytes32(TREASURY),
                             protocol_ph=drv.bytes32(protocol_ph))
    _bundle, new_state = drv.registry_spend(reg0, "forge_registry_init", [])
    after = reg0.advance([x.as_int() for x in new_state.as_iter()])
    coin = lambda c: {"parent_coin_info": c.parent_coin_info.hex(), "puzzle_hash": c.puzzle_hash.hex(), "amount": int(c.amount)}
    return {"network": network, "pools": [], "log": [], "registry": {
        "launcher_parent": reg0.launcher_parent.hex(), "launcher_id": reg0.launcher_id.hex(),
        "creation_fee": 1_000_000_000_000, "treasury_ph": TREASURY.hex(), "protocol_ph": protocol_ph.hex(),
        "state": list(after.state), "price_scale": drv.PRICE_SCALE, "oracle_window": drv.ORACLE_WINDOW,
        "coin": coin(after.coin), "lineage": {"parent_name": after.lineage.parent_name.hex(),
                                             "inner_puzzle_hash": after.lineage.inner_puzzle_hash.hex() if after.lineage.inner_puzzle_hash else None,
                                             "amount": int(after.lineage.amount)},
        "slots": {"00": {}},
    }}


def verdict(record, network="mainnet"):
    try:
        return "ok", rr.verify(record, network, None, "xch")
    except rr.Refused as exc:
        return exc.code, str(exc)


def main() -> int:
    if not drv.registry_available():
        print("  [skip] V14 build outputs are absent"); return 2

    print("the creation lane rebuilds the registry it was launched as")
    good = record_for(DEV)
    rebuilt = _lane_registry(good["registry"], drv)
    check("  with a dev fee address apart from the treasury, it rebuilds THAT registry",
          rebuilt.protocol_ph == drv.bytes32(DEV) and rebuilt.inner_hash.hex() != "", rebuilt.protocol_ph.hex()[:8])
    fresh = drv.make_registry(creation_fee=1_000_000_000_000, treasury_ph=drv.bytes32(TREASURY), launcher_parent=rebuilt.launcher_parent,
                              state=good["registry"]["state"], protocol_ph=drv.bytes32(DEV))
    check("  and its puzzle is the recorded coin's", fresh.coin.puzzle_hash.hex() == good["registry"]["coin"]["puzzle_hash"])

    print("a record the router may keep")
    code, summary = verdict(good)
    check("  a consistent mainnet record passes", code == "ok", str(summary)[:80])
    if code == "ok":
        check("  and reports its treasury and dev fee addresses as xch addresses",
              summary["treasury"].startswith("xch1") and summary["protocol"].startswith("xch1") and summary["treasury"] != summary["protocol"])

    print("records it must refuse")
    for label, mutate, want in [
        ("another network's", lambda r: r.update(network="testnet11"), "WRONG_NETWORK"),
        ("one with the dev fee address changed", lambda r: r["registry"].update(protocol_ph="cc" * 32), "INCONSISTENT"),
        ("one with the treasury changed", lambda r: r["registry"].update(treasury_ph="cc" * 32), "INCONSISTENT"),
        ("one with the creation fee changed", lambda r: r["registry"].update(creation_fee=1), "INCONSISTENT"),
        ("one with its state changed", lambda r: r["registry"].update(state=[9, 9]), "INCONSISTENT"),
        ("one whose launcher parent does not make its launcher", lambda r: r["registry"].update(launcher_parent="dd" * 32), "INCONSISTENT"),
        ("one with no dev fee address at all", lambda r: r["registry"].pop("protocol_ph"), "INCOMPLETE"),
        ("one with no registry", lambda r: r.pop("registry"), "NO_REGISTRY"),
    ]:
        bad = copy.deepcopy(good)
        mutate(bad)
        code, detail = verdict(bad)
        check(f"  {label}", code == want, f"{code}: {detail[:70]}")

    print("merged, never swapped blind")
    A, B = "aa" * 32, "bb" * 32
    rec = lambda reg, pools=(), retired=(): {"network": "mainnet", "registry": {"launcher_id": reg}, "pools": list(pools),
                                             "log": [], **({"retired_registries": [{"launcher_id": r} for r in retired]} if retired else {})}
    pool = lambda i, note: {"launcher_id": i * 32, "note": note}
    first = rr.merge(None, rec(A, [pool("11", "x")]))
    check("  the first record on an empty router is kept as it came", first["registry"]["launcher_id"] == A
          and len(first["pools"]) == 1 and first["log"][-1]["step"] == "uploaded")
    try:
        rr.merge(rec(A), rec(B))
        check("  a different registry that does not retire the held one is refused", False)
    except rr.Refused as exc:
        check("  a different registry that does not retire the held one is refused", exc.code == "REPLACES_REGISTRY")
    rolled = rr.merge(rec(A, [pool("11", "x")]), rec(B, [pool("11", "x")], [A]))
    check("  a rollover (the upload retires the held registry) replaces it",
          rolled["registry"]["launcher_id"] == B and [r["launcher_id"] for r in rolled["retired_registries"]] == [A])
    kept = rr.merge(rec(A, [pool("11", "router"), pool("22", "router")]), rec(A, [pool("11", "upload"), pool("33", "upload")]))
    check("  the router's own pool records are kept, and win over the upload's",
          {p["launcher_id"][:2]: p["note"] for p in kept["pools"]} == {"11": "router", "22": "router", "33": "upload"})
    acc = rr.merge(rec(B, [], [A]), rec("cc" * 32, [], [B]))
    check("  retired registries accumulate", sorted(r["launcher_id"] for r in acc["retired_registries"]) == sorted([A, B]))

    print("kept exactly: integers past 2**53 survive the upload")
    import tempfile
    with tempfile.TemporaryDirectory() as scratch:
        held = Path(scratch) / "v14-mainnet.json"
        text = json.dumps(good, indent=1)
        out = rr.upload(text, held, "mainnet", None, "xch")
        written = json.loads(held.read_text(encoding="utf-8"))
        check("  price_scale is still exactly 2**64 on the router's volume", written["registry"]["price_scale"] == 2 ** 64,
              str(written["registry"]["price_scale"]))
        check("  and the kept record verifies again", rr.verify(written, "mainnet", None, "xch")["tip"] == out["summary"]["tip"])
        rr.upload(text, held, "mainnet", None, "xch")
        check("  a second upload copies the record it replaces aside", any(p.name.startswith("v14-mainnet.json.before-upload-")
                                                                          for p in Path(scratch).iterdir()))
        rounded = text.replace(str(2 ** 64), "18446744073709552000")     # what a JavaScript JSON round trip makes
        try:
            rr.upload(rounded, held, "mainnet", None, "xch")
            check("  a record rounded by JavaScript is refused, not kept", False)
        except rr.Refused as exc:
            check("  a record rounded by JavaScript is refused, not kept", exc.code == "INCONSISTENT", str(exc)[:60])

    print("one network's registry is never used on the other")
    code, detail = verdict(record_for(DEV, network="testnet11"), network="mainnet")
    check("  a mainnet router refuses testnet's record", code == "WRONG_NETWORK", detail[:60])
    code, detail = verdict(good, network="testnet11")
    check("  a testnet router refuses mainnet's record", code == "WRONG_NETWORK", detail[:60])
    import importlib.util
    spec = importlib.util.spec_from_file_location("deploy_v14", HERE.parent / "scripts" / "deploy-v14-testnet.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    with tempfile.TemporaryDirectory() as scratch:
        deploy.STATE_FILE = Path(scratch) / f"v14-{'mainnet' if deploy.NETWORK == 'mainnet' else 'testnet'}.json"
        other = "testnet11" if deploy.NETWORK == "mainnet" else "mainnet"
        deploy.STATE_FILE.write_text(json.dumps({"network": other, "registry": None, "pools": [], "log": []}), encoding="utf-8")
        try:
            deploy.load()
            check(f"  the deploy script on {deploy.NETWORK} refuses a record that says it is {other}'s", False)
        except SystemExit as exc:
            check(f"  the deploy script on {deploy.NETWORK} refuses a record that says it is {other}'s", "refusing" in str(exc), str(exc)[:60])
        deploy.STATE_FILE.write_text(json.dumps({"network": deploy.NETWORK, "registry": None, "pools": [], "log": []}), encoding="utf-8")
        check("  and loads its own", deploy.load()["network"] == deploy.NETWORK)
        prefix_other = "xch1" if deploy.NETWORK != "mainnet" else "txch1"
        try:
            deploy._recipient(prefix_other + "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq", "treasury", None)
            check("  a registry recipient on the other network's address is refused", False)
        except SystemExit as exc:
            check("  a registry recipient on the other network's address is refused", "not a" in str(exc), str(exc)[:60])

    if "--live" in sys.argv:
        print("testnet11's own record, against the testnet11 chain")
        os.environ["FORGE_NETWORK"] = "testnet11"
        path = HERE.parent / ".awizard" / "v14-testnet.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        try:
            summary = rr.verify(record, "testnet11", "https://testnet11.api.coinset.org", "txch")
            check("  passes at its current tip", True, f"{summary['launcher_id'][:12]} {summary['pools']} pools")
        except rr.Refused as exc:
            check("  is either current, or refused as stale (never as malformed)", exc.code == "STALE", f"{exc.code}: {exc}")

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} registry record checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
