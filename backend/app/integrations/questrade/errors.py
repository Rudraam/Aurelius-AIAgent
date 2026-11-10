"""Typed Questrade integration errors.

Messages must never contain token values, PKCE verifiers or authorization codes.
"""

from __future__ import annotations


class QuestradeError(Exception):
    """Base class for all Questrade integration errors."""


class OAuthProtocolError(QuestradeError):
    """The authorization server or callback violated the protocol (state/iss mismatch, bad metadata)."""


class ClientIdMismatch(OAuthProtocolError):
    """A request was about to carry a client_id other than the configured CIMD URL. Never sent."""


class ReauthRequired(QuestradeError):
    """A human must run `aurelius auth login` again.

    Raised when the refresh grant is rejected, no tokens are stored, or the server demands a scope we
    were not granted (403 insufficient_scope). ``scope`` carries the server's requested scope when known.
    """

    def __init__(self, reason: str, *, scope: str | None = None) -> None:
        self.reason = reason
        self.scope = scope
        super().__init__(reason if scope is None else f"{reason} (required scope: {scope})")


class AuthExpired(QuestradeError):
    """Access token was rejected and could not be renewed right now (transient: token endpoint down)."""


class TokenStoreConflict(QuestradeError):
    """Optimistic-lock failure: the stored auth state changed since it was loaded."""


def find_in_chain[E: BaseException](exc: BaseException, kind: type[E]) -> E | None:
    """Find ``kind`` in ``exc``, its exception-group members, or its cause/context chain.

    The MCP SDK runs the transport in a task group, so an error raised by our ``httpx2.Auth`` may
    surface wrapped in an ``ExceptionGroup`` or another exception rather than as itself.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, kind):
            return e
        if isinstance(e, BaseExceptionGroup):
            stack.extend(e.exceptions)
        stack.extend(x for x in (e.__cause__, e.__context__) if x is not None)
    return None
