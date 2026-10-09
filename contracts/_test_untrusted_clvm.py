#!/usr/bin/env python3
"""Untrusted CLVM is parsed without back-references and run under a cost cap (audit U3,
2026-10-07; Chialisp docs rule I2).

Pins two things about the installed chia stack and proves the helper bites:

* `INFINITE_COST`, which `Program.run` uses, is MAX_BLOCK_COST_CLVM (11,000,000,000). The
  requirements floor is chia-blockchain 2.3.0 and that version agrees; a resolution that
  moves the constant fails here instead of silently uncapping a `.run`.
* `Program.from_bytes` accepts a back-reference (`ff61fe02` -> `ff6161`) and
  `Offer.from_bech32` carries one through, so the sites that take CLVM from a caller, a
  node or an offer string must go through `untrusted_clvm`, and this file checks each of
  them: by behaviour where the site can be driven without a chain, and by a source scan
  that fails if any of them is pointed back at the raw parser or at `.run(`.
"""
from __future__ import annotations

import inspect
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from chia.types.blockchain_format.program import INFINITE_COST, Program  # noqa: E402
from chia.types.coin_spend import make_spend  # noqa: E402
from chia.wallet.trading.offer import Offer  # noqa: E402
from chia.wallet.wallet_spend_bundle import WalletSpendBundle  # noqa: E402
from chia_rs import Coin, G2Element  # noqa: E402
from chia_rs import Program as RsProgram  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402
from chia_rs.sized_ints import uint64  # noqa: E402
from clvm_tools.binutils import assemble  # noqa: E402

import untrusted_clvm as uc  # noqa: E402
from untrusted_clvm import (  # noqa: E402
    MAX_BLOB_BYTES, MAX_COST, MAX_OFFER_CHARS, UntrustedClvmError,
    check_serialization, offer_from_bech32, parse_untrusted, parse_untrusted_hex, run_capped,
)

FAILED = 0
BACKREF = bytes.fromhex("ff61fe02")        # the audit's probe: (0x61 . <backref 2>) == (0x61 . 0x61)
TARGET_PH = bytes32(b"\x11" * 32)


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + detail) if detail and not condition else ''}")
    if not condition:
        FAILED += 1


def refused(label: str, fn, exc=UntrustedClvmError) -> None:
    try:
        fn()
    except exc as error:
        check(label, True, str(error))
    except Exception as error:  # noqa: BLE001 -- the wrong exception is a failure, not a crash
        check(label, False, f"raised {type(error).__name__}: {error}")
    else:
        check(label, False, "accepted")


print("the installed chia's constants:")
check(f"INFINITE_COST is MAX_BLOCK_COST_CLVM (got {INFINITE_COST})", INFINITE_COST == 11_000_000_000)
check("the helper's cap is the same number", MAX_COST == INFINITE_COST == 11_000_000_000)

print("\nthe stack's own parser accepts back-references (why the helper exists):")
check("Program.from_bytes(ff61fe02) parses to ff6161", bytes(Program.from_bytes(BACKREF)).hex() == "ff6161")

print("\nparse_untrusted refuses what the docs say to refuse:")
refused("the back-reference sample", lambda: parse_untrusted(BACKREF))
refused("a leading back-reference", lambda: parse_untrusted(bytes.fromhex("fe02")))
refused("the same sample as hex", lambda: parse_untrusted_hex("0xff61fe02"))
refused("trailing bytes after the program", lambda: parse_untrusted(bytes.fromhex("ff616100")))
refused("a truncated pair", lambda: parse_untrusted(bytes.fromhex("ff61")))
refused("an atom header promising more than the stream holds", lambda: parse_untrusted(bytes.fromhex("c1ff00")))
refused("empty bytes", lambda: parse_untrusted(b""))
refused("non-hex text", lambda: parse_untrusted_hex("zz"))
oversized = bytes(Program.to(b"x" * (MAX_BLOB_BYTES + 1)))
refused(f"a blob over {MAX_BLOB_BYTES} bytes", lambda: parse_untrusted(oversized))
check("the same blob under a raised cap parses", bytes(parse_untrusted(oversized, max_bytes=len(oversized))) == oversized)

print("\na normal reveal parses to the same tree and runs:")
quoted = Program.to((1, [[51, TARGET_PH, 7, [b"name", b"SYM"]]]))     # (q (51 ph 7 (memos)))
check("parse_untrusted agrees with Program.from_bytes", parse_untrusted(bytes(quoted)) == quoted)
check("parse_untrusted_hex strips 0x", parse_untrusted_hex("0x" + bytes(quoted).hex()) == quoted)
out = run_capped(quoted, Program.to([]))
check("run_capped returns the program's output", out.first().first().as_int() == 51)
check("run_capped at a tight cap still runs a cheap program", run_capped(quoted, Program.to([]), 10_000) == out)

print("\na looping program is cut off, not waited for:")
loop = Program.to(assemble("(a (q 2 2 (c 2 ())) (c (q 2 2 (c 2 ())) ()))"))
started = time.monotonic()
refused("run_capped with a low cap raises", lambda: run_capped(loop, Program.to([]), 100_000), exc=ValueError)
check("and returns in well under a second", time.monotonic() - started < 1.0, f"{time.monotonic() - started:.2f}s")

print("\noffer strings:")
coin = Coin(bytes32(b"\x22" * 32), quoted.get_tree_hash(), uint64(7))
good = Offer({}, WalletSpendBundle([make_spend(coin, quoted, Program.to([]))], G2Element()), {})
decoded = offer_from_bech32(good.to_bech32())
check("a normal offer decodes", len(decoded._bundle.coin_spends) == 1)
compressed_reveal = RsProgram.from_bytes(BACKREF)       # chia_rs keeps the bytes verbatim
bad_coin = Coin(bytes32(b"\x33" * 32), bytes32(b"\x44" * 32), uint64(1))
bad = Offer({}, WalletSpendBundle([make_spend(bad_coin, compressed_reveal, Program.to([]))], G2Element()), {})
carried = Offer.from_bech32(bad.to_bech32())
check("Offer.from_bech32 carries a back-referenced reveal through",
      bytes(carried._bundle.coin_spends[0].puzzle_reveal) == BACKREF)
refused("offer_from_bech32 refuses that offer", lambda: offer_from_bech32(bad.to_bech32()))
refused("an offer string over the size bound", lambda: offer_from_bech32("offer1" + "q" * MAX_OFFER_CHARS))
refused("an empty offer string", lambda: offer_from_bech32("  "))

print("\nthe sites, driven without a chain:")
import forge_names  # noqa: E402

answers = {"backref": {"puzzle_reveal": BACKREF.hex(), "solution": "80"},
           "good": {"puzzle_reveal": "0x" + bytes(quoted).hex(), "solution": "80"},
           "loop": {"puzzle_reveal": bytes(loop).hex(), "solution": "80"}}
current = {"which": "good"}
forge_names._rpc = lambda node, route, body: {"coin_solution": answers[current["which"]]}  # noqa: E731
memos = forge_names._memos_of("http://node", "ab" * 32, 1, TARGET_PH.hex(), 7)
check("forge_names reads memos from a normal spend", memos == [b"name", b"SYM"])
current["which"] = "backref"
refused("forge_names refuses a back-referenced reveal", lambda: forge_names._memos_of("http://node", "ab" * 32, 1, TARGET_PH.hex(), 7))
current["which"] = "loop"
started = time.monotonic()
refused("forge_names cuts off a looping reveal", lambda: forge_names._memos_of("http://node", "ab" * 32, 1, TARGET_PH.hex(), 7), exc=ValueError)
check("in bounded time (the block cost limit)", time.monotonic() - started < 60.0, f"{time.monotonic() - started:.1f}s")

import breadcrumb_proof as bp  # noqa: E402

chain = bp.Consensus(["http://node"], fetch=lambda source, route, body: {"success": True, "coin_solution": answers["backref"]})
refused("breadcrumb_proof refuses a back-referenced reveal", lambda: chain.spend("ab" * 32, 1))
chain = bp.Consensus(["http://node"], fetch=lambda source, route, body: {"success": True, "coin_solution": answers["good"]})
check("and reads a normal one", chain.spend("ab" * 32, 1)[0] == quoted)

import vault_tool  # noqa: E402

spend_json = {"coin": {"parent_coin_info": "22" * 32, "puzzle_hash": quoted.get_tree_hash().hex(), "amount": 7},
              "puzzle_reveal": bytes(quoted).hex(), "solution": "80"}
check("vault_tool.spend_from_json restores a normal spend", bytes(vault_tool.spend_from_json(spend_json).puzzle_reveal) == bytes(quoted))
refused("vault_tool.spend_from_json refuses a back-referenced reveal",
        lambda: vault_tool.spend_from_json({**spend_json, "puzzle_reveal": BACKREF.hex()}))

import forge_offer_build  # noqa: E402

refused("forge_offer_build.finalize refuses a back-referenced reveal",
        lambda: forge_offer_build.finalize({"coin_spends": [{"coin": spend_json["coin"], "puzzle_reveal": BACKREF.hex(), "solution": "80"}],
                                            "signature": bytes(G2Element()).hex(), "requested": [], "change_puzzle_hash": "aa" * 32}))

print("\nthe sites, by source (detect over-broadly: a site pointed back at the raw parser fails here):")
RAW = re.compile(r"Program\.from_bytes\(|Program\.fromhex\(|(?<!subprocess)\.run\(|Offer\.from_bech32\(")
GUARDED = {
    "parse_offer.py": RAW,
    "forge_names.py": RAW,
    "breadcrumb_proof.py": RAW,
    "forge_offer_build.py": RAW,
    "forge_stdin.py": re.compile(r"Offer\.from_bech32\("),
    "forge_stdin_nasset.py": re.compile(r"Offer\.from_bech32\("),
    "forge_create_pool.py": re.compile(r"Offer\.from_bech32\("),
    # Its own compiled modules may use the raw parser; nothing in it may `.run(` uncapped.
    "forge_v15_driver.py": re.compile(r"(?<!subprocess)\.run\("),
    "forge_v16_driver.py": re.compile(r"(?<!subprocess)\.run\("),
}
for name, pattern in GUARDED.items():
    hits = [f"{i}: {line.strip()}" for i, line in enumerate((HERE / name).read_text(encoding="utf-8").splitlines(), 1)
            if pattern.search(line) and not line.lstrip().startswith("#")]
    check(f"{name} has no raw parse or uncapped run", not hits, "; ".join(hits[:3]))
body = inspect.getsource(vault_tool.spend_from_json)
check("vault_tool.spend_from_json goes through parse_untrusted_hex",
      "parse_untrusted_hex(" in body and "Program.from_bytes(" not in body)
check("the helper itself hands only a checked stream to Program.from_bytes",
      inspect.getsource(uc.parse_untrusted).index("check_serialization(") < inspect.getsource(uc.parse_untrusted).index("Program.from_bytes("))

print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    raise SystemExit(1)
print("ALL PASSED -- untrusted CLVM is parsed without back-references and run under MAX_BLOCK_COST_CLVM")
