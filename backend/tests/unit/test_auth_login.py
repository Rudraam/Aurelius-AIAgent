from __future__ import annotations

import base64
import hashlib
import logging
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from app.integrations.questrade.auth import (
    READ_SCOPES,
    begin_login,
    complete_login,
    pkce_pair,
)
from app.integrations.questrade.errors import OAuthProtocolError
from app.integrations.questrade.token_store import InMemoryTokenStore
from tests.fakes import FakeHttp
from tests.unit.conftest import CLIENT_ID, ISSUER, REDIRECT, TOKEN_URL

TOKEN_BODY = {
    "access_token": "ACCESS-login-secret",
    "token_type": "Bearer",
    "expires_in": 1800,
    "refresh_token": "REFRESH-login-secret",
    "scope": "offline_access mcp:read",
}


@pytest.fixture
def fake() -> FakeHttp:
    f = FakeHttp()
    f.route("POST", TOKEN_URL, httpx2.Response(200, json=TOKEN_BODY))
    return f


def _callback(pending, **over):
    cb = {"code": "AUTHCODE-secret-123", "state": pending.state, "iss": ISSUER}
    cb.update(over)
    return cb


def test_pkce_s256_challenge_matches_verifier():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def test_authorize_url_requests_only_read_scopes(meta, client_reg):
    pending = begin_login(meta, client_reg, CLIENT_ID, REDIRECT)
    q = {k: v[0] for k, v in parse_qs(urlsplit(pending.authorize_url).query).items()}
    assert q["code_challenge_method"] == "S256"
    assert q["resource"] == meta.prm.resource
    assert q["state"] == pending.state
    scopes = q["scope"].split()
    assert scopes == list(READ_SCOPES)
    assert not any(s.endswith(".all") or s == "mcp:write" for s in scopes)


def test_unregistered_redirect_uri_rejected(meta, client_reg):
    with pytest.raises(OAuthProtocolError):
        begin_login(meta, client_reg, CLIENT_ID, "https://evil.example/cb")


async def test_state_mismatch_rejected_and_no_token_request(meta, client_reg, clock, fake):
    store = InMemoryTokenStore()
    pending = begin_login(meta, client_reg, CLIENT_ID, REDIRECT)
    async with fake.client() as http:
        with pytest.raises(OAuthProtocolError, match="state mismatch") as ei:
            await complete_login(
                http, meta, client_reg, CLIENT_ID, pending, _callback(pending, state="attacker"), store, clock
            )
    assert fake.count("POST", TOKEN_URL) == 0
    assert (await store.load()).tokens is None
    assert pending.state not in str(ei.value)


async def test_missing_state_rejected(meta, client_reg, clock, fake):
    pending = begin_login(meta, client_reg, CLIENT_ID, REDIRECT)
    cb = _callback(pending)
    del cb["state"]
    async with fake.client() as http:
        with pytest.raises(OAuthProtocolError):
            await complete_login(http, meta, client_reg, CLIENT_ID, pending, cb, InMemoryTokenStore(), clock)
    assert fake.count("POST", TOKEN_URL) == 0


async def test_issuer_mismatch_rejected(meta, client_reg, clock, fake):
    pending = begin_login(meta, client_reg, CLIENT_ID, REDIRECT)
    async with fake.client() as http:
        with pytest.raises(OAuthProtocolError, match="issuer"):
            await complete_login(
                http,
                meta,
                client_reg,
                CLIENT_ID,
                pending,
                _callback(pending, iss="https://evil.example"),
                InMemoryTokenStore(),
                clock,
            )
    assert fake.count("POST", TOKEN_URL) == 0


async def test_successful_login_persists_and_sends_verifier(meta, client_reg, clock, fake):
    store = InMemoryTokenStore()
    pending = begin_login(meta, client_reg, CLIENT_ID, REDIRECT)
    async with fake.client() as http:
        saved = await complete_login(http, meta, client_reg, CLIENT_ID, pending, _callback(pending), store, clock)
    form = parse_qs(fake.calls[("POST", TOKEN_URL)][-1].content.decode())
    assert form["code_verifier"] == [pending.code_verifier]
    assert form["resource"] == [meta.prm.resource]
    assert saved.version == 1 and saved.client == client_reg
    assert saved.tokens is not None and saved.tokens.refresh_token == "REFRESH-login-secret"
    assert saved.tokens.lifetime_s == 1800


async def test_login_never_logs_or_reprs_secrets(meta, client_reg, clock, fake, caplog):
    caplog.set_level(logging.DEBUG)
    pending = begin_login(meta, client_reg, CLIENT_ID, REDIRECT)
    cb = _callback(pending)
    async with fake.client() as http:
        saved = await complete_login(http, meta, client_reg, CLIENT_ID, pending, cb, InMemoryTokenStore(), clock)
    secrets_ = [pending.code_verifier, pending.state, cb["code"], "ACCESS-login-secret", "REFRESH-login-secret"]
    text = caplog.text + repr(pending) + repr(saved)
    assert caplog.records, "expected at least one log line from the login"
    for s in secrets_:
        assert s not in text
