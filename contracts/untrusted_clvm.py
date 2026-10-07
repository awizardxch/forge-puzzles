#!/usr/bin/env python3
"""CLVM that arrived from outside: parse it without back-references, run it under a cost cap.

Two rules from the Chialisp docs (attacks-and-countermeasures, "CLVM Denial-of-Service
Vectors"), applied to the drivers in this directory (2026-10-07 audit, finding U3):

* untrusted CLVM is deserialized with the NON-backref deserializer; the backref-aware one
  is only for bytes we serialized ourselves;
* every production run uses MAX_BLOCK_COST_CLVM (11,000,000,000) as its cost cap.

Why the deserializer matters: `Program.from_bytes` / `Program.fromhex` in the pinned
chia stack (chia-blockchain 2.5.6 over chia_rs 0.27) accept the back-reference marker
unconditionally -- `chia_rs.ALLOW_BACKREFS` is 0 there, so there is no flag to turn it
off -- and `ff61fe02` parses to `ff6161`. A ladder of back-references expands to a tree
exponentially larger than the stream, and chia_rs's allocator stops it only after about a
second of CPU. The pure-Python `clvm.serialize.sexp_from_stream` does refuse backrefs, but
it is three orders of magnitude slower than chia_rs (5 s for 240 KiB here), which would be
its own stall. So the stream is scanned first (iteratively, in O(len)), and only a stream
with no marker is handed to the fast parser; on such a stream the backref-aware parser
reads exactly what the non-backref one would, so the result is the same tree.

Why the cost cap matters: `Program.run` uses chia-blockchain's `INFINITE_COST`. It is
11,000,000,000 in 2.3.0 and 2.5.6 alike, but the value is the library's to change, and a
reveal from an offer string or a chain spend must not be able to run for as long as it
likes in our process. `run_capped` passes the cap explicitly; `_test_untrusted_clvm.py`
pins `INFINITE_COST` to MAX_COST so a resolution that moves it is noticed.

Dependency-free beyond what every driver already imports.
"""
from __future__ import annotations

from typing import Any

from chia.types.blockchain_format.program import Program

# MAX_BLOCK_COST_CLVM: the only cap a production run may use (docs rule I2).
MAX_COST = 11_000_000_000

# One serialized program from outside: a puzzle reveal or a solution. The API already
# bounds a build's reveals at 256 KiB in total (api/forge-offer-build.js
# MAX_REVEAL_BYTES_PER_BUILD); the same figure bounds one program here. Real reveals
# are a few KiB; a block generator, the largest CLVM Chia ever carries, is under 400 KiB.
MAX_BLOB_BYTES = 256 * 1024

# An offer string before decoding. The decoded bundle is already capped at 6 MiB by
# chia's zlib step (puzzle_compression.decompress_with_zdict, max_length); bech32m
# carries 5 bits per character, so a string this long cannot encode more than that.
MAX_OFFER_CHARS = 10 * 1024 * 1024

_CONS = 0xFF
_BACKREF = 0xFE


class UntrustedClvmError(ValueError):
    """The bytes are not a CLVM program this process is willing to parse or run."""


def check_serialization(blob: bytes, max_bytes: int = MAX_BLOB_BYTES) -> None:
    """Refuse a stream that is too long, uses a back-reference, is truncated, or has trailing bytes.

    Walks the serialization with a counter instead of a stack: a cons byte consumes one
    pending node and promises two, an atom consumes one. The walk ends when nothing is
    pending, and must end exactly at the end of the stream.

    Rejecting every 0xfe in header position is sound for the non-backref format: there
    0xfe would be a seven-leading-ones atom header, i.e. an atom of at least 2**48
    bytes, which no stream under `max_bytes` can carry. In the backref format it is the
    marker. Either way a stream containing it cannot be a program we accept.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise UntrustedClvmError(f"CLVM must be bytes, not {type(blob).__name__}")
    size = len(blob)
    if size == 0:
        raise UntrustedClvmError("empty CLVM")
    if size > max_bytes:
        raise UntrustedClvmError(f"CLVM is {size} bytes; at most {max_bytes}")
    view = memoryview(blob)
    pos = 0
    pending = 1
    while pending:
        if pos >= size:
            raise UntrustedClvmError("truncated CLVM")
        head = view[pos]
        if head == _CONS:
            pending += 1
            pos += 1
            continue
        if head == _BACKREF:
            raise UntrustedClvmError(f"CLVM uses a back-reference at byte {pos}; refused")
        pending -= 1
        if head <= 0x80:
            pos += 1                                   # nil, or a one-byte atom
            continue
        # Length-prefixed atom: the count of leading one bits is the header width in bytes,
        # the remaining bits of the first byte are the high bits of the length.
        width = 1
        mask = 0x40
        while head & mask:
            width += 1
            mask >>= 1
        length = head & (mask - 1)
        if pos + width > size:
            raise UntrustedClvmError("truncated CLVM atom header")
        for extra in view[pos + 1:pos + width]:
            length = (length << 8) | extra
        pos += width
        if length > size - pos:
            raise UntrustedClvmError(f"truncated CLVM atom: header promises {length} bytes")
        pos += length
    if pos != size:
        raise UntrustedClvmError(f"{size - pos} trailing byte(s) after the CLVM program")


def parse_untrusted(blob: bytes, max_bytes: int = MAX_BLOB_BYTES) -> Program:
    """A Program from bytes somebody else serialized: size-capped, no back-references."""
    check_serialization(blob, max_bytes)
    return Program.from_bytes(bytes(blob))


def parse_untrusted_hex(text: Any, max_bytes: int = MAX_BLOB_BYTES) -> Program:
    """`parse_untrusted` for the hex most JSON payloads and node RPCs carry, `0x` or not."""
    cleaned = str(text or "").strip()
    if cleaned[:2].lower() == "0x":
        cleaned = cleaned[2:]
    if len(cleaned) > 2 * max_bytes:
        raise UntrustedClvmError(f"CLVM hex is {len(cleaned)} characters; at most {2 * max_bytes}")
    try:
        blob = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise UntrustedClvmError(f"CLVM is not hex: {exc}") from None
    return parse_untrusted(blob, max_bytes)


def run_capped(puzzle: Program, solution: Program, max_cost: int = MAX_COST) -> Program:
    """`puzzle.run(solution)` with an explicit cost cap; raises when the cap is exceeded."""
    _cost, output = puzzle.run_with_cost(max_cost, solution)
    return output


def check_offer_programs(offer: Any, max_bytes: int = MAX_BLOB_BYTES) -> None:
    """Every reveal and solution in a decoded offer passes `check_serialization`.

    `Offer.from_bech32` hands the bundle's bytes to chia_rs, which keeps each program's
    serialization verbatim (a backref-compressed reveal comes back as its compressed
    bytes), so the scan sees what the stream carried.
    """
    for spend in offer._bundle.coin_spends:
        check_serialization(bytes(spend.puzzle_reveal), max_bytes)
        check_serialization(bytes(spend.solution), max_bytes)


def offer_from_bech32(text: Any, max_bytes: int = MAX_BLOB_BYTES):
    """`Offer.from_bech32` on a caller's string, with the decoded programs checked before use."""
    from chia.wallet.trading.offer import Offer  # the drivers that take offers import this already

    cleaned = str(text or "").strip()
    if not cleaned:
        raise UntrustedClvmError("empty offer string")
    if len(cleaned) > MAX_OFFER_CHARS:
        raise UntrustedClvmError(f"offer string is {len(cleaned)} characters; at most {MAX_OFFER_CHARS}")
    offer = Offer.from_bech32(cleaned)
    check_offer_programs(offer, max_bytes)
    return offer
