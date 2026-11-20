#!/usr/bin/env python3
"""Which revision a NEW pool is built on, per network -- pinned, with V20 the target.

V20 (protocol 21; an add's effective term must be positive) is carried (forge_stdin._LANES[21])
and is the creation target on both networks (owner, 2026-11-18: the router and site serve V20),
once that network's V20 registry record, v20-<net>.json, is held. Until then each network falls
back down its ladder:

  testnet  21, unconditionally (V18 and V19 are unserved there);
  mainnet  21 with v20-mainnet.json held, else 19 with v18-mainnet.json held, else 17 (V16).
           Mainnet never creates V19: it goes from V18 straight to V20, so a v19-mainnet.json
           moves nothing, and its fallback is exactly what it was before V20.

The V19 cut's own description follows (its "20 with v19-<net>.json held" is superseded above).


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

EXPECTED = {"mainnet": 21, "testnet11": 21}
# mainnet: exactly its pre-V20 ladder (V19 is never a mainnet rung: V18 goes straight to V20);
# testnet: none -- V20 unconditionally (V18 unserved there since 2026-11-18, V19 since 2026-11-19).
FALLBACK = {"mainnet": [], "testnet11": []}
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
        check("forge_network.CREATE_PROTOCOL_BY_NETWORK is mainnet 21, testnet 21 (V20 once its record is held)",
              forge_network.CREATE_PROTOCOL_BY_NETWORK == EXPECTED, str(forge_network.CREATE_PROTOCOL_BY_NETWORK))
        check("forge_network.CREATE_FALLBACK_BY_NETWORK is mainnet [] and testnet []: V20 always (V18 unserved on mainnet since 2026-11-19)",
              forge_network.CREATE_FALLBACK_BY_NETWORK == FALLBACK, str(forge_network.CREATE_FALLBACK_BY_NETWORK))
        check("mainnet never creates V18 (19) or V19 (20): V20 is its only rung",
              EXPECTED["mainnet"] == 21 and not FALLBACK["mainnet"])
        target, fallback = api_tables()
        check("api/_forgeVersion.js FORGE_CREATE_PROTOCOL_BY_NETWORK says the same", target == EXPECTED, str(target))
        check("api/_forgeVersion.js FORGE_CREATE_FALLBACK_BY_NETWORK says the same", fallback == FALLBACK, str(fallback))
        check("no network ever creates V17 (18)", 18 not in EXPECTED.values() and all(18 not in f for f in FALLBACK.values()))
        check("the V18 lane is carried (19 in forge_stdin._LANES)", 19 in forge_stdin._LANES)
        check("the V19 lane is carried (20 in forge_stdin._LANES)", 20 in forge_stdin._LANES)
        check("V19 (20) is no network's target or fallback (testnet stopped serving it on 2026-11-19)",
              20 not in EXPECTED.values() and all(20 not in f for f in FALLBACK.values()))
        check("the V20 lane is carried (21 in forge_stdin._LANES)", 21 in forge_stdin._LANES
              and int(forge_stdin._LANES[21][3].PROTOCOL_VERSION) == 21)
        check("V20 (21) is both networks' target, and never a fallback (testnet has none: it creates V20 always)",
              set(EXPECTED.values()) == {21} and all(21 not in f for f in FALLBACK.values()))
        check("every default is a lane forge_stdin carries",
              all(v in forge_stdin._LANES for v in [*EXPECTED.values(), *(p for f in FALLBACK.values() for p in f)]))

        creation = {"action": "prepare-create", "config": {"asset_ids": ["xch", "aa" * 32]}}
        cases = (
            ("mainnet", "mainnet, no record held", (), 21),
            ("mainnet", "mainnet, only v16-mainnet.json held", ("v16-mainnet.json",), 21),
            ("mainnet", "mainnet, a v18-TESTNET record held (the other network's)", ("v18-testnet.json",), 21),
            ("mainnet", "mainnet, a v19-TESTNET record held (the other network's)", ("v16-mainnet.json", "v19-testnet.json"), 21),
            ("mainnet", "mainnet, a v17-mainnet.json held (V17 never moves mainnet)", ("v16-mainnet.json", "v17-mainnet.json"), 21),
            ("mainnet", "mainnet, v18-mainnet.json held", ("v16-mainnet.json", "v18-mainnet.json"), 21),
            ("MainNet ", "' MainNet ' (normalized), v18-mainnet.json held", ("v18-mainnet.json",), 21),
            # Mainnet never creates V19: a v19-mainnet.json is no rung of its ladder.
            ("mainnet", "mainnet, v19-mainnet.json held beside v16 and v18 (still V18)", ("v16-mainnet.json", "v18-mainnet.json", "v19-mainnet.json"), 21),
            ("mainnet", "mainnet, v19-mainnet.json held without a v18 record (still V16)", ("v16-mainnet.json", "v19-mainnet.json"), 21),
            ("MainNet ", "' MainNet ' (normalized), v19-mainnet.json held (still V16)", ("v19-mainnet.json",), 21),
            ("testnet11", "testnet11, no record held (V20: V18 and V19 are not served on testnet)", (), 21),
            ("testnet11", "testnet11, v18-testnet.json held (still V20)", ("v18-testnet.json",), 21),
            ("testnet11", "testnet11, a v19-MAINNET record held (the other network's)", ("v18-testnet.json", "v19-mainnet.json"), 21),
            ("testnet11", "testnet11, v19-testnet.json held", ("v18-testnet.json", "v19-testnet.json"), 21),
            (None, "unset (testnet11)", (), 21),
            (None, "unset (testnet11), v19-testnet.json held", ("v19-testnet.json",), 21),
            ("bogus", "an unknown name (testnet11)", (), 21),
            # V20: a held V20 record moves that network, and only that network, onto V20.
            ("mainnet", "mainnet, v20-mainnet.json held beside v16", ("v16-mainnet.json", "v20-mainnet.json"), 21),
            ("mainnet", "mainnet, v20-mainnet.json held beside v18 and v19",
             ("v16-mainnet.json", "v18-mainnet.json", "v19-mainnet.json", "v20-mainnet.json"), 21),
            ("MainNet ", "' MainNet ' (normalized), v20-mainnet.json held", ("v20-mainnet.json",), 21),
            ("mainnet", "mainnet, a v20-TESTNET record held (the other network's; still V18)", ("v18-mainnet.json", "v20-testnet.json"), 21),
            ("testnet11", "testnet11, v20-testnet.json held", ("v19-testnet.json", "v20-testnet.json"), 21),
            ("testnet11", "testnet11, a v20-MAINNET record held (the other network's; still V20)", ("v19-testnet.json", "v20-mainnet.json"), 21),
            (None, "unset (testnet11), v20-testnet.json held", ("v20-testnet.json",), 21),
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
                check("  a declared protocol_version 21 resolves to the V20 lane",
                      forge_stdin._lane_version({**creation, "protocol_version": 21}) == 21
                      and int(forge_stdin._lane({**creation, "protocol_version": 21})[3].PROTOCOL_VERSION) == 21)
                check("  a declared protocol_version 17 resolves to the V16 lane",
                      forge_stdin._lane_version({**creation, "protocol_version": 17}) == 17)

        on("mainnet")
        print("\nthe gate asks for the target revision's record, by tag:")
        check("  create_protocol(held) is 21 on mainnet when only the v18 record is held: V18 is unserved",
              forge_network.create_protocol(lambda tag: tag == "v18") == 21)
        check("  and 21 when only a v17 record is", forge_network.create_protocol(lambda tag: tag == "v17") == 21)
        check("  21 on mainnet when only the v19 record is held: mainnet never creates V19",
              forge_network.create_protocol(lambda tag: tag == "v19") == 21)
        check("  21 on mainnet when the v20 record is held", forge_network.create_protocol(lambda tag: tag == "v20") == 21)
        check("  21 on mainnet when every record is held", forge_network.create_protocol(lambda tag: True) == 21)
        on("testnet11")
        check("  21 on testnet when the v20 record is held", forge_network.create_protocol(lambda tag: tag == "v20") == 21)
        check("  21 on testnet when only the v19 record is held: testnet never creates V19 (unserved since 2026-11-19)",
              forge_network.create_protocol(lambda tag: tag == "v19") == 21)
        check("  21 on testnet when nothing is: V20 is its only rung",
              forge_network.create_protocol(lambda tag: False) == 21)
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
        check("  a protocol-21 snapshot resolves to V20",
              forge_stdin._lane_version({"action": "swap", "pool": {"protocol_version": 21}}) == 21)
        check("  a route across V18, V19 and V20 is built by the V20 lane",
              forge_stdin._lane_version({"action": "multihop-swap", "pools": [{"protocol_version": v} for v in (19, 20, 21)]}) == 21)
        check("  a held v20 record alone moves mainnet onto V20",
              forge_network.create_protocol(lambda tag: tag == "v20") == 21)
        for action, (src, dst) in (("migrate-v16-to-v20", (17, 21)), ("migrate-v17-to-v20", (18, 21)),
                                   ("migrate-v18-to-v20", (19, 21)), ("migrate-v19-to-v20", (20, 21))):
            got = forge_stdin._lane_version({"action": action, "sourcePool": {"protocol_version": src}, "pool": {"protocol_version": dst}})
            check(f"  {action} is built by the protocol-21 lane", got == 21 and forge_stdin._MIGRATIONS.get(action) == (src, dst), str(got))
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
