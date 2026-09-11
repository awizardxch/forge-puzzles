#!/usr/bin/env python3
"""Give a lock a DID, through the locally connected Sage wallet.

A lock cannot mint its own DID: a DID is a singleton launched by a wallet spend,
and the lock has no key of its own to sign one. Sage does have those functions,
so this creates the DID in the connected wallet and then transfers it to the
lock's deposit address, which is what actually makes the lock its owner. Both
steps go through Sage; nothing here holds a key.

Two steps, not one, because Sage's `create_did` always mints to the wallet. The
transfer is what matters: after it, the DID's inner puzzle is the lock's deposit
puzzle, so spending it needs the lock's own singleton to authorise, exactly like
the lock's XCH and CATs. They are separate transactions because the DID has to
exist before it can be moved.

Nothing here signs or broadcasts. Sage is asked to BUILD (`auto_submit: false`)
and the coin spends go back to the caller for the wallet to sign, so the user
sees and approves every spend. Sage's RPC would happily sign and push on its own
-- a client holding its certificate needs no approval -- which is exactly why
this does not ask it to.

    stdin:  {"action": "build-create",   "name": "...", "fee": 0, "host": ..., "port": ...}
            {"action": "build-transfer", "did_ids": [...], "address": "txch1...", "fee": 0}
            {"action": "list", "host": ..., "port": ...}
    stdout: one JSON object; `success` false with `error` on failure.

WHOSE WALLET: the Sage this talks to is whichever wallet is connected to the
host running it. On a developer's machine that is their own. In a hosted
deployment it would be the operator's, and the DID would be paid for and minted
by the operator before landing on the lock -- which is why the route in front of
this is gated and says so.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from sage_rpc import SageRPC  # noqa: E402


def _sage(payload: dict) -> SageRPC:
    return SageRPC(host=str(payload.get("host") or "127.0.0.1"),
                   port=int(payload.get("port") or 9257))


def _did_ids(rows) -> list[str]:
    out = []
    for row in rows or []:
        did = row.get("launcher_id") or row.get("did_id") or row.get("id")
        if did:
            out.append(str(did))
    return out


def list_dids(payload: dict) -> dict:
    sage = _sage(payload)
    result = sage.call("get_dids", {})
    rows = result.get("dids") if isinstance(result, dict) else result
    return {"success": True, "dids": rows or []}


def build_create(payload: dict) -> dict:
    """Unsigned spends that mint a DID. Nothing is broadcast here.

    `auto_submit: False` is what makes Sage hand back the coin spends instead of
    signing and pushing them itself. The wallet signs them, so the user sees the
    request and approves it.
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("a name is required")
    fee = int(payload.get("fee") or 0)
    built = _sage(payload).call("create_did", {"name": name, "fee": fee, "auto_submit": False})
    spends = built.get("coin_spends") if isinstance(built, dict) else None
    if not spends:
        raise ValueError("Sage returned no coin spends for the mint")
    return {"success": True, "coin_spends": spends,
            "summary": built.get("summary") if isinstance(built, dict) else None}


def build_transfer(payload: dict) -> dict:
    """Unsigned spends that move DIDs to an address. Nothing is broadcast here."""
    did_ids = [str(x) for x in (payload.get("did_ids") or []) if str(x).strip()]
    if not did_ids:
        raise ValueError("at least one did_id is required")
    address = str(payload.get("address") or "").strip()
    if not address:
        raise ValueError("a destination address is required")
    fee = int(payload.get("fee") or 0)
    built = _sage(payload).call("transfer_dids",
                                {"did_ids": did_ids, "address": address, "fee": fee, "auto_submit": False})
    spends = built.get("coin_spends") if isinstance(built, dict) else None
    if not spends:
        raise ValueError("Sage returned no coin spends for the transfer")
    return {"success": True, "coin_spends": spends, "address": address, "did_ids": did_ids,
            "summary": built.get("summary") if isinstance(built, dict) else None}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        action = str(payload.get("action", "")).lower()
        if action == "list":
            print(json.dumps(list_dids(payload)))
        elif action == "build-create":
            print(json.dumps(build_create(payload)))
        elif action == "build-transfer":
            print(json.dumps(build_transfer(payload)))
        else:
            raise ValueError("action must be list, build-create or build-transfer")
        return 0
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
