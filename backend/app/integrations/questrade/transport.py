"""MCP SDK sessions over our own httpx2 clients. Public SDK API only.

Read and approval traffic never share an auth instance or HTTP client:
- ``read_session``: ``ReadBearerAuth`` (one refresh + resend on 401 ``invalid_token``).
- ``approval_session``: ``ApprovalBearerAuth`` (pre-refresh if <5 min left; never resends).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from app.integrations.questrade.auth import ApprovalBearerAuth, ReadBearerAuth, TokenManager

# Same shape as the SDK's defaults: short connect/write, long read for streamed responses.
MCP_TIMEOUT = httpx2.Timeout(30.0, read=300.0)


@asynccontextmanager
async def _session(url: str, auth: httpx2.Auth, transport: httpx2.AsyncBaseTransport | None) -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(auth=auth, timeout=MCP_TIMEOUT, follow_redirects=False, transport=transport) as http:
        async with Client(streamable_http_client(url, http_client=http, terminate_on_close=False)) as client:
            yield client


@asynccontextmanager
async def read_session(
    url: str, tokens: TokenManager, *, transport: httpx2.AsyncBaseTransport | None = None
) -> AsyncIterator[Client]:
    async with _session(url, ReadBearerAuth(tokens), transport) as client:
        yield client


@asynccontextmanager
async def approval_session(
    url: str, tokens: TokenManager, *, transport: httpx2.AsyncBaseTransport | None = None
) -> AsyncIterator[Client]:
    async with _session(url, ApprovalBearerAuth(tokens), transport) as client:
        yield client
