"""Our httpx2 auth classes driving the real MCP SDK client against an in-process MCPServer."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app.domain.order_args import order_args_hash
from app.domain.order_state import OrderDraft, OrderStatus
from app.integrations.questrade.auth import TokenManager
from app.integrations.questrade.errors import AuthExpired, ReauthRequired, find_in_chain
from app.integrations.questrade.token_store import AuthState, ClientRegistration, InMemoryTokenStore, TokenSet
from app.integrations.questrade.transport import approval_session, read_session
from app.services.order_submission import CreateOutcome, SubmitPolicy, submit_draft
from tests.fakes import FakeHttp

MCP_URL = "https://mcp.example.test/mcp"
TOKEN_URL = "https://login.example.test/connect/token"


class FakeQuestrade:
    """MCPServer behind an edge that checks bearer tokens like Questrade's Envoy does."""

    def __init__(self, valid: set[str]) -> None:
        self.valid = valid
        self.seen_auth: list[str] = []
        self.creates_executed = 0
        server = MCPServer("fake-questrade")

        @server.tool()
        def list_accounts() -> dict[str, Any]:
            return {"accounts": [{"id": "00000000-0000-0000-0000-000000000001", "type": "TFSA"}]}

        @server.tool()
        def create_order_instruction(accountId: str, operation: str = "create") -> dict[str, Any]:
            self.creates_executed += 1
            return {"instructionId": "inst-1", "status": "pending_approval"}

        self.inner = server.streamable_http_app(
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            self.seen_auth.append(auth)
            if auth.removeprefix("Bearer ") not in self.valid:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"www-authenticate", b'Bearer realm="x", error="invalid_token"'),
                            (b"content-type", b"text/plain"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": b"Jwt is expired"})
                return
        await self.inner(scope, receive, send)


def store(expired: bool, minutes_left: float = 30) -> InMemoryTokenStore:
    now = datetime.now(UTC)
    return InMemoryTokenStore(
        AuthState(
            version=1,
            client=ClientRegistration(
                client_id="https://aurelius.example.test/oauth/client-metadata.json",
                strategy="cimd",
                issuer="https://login.example.test",
                redirect_uris=("http://127.0.0.1:8765/callback",),
            ),
            tokens=TokenSet(
                access_token="OLD",
                refresh_token="R1",
                expires_at=now + (timedelta(seconds=-5) if expired else timedelta(minutes=minutes_left)),
                obtained_at=now - timedelta(minutes=1),
            ),
        )
    )


def token_fake(response: httpx2.Response | None = None) -> FakeHttp:
    f = FakeHttp()
    f.route(
        "POST",
        TOKEN_URL,
        response
        or httpx2.Response(
            200, json={"access_token": "NEW", "token_type": "Bearer", "expires_in": 1800, "refresh_token": "R2"}
        ),
    )
    return f


@pytest.mark.parametrize("locally_expired", [False, True], ids=["server-401", "restart-after-expiry"])
async def test_read_session_refreshes_and_calls_tool(locally_expired):
    fake, app = token_fake(), FakeQuestrade(valid={"NEW"})
    st = store(expired=locally_expired)
    async with fake.client() as http, app.inner.router.lifespan_context(app.inner):
        tokens = TokenManager(st, http, TOKEN_URL, MCP_URL)
        async with read_session(MCP_URL, tokens, transport=httpx2.ASGITransport(app=app)) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            res = await client.call_tool("list_accounts", {})
    assert "list_accounts" in names
    assert res.structured_content is not None
    assert fake.count("POST", TOKEN_URL) == 1
    if locally_expired:
        assert "Bearer OLD" not in app.seen_auth  # restored expiry -> refresh before the first request
    s = await st.load()
    assert s.tokens is not None and s.tokens.refresh_token == "R2"


async def test_read_refresh_rejected_surfaces_reauth_through_sdk():
    fake, app = token_fake(httpx2.Response(400, json={"error": "invalid_grant"})), FakeQuestrade(valid=set())
    async with fake.client() as http, app.inner.router.lifespan_context(app.inner):
        tokens = TokenManager(store(expired=False), http, TOKEN_URL, MCP_URL)
        with pytest.raises(BaseException) as ei:
            async with read_session(MCP_URL, tokens, transport=httpx2.ASGITransport(app=app)) as client:
                await client.list_tools()
    assert find_in_chain(ei.value, ReauthRequired) is not None, repr(ei.value)


async def test_approval_401_through_sdk_sends_once_executes_nothing_and_marks_submit_failed():
    """The server revoked our token. The approval session must not refresh-and-resend; the draft
    ends in submit_failed and the fake confirms create_order_instruction never executed."""
    fake, app = token_fake(), FakeQuestrade(valid={"NEW"})  # "OLD" is rejected at the edge
    async with fake.client() as http, app.inner.router.lifespan_context(app.inner):
        tokens = TokenManager(store(expired=False), http, TOKEN_URL, MCP_URL)

        async def create(args: Mapping[str, Any]) -> CreateOutcome:
            async with approval_session(MCP_URL, tokens, transport=httpx2.ASGITransport(app=app)) as client:
                await client.call_tool("create_order_instruction", dict(args))
            raise AssertionError("unreachable: the approval session must fail on 401")

        now = datetime.now(UTC)
        order = {"accountId": "a"}
        draft = OrderDraft(
            id="d",
            account_id="a",
            idempotency_key="k",
            preview_args=order,
            preview_args_hash=order_args_hash(order),
            previewed_at=now - timedelta(seconds=20),
        )
        d = await submit_draft(
            draft,
            create_args={**order, "operation": "create"},
            confirmed_preview_hash=order_args_hash(order),
            create=create,
            policy=SubmitPolicy(trading_enabled=True),
            clock=lambda: datetime.now(UTC),
        )
    assert d.status == OrderStatus.SUBMIT_FAILED
    assert app.creates_executed == 0
    assert app.seen_auth == ["Bearer OLD"]  # exactly one HTTP attempt, no resend
    assert fake.count("POST", TOKEN_URL) == 0  # no refresh-and-retry


async def test_approval_session_pre_refreshes_and_calls_create_once():
    fake, app = token_fake(), FakeQuestrade(valid={"OLD", "NEW"})
    async with fake.client() as http, app.inner.router.lifespan_context(app.inner):
        tokens = TokenManager(store(expired=False, minutes_left=3), http, TOKEN_URL, MCP_URL)
        async with approval_session(MCP_URL, tokens, transport=httpx2.ASGITransport(app=app)) as client:
            await client.call_tool("create_order_instruction", {"accountId": "a", "operation": "create"})
    assert fake.count("POST", TOKEN_URL) == 1
    assert "Bearer OLD" not in app.seen_auth
    assert app.creates_executed == 1


async def test_auth_expired_type_is_findable_from_sdk_error():
    """Guards the contract submit_draft relies on: our AuthExpired survives the SDK's wrapping."""
    fake, app = token_fake(), FakeQuestrade(valid=set())
    async with fake.client() as http, app.inner.router.lifespan_context(app.inner):
        tokens = TokenManager(store(expired=False), http, TOKEN_URL, MCP_URL)
        with pytest.raises(BaseException) as ei:
            async with approval_session(MCP_URL, tokens, transport=httpx2.ASGITransport(app=app)) as client:
                await client.list_tools()
    assert find_in_chain(ei.value, AuthExpired) is not None, repr(ei.value)
