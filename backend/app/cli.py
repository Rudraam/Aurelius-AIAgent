"""`aurelius` command line. Phase 1 so far: `aurelius auth login|status|refresh`.

Environment:
  AURELIUS_ENCRYPTION_KEY       base64 32-byte key (generate with `aurelius auth genkey`)
  QUESTRADE_MCP_URL             default https://mcp.questrade.com/v1/brokerage/mcp
  AURELIUS_CIMD_CLIENT_ID_URL   https://<final-domain>/oauth/client-metadata.json (enables CIMD)
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
    TokenManager,
    begin_login,
    choose_strategy,
    cimd_registration,
    complete_login,
    dcr_register,
    discover,
)
from app.integrations.questrade.errors import QuestradeError
from app.integrations.questrade.token_store import AuthState, EncryptedFileTokenStore, Strategy, utcnow

DEFAULT_MCP_URL = "https://mcp.questrade.com/v1/brokerage/mcp"
LOOPBACK_PORT = 8765
LOOPBACK_REDIRECT = f"http://127.0.0.1:{LOOPBACK_PORT}/callback"
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / ".spike" / "auth.enc"


def mcp_url() -> str:
    return os.environ.get("QUESTRADE_MCP_URL", DEFAULT_MCP_URL)


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


async def login(strategy_arg: str, scopes: list[str]) -> None:
    store = open_store()
    cimd_url = os.environ.get("AURELIUS_CIMD_CLIENT_ID_URL")
    async with httpx2.AsyncClient(timeout=30) as http:
        meta = await discover(http, mcp_url())
        strategy: Strategy
        if strategy_arg == "auto":
            strategy = choose_strategy(meta.asm, cimd_url)
        elif strategy_arg == "cimd":
            strategy = "cimd"
        else:
            strategy = "dcr"
        existing = (await store.load()).client
        if existing is not None and existing.strategy == strategy and existing.issuer == meta.asm.issuer:
            client = existing  # reuse a DCR registration instead of registering again
        elif strategy == "cimd":
            if not cimd_url:
                sys.exit("CIMD needs AURELIUS_CIMD_CLIENT_ID_URL (the hosted static document).")
            client = await cimd_registration(http, meta.asm, cimd_url)
            if existing is not None and existing.strategy == "dcr":
                print(
                    f"NOTE: a successful CIMD login replaces the stored DCR client {existing.client_id}. "
                    "Afterwards, revoke the DCR grant in Questrade (see docs/runbooks/dcr-to-cimd.md)."
                )
        else:
            client = await dcr_register(http, meta.asm, [LOOPBACK_REDIRECT], scopes)
        print(f"strategy={client.strategy} client_id={client.client_id}")

        pending = begin_login(meta, client, LOOPBACK_REDIRECT, scopes)
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
        state = await complete_login(http, meta, client, pending, _Callback.params, store)
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
                "strategy": s.client.strategy if s.client else None,
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


async def forget_dcr(confirmed: bool) -> None:
    """Delete stored DCR client credentials (and the tokens bound to them). Never touches a CIMD client."""
    store = open_store()
    state = await store.load()
    if state.client is None or state.client.strategy != "dcr":
        print("No DCR client stored; nothing to delete.")
        return
    if not confirmed:
        sys.exit(f"Would delete DCR client {state.client.client_id} and its tokens. Re-run with --yes.")
    await store.save(AuthState(), expected_version=state.version)
    print(f"Deleted DCR client {state.client.client_id} and its tokens from the local store.")


async def refresh() -> None:
    store = open_store()
    async with httpx2.AsyncClient(timeout=30) as http:
        meta = await discover(http, mcp_url())
        mgr = TokenManager(store, http, meta.asm.token_endpoint, meta.prm.resource)
        await mgr.refresh(stale_access_token=None)
    await status()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aurelius")
    sub = ap.add_subparsers(dest="group", required=True)
    auth = sub.add_parser("auth").add_subparsers(dest="cmd", required=True)
    p = auth.add_parser("login")
    p.add_argument("--strategy", choices=["auto", "cimd", "dcr"], default="auto")
    p.add_argument("--scopes", help="space-separated override; default is the read-only set")
    auth.add_parser("status")
    auth.add_parser("refresh")
    auth.add_parser("genkey")
    p = auth.add_parser("forget-dcr", help="delete stored DCR client credentials and their tokens")
    p.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "genkey":
            print(generate_key_b64())
        elif a.cmd == "login":
            asyncio.run(login(a.strategy, a.scopes.split() if a.scopes else list(READ_SCOPES)))
        elif a.cmd == "status":
            asyncio.run(status())
        elif a.cmd == "refresh":
            asyncio.run(refresh())
        elif a.cmd == "forget-dcr":
            asyncio.run(forget_dcr(a.yes))
    except QuestradeError as e:
        sys.exit(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
