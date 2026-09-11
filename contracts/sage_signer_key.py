"""The signer key of the local Sage wallet, and the wallet address for a key.

Two uses, one script:

* No ``--pubkey``: ask the local Sage RPC which of its keys the operator is.

  Not simply the first derivation. A wallet holds many keys and an owner may
  have been registered from any of them -- the live testnet lock's "fish" owner
  is that wallet's derivation 19, not its 0 -- so reporting derivation 0 tells an
  owner they own nothing. With ``--match`` this scans a window of the wallet's
  unhardened derivations for any key in the list and reports that one; without a
  match, or without ``--match``, it falls back to derivation 0, which is what
  Sage hands out first over WalletConnect.
* ``--pubkey``: just derive the standard wallet address for a given key —
  what the owner's wallet shows for that key, on the requested network.

Prints one JSON object.
"""

from __future__ import annotations

import argparse
import json

from chia.util.bech32m import encode_puzzle_hash
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_hash_for_synthetic_public_key
from chia_rs import G1Element

HRP = {"testnet11": "txch", "mainnet": "xch"}

# How far down the wallet's unhardened derivations a match is looked for. Wallets
# hand out addresses in order and rarely run far ahead of what has been used, so
# this is generous; it is one RPC call regardless of the number.
DEFAULT_WINDOW = 250


def strip0x(value: str) -> str:
    text = (value or "").strip().lower()
    return text[2:] if text.startswith("0x") else text


def address_for(pubkey_hex: str, hrp: str) -> str:
    key = G1Element.from_bytes(bytes.fromhex(strip0x(pubkey_hex)))
    return encode_puzzle_hash(puzzle_hash_for_synthetic_public_key(key), hrp)


def _logged_in_fingerprint(sage) -> int | None:
    """The fingerprint of the key Sage is logged in as, or None."""
    try:
        key = (sage.call("get_key", {}) or {}).get("key") or {}
        value = key.get("fingerprint")
        return int(value) if value is not None else None
    except Exception:                                          # noqa: BLE001
        return None


def choose_owner_derivation(derivations: list, volunteered: str, wanted: set[str]):
    """The wallet's own derivation that is an owner key, or None.

    Pure, so the rule that matters can be tested without a wallet: substitution
    happens only when the VOLUNTEERED key is also in these derivations. That is
    the proof the connected session and the local wallet are the same wallet.
    Without it, a WalletConnect session could be silently relabelled with a local
    wallet's identity, which would show one person another person's locks.
    """
    keys = {strip0x(str(entry.get("public_key") or "")): entry for entry in derivations}
    if strip0x(volunteered) not in keys:
        return None
    for key, entry in keys.items():
        if key in wanted:
            return entry
    return None


def _match_within_wallet(args, volunteered: str, wanted: set[str]) -> dict | None:
    """An owner key held by the same wallet that volunteered ``volunteered``.

    Returns None unless BOTH keys are found in the local wallet's derivations:
    the volunteered one, which proves the connected session is this wallet, and a
    candidate, which is the identity to report. Anything less and the answer is
    left alone.
    """
    try:
        from sage_rpc import SageRPC

        sage = SageRPC(host=args.host, port=args.port)
        response = sage.call(
            "get_derivations", {"offset": 0, "limit": max(1, int(args.window)), "hardened": False})
        derivations = response.get("derivations") or []
    except Exception:                                          # noqa: BLE001
        return None

    entry = choose_owner_derivation(derivations, volunteered, wanted)
    if entry is not None:
        key = strip0x(str(entry.get("public_key") or ""))
        if True:
            hrp = HRP[args.network]
            derived = address_for(key, hrp)
            sage_address = str(entry.get("address") or "")
            return {
                "success": True,
                "source": "sage-rpc",
                "public_key": key,
                "address": sage_address or derived,
                "derived_address": derived,
                "address_agrees": (not sage_address) or sage_address == derived,
                "index": entry.get("index", 0),
                "matched": True,
                "searched": len(derivations),
                "fingerprint": _logged_in_fingerprint(sage),
            }
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9257)
    parser.add_argument("--network", default="testnet11", choices=sorted(HRP))
    parser.add_argument("--pubkey", default="")
    parser.add_argument(
        "--match", default="",
        help="comma-separated candidate public keys; report whichever of them this wallet holds")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help="how many unhardened derivations to search for a match")
    args = parser.parse_args()
    hrp = HRP[args.network]

    wanted = {strip0x(part) for part in args.match.split(",") if strip0x(part)}

    try:
        if args.pubkey:
            pubkey = strip0x(args.pubkey)
            # A key the wallet volunteered, usually its first. If it is not an
            # owner, the same wallet may still hold one at another derivation --
            # but substituting takes proof that it IS the same wallet, or a
            # WalletConnect session could be relabelled with a local wallet's
            # identity. The proof is finding the volunteered key in the local
            # wallet's own derivations.
            if wanted and pubkey not in wanted:
                swapped = _match_within_wallet(args, pubkey, wanted)
                if swapped is not None:
                    swapped["substituted_for"] = pubkey
                    print(json.dumps(swapped))
                    return 0
            print(json.dumps({
                "success": True, "source": "derived", "public_key": pubkey,
                "address": address_for(pubkey, hrp),
                "matched": pubkey in wanted,
            }))
            return 0

        from sage_rpc import SageRPC

        sage = SageRPC(host=args.host, port=args.port)
        limit = max(1, int(args.window)) if wanted else 1
        response = sage.call("get_derivations", {"offset": 0, "limit": limit, "hardened": False})
        derivations = response.get("derivations") or []
        if not derivations:
            raise RuntimeError("Sage returned no derivations; is a wallet logged in?")

        first = derivations[0]
        matched = False
        if wanted:
            for entry in derivations:
                if strip0x(str(entry.get("public_key") or "")) in wanted:
                    first, matched = entry, True
                    break

        pubkey = strip0x(str(first.get("public_key") or ""))
        if len(pubkey) != 96:
            raise RuntimeError("Sage derivation did not include a public key")
        # Sage's own address for the key is authoritative; ours must agree, and
        # if it does not, say so rather than show two different addresses.
        derived = address_for(pubkey, hrp)
        sage_address = str(first.get("address") or "")
        print(json.dumps({
            "success": True,
            "source": "sage-rpc",
            "public_key": pubkey,
            "address": sage_address or derived,
            "derived_address": derived,
            "address_agrees": (not sage_address) or sage_address == derived,
            "index": first.get("index", 0),
            # True when this key was found among the candidates, so the caller
            # knows the identity is an owner's rather than just the first key.
            "matched": matched,
            "searched": len(derivations),
            # Which wallet answered. A caller comparing this against the
            # connected session's fingerprint learns whether the local Sage and
            # the WalletConnect session are the same wallet, which is what makes
            # it safe to read coins from here instead of over the relay.
            "fingerprint": _logged_in_fingerprint(sage),
        }))
        return 0
    except Exception as exc:  # noqa: BLE001 — one JSON line either way
        print(json.dumps({"success": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
