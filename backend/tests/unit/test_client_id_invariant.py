"""Invariant: every authorize, token and refresh request carries exactly the configured CIMD URL as
client_id. Any other client_id, anywhere, fails before a request is sent. DCR is gone.

Background: on 2026-11-09 Questrade's DCR endpoint answered Aurelius with Claude Code's own client
(``claude-code-public``). Aurelius must never operate under another application's client_id.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from app.cli import main as cli_main
from app.integrations.questrade.auth import (
    AuthServerMetadata,
    CimdClientId,
    ReadBearerAuth,
    TokenManager,
    _token_request,
    begin_login,
    cimd_document,
    cimd_registration,
    complete_login,
)
from app.integrations.questrade.errors import ClientIdMismatch, OAuthProtocolError
from app.integrations.questrade.token_store import (
    AuthState,
    ClientRegistration,
    InMemoryTokenStore,
    NonCimdStateError,
    state_from_dict,
)
from tests.fakes import FakeHttp
from tests.unit.conftest import CIMD_URL, CLIENT_ID, ISSUER, MCP_URL, REDIRECT, TOKEN_URL, logged_in_store

FOREIGN_IDS = [
    "claude-code-public",  # what Questrade's DCR shim handed back
    "https://aurelius.example.test/oauth/client-metadata.json/",  # trailing slash: not the same URL
    "https://AURELIUS.example.test/oauth/client-metadata.json",  # case differs: exact match only
    "https://evil.example/oauth/client-metadata.json",
    "",
]
TOKEN_BODY = {"access_token": "A", "token_type": "Bearer", "expires_in": 1800, "refresh_token": "R2"}


def token_fake() -> FakeHttp:
    fake = FakeHttp()
    fake.route("POST", TOKEN_URL, httpx2.Response(200, json=TOKEN_BODY))
    return fake


def foreign_client(cid: str) -> ClientRegistration:
    return ClientRegistration(client_id=cid, issuer=ISSUER, redirect_uris=(REDIRECT,))


# --------------------------------------------------------------------------- the configured URL itself


@pytest.mark.parametrize(
    "bad",
    [
        "http://aurelius.example.test/oauth/client-metadata.json",  # not https
        "https://aurelius.example.test/",  # root path
        "https://aurelius.example.test",  # no path
        "https://aurelius.example.test/oauth/client-metadata.json?x=1",  # query
        "https://aurelius.example.test/oauth/client-metadata.json#frag",  # fragment
        "https://user:pw@aurelius.example.test/oauth/client-metadata.json",  # credentials
        "claude-code-public",  # not a URL at all
    ],
)
def test_cimd_client_id_rejects_invalid_urls(bad):
    with pytest.raises(ValueError):
        CimdClientId(bad)


def test_production_client_id_is_valid():
    CimdClientId("https://aurelius.rivenpath.ai/oauth/client-metadata.json")


# --------------------------------------------------------------------------- happy path, observed on the wire


async def test_every_request_on_the_wire_uses_the_configured_client_id(meta, client_reg, clock):
    """Full login, a forced refresh, and a 401-triggered refresh: inspect every request actually sent."""
    fake = token_fake()
    fake.route(
        "POST",
        MCP_URL,
        lambda r: httpx2.Response(
            200 if r.headers.get("authorization") == "Bearer A" else 401,
            headers={"www-authenticate": 'Bearer error="invalid_token"'},
        ),
    )
    store = InMemoryTokenStore()

    pending = begin_login(meta, client_reg, CLIENT_ID, REDIRECT)
    cb = {"code": "c", "state": pending.state, "iss": ISSUER}
    async with fake.client() as http:
        await complete_login(http, meta, client_reg, CLIENT_ID, pending, cb, store, clock)
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        await mgr.refresh(stale_access_token=None)
        clock.advance(3600)  # expired: next request refreshes first
        async with httpx2.AsyncClient(auth=ReadBearerAuth(mgr), transport=fake.transport()) as mcp:
            await mcp.post(MCP_URL, json={})

    authorize_ids = parse_qs(urlsplit(pending.authorize_url).query)["client_id"]
    assert authorize_ids == [CIMD_URL]
    token_calls = fake.calls[("POST", TOKEN_URL)]
    assert len(token_calls) == 3  # code exchange + forced refresh + expiry refresh
    for req in token_calls:
        form = parse_qs(req.content.decode())
        assert form["client_id"] == [CIMD_URL]
        assert "client_secret" not in form
        assert "authorization" not in req.headers  # public client: no client authentication
    assert (await store.load()).client == client_reg


# --------------------------------------------------------------------------- every chokepoint refuses, nothing sent


@pytest.mark.parametrize("foreign", FOREIGN_IDS)
def test_authorize_refuses_foreign_client(meta, foreign):
    with pytest.raises(ClientIdMismatch):
        begin_login(meta, foreign_client(foreign), CLIENT_ID, REDIRECT)


@pytest.mark.parametrize("foreign", FOREIGN_IDS)
async def test_code_exchange_refuses_foreign_client(meta, client_reg, clock, foreign):
    pending = begin_login(meta, client_reg, CLIENT_ID, REDIRECT)
    fake, store = token_fake(), InMemoryTokenStore()
    async with fake.client() as http:
        with pytest.raises(ClientIdMismatch):
            await complete_login(
                http,
                meta,
                foreign_client(foreign),
                CLIENT_ID,
                pending,
                {"code": "c", "state": pending.state, "iss": ISSUER},
                store,
                clock,
            )
    assert fake.count("POST", TOKEN_URL) == 0
    assert (await store.load()).tokens is None


@pytest.mark.parametrize("foreign", FOREIGN_IDS)
async def test_refresh_refuses_foreign_stored_client(clock, foreign):
    store = logged_in_store(clock, foreign_client(foreign))
    fake = token_fake()
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        with pytest.raises(ClientIdMismatch):
            await mgr.refresh(stale_access_token=None)
        clock.advance(3600)
        with pytest.raises(ClientIdMismatch):
            await mgr.access_token()  # expiry-driven refresh path
    assert fake.count("POST", TOKEN_URL) == 0


async def test_401_driven_refresh_refuses_foreign_stored_client(clock):
    store = logged_in_store(clock, foreign_client("claude-code-public"))
    fake = token_fake()
    fake.route("POST", MCP_URL, httpx2.Response(401, headers={"www-authenticate": 'Bearer error="invalid_token"'}))
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ReadBearerAuth(mgr), transport=fake.transport()) as mcp:
            with pytest.raises(ClientIdMismatch):
                await mcp.post(MCP_URL, json={})
    assert fake.count("POST", TOKEN_URL) == 0


async def test_reconfigured_client_id_does_not_reuse_old_registration(clock, client_reg):
    """Tokens stored under one CIMD URL are never refreshed under another, even a valid one."""
    store = logged_in_store(clock, client_reg)
    other = CimdClientId("https://other.example.test/oauth/client-metadata.json")
    fake = token_fake()
    async with fake.client() as http:
        with pytest.raises(ClientIdMismatch):
            await TokenManager(store, http, TOKEN_URL, MCP_URL, other, clock=clock).refresh(stale_access_token=None)
    assert fake.count("POST", TOKEN_URL) == 0


@pytest.mark.parametrize("form_client_id", [*FOREIGN_IDS, None])
async def test_token_request_chokepoint_refuses_any_other_client_id(clock, form_client_id):
    form = {"grant_type": "refresh_token", "refresh_token": "R1"}
    if form_client_id is not None:
        form["client_id"] = form_client_id
    fake = token_fake()
    async with fake.client() as http:
        with pytest.raises(ClientIdMismatch):
            await _token_request(http, TOKEN_URL, CLIENT_ID, form, clock, previous=None)
    assert fake.count("POST", TOKEN_URL) == 0


# --------------------------------------------------------------------------- CIMD registration


async def test_cimd_registration_requires_exact_document_client_id(meta):
    for published in FOREIGN_IDS:
        fake = FakeHttp()
        fake.route("GET", CIMD_URL, httpx2.Response(200, json=cimd_document(published, [REDIRECT])))
        async with fake.client() as http:
            with pytest.raises(ClientIdMismatch):
                await cimd_registration(http, meta.asm, CLIENT_ID)


async def test_cimd_registration_requires_server_cimd_support(meta):
    no_cimd = AuthServerMetadata(**{**meta.asm.model_dump(), "client_id_metadata_document_supported": False})
    fake = FakeHttp()
    async with fake.client() as http:
        with pytest.raises(OAuthProtocolError, match="CIMD"):
            await cimd_registration(http, no_cimd, CLIENT_ID)
    assert fake.count("GET", CIMD_URL) == 0


async def test_cimd_registration_happy_path(meta):
    fake = FakeHttp()
    fake.route("GET", CIMD_URL, httpx2.Response(200, json=cimd_document(CIMD_URL, [REDIRECT])))
    async with fake.client() as http:
        reg = await cimd_registration(http, meta.asm, CLIENT_ID)
    assert reg == ClientRegistration(client_id=CIMD_URL, issuer=ISSUER, redirect_uris=(REDIRECT,))


# --------------------------------------------------------------------------- stored state


@pytest.mark.parametrize(
    "client",
    [
        {"client_id": "claude-code-public", "strategy": "dcr", "issuer": ISSUER, "redirect_uris": [REDIRECT]},
        {"client_id": CIMD_URL, "issuer": ISSUER, "redirect_uris": [REDIRECT], "client_secret": "s3cret"},
    ],
)
def test_non_cimd_stored_state_is_refused(client):
    with pytest.raises(NonCimdStateError):
        state_from_dict({"version": 1, "client": client, "tokens": None})


def test_cimd_stored_state_loads():
    s = state_from_dict(
        {"version": 1, "client": {"client_id": CIMD_URL, "issuer": ISSUER, "redirect_uris": [REDIRECT]}, "tokens": None}
    )
    assert s == AuthState(version=1, client=ClientRegistration(CIMD_URL, ISSUER, (REDIRECT,)))


# --------------------------------------------------------------------------- CLI


@pytest.mark.parametrize("cmd", ["login", "refresh"])
def test_cli_errors_clearly_without_cimd_url_before_any_io(monkeypatch, cmd):
    monkeypatch.delenv("AURELIUS_CIMD_CLIENT_ID_URL", raising=False)
    monkeypatch.delenv("AURELIUS_ENCRYPTION_KEY", raising=False)  # proves the CIMD check comes first

    async def no_network(*a, **k):
        raise AssertionError("network used before the CIMD check")

    monkeypatch.setattr("app.cli.discover", no_network)
    with pytest.raises(SystemExit) as ei:
        cli_main(["auth", cmd])
    assert "AURELIUS_CIMD_CLIENT_ID_URL is not set" in str(ei.value)
    assert "CIMD" in str(ei.value)


def test_cli_rejects_invalid_cimd_url(monkeypatch):
    monkeypatch.setenv("AURELIUS_CIMD_CLIENT_ID_URL", "http://aurelius.rivenpath.ai/oauth/client-metadata.json")
    with pytest.raises(SystemExit) as ei:
        cli_main(["auth", "login"])
    assert "must be https" in str(ei.value)


@pytest.mark.parametrize("argv", [["auth", "forget-dcr"], ["auth", "login", "--strategy", "dcr"]])
def test_dcr_cli_surface_is_gone(argv, capsys):
    with pytest.raises(SystemExit) as ei:
        cli_main(argv)
    assert ei.value.code == 2  # argparse: unknown command / unrecognized argument


# --------------------------------------------------------------------------- DCR removed from the code


def test_no_dcr_code_remains_in_app():
    app = Path(__file__).resolve().parents[2] / "app"
    forbidden = re.compile(r"registration_endpoint|def dcr_|dcr_register|forget[-_]dcr|--strategy|choose_strategy")
    hits = [
        f"{p.relative_to(app)}:{i}"
        for p in app.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if forbidden.search(line)
    ]
    assert hits == []
