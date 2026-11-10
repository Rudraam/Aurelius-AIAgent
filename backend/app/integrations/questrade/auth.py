"""Questrade OAuth: discovery, CIMD client identity (the only strategy), hand-rolled login, token
lifecycle, and the bearer ``httpx2.Auth`` classes handed to the MCP SDK transport (one for read
sessions, a separate never-resend one for approval sessions). One HTTP stack: httpx2 throughout.

Why hand-rolled: mcp 2.3.0's ``OAuthClientProvider`` replaces the requested scope with every scope in
the protected-resource metadata, does not restore token expiry after a restart, and falls into the
interactive flow when a refresh fails. We use no private SDK API; the SDK only sees an ``httpx2.Auth``.

Security rules for this module:
- Never log or put into an exception message: access/refresh tokens, PKCE verifier, auth code,
  OAuth ``state``.
- Invariant: every authorize, token and refresh request carries exactly the configured CIMD URL as
  ``client_id`` (``CimdClientId.require``). There is no dynamic client registration: Questrade's DCR
  endpoint returned Claude Code's ``claude-code-public`` client (2026-11-09), so DCR was removed.
- A rotated refresh token is durably saved (``TokenStore.save``) before the new access token is
  returned to any caller.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from collections.abc import AsyncGenerator, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, ValidationError

from app.integrations.questrade.errors import (
    AuthExpired,
    ClientIdMismatch,
    OAuthProtocolError,
    ReauthRequired,
    TokenStoreConflict,
)
from app.integrations.questrade.token_store import (
    AuthState,
    ClientRegistration,
    TokenSet,
    TokenStore,
    utcnow,
)

log = logging.getLogger("aurelius.questrade.auth")

Clock = Callable[[], datetime]

# Least privilege for Phase 1 reads. Trading adds "mcp:brokerage.orders.all" via re-login at the
# order milestone. Never request *.all for watchlists/custom-indexing/trading-workflows, or mcp:write.
READ_SCOPES: tuple[str, ...] = (
    "offline_access",
    "mcp:read",
    "mcp:brokerage.accounts.read",
    "mcp:brokerage.balances.read",
    "mcp:brokerage.positions.read",
    "mcp:brokerage.orders.read",
    "mcp:brokerage.account-transactions.read",
    "mcp:brokerage.securities.read",
    "mcp:brokerage.quotes.read",
    "mcp:brokerage.charts.read",
)

_REAUTH_TOKEN_ERRORS = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})


# --------------------------------------------------------------------------- metadata


class ProtectedResourceMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    resource: str
    authorization_servers: list[str]
    scopes_supported: list[str] = []


class AuthServerMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    client_id_metadata_document_supported: bool = False
    code_challenge_methods_supported: list[str] = []
    authorization_response_iss_parameter_supported: bool = False
    scopes_supported: list[str] = []


@dataclass(frozen=True, slots=True)
class OAuthMetadata:
    prm: ProtectedResourceMetadata
    asm: AuthServerMetadata


_WWW_AUTH_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s,]+))')


def parse_www_authenticate(header: str) -> dict[str, str]:
    return {
        m.group(1): (m.group(2) if m.group(2) is not None else m.group(3)) for m in _WWW_AUTH_PARAM.finditer(header)
    }


async def discover(http: httpx2.AsyncClient, mcp_url: str) -> OAuthMetadata:
    """RFC 9728 + RFC 8414 discovery starting from an unauthenticated MCP request."""
    probe = await http.post(
        mcp_url,
        json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    params = parse_www_authenticate(probe.headers.get("www-authenticate", ""))
    p = urlsplit(mcp_url)
    prm_url = params.get("resource_metadata") or f"{p.scheme}://{p.netloc}/.well-known/oauth-protected-resource"
    try:
        prm = ProtectedResourceMetadata.model_validate(await _get_json(http, prm_url))
    except ValidationError as e:
        raise OAuthProtocolError("invalid protected resource metadata") from e
    if prm.resource.rstrip("/") != mcp_url.rstrip("/"):
        raise OAuthProtocolError(f"protected resource {prm.resource} does not match {mcp_url}")
    if not prm.authorization_servers:
        raise OAuthProtocolError("protected resource metadata lists no authorization server")
    issuer = prm.authorization_servers[0].rstrip("/")

    asm: AuthServerMetadata | None = None
    for suffix in ("oauth-authorization-server", "openid-configuration"):
        try:
            asm = AuthServerMetadata.model_validate(await _get_json(http, f"{issuer}/.well-known/{suffix}"))
            break
        except (OAuthProtocolError, ValidationError):
            continue
    if asm is None:
        raise OAuthProtocolError(f"no authorization server metadata at {issuer}")
    if asm.issuer.rstrip("/") != issuer:
        raise OAuthProtocolError(f"issuer mismatch: metadata says {asm.issuer}, expected {issuer}")
    return OAuthMetadata(prm=prm, asm=asm)


async def _get_json(http: httpx2.AsyncClient, url: str) -> Any:
    r = await http.get(url, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OAuthProtocolError(f"GET {url} returned {r.status_code}")
    return r.json()


# --------------------------------------------------------------------------- client identity (CIMD only)


@dataclass(frozen=True, slots=True)
class CimdClientId:
    """Aurelius's one and only OAuth client_id: the URL of its Client ID Metadata Document.

    Every function that puts a client_id on the wire takes this value and refuses to send anything else.
    """

    url: str

    def __post_init__(self) -> None:
        p = urlsplit(self.url)
        problems = []
        if p.scheme != "https":
            problems.append("must be https")
        if not p.hostname:
            problems.append("must have a host")
        if p.path in ("", "/"):
            problems.append("must have a non-root path")
        if p.query or p.fragment:
            problems.append("must not have a query or fragment")
        if p.username or p.password:
            problems.append("must not contain credentials")
        if problems:
            raise ValueError(f"invalid CIMD client_id URL {self.url!r}: {', '.join(problems)}")

    def require(self, actual: object, where: str) -> None:
        """Raise ``ClientIdMismatch`` unless ``actual`` is exactly this client_id."""
        if not isinstance(actual, str) or not secrets.compare_digest(actual.encode(), self.url.encode()):
            raise ClientIdMismatch(f"{where}: client_id {actual!r} is not the configured CIMD URL {self.url!r}")


def cimd_document(client_id_url: str, redirect_uris: Sequence[str]) -> dict[str, Any]:
    """The static JSON served at ``client_id_url``. The URL itself is the permanent client_id."""
    return {
        "client_id": client_id_url,
        "client_name": "Aurelius",
        "redirect_uris": list(redirect_uris),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


async def cimd_registration(
    http: httpx2.AsyncClient, asm: AuthServerMetadata, client_id: CimdClientId
) -> ClientRegistration:
    """Verify the server accepts CIMD and the published document names exactly ``client_id``."""
    if not asm.client_id_metadata_document_supported:
        raise OAuthProtocolError("authorization server does not advertise CIMD support")
    doc = await _get_json(http, client_id.url)
    if not isinstance(doc, dict):
        raise OAuthProtocolError(f"CIMD document at {client_id.url} is not a JSON object")
    client_id.require(doc.get("client_id"), "published CIMD document")
    uris = doc.get("redirect_uris") or []
    if not uris:
        raise OAuthProtocolError("CIMD document lists no redirect_uris")
    return ClientRegistration(client_id=client_id.url, issuer=asm.issuer, redirect_uris=tuple(uris))


# --------------------------------------------------------------------------- login (authorization code + PKCE)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)  # 86 chars, within RFC 7636's 43..128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


@dataclass(frozen=True, slots=True)
class PendingLogin:
    authorize_url: str = field(repr=False)  # contains state + challenge
    redirect_uri: str
    scopes: tuple[str, ...]
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)


def begin_login(
    meta: OAuthMetadata,
    client: ClientRegistration,
    client_id: CimdClientId,
    redirect_uri: str,
    scopes: Sequence[str] = READ_SCOPES,
) -> PendingLogin:
    client_id.require(client.client_id, "authorize request")
    if "S256" not in meta.asm.code_challenge_methods_supported:
        raise OAuthProtocolError("authorization server does not support PKCE S256")
    if redirect_uri not in client.redirect_uris:
        raise OAuthProtocolError(f"redirect_uri {redirect_uri} is not registered for this client")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id.url,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": meta.prm.resource,
            "prompt": "consent",
        }
    )
    return PendingLogin(
        authorize_url=f"{meta.asm.authorization_endpoint}?{query}",
        redirect_uri=redirect_uri,
        scopes=tuple(scopes),
        state=state,
        code_verifier=verifier,
    )


async def complete_login(
    http: httpx2.AsyncClient,
    meta: OAuthMetadata,
    client: ClientRegistration,
    client_id: CimdClientId,
    pending: PendingLogin,
    callback: Mapping[str, str],
    store: TokenStore,
    clock: Clock = utcnow,
) -> AuthState:
    """Validate the redirect, exchange the code, persist client + tokens. Returns the saved state."""
    client_id.require(client.client_id, "token exchange")
    if "error" in callback:
        raise OAuthProtocolError(f"authorization failed: {callback.get('error')}")
    if not secrets.compare_digest(callback.get("state", "").encode(), pending.state.encode()):
        raise OAuthProtocolError("state mismatch: possible CSRF or stale login; start again")
    if meta.asm.authorization_response_iss_parameter_supported and callback.get("iss") != meta.asm.issuer:
        raise OAuthProtocolError("authorization response issuer mismatch")
    code = callback.get("code")
    if not code:
        raise OAuthProtocolError("authorization response has no code")

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": client_id.url,
        "code_verifier": pending.code_verifier,
        "resource": meta.prm.resource,
    }
    tokens = await _token_request(http, meta.asm.token_endpoint, client_id, form, clock, previous=None)
    current = await store.load()
    saved = await store.save(replace(current, client=client, tokens=tokens), expected_version=current.version)
    log.info(
        "questrade login complete: scope=%r lifetime_s=%d refresh_token=%s",
        tokens.scope,
        tokens.lifetime_s,
        tokens.refresh_token is not None,
    )
    return saved


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str | None = None
    scope: str | None = None


async def _token_request(
    http: httpx2.AsyncClient,
    token_endpoint: str,
    client_id: CimdClientId,
    form: dict[str, str],
    clock: Clock,
    previous: TokenSet | None,
) -> TokenSet:
    """The single path for every token-endpoint call. Public client: no secret, no client auth header."""
    client_id.require(form.get("client_id"), "token endpoint request")
    try:
        r = await http.post(token_endpoint, data=form, headers={"Accept": "application/json"})
    except httpx2.HTTPError as e:
        raise AuthExpired(f"token endpoint unreachable: {type(e).__name__}") from None
    if r.status_code != 200:
        err = _oauth_error(r)
        if r.status_code in (400, 401) and err in _REAUTH_TOKEN_ERRORS:
            raise ReauthRequired(f"token endpoint rejected the grant ({err})")
        raise AuthExpired(f"token endpoint returned HTTP {r.status_code} ({err})")
    try:
        body = _TokenResponse.model_validate(r.json())
    except (ValidationError, ValueError):
        raise OAuthProtocolError("malformed token response") from None
    if body.token_type.lower() != "bearer":
        raise OAuthProtocolError(f"unexpected token_type {body.token_type!r}")
    now = clock()
    return TokenSet(
        access_token=body.access_token,
        refresh_token=body.refresh_token or (previous.refresh_token if previous else None),
        expires_at=now + timedelta(seconds=body.expires_in),
        obtained_at=now,
        scope=body.scope or (previous.scope if previous else None),
    )


def _oauth_error(r: httpx2.Response) -> str | None:
    try:
        v = r.json().get("error")
        return v if isinstance(v, str) else None
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- token lifecycle


class TokenManager:
    """Hands out access tokens; refreshes single-flight; persists rotation before use."""

    def __init__(
        self,
        store: TokenStore,
        http: httpx2.AsyncClient,
        token_endpoint: str,
        resource: str,
        client_id: CimdClientId,
        *,
        clock: Clock = utcnow,
        skew: timedelta = timedelta(seconds=30),
    ) -> None:
        self._client_id = client_id
        self._store = store
        self._http = http
        self._token_endpoint = token_endpoint
        self._resource = resource
        self._clock = clock
        self._skew = skew

    def _usable(self, t: TokenSet, min_remaining: timedelta | None = None) -> bool:
        margin = max(self._skew, min_remaining or timedelta(0))
        return self._clock() < t.expires_at - margin

    async def access_token(self, *, min_remaining: timedelta | None = None) -> str:
        """A token valid for at least ``min_remaining`` (default: the clock skew), refreshing if needed."""
        state = await self._store.load()
        if state.tokens is None:
            raise ReauthRequired("no stored Questrade tokens")
        if self._usable(state.tokens, min_remaining):
            return state.tokens.access_token
        return await self.refresh(stale_access_token=state.tokens.access_token, min_remaining=min_remaining)

    async def needs_proactive_refresh(self, remaining_fraction: float = 0.2) -> bool:
        t = (await self._store.load()).tokens
        if t is None:
            return False
        return (t.expires_at - self._clock()).total_seconds() < remaining_fraction * t.lifetime_s

    async def refresh(self, stale_access_token: str | None, *, min_remaining: timedelta | None = None) -> str:
        """Refresh unless another caller already replaced ``stale_access_token`` with a token that
        satisfies ``min_remaining``. ``stale_access_token=None`` forces a refresh.
        """
        async with self._store.refresh_lock():
            state = await self._store.load()
            t = state.tokens
            if t is None or state.client is None:
                raise ReauthRequired("no stored Questrade tokens")
            if (
                stale_access_token is not None
                and t.access_token != stale_access_token
                and self._usable(t, min_remaining)
            ):
                return t.access_token  # someone refreshed while we waited
            if not t.refresh_token:
                raise ReauthRequired("no refresh token; access token expired")

            # The stored registration must be the configured CIMD client; anything else fails closed.
            self._client_id.require(state.client.client_id, "refresh (stored client)")
            form = {
                "grant_type": "refresh_token",
                "refresh_token": t.refresh_token,
                "client_id": self._client_id.url,
                "resource": self._resource,
            }
            new = await _token_request(self._http, self._token_endpoint, self._client_id, form, self._clock, previous=t)
            rotated = new.refresh_token != t.refresh_token
            try:
                # Durable BEFORE the new access token leaves this method.
                await self._store.save(
                    replace(
                        state, tokens=new, refresh_rotated_at=self._clock() if rotated else state.refresh_rotated_at
                    ),
                    expected_version=state.version,
                )
            except TokenStoreConflict:
                log.error("questrade token store changed during refresh; another process refreshed")
                winner = (await self._store.load()).tokens
                if winner is not None and self._usable(winner):
                    return winner.access_token
                raise ReauthRequired("concurrent refresh conflict; tokens may be invalidated") from None
            log.info("questrade token refreshed: lifetime_s=%d rotated=%s", new.lifetime_s, rotated)
            return new.access_token


# --------------------------------------------------------------------------- httpx2 auth for the SDK transport


#: Before any approval call the access token must have at least this long left, so it cannot
#: expire between the edge check and the tool executing.
APPROVAL_MIN_TOKEN_REMAINING = timedelta(minutes=5)


def _challenge(response: httpx2.Response) -> dict[str, str]:
    return parse_www_authenticate(response.headers.get("www-authenticate", ""))


def _raise_for_insufficient_scope(response: httpx2.Response) -> None:
    if response.status_code == 403:
        ch = _challenge(response)
        if ch.get("error") == "insufficient_scope":
            raise ReauthRequired("insufficient scope", scope=ch.get("scope"))


class _AsyncOnlyAuth(httpx2.Auth):
    def __init__(self, tokens: TokenManager) -> None:
        self._tokens = tokens

    def sync_auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response, None]:
        raise RuntimeError(f"{type(self).__name__} is async-only")


class ReadBearerAuth(_AsyncOnlyAuth):
    """For read-only MCP sessions. On a 401 whose ``WWW-Authenticate`` says ``error="invalid_token"``,
    refresh once and resend once. Any other 401 raises ``AuthExpired`` without a resend.

    Never attach this to a session that can reach an approval tool: use ``ApprovalBearerAuth``.
    """

    requires_request_body = True  # so the body can be replayed after a refresh

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        token = await self._tokens.access_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request

        if response.status_code == 401:
            if _challenge(response).get("error") != "invalid_token":
                raise AuthExpired("read call got 401 without error=invalid_token; not resent")
            token = await self._tokens.refresh(stale_access_token=token)
            request.headers["Authorization"] = f"Bearer {token}"
            response = yield request
            if response.status_code == 401:
                raise ReauthRequired("access token rejected immediately after refresh")

        _raise_for_insufficient_scope(response)


class ApprovalBearerAuth(_AsyncOnlyAuth):
    """For the session that calls approval tools. Never resends a request (invariant 5, absolute).

    Before each request it makes sure the token has ``APPROVAL_MIN_TOKEN_REMAINING`` left, refreshing
    first if not. Any 401 raises ``AuthExpired``; the order service moves the draft to ``submit_failed``.
    """

    def __init__(self, tokens: TokenManager, *, min_remaining: timedelta = APPROVAL_MIN_TOKEN_REMAINING) -> None:
        super().__init__(tokens)
        self._min_remaining = min_remaining

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        token = await self._tokens.access_token(min_remaining=self._min_remaining)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            raise AuthExpired("approval call got 401; not resent (resubmission is a human action)")
        _raise_for_insufficient_scope(response)
