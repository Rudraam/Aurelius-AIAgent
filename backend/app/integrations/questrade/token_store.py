"""Auth state persistence: client registration + token set, with optimistic locking.

Contract for every ``TokenStore`` implementation:
- ``save(state, expected_version=v)`` succeeds only if the stored version is still ``v`` and returns the
  state with ``version = v + 1``; otherwise it raises ``TokenStoreConflict`` and writes nothing.
- ``save`` is durable when it returns. Callers rely on this to persist a rotated refresh token
  *before* handing the new access token to anything.
- ``refresh_lock()`` serializes refreshes among all callers that share the store. In-process
  implementations use an asyncio lock; the Postgres implementation (M1) uses an advisory lock.

Secrets live only inside ``TokenSet``/``ClientRegistration`` fields marked ``repr=False``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.crypto import SecretBox
from app.integrations.questrade.errors import TokenStoreConflict


@dataclass(frozen=True, slots=True)
class ClientRegistration:
    """Aurelius's CIMD client: a public client, so there is no secret. ``client_id`` is the CIMD URL."""

    client_id: str
    issuer: str
    redirect_uris: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str = field(repr=False)
    expires_at: datetime
    obtained_at: datetime
    refresh_token: str | None = field(default=None, repr=False)
    scope: str | None = None

    @property
    def lifetime_s(self) -> float:
        return (self.expires_at - self.obtained_at).total_seconds()


@dataclass(frozen=True, slots=True)
class AuthState:
    version: int = 0
    client: ClientRegistration | None = None
    tokens: TokenSet | None = None
    refresh_rotated_at: datetime | None = None


class TokenStore(Protocol):
    async def load(self) -> AuthState: ...

    async def save(self, state: AuthState, *, expected_version: int) -> AuthState: ...

    def refresh_lock(self) -> Any:  # async context manager
        ...


# --------------------------------------------------------------------------- serialization


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def state_to_dict(s: AuthState) -> dict[str, Any]:
    c, t = s.client, s.tokens
    return {
        "version": s.version,
        "client": None
        if c is None
        else {
            "client_id": c.client_id,
            "issuer": c.issuer,
            "redirect_uris": list(c.redirect_uris),
        },
        "tokens": None
        if t is None
        else {
            "access_token": t.access_token,
            "refresh_token": t.refresh_token,
            "expires_at": t.expires_at.isoformat(),
            "obtained_at": t.obtained_at.isoformat(),
            "scope": t.scope,
        },
        "refresh_rotated_at": s.refresh_rotated_at.isoformat() if s.refresh_rotated_at else None,
    }


class NonCimdStateError(ValueError):
    """Stored auth state describes a client that is not a CIMD public client. Refused, never used."""


def state_from_dict(d: dict[str, Any]) -> AuthState:
    c, t = d.get("client"), d.get("tokens")
    if c and (c.get("strategy", "cimd") != "cimd" or c.get("client_secret")):
        raise NonCimdStateError("stored client is not a CIMD public client; delete the state file and log in again")
    exp = _dt(t["expires_at"]) if t else None
    obt = _dt(t["obtained_at"]) if t else None
    return AuthState(
        version=int(d["version"]),
        client=None
        if not c
        else ClientRegistration(
            client_id=c["client_id"],
            issuer=c["issuer"],
            redirect_uris=tuple(c["redirect_uris"]),
        ),
        tokens=None
        if not t or exp is None or obt is None
        else TokenSet(
            access_token=t["access_token"],
            refresh_token=t.get("refresh_token"),
            expires_at=exp,
            obtained_at=obt,
            scope=t.get("scope"),
        ),
        refresh_rotated_at=_dt(d.get("refresh_rotated_at")),
    )


# --------------------------------------------------------------------------- implementations


class InMemoryTokenStore:
    """For tests. Same CAS semantics as the durable stores."""

    def __init__(self, state: AuthState | None = None) -> None:
        self._state = state or AuthState()
        self._lock = asyncio.Lock()

    async def load(self) -> AuthState:
        return self._state

    async def save(self, state: AuthState, *, expected_version: int) -> AuthState:
        if self._state.version != expected_version:
            raise TokenStoreConflict(f"expected version {expected_version}, found {self._state.version}")
        self._state = replace(state, version=expected_version + 1)
        return self._state

    @asynccontextmanager
    async def refresh_lock(self) -> AsyncIterator[None]:
        async with self._lock:
            yield


class EncryptedFileTokenStore:
    """AES-GCM encrypted JSON file with atomic replace. For local dev and the spike only.

    Serializes refreshes within one process. It does not lock across processes: do not run two
    long-lived processes against the same file. Production uses the Postgres store.
    """

    _AAD = b"aurelius.questrade.auth.v1"

    def __init__(self, path: Path, box: SecretBox) -> None:
        self._path = path
        self._box = box
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()  # makes check-version + write one step

    def _read(self) -> bytes | None:
        return self._path.read_bytes() if self._path.exists() else None

    def _write(self, blob: bytes) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    async def load(self) -> AuthState:
        blob = await asyncio.to_thread(self._read)
        if blob is None:
            return AuthState()
        return state_from_dict(json.loads(self._box.decrypt(blob, self._AAD)))

    async def save(self, state: AuthState, *, expected_version: int) -> AuthState:
        async with self._write_lock:
            current = await self.load()
            if current.version != expected_version:
                raise TokenStoreConflict(f"expected version {expected_version}, found {current.version}")
            new = replace(state, version=expected_version + 1)
            blob = self._box.encrypt(json.dumps(state_to_dict(new)).encode(), self._AAD)
            await asyncio.to_thread(self._write, blob)
            return new

    @asynccontextmanager
    async def refresh_lock(self) -> AsyncIterator[None]:
        async with self._lock:
            yield


def utcnow() -> datetime:
    return datetime.now(UTC)
