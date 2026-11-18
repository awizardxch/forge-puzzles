#!/usr/bin/env python3
"""Which revision a NEW pool is built on, per network -- pinned, with the V19 lane carried.

V19 (protocol 20; external review 2026-11-16, findings 08 and 09) is carried (forge_stdin._LANES[20])
and is the creation target on both networks (owner, 2026-11-17: start the router and site work),
but only once that network's V19 registry record, v19-<net>.json, is held. Until then each network
falls back down its ladder (CREATE_FALLBACK_BY_NETWORK, the first held winning, the last taken
unconditionally) to exactly what it created before V19:

  testnet  20 with v19-testnet.json held, else 19 (V18, since 2026-11-14: registry 881f66fc);
  mainnet  20 with v19-mainnet.json held, else 19 with v18-mainnet.json held, else 17 (V16).

Carrying the V18 lane (forge_stdin._LANES[19]) must not make V18 the default creation lane by
accident. Before the V18 lanes, a forge_stdin request that named no revision and had no snapshot
took `max(_LANES)`. The default is now the network's, from one table
(forge_network.CREATE_PROTOCOL_BY_NETWORK, with CREATE_FALLBACK_BY_NETWORK), which mirrors
FORGE_CREATE_PROTOCOL_BY_NETWORK / FORGE_CREATE_FALLBACK_BY_NETWORK in api/_forgeVersion.js:

  testnet  19 (V18) since 2026-11-14, when the V18 testnet registry 881f66fc was launched;
  mainnet  19 (V18) once v18-mainnet.json (the mainnet V18 registry's record) is held, and 17
           (V16) until then (owner, 2026-11-14: V18 ships to mainnet beside V16; no V17 there).

Flipping either is deliberate: edit both tables and the expectations below (and the api check
api/__checks__/createProtocolByNetwork.check.mjs). This suite fails on any drift.
Exit 0 all pass, 1 otherwise.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import forge_network  # noqa: E402
import forge_stdin  # noqa: E402

EXPECTED = {"mainnet": 20, "testnet11": 20}
FALLBACK = {"mainnet": [19, 17], "testnet11": [19]}
results: list[bool] = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")
    results.append(bool(ok))
    return bool(ok)


def on(network: str | None):
    """Set FORGE_NETWORK for the next reads (forge_network reads the environment on every call)."""
    if network is None:
        os.environ.pop("FORGE_NETWORK", None)
    else:
        os.environ["FORGE_NETWORK"] = network


def api_tables() -> tuple[dict, dict]:
    text = (Path(__file__).resolve().parent.parent / "api" / "_forgeVersion.js").read_text(encoding="utf-8")
    m = re.search(r"FORGE_CREATE_PROTOCOL_BY_NETWORK\s*=\s*Object\.freeze\(\{\s*mainnet:\s*(\d+),\s*testnet:\s*(\d+)\s*\}\)", text)
    f = re.search(r"FORGE_CREATE_FALLBACK_BY_NETWORK\s*=\s*Object\.freeze\(\{\s*mainnet:\s*Object\.freeze\(\[([\d,\s]*)\]\),"
                  r"\s*testnet:\s*Object\.freeze\(\[([\d,\s]*)\]\)\s*\}\)", text)
    ladder = lambda g: [int(x) for x in g.replace(" ", "").split(",") if x]  # noqa: E731
    return ({"mainnet": int(m.group(1)), "testnet11": int(m.group(2))} if m else {},
            {"mainnet": ladder(f.group(1)), "testnet11": ladder(f.group(2))} if f else {})


class Volume:
    """A scratch state directory standing in for the router's volume, with forge_network.record_path
    pointed at it alone (the checkout's .awizard/ may hold a real record; it is not consulted)."""

    def __init__(self, held: tuple[str, ...]):
        self.dir = Path(tempfile.mkdtemp(prefix="forge-create-default-"))
        for name in held:
            (self.dir / name).write_text("{}", encoding="utf-8")

    def __enter__(self):
        self.saved = forge_network.record_path
        forge_network.record_path = lambda tag="v15": self.dir / f"{tag}-{forge_network.facts()['record_suffix']}.json"
        return self

    def __exit__(self, *exc):
        forge_network.record_path = self.saved
        for f in self.dir.iterdir():
            f.unlink()
        self.dir.rmdir()


def main() -> int:
    saved = os.environ.get("FORGE_NETWORK")
    try:
        print("the tables:")
        check("forge_network.CREATE_PROTOCOL_BY_NETWORK is mainnet 20, testnet 20 (V19 once its record is held)",
              forge_network.CREATE_PROTOCOL_BY_NETWORK == EXPECTED, str(forge_network.CREATE_PROTOCOL_BY_NETWORK))
        check("forge_network.CREATE_FALLBACK_BY_NETWORK is mainnet [19, 17] (V18 with v18-mainnet.json, else V16), testnet [19]",
              forge_network.CREATE_FALLBACK_BY_NETWORK == FALLBACK, str(forge_network.CREATE_FALLBACK_BY_NETWORK))
        target, fallback = api_tables()
        check("api/_forgeVersion.js FORGE_CREATE_PROTOCOL_BY_NETWORK says the same", target == EXPECTED, str(target))
        check("api/_forgeVersion.js FORGE_CREATE_FALLBACK_BY_NETWORK says the same", fallback == FALLBACK, str(fallback))
        check("no network ever creates V17 (18)", 18 not in EXPECTED.values() and all(18 not in f for f in FALLBACK.values()))
        check("the V18 lane is carried (19 in forge_stdin._LANES)", 19 in forge_stdin._LANES)
        check("the V19 lane is carried (20 in forge_stdin._LANES)", 20 in forge_stdin._LANES)
        check("V19 (20) is only ever the target, never a fallback: no network creates it without its record",
              all(20 not in f for f in FALLBACK.values()))
        check("every default is a lane forge_stdin carries",
              all(v in forge_stdin._LANES for v in [*EXPECTED.values(), *(p for f in FALLBACK.values() for p in f)]))

        creation = {"action": "prepare-create", "config": {"asset_ids": ["xch", "aa" * 32]}}
        cases = (
            ("mainnet", "mainnet, no record held", (), 17),
            ("mainnet", "mainnet, only v16-mainnet.json held", ("v16-mainnet.json",), 17),
            ("mainnet", "mainnet, a v18-TESTNET record held (the other network's)", ("v18-testnet.json",), 17),
            ("mainnet", "mainnet, a v19-TESTNET record held (the other network's)", ("v16-mainnet.json", "v19-testnet.json"), 17),
            ("mainnet", "mainnet, a v17-mainnet.json held (V17 never moves mainnet)", ("v16-mainnet.json", "v17-mainnet.json"), 17),
            ("mainnet", "mainnet, v18-mainnet.json held", ("v16-mainnet.json", "v18-mainnet.json"), 19),
            ("MainNet ", "' MainNet ' (normalized), v18-mainnet.json held", ("v18-mainnet.json",), 19),
            ("mainnet", "mainnet, v19-mainnet.json held beside v16 and v18", ("v16-mainnet.json", "v18-mainnet.json", "v19-mainnet.json"), 20),
            ("mainnet", "mainnet, v19-mainnet.json held without a v18 record", ("v16-mainnet.json", "v19-mainnet.json"), 20),
            ("MainNet ", "' MainNet ' (normalized), v19-mainnet.json held", ("v19-mainnet.json",), 20),
            ("testnet11", "testnet11, no record held", (), 19),
            ("testnet11", "testnet11, v18-testnet.json held", ("v18-testnet.json",), 19),
            ("testnet11", "testnet11, a v19-MAINNET record held (the other network's)", ("v18-testnet.json", "v19-mainnet.json"), 19),
            ("testnet11", "testnet11, v19-testnet.json held", ("v18-testnet.json", "v19-testnet.json"), 20),
            (None, "unset (testnet11)", (), 19),
            (None, "unset (testnet11), v19-testnet.json held", ("v19-testnet.json",), 20),
            ("bogus", "an unknown name (testnet11)", (), 19),
        )
        for network, label, held, want in cases:
            on(network)
            print(f"\nFORGE_NETWORK={label}:")
            with Volume(held):
                check(f"  forge_network.create_protocol() is {want}", forge_network.create_protocol() == want, str(forge_network.create_protocol()))
                got = forge_stdin._lane_version(dict(creation))
                check(f"  an undeclared creation takes protocol {want}, not max(_LANES) = {max(forge_stdin._LANES)}", got == want, str(got))
                check("  the lane it resolves to is that revision's", int(forge_stdin._lane(dict(creation))[3].PROTOCOL_VERSION) == want)
                # A declared revision still picks its own lane: the router declares the protocol it resolved.
                check("  a declared protocol_version 19 resolves to the V18 lane",
                      forge_stdin._lane_version({**creation, "protocol_version": 19}) == 19)
                check("  a declared protocol_version 20 resolves to the V19 lane",
                      forge_stdin._lane_version({**creation, "protocol_version": 20}) == 20
                      and int(forge_stdin._lane({**creation, "protocol_version": 20})[3].PROTOCOL_VERSION) == 20)
                check("  a declared protocol_version 17 resolves to the V16 lane",
                      forge_stdin._lane_version({**creation, "protocol_version": 17}) == 17)

        on("mainnet")
        print("\nthe gate asks for the target revision's record, by tag:")
        check("  create_protocol(held) is 19 on mainnet when the v18 record is held",
              forge_network.create_protocol(lambda tag: tag == "v18") == 19)
        check("  and 17 when only a v17 record is", forge_network.create_protocol(lambda tag: tag == "v17") == 17)
        check("  20 on mainnet when the v19 record is held", forge_network.create_protocol(lambda tag: tag == "v19") == 20)
        check("  20 on mainnet when every record is held", forge_network.create_protocol(lambda tag: True) == 20)
        on("testnet11")
        check("  20 on testnet when the v19 record is held", forge_network.create_protocol(lambda tag: tag == "v19") == 20)
        check("  19 on testnet when it is not (the ladder's last, unconditionally)",
              forge_network.create_protocol(lambda tag: False) == 19)
        on("mainnet")

        print("\nnon-creation requests are unchanged (a snapshot names its own revision):")
        check("  a protocol-19 snapshot resolves to V18 on mainnet",
              forge_stdin._lane_version({"action": "swap", "pool": {"protocol_version": 19}}) == 19)
        check("  a protocol-18 snapshot resolves to V17 on mainnet",
              forge_stdin._lane_version({"action": "swap", "pool": {"protocol_version": 18}}) == 18)
        try:
            forge_stdin._lane_version({"action": "swap", "pool": {"protocol_version": 19}, "protocol_version": 18})
            check("  a single-pool action naming two revisions is refused", False, "accepted")
        except ValueError as exc:
            check("  a single-pool action naming two revisions is refused", "mix" in str(exc))
        check("  a route across V16, V17 and V18 is built by the V18 lane",
              forge_stdin._lane_version({"action": "multihop-swap", "pools": [{"protocol_version": v} for v in (17, 18, 19)]}) == 19)
        check("  a protocol-20 snapshot resolves to V19",
              forge_stdin._lane_version({"action": "swap", "pool": {"protocol_version": 20}}) == 20)
        check("  a route across V16, V18 and V19 is built by the V19 lane",
              forge_stdin._lane_version({"action": "multihop-swap", "pools": [{"protocol_version": v} for v in (17, 19, 20)]}) == 20)
        pairs = {"migrate-v16-to-v17": (17, 18), "migrate-v16-to-v18": (17, 19), "migrate-v17-to-v18": (18, 19),
                 "migrate-v16-to-v19": (17, 20), "migrate-v17-to-v19": (18, 20), "migrate-v18-to-v19": (19, 20)}
        for action, want in (("migrate-v16-to-v17", 18), ("migrate-v16-to-v18", 19), ("migrate-v17-to-v18", 19),
                             ("migrate-v16-to-v19", 20), ("migrate-v17-to-v19", 20), ("migrate-v18-to-v19", 20)):
            src, dst = pairs[action]
            got = forge_stdin._lane_version({"action": action, "sourcePool": {"protocol_version": src}, "pool": {"protocol_version": dst}})
            check(f"  {action} is built by the protocol-{want} lane", got == want, str(got))
    finally:
        on(saved)

    print(f"\n{sum(results)}/{len(results)} creation-default checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
