from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.integrations.questrade.auth import AuthServerMetadata, OAuthMetadata, ProtectedResourceMetadata
from app.integrations.questrade.token_store import AuthState, ClientRegistration, InMemoryTokenStore, TokenSet

MCP_URL = "https://mcp.example.test/v1/brokerage/mcp"
ISSUER = "https://login.example.test"
TOKEN_URL = f"{ISSUER}/connect/token"
CIMD_URL = "https://aurelius.example.test/oauth/client-metadata.json"
REDIRECT = "http://127.0.0.1:8765/callback"


class FrozenClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def meta() -> OAuthMetadata:
    return OAuthMetadata(
        prm=ProtectedResourceMetadata(resource=MCP_URL, authorization_servers=[ISSUER], scopes_supported=["mcp:read"]),
        asm=AuthServerMetadata(
            issuer=ISSUER,
            authorization_endpoint=f"{ISSUER}/connect/authorize",
            token_endpoint=TOKEN_URL,
            registration_endpoint="https://mcp.example.test/connect/register",
            client_id_metadata_document_supported=True,
            code_challenge_methods_supported=["plain", "S256"],
            authorization_response_iss_parameter_supported=True,
        ),
    )


@pytest.fixture
def client_reg() -> ClientRegistration:
    return ClientRegistration(client_id=CIMD_URL, strategy="cimd", issuer=ISSUER, redirect_uris=(REDIRECT,))


def logged_in_store(clock: FrozenClock, client: ClientRegistration, *, lifetime: int = 1800) -> InMemoryTokenStore:
    return InMemoryTokenStore(
        AuthState(
            version=1,
            client=client,
            tokens=TokenSet(
                access_token="ACCESS-1-secretvalue",
                refresh_token="REFRESH-1-secretvalue",
                expires_at=clock() + timedelta(seconds=lifetime),
                obtained_at=clock(),
                scope="mcp:read",
            ),
        )
    )
