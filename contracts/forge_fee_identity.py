#!/usr/bin/env python3
"""Who pays a trade, and what their wallet holds, for the router's fee ladder.

The router charges its fee at a rate that depends on the wallet trading: the public
rate, lower ones for wallets holding listed NFT collections or tokens, none for listed
addresses (api/_feeLadder.js). A puzzle cannot read holdings, so the rate is decided
here, off chain, from things the router can check rather than believe:

  1. the coins the offer SPENDS: their puzzle hashes name the addresses that pay (a
     CAT coin's is the inner puzzle's hash). The creation gate's rule: the coin that
     pays, never a sent key.
  2. a KEY PROOF (owner, 2026-10-07: "NFTs held across all the wallet"): one
     aggregate BLS signature, made in ONE wallet prompt, by every key whose address
     holds a listed asset. The wallet signs a single synthetic coin spend whose
     puzzle is `(q . ((49 k1 d) (49 k2 d) ...))` -- AGG_SIG_UNSAFE per key over the
     digest of the router's message -- exactly as a lock owner proves a key
     (src/lib/multisig.ts, ownerProofSpend). Each key's address is
     puzzle_for_synthetic_public_key(key), so the proof PROVES control of those
     addresses, and the router may sum holdings across them. Summing over addresses
     a wallet merely names would let anyone add a whale's address to their count.
     The message also names the payer puzzle hashes the proof vouches for (every
     address the wallet scanned), the network and an expiry.

stdin JSON, by "mode":
  identify       {network_id, offer?, proof?}        -> {payers, proof: {ok, reason, proven, payers}}
  keys-to-phs    {keys}                              -> {phs: {key: ph}}
  message        {network_id, keys, payers, expires_at} -> {message}
  token-balances {node_url, phs, assets}             -> {balances: {ph: {asset: mojos}}}
  sign-keys      {secret_keys, message}              (checks only) -> {keys, signature, phs}
  probe-offer    {payers}                            (checks only) -> {offer}
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone

from chia.types.blockchain_format.program import Program
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    DEFAULT_HIDDEN_PUZZLE_HASH,
    calculate_synthetic_secret_key,
    puzzle_for_synthetic_public_key,
)
from chia.wallet.trading.offer import OFFER_MOD_HASH, Offer
from chia_rs import AugSchemeMPL, G1Element, G2Element, PrivateKey
from chia_rs.sized_bytes import bytes32

PROOF_TITLE = "The Forge - fee rate"   # ASCII only: it must survive every wallet's signing prompt
MAX_KEYS = 600
# The creation gate matches a whole wallet scan (WalletConnect pages run to 5,000 keys);
# ~0.26 ms a key, so 6,000 is under two seconds.
MAX_GATE_KEYS = 6000
MAX_PAYERS = 600
MAX_EMBEDDED_KEYS = 20

# AGG_SIG_ME's additional data is the network's genesis challenge.
GENESIS = {
    "testnet11": bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615"),
    "mainnet": bytes.fromhex("ccd5bb71183532bff220ba46c268991a3ff07eb358e8255a65c30a2dce0e5fbb"),
}
AGG_SIG_UNSAFE = 49
AGG_SIG_ME = 50
AGG_SIG_OPCODES = set(range(43, 51))
RUN_MAX_COST = 11_000_000_000


# ── the proof embedded in a trade (owner, 2026-10-07: "build it into the transaction") ──
#
# The site's offer builder (forge_offer_build.py) adds one `(49 key digest)` per
# holding key to the trader's own first spend, the digest bound to the exact coins
# that offer spends. The wallet signs them in the SAME prompt as the trade, the
# router reads them back out of the offer and checks the offer's aggregate
# signature against every signature condition its spends make, and the chain
# checks them again on inclusion. Bound to the offer's coins, a proof cannot be
# lifted into another trade; signed with the trade, it cannot be lent.

def fee_proof_digest(network_id: str, coin_ids: list[bytes]) -> bytes:
    """What every embedded key signs: the network and the offer's own coins, sorted."""
    return hashlib.sha256(b"forge-fee-rate|" + network_id.encode() + b"|" + b"".join(sorted(bytes(c) for c in coin_ids))).digest()


def _conditions(spend) -> list[list]:
    puzzle = Program.from_bytes(bytes(spend.puzzle_reveal))
    solution = Program.from_bytes(bytes(spend.solution))
    _cost, out = puzzle.run_with_cost(RUN_MAX_COST, solution)
    return [list(c.as_iter()) for c in out.as_iter()]


def agg_sig_pairs(spends, network_id: str) -> tuple[list[tuple[bytes, bytes]], list[tuple[bytes, bytes, bytes]]]:
    """Every (key, message) the spends require signed, as consensus would form them,
    and the (key, raw message, coin id) of each AGG_SIG_UNSAFE among them. Only the
    two kinds wallets make (49, 50) are understood; any other refuses."""
    genesis = GENESIS[network_id]
    pairs: list[tuple[bytes, bytes]] = []
    unsafe: list[tuple[bytes, bytes, bytes]] = []
    for spend in spends:
        coin_id = spend.coin.name()
        for cond in _conditions(spend):
            if not cond or len(bytes(cond[0].as_atom() or b"")) != 1:
                continue
            opcode = cond[0].as_int()
            if opcode not in AGG_SIG_OPCODES:
                continue
            key = bytes(cond[1].as_atom())
            msg = bytes(cond[2].as_atom())
            if opcode == AGG_SIG_UNSAFE:
                pairs.append((key, msg))
                unsafe.append((key, msg, bytes(coin_id)))
            elif opcode == AGG_SIG_ME:
                pairs.append((key, msg + bytes(coin_id) + genesis))
            else:
                raise ValueError(f"the offer asks for an AGG_SIG kind ({opcode}) the router does not verify")
    return pairs, unsafe


def embedded_proof(offer_text: str, network_id: str) -> dict:
    """{"present", "ok", "reason", "proven", "keys"}: the holding keys a trade proves
    by its own signature. `present` is false when the offer carries no proof."""
    none = {"present": False, "ok": False, "reason": None, "proven": [], "keys": []}
    offer = Offer.from_bech32(offer_text)
    bundle = offer.to_spend_bundle()
    # An offer file carries a placeholder spend per requested asset (a zero-parent,
    # zero-amount settlement coin holding the notarized payments). It is no coin of
    # the trader's, cannot run alone, and asks for no signature: skip it.
    spends = [s for s in bundle.coin_spends
              if s.coin.puzzle_hash != OFFER_MOD_HASH and bytes(s.coin.parent_coin_info) != bytes(32)]
    digest = fee_proof_digest(network_id, [s.coin.name() for s in spends])
    try:
        pairs, unsafe = agg_sig_pairs(spends, network_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable offer proves nothing
        return {**none, "present": True, "reason": f"the offer's conditions could not be read: {exc}"}
    keys = sorted({_hex(k) for k, msg, _ in unsafe if msg == digest})
    if not keys:
        return none
    if len(keys) > MAX_EMBEDDED_KEYS:
        return {**none, "present": True, "reason": f"an offer proves at most {MAX_EMBEDDED_KEYS} keys"}
    try:
        pks = [G1Element.from_bytes(k) for k, _ in pairs]
        ok = AugSchemeMPL.aggregate_verify(pks, [m for _, m in pairs], G2Element.from_bytes(bytes(bundle.aggregated_signature)))
    except Exception as exc:  # noqa: BLE001
        return {**none, "present": True, "reason": f"the offer's signature could not be checked: {exc}"}
    if not ok:
        return {**none, "present": True, "reason": "the offer's signature does not cover every key it names"}
    return {"present": True, "ok": True, "reason": None, "proven": sorted({key_ph(k) for k in keys}), "keys": keys}


def _hex(b: bytes) -> str:
    return bytes(b).hex()


def _clean(h: object) -> str:
    return str(h or "").strip().lower().removeprefix("0x")


def payers_of(offer_text: str) -> list[str]:
    """The puzzle hashes of the addresses whose coins the offer spends."""
    offer = Offer.from_bech32(offer_text)
    out: set[str] = set()
    for cs in offer.to_spend_bundle().coin_spends:
        # requested-payment placeholders (zero parent) are nobody's coins
        if bytes(cs.coin.parent_coin_info) == bytes(32):
            continue
        puzzle = Program.from_bytes(bytes(cs.puzzle_reveal))
        mod, args = puzzle.uncurry()
        # (MOD_HASH TAIL_HASH INNER_PUZZLE): a CAT coin's address is the inner puzzle's hash
        ph = args.at("rrf").get_tree_hash() if mod == CAT_MOD else cs.coin.puzzle_hash
        # the settlement stand-ins for the requested payments are nobody's coins
        if ph == OFFER_MOD_HASH:
            continue
        out.add(_hex(ph))
    return sorted(out)


def list_digest(items: list[str]) -> str:
    return hashlib.sha256(",".join(sorted(_clean(i) for i in items)).encode()).hexdigest()


def key_ph(key_hex: str) -> str:
    """A wallet key's address: the standard puzzle of that SYNTHETIC key (Sage's derivation keys)."""
    return _hex(puzzle_for_synthetic_public_key(G1Element.from_bytes(bytes.fromhex(_clean(key_hex)))).get_tree_hash())


def build_message(network_id: str, keys: list[str], payers: list[str], expires_at: str) -> str:
    """The text whose digest every key signs."""
    return "\n".join([
        PROOF_TITLE,
        "",
        "Prove these wallet addresses are yours, for the router's fee rate.",
        "It authorizes nothing, spends nothing, and creates no transaction.",
        "",
        f"Network: {network_id}",
        f"Keys:    {list_digest(keys)}",
        f"Payers:  {list_digest(payers)}",
        f"Expires: {expires_at}",
    ])


def message_digest(message: str) -> bytes:
    return hashlib.sha256(message.encode("utf-8")).digest()


def _field(message: str, name: str) -> str | None:
    for line in message.splitlines():
        if line.startswith(name + ":"):
            return line.split(":", 1)[1].strip()
    return None


def verify_proof(proof: dict, network_id: str, offer_payers: list[str] | None) -> dict:
    """{"ok", "reason", "proven", "payers", "expires_at"}. ok means: every listed key
    signed AGG_SIG_UNSAFE over the message's digest, the message names THIS network, a
    future expiry, the digest of exactly these keys and of exactly these payers, and
    (given an offer) the payers cover every coin the offer spends. `proven` are the
    keys' addresses: holdings may be summed over them."""
    def refuse(reason: str) -> dict:
        return {"ok": False, "reason": reason, "proven": [], "payers": [], "expires_at": None}
    try:
        message = str(proof.get("message") or "")
        keys = [_clean(k) for k in (proof.get("keys") or [])]
        payers = [_clean(p) for p in (proof.get("payers") or [])]
        sig = G2Element.from_bytes(bytes.fromhex(_clean(proof.get("signature"))))
        pks = [G1Element.from_bytes(bytes.fromhex(k)) for k in keys]
    except Exception as exc:  # noqa: BLE001 -- a malformed proof is a reason, not a crash
        return refuse(f"malformed proof: {exc}")
    if not keys or len(keys) > MAX_KEYS or len(set(keys)) != len(keys):
        return refuse(f"a proof names 1 to {MAX_KEYS} distinct keys")
    if len(payers) > MAX_PAYERS:
        return refuse(f"a proof vouches for at most {MAX_PAYERS} payers")
    if not message.startswith(PROOF_TITLE):
        return refuse("not a Forge fee proof")
    if _field(message, "Network") != network_id:
        return refuse("the proof names another network")
    expires = _field(message, "Expires") or ""
    try:
        when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError:
        return refuse("the proof has no readable expiry")
    if when <= datetime.now(timezone.utc):
        return refuse("the proof has expired")
    if _field(message, "Keys") != list_digest(keys):
        return refuse("the proof's key digest does not match the keys it lists")
    if _field(message, "Payers") != list_digest(payers):
        return refuse("the proof's payer digest does not match the payers it lists")
    digest = message_digest(message)
    if not AugSchemeMPL.aggregate_verify(pks, [digest] * len(pks), sig):
        return refuse("the signature is not every listed key's over the message")
    proven = sorted({key_ph(k) for k in keys})
    vouched = set(payers) | set(proven)
    if offer_payers is not None:
        missing = [p for p in offer_payers if p not in vouched]
        if missing:
            return refuse(f"the offer spends a coin the proof does not vouch for ({missing[0][:8]}...)")
    return {"ok": True, "reason": None, "proven": proven, "payers": sorted(vouched), "expires_at": expires}


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "forge-fee-ladder"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def token_balances(node_url: str, phs: list[str], assets: list[str]) -> dict[str, dict[str, int]]:
    """Unspent balance of each listed CAT at each address: the CAT puzzle hash of
    (asset, address) is computed, so only the listed tokens are ever looked up."""
    outer: dict[str, tuple[str, str]] = {}
    for ph in phs:
        inner_hash = bytes32.fromhex(_clean(ph))
        for asset in assets:
            # the CAT puzzle curries the inner puzzle, but its hash needs only the inner HASH
            outer[_hex(_cat_hash(bytes32.fromhex(_clean(asset)), inner_hash))] = (_clean(asset), _clean(ph))
    balances: dict[str, dict[str, int]] = {}
    keys = list(outer)
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        out = _post(f"{node_url.rstrip('/')}/get_coin_records_by_puzzle_hashes",
                    {"puzzle_hashes": ["0x" + k for k in chunk], "include_spent_coins": False})
        if not out.get("success", True) and out.get("error"):
            raise RuntimeError(str(out["error"]))
        for rec in out.get("coin_records") or []:
            ph_out = _clean(rec["coin"]["puzzle_hash"])
            if ph_out in outer:
                asset, ph = outer[ph_out]
                balances.setdefault(ph, {})
                balances[ph][asset] = balances[ph].get(asset, 0) + int(rec["coin"]["amount"])
    return balances


def _cat_hash(asset: bytes32, inner_hash: bytes32) -> bytes32:
    """The CAT2 puzzle hash for an inner puzzle known only by its hash (curry by hashes)."""
    from chia.wallet.cat_wallet.cat_utils import CAT_MOD_HASH
    from chia.wallet.util.curry_and_treehash import curry_and_treehash, calculate_hash_of_quoted_mod_hash
    quoted = calculate_hash_of_quoted_mod_hash(CAT_MOD_HASH)
    return curry_and_treehash(quoted, Program.to(CAT_MOD_HASH).get_tree_hash(), Program.to(asset).get_tree_hash(), inner_hash)


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    mode = str(payload.get("mode") or "identify")
    if mode == "keys-to-phs":
        limit = min(int(payload.get("limit") or MAX_KEYS), MAX_GATE_KEYS)
        keys = [_clean(k) for k in (payload.get("keys") or [])][:limit]
        phs = {}
        for k in keys:
            try:
                phs[k] = key_ph(k)
            except Exception:  # noqa: BLE001 -- a key that is not a G1 point is dropped
                continue
        print(json.dumps({"success": True, "phs": phs}))
        return 0
    if mode == "message":
        print(json.dumps({"success": True, "message": build_message(
            str(payload["network_id"]), list(payload.get("keys") or []), list(payload.get("payers") or []),
            str(payload["expires_at"]))}))
        return 0
    if mode == "token-balances":
        print(json.dumps({"success": True, "balances": token_balances(
            str(payload["node_url"]), list(payload.get("phs") or []), list(payload.get("assets") or []))}))
        return 0
    if mode == "sign-keys":
        digest = message_digest(str(payload["message"]))
        keys, sigs, phs = [], [], []
        for raw in payload["secret_keys"]:
            sk = calculate_synthetic_secret_key(PrivateKey.from_bytes(bytes.fromhex(_clean(raw))), DEFAULT_HIDDEN_PUZZLE_HASH)
            pk = sk.get_g1()
            keys.append(_hex(bytes(pk)))
            sigs.append(AugSchemeMPL.sign(sk, digest))
            phs.append(_hex(puzzle_for_synthetic_public_key(pk).get_tree_hash()))
        print(json.dumps({"success": True, "keys": keys, "signature": _hex(bytes(AugSchemeMPL.aggregate(sigs))), "phs": phs}))
        return 0
    if mode == "probe-offer":
        from chia.types.blockchain_format.coin import Coin
        from chia.types.coin_spend import make_spend
        from chia.wallet.conditions import CreateCoin
        from chia_rs import SpendBundle
        from chia_rs.sized_ints import uint64
        identity = Program.to(1)
        coins, spends = [], []
        for i, ph in enumerate(payload["payers"]):
            coin = Coin(bytes32(bytes([0x60 + i]) * 32), bytes32.fromhex(_clean(ph)), uint64(1000))
            spends.append(make_spend(coin, identity, Program.to([[51, OFFER_MOD_HASH, 1000]])))
            coins.append(coin)
        requested = {None: [CreateCoin(bytes32(b"\x11" * 32), uint64(1), [bytes32(b"\x11" * 32)])]}
        offer = Offer(Offer.notarize_payments(requested, coins), SpendBundle(spends, G2Element()), {})
        print(json.dumps({"success": True, "offer": offer.to_bech32()}))
        return 0
    if mode == "sign-spends":
        # checks only: sign every AGG_SIG the spends ask for with the matching synthetic keys
        from chia.types.coin_spend import make_spend
        from chia.types.blockchain_format.coin import Coin
        from chia_rs.sized_ints import uint64
        network_id = str(payload.get("network_id") or "testnet11")
        by_pk = {}
        for raw in payload["secret_keys"]:
            sk = calculate_synthetic_secret_key(PrivateKey.from_bytes(bytes.fromhex(_clean(raw))), DEFAULT_HIDDEN_PUZZLE_HASH)
            by_pk[bytes(sk.get_g1())] = sk
        spends = [make_spend(Coin(bytes32.fromhex(_clean(s["coin"]["parent_coin_info"])), bytes32.fromhex(_clean(s["coin"]["puzzle_hash"])),
                                  uint64(int(s["coin"]["amount"]))), Program.fromhex(_clean(s["puzzle_reveal"])), Program.fromhex(_clean(s["solution"])))
                  for s in payload["coin_spends"]]
        pairs, _ = agg_sig_pairs(spends, network_id)
        sigs = [AugSchemeMPL.sign(by_pk[k], m) for k, m in pairs if k in by_pk]
        print(json.dumps({"success": True, "signature": _hex(bytes(AugSchemeMPL.aggregate(sigs))), "signed": len(sigs), "required": len(pairs)}))
        return 0
    if mode == "key-puzzle":
        # checks only: the standard puzzle of a wallet key, as a coin's puzzle reveal
        pk = G1Element.from_bytes(bytes.fromhex(_clean(payload["key"])))
        print(json.dumps({"success": True, "puzzle": bytes(puzzle_for_synthetic_public_key(pk)).hex()}))
        return 0
    if mode == "embedded-digest":
        print(json.dumps({"success": True, "digest": _hex(fee_proof_digest(str(payload["network_id"]), [bytes.fromhex(_clean(c)) for c in payload["coin_ids"]]))}))
        return 0
    network_id = str(payload.get("network_id") or "testnet11")
    payers = payers_of(str(payload["offer"])) if payload.get("offer") else None
    result: dict = {"success": True, "payers": payers or []}
    if payload.get("offer"):
        result["embedded"] = embedded_proof(str(payload["offer"]), network_id)
    proof = payload.get("proof")
    if isinstance(proof, dict):
        result["proof"] = verify_proof(proof, network_id, payers)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)
