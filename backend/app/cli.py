"""`aurelius` command line. Phase 1 so far: `aurelius auth login|status|refresh|genkey`.

Environment:
  AURELIUS_ENCRYPTION_KEY       base64 32-byte key (generate with `aurelius auth genkey`)
  AURELIUS_CIMD_CLIENT_ID_URL   REQUIRED for login/refresh: https://aurelius.rivenpath.ai/oauth/client-metadata.json
                                (the permanent CIMD client_id; CIMD is the only sign-in strategy)
  QUESTRADE_MCP_URL             default https://mcp.questrade.com/v1/brokerage/mcp
  AURELIUS_AUTH_STATE_PATH      encrypted auth state file (dev store until the Postgres store lands)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import httpx2

from app.crypto import SecretBox, generate_key_b64
from app.integrations.questrade.auth import (
    READ_SCOPES,
    CimdClientId,
    TokenManager,
    begin_login,
    cimd_registration,
    complete_login,
    discover,
)
from app.integrations.questrade.errors import QuestradeError
from app.integrations.questrade.token_store import EncryptedFileTokenStore, utcnow

DEFAULT_MCP_URL = "https://mcp.questrade.com/v1/brokerage/mcp"
CIMD_ENV = "AURELIUS_CIMD_CLIENT_ID_URL"
LOOPBACK_PORT = 8765
LOOPBACK_REDIRECT = f"http://127.0.0.1:{LOOPBACK_PORT}/callback"
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / ".spike" / "auth.enc"


def mcp_url() -> str:
    return os.environ.get("QUESTRADE_MCP_URL", DEFAULT_MCP_URL)


def configured_client_id() -> CimdClientId:
    """The CIMD client_id from the environment. Exits with a clear message if missing or invalid."""
    raw = os.environ.get(CIMD_ENV)
    if not raw:
        sys.exit(
            f"{CIMD_ENV} is not set. Aurelius signs in only with CIMD; set it to the published document URL, e.g.\n"
            f'  $env:{CIMD_ENV} = "https://aurelius.rivenpath.ai/oauth/client-metadata.json"'
        )
    try:
        return CimdClientId(raw)
    except ValueError as e:
        sys.exit(f"{CIMD_ENV}: {e}")


def open_store() -> EncryptedFileTokenStore:
    key = os.environ.get("AURELIUS_ENCRYPTION_KEY")
    if not key:
        sys.exit("AURELIUS_ENCRYPTION_KEY is not set (run `aurelius auth genkey`).")
    path = Path(os.environ.get("AURELIUS_AUTH_STATE_PATH", DEFAULT_STATE_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    gitignore = path.parent / ".gitignore"
    if path.parent.name == ".spike" and not gitignore.exists():
        gitignore.write_text("*\n")
    return EncryptedFileTokenStore(path, SecretBox.from_b64(key))


class _Callback(BaseHTTPRequestHandler):
    params: ClassVar[dict[str, str]] = {}
    done: ClassVar[threading.Event] = threading.Event()

    def do_GET(self) -> None:
        u = urlsplit(self.path)
        if u.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        _Callback.params = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<h3>Aurelius: authorization received. You can close this tab.</h3>")
        _Callback.done.set()

    def log_message(self, format: str, *args: Any) -> None:  # the URL carries the code: never log it
        return


async def login(scopes: list[str]) -> None:
    client_id = configured_client_id()  # before any network call or store access
    store = open_store()
    async with httpx2.AsyncClient(timeout=30) as http:
        meta = await discover(http, mcp_url())
        client = await cimd_registration(http, meta.asm, client_id)
        print(f"client_id={client_id.url}")

        pending = begin_login(meta, client, client_id, LOOPBACK_REDIRECT, scopes)
        _Callback.done.clear()
        server = HTTPServer(("127.0.0.1", LOOPBACK_PORT), _Callback)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            print("Opening the Questrade login in your browser. Grant READ permission only.")
            print("If no browser opens, copy this URL into one:\n")
            print(pending.authorize_url + "\n")
            webbrowser.open(pending.authorize_url)
            if not await asyncio.to_thread(_Callback.done.wait, 600):
                sys.exit("Timed out after 10 minutes waiting for the login redirect.")
        finally:
            server.shutdown()
        state = await complete_login(http, meta, client, client_id, pending, _Callback.params, store)
    assert state.tokens is not None
    print(
        f"Logged in. granted scope: {state.tokens.scope!r}; access lifetime: {int(state.tokens.lifetime_s)}s; "
        f"refresh token: {'yes' if state.tokens.refresh_token else 'NO'}"
    )


async def status() -> None:
    s = await open_store().load()
    t = s.tokens
    print(
        json.dumps(
            {
                "version": s.version,
                "client_id": s.client.client_id if s.client else None,
                "granted_scope": t.scope if t else None,
                "has_refresh_token": bool(t and t.refresh_token),
                "access_expires_in_s": int((t.expires_at - utcnow()).total_seconds()) if t else None,
                "access_lifetime_s": int(t.lifetime_s) if t else None,
                "refresh_rotated_at": s.refresh_rotated_at.isoformat() if s.refresh_rotated_at else None,
            },
            indent=2,
        )
    )


async def refresh() -> None:
    client_id = configured_client_id()
    store = open_store()
    async with httpx2.AsyncClient(timeout=30) as http:
        meta = await discover(http, mcp_url())
        mgr = TokenManager(store, http, meta.asm.token_endpoint, meta.prm.resource, client_id)
        await mgr.refresh(stale_access_token=None)
    await status()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aurelius")
    sub = ap.add_subparsers(dest="group", required=True)
    auth = sub.add_parser("auth").add_subparsers(dest="cmd", required=True)
    p = auth.add_parser("login", help=f"CIMD sign-in; requires {CIMD_ENV}")
    p.add_argument("--scopes", help="space-separated override; default is the read-only set")
    auth.add_parser("status")
    auth.add_parser("refresh")
    auth.add_parser("genkey")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "genkey":
            print(generate_key_b64())
        elif a.cmd == "login":
            asyncio.run(login(a.scopes.split() if a.scopes else list(READ_SCOPES)))
        elif a.cmd == "status":
            asyncio.run(status())
        elif a.cmd == "refresh":
            asyncio.run(refresh())
    except QuestradeError as e:
        sys.exit(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
