"""Token lifecycle + the two auth classes (read vs approval)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import httpx2
import pytest

from app.integrations.questrade.auth import ApprovalBearerAuth, ReadBearerAuth, TokenManager
from app.integrations.questrade.errors import AuthExpired, ReauthRequired
from app.integrations.questrade.token_store import AuthState, InMemoryTokenStore
from tests.fakes import FakeHttp
from tests.unit.conftest import CLIENT_ID, MCP_URL, TOKEN_URL, logged_in_store

ROTATED = {
    "access_token": "ACCESS-2-secretvalue",
    "token_type": "Bearer",
    "expires_in": 1800,
    "refresh_token": "REFRESH-2-secretvalue",
}
INVALID_TOKEN = {"www-authenticate": 'Bearer realm="x", error="invalid_token"'}


class SpyStore:
    """Wraps InMemoryTokenStore and writes saves into a shared event log."""

    def __init__(self, inner: InMemoryTokenStore, events: list[tuple[str, str]], fail_on_save: bool = False) -> None:
        self.inner, self.events, self.fail = inner, events, fail_on_save
        self.saves = 0

    async def load(self) -> AuthState:
        return await self.inner.load()

    async def save(self, state: AuthState, *, expected_version: int) -> AuthState:
        if self.fail:
            raise RuntimeError("disk full")
        await asyncio.sleep(0.01)  # a slow durable write widens any ordering bug
        out = await self.inner.save(state, expected_version=expected_version)
        self.saves += 1
        self.events.append(("save", (state.tokens.refresh_token or "") if state.tokens else ""))
        return out

    def refresh_lock(self) -> Any:
        return self.inner.refresh_lock()


async def _slow_rotation(request: httpx2.Request) -> httpx2.Response:
    await asyncio.sleep(0.05)
    return httpx2.Response(200, json=ROTATED)


def setup(
    events: list[tuple[str, str]] | None = None,
    *,
    valid: set[str],
    reject: httpx2.Response | None = None,
    token: Any = None,
) -> FakeHttp:
    """One fake for both hosts: the token endpoint and the MCP endpoint."""
    fake = FakeHttp()
    fake.route("POST", TOKEN_URL, token if token is not None else _slow_rotation)
    log = events if events is not None else []

    def mcp(request: httpx2.Request) -> httpx2.Response:
        auth = request.headers.get("authorization", "")
        log.append(("request", auth))
        if auth.removeprefix("Bearer ") in valid:
            return httpx2.Response(200, json={"ok": True})
        return reject if reject is not None else httpx2.Response(401, headers=INVALID_TOKEN, text="Jwt is expired")

    fake.route("POST", MCP_URL, mcp)
    return fake


# --------------------------------------------------------------------------- refresh core


async def test_concurrent_401s_cause_exactly_one_rotation(clock, client_reg):
    events: list[tuple[str, str]] = []
    inner = logged_in_store(clock, client_reg)
    store = SpyStore(inner, events)
    fake = setup(events, valid={"ACCESS-2-secretvalue"})  # server already revoked ACCESS-1
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ReadBearerAuth(mgr), transport=fake.transport()) as mcp:
            responses = await asyncio.gather(*(mcp.post(MCP_URL, json={"i": i}) for i in range(20)))
    assert all(r.status_code == 200 for r in responses)
    assert fake.count("POST", TOKEN_URL) == 1
    assert store.saves == 1
    final = await inner.load()
    assert final.version == 2
    assert final.tokens is not None and final.tokens.refresh_token == "REFRESH-2-secretvalue"
    assert final.refresh_rotated_at == clock()


async def test_concurrent_expiry_refresh_is_single_flight(clock, client_reg):
    fake = setup(valid=set())
    store = logged_in_store(clock, client_reg, lifetime=60)
    clock.advance(120)
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        tokens = await asyncio.gather(*(mgr.access_token() for _ in range(15)))
    assert set(tokens) == {"ACCESS-2-secretvalue"}
    assert fake.count("POST", TOKEN_URL) == 1


async def test_rotated_refresh_token_saved_before_new_access_token_used(clock, client_reg):
    events: list[tuple[str, str]] = []
    store = SpyStore(logged_in_store(clock, client_reg), events)
    fake = setup(events, valid={"ACCESS-2-secretvalue"})
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ReadBearerAuth(mgr), transport=fake.transport()) as mcp:
            r = await mcp.post(MCP_URL, json={})
    assert r.status_code == 200
    assert events.index(("save", "REFRESH-2-secretvalue")) < events.index(("request", "Bearer ACCESS-2-secretvalue"))


async def test_new_access_token_never_used_if_save_fails(clock, client_reg):
    events: list[tuple[str, str]] = []
    store = SpyStore(logged_in_store(clock, client_reg), events, fail_on_save=True)
    fake = setup(events, valid={"ACCESS-2-secretvalue"})
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ReadBearerAuth(mgr), transport=fake.transport()) as mcp:
            with pytest.raises(RuntimeError, match="disk full"):
                await mcp.post(MCP_URL, json={})
    assert ("request", "Bearer ACCESS-2-secretvalue") not in events


async def test_non_rotating_server_keeps_old_refresh_token(clock, client_reg):
    body = {k: v for k, v in ROTATED.items() if k != "refresh_token"}
    fake = setup(valid=set(), token=httpx2.Response(200, json=body))
    store = logged_in_store(clock, client_reg)
    async with fake.client() as http:
        await TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock).refresh(stale_access_token=None)
    s = await store.load()
    assert s.tokens is not None and s.tokens.refresh_token == "REFRESH-1-secretvalue"
    assert s.refresh_rotated_at is None


async def test_invalid_grant_maps_to_reauth_required(clock, client_reg):
    fake = setup(valid=set(), token=httpx2.Response(400, json={"error": "invalid_grant"}))
    async with fake.client() as http:
        mgr = TokenManager(logged_in_store(clock, client_reg), http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ReadBearerAuth(mgr), transport=fake.transport()) as mcp:
            with pytest.raises(ReauthRequired, match="invalid_grant"):
                await mcp.post(MCP_URL, json={})


async def test_no_tokens_is_reauth_required(clock):
    async with FakeHttp().client() as http:
        with pytest.raises(ReauthRequired):
            await TokenManager(InMemoryTokenStore(), http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock).access_token()


async def test_proactive_refresh_threshold(clock, client_reg):
    async with FakeHttp().client() as http:
        mgr = TokenManager(
            logged_in_store(clock, client_reg, lifetime=1000), http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock
        )
        assert not await mgr.needs_proactive_refresh()
        clock.advance(810)  # 190s of 1000s left (<20%)
        assert await mgr.needs_proactive_refresh()


async def test_refresh_never_logs_tokens(clock, client_reg, caplog):
    caplog.set_level(logging.DEBUG)
    store = logged_in_store(clock, client_reg)
    fake = setup(valid={"ACCESS-2-secretvalue"})
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ReadBearerAuth(mgr), transport=fake.transport()) as mcp:
            await mcp.post(MCP_URL, json={})
    assert "rotated=True" in caplog.text
    text = caplog.text + repr(await store.load())
    for s in ("ACCESS-1-secretvalue", "REFRESH-1-secretvalue", "ACCESS-2-secretvalue", "REFRESH-2-secretvalue"):
        assert s not in text


# --------------------------------------------------------------------------- read auth


async def test_read_retries_once_on_401_invalid_token(clock, client_reg):
    events: list[tuple[str, str]] = []
    fake = setup(events, valid={"ACCESS-2-secretvalue"})
    async with fake.client() as http:
        mgr = TokenManager(logged_in_store(clock, client_reg), http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ReadBearerAuth(mgr), transport=fake.transport()) as mcp:
            r = await mcp.post(MCP_URL, json={})
    assert r.status_code == 200
    assert [e for e in events if e[0] == "request"] == [
        ("request", "Bearer ACCESS-1-secretvalue"),
        ("request", "Bearer ACCESS-2-secretvalue"),
    ]


@pytest.mark.parametrize(
    "www_auth",
    ['Bearer resource_metadata="https://x/.well-known/oauth-protected-resource"', 'Bearer error="invalid_request"', ""],
    ids=["no-error-param", "other-error", "no-header"],
)
async def test_read_does_not_retry_401_without_invalid_token(clock, client_reg, www_auth):
    events: list[tuple[str, str]] = []
    reject = httpx2.Response(401, headers={"www-authenticate": www_auth} if www_auth else {})
    fake = setup(events, valid={"ACCESS-2-secretvalue"}, reject=reject)
    async with fake.client() as http:
        mgr = TokenManager(logged_in_store(clock, client_reg), http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ReadBearerAuth(mgr), transport=fake.transport()) as mcp:
            with pytest.raises(AuthExpired):
                await mcp.post(MCP_URL, json={})
    assert fake.count("POST", MCP_URL) == 1
    assert fake.count("POST", TOKEN_URL) == 0


async def test_read_401_after_refresh_is_reauth_not_a_loop(clock, client_reg):
    fake = setup(valid=set())
    async with fake.client() as http:
        mgr = TokenManager(logged_in_store(clock, client_reg), http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ReadBearerAuth(mgr), transport=fake.transport()) as mcp:
            with pytest.raises(ReauthRequired):
                await mcp.post(MCP_URL, json={})
    assert fake.count("POST", TOKEN_URL) == 1
    assert fake.count("POST", MCP_URL) == 2


@pytest.mark.parametrize("auth_cls", [ReadBearerAuth, ApprovalBearerAuth])
async def test_403_insufficient_scope_maps_to_reauth_with_scope(clock, client_reg, auth_cls):
    reject = httpx2.Response(
        403, headers={"www-authenticate": 'Bearer error="insufficient_scope", scope="mcp:brokerage.orders.all"'}
    )
    fake = setup(valid=set(), reject=reject)
    async with fake.client() as http:
        mgr = TokenManager(logged_in_store(clock, client_reg), http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=auth_cls(mgr), transport=fake.transport()) as mcp:
            with pytest.raises(ReauthRequired) as ei:
                await mcp.post(MCP_URL, json={})
    assert ei.value.scope == "mcp:brokerage.orders.all"
    assert fake.count("POST", MCP_URL) == 1


# --------------------------------------------------------------------------- approval auth


@pytest.mark.parametrize(
    "www_auth", ['Bearer error="invalid_token"', 'Bearer resource_metadata="x"'], ids=["invalid_token", "other"]
)
async def test_approval_401_raises_auth_expired_and_never_resends(clock, client_reg, www_auth):
    reject = httpx2.Response(401, headers={"www-authenticate": www_auth})
    fake = setup(valid={"ACCESS-2-secretvalue"}, reject=reject)
    async with fake.client() as http:
        mgr = TokenManager(logged_in_store(clock, client_reg), http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ApprovalBearerAuth(mgr), transport=fake.transport()) as mcp:
            with pytest.raises(AuthExpired):
                await mcp.post(MCP_URL, json={"method": "tools/call", "params": {"name": "create_order_instruction"}})
    assert fake.count("POST", MCP_URL) == 1  # exactly one attempt
    assert fake.count("POST", TOKEN_URL) == 0  # no refresh-and-retry


async def test_approval_refreshes_first_when_under_5_minutes_left(clock, client_reg):
    events: list[tuple[str, str]] = []
    store = logged_in_store(clock, client_reg, lifetime=1800)
    clock.advance(1800 - 4 * 60)  # 4 minutes left: fine for reads, too little for approval
    fake = setup(events, valid={"ACCESS-1-secretvalue", "ACCESS-2-secretvalue"})
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ApprovalBearerAuth(mgr), transport=fake.transport()) as mcp:
            r = await mcp.post(MCP_URL, json={})
    assert r.status_code == 200
    assert fake.count("POST", TOKEN_URL) == 1
    assert [e for e in events if e[0] == "request"] == [("request", "Bearer ACCESS-2-secretvalue")]


async def test_approval_does_not_refresh_with_plenty_of_time(clock, client_reg):
    store = logged_in_store(clock, client_reg, lifetime=1800)
    clock.advance(1800 - 10 * 60)  # 10 minutes left
    fake = setup(valid={"ACCESS-1-secretvalue"})
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ApprovalBearerAuth(mgr), transport=fake.transport()) as mcp:
            assert (await mcp.post(MCP_URL, json={})).status_code == 200
    assert fake.count("POST", TOKEN_URL) == 0


async def test_approval_pre_refresh_failure_sends_nothing(clock, client_reg):
    store = logged_in_store(clock, client_reg, lifetime=1800)
    clock.advance(1800 - 60)
    fake = setup(valid={"ACCESS-1-secretvalue"}, token=httpx2.Response(503, json={"error": "temporarily_unavailable"}))
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        async with httpx2.AsyncClient(auth=ApprovalBearerAuth(mgr), transport=fake.transport()) as mcp:
            with pytest.raises(AuthExpired):
                await mcp.post(MCP_URL, json={})
    assert fake.count("POST", MCP_URL) == 0


async def test_read_and_approval_min_remaining_differ(clock, client_reg):
    """With 4 minutes left, a read uses the current token; an approval call refreshes first."""
    store = logged_in_store(clock, client_reg, lifetime=1800)
    clock.advance(1800 - 4 * 60)
    fake = setup(valid=set())
    async with fake.client() as http:
        mgr = TokenManager(store, http, TOKEN_URL, MCP_URL, CLIENT_ID, clock=clock)
        assert await mgr.access_token() == "ACCESS-1-secretvalue"
        assert await mgr.access_token(min_remaining=timedelta(minutes=5)) == "ACCESS-2-secretvalue"
    assert fake.count("POST", TOKEN_URL) == 1
