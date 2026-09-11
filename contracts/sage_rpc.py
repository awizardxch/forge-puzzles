"""
sage_rpc.py — Sage Wallet RPC client for CFMM pool deployment.

Connects to Sage's local mTLS HTTPS RPC on 127.0.0.1:9257.
Certs auto-detected from %APPDATA%/com.rigidnetwork.sage/ssl/ (Windows).

Usage:
    from contracts.sage_rpc import SageRPC

    sage = SageRPC()
    cats = sage.call("get_cats")
    coins = sage.call("get_coins", {"limit": 50})
"""

from __future__ import annotations

import json
import os
import platform
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _sage_data_dir() -> Path:
    """Return the OS-appropriate Sage data directory."""
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    return base / "com.rigidnetwork.sage"


class SageRPC:
    """Synchronous Sage wallet RPC client using mTLS."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9257,
        cert_path: str | Path | None = None,
        key_path: str | Path | None = None,
        timeout: int = 30,
    ) -> None:
        sage_dir = _sage_data_dir() / "ssl"
        self.cert_path = Path(cert_path) if cert_path else sage_dir / "wallet.crt"
        self.key_path = Path(key_path) if key_path else sage_dir / "wallet.key"
        self.base_url = f"https://{host}:{port}"
        self.timeout = timeout

        if not self.cert_path.exists() or not self.key_path.exists():
            raise FileNotFoundError(
                f"[aWizard] Sage SSL certs not found at {sage_dir}\n"
                "Ensure Sage is installed and RPC is enabled."
            )

        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        self._ctx.load_cert_chain(
            certfile=str(self.cert_path),
            keyfile=str(self.key_path),
        )

    def call(self, endpoint: str, body: dict[str, Any] | None = None) -> Any:
        """Call a Sage RPC endpoint. Returns parsed JSON response."""
        url = f"{self.base_url}/{endpoint}"
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=self.timeout) as resp:
                text = resp.read().decode()
                if resp.status != 200:
                    raise RuntimeError(f"[aWizard] Sage RPC {endpoint!r} HTTP {resp.status}: {text}")
                return json.loads(text)
        except urllib.error.HTTPError as exc:
            error_text = exc.read().decode(errors="replace")
            raise RuntimeError(f"[aWizard] Sage RPC {endpoint!r} HTTP {exc.code}: {error_text}") from exc

    def get_sync_status(self) -> dict[str, Any]:
        return self.call("get_sync_status")

    def get_cats(self) -> list[dict[str, Any]]:
        return self.call("get_cats")["cats"]

    def get_coins(self, asset_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        body: dict[str, Any] = {"limit": limit, "offset": 0}
        if asset_id is not None:
            body["asset_id"] = asset_id
        return self.call("get_coins", body)

    def get_keys(self) -> list[dict[str, Any]]:
        return self.call("get_keys")["keys"]

    def login(self, fingerprint: int) -> dict[str, Any]:
        return self.call("login", {"fingerprint": fingerprint})

    def sign_coin_spends(self, coin_spends: list[dict]) -> dict[str, Any]:
        return self.call("sign_coin_spends", {"coin_spends": coin_spends})

    def submit_transaction(self, spend_bundle: dict) -> dict[str, Any]:
        return self.call("submit_transaction", {"spend_bundle": spend_bundle})

    def make_offer(
        self,
        offered_assets: list[dict[str, Any]],
        requested_assets: list[dict[str, Any]],
        fee: int | str = 0,
    ) -> dict[str, Any]:
        return self.call("make_offer", {
            "offered_assets": offered_assets,
            "requested_assets": requested_assets,
            "fee": fee,
        })

    def import_offer(self, offer: str) -> dict[str, Any]:
        return self.call("import_offer", {"offer": offer})

    def cancel_offer(self, offer: str, fee: int | str = 0) -> dict[str, Any]:
        return self.call("cancel_offer", {"offer": offer, "fee": int(fee)})

    def take_offer(self, offer: str, fee: int | str = 0) -> dict[str, Any]:
        return self.call("take_offer", {
            "offer": offer,
            "fee": fee,
        })

    def send_xch(
        self,
        address: str,
        amount: int,
        fee: int = 0,
    ) -> dict[str, Any]:
        return self.call("send_xch", {
            "address": address,
            "amount": amount,
            "fee": fee,
        })