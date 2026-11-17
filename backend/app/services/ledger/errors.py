"""Typed service errors. M4 maps them onto RFC 9457 problem responses."""

from __future__ import annotations


class LedgerServiceError(Exception):
    """Base class for ledger service errors."""


class NotFound(LedgerServiceError):
    pass


class Conflict(LedgerServiceError):
    """The request is valid but the ledger's current state forbids it (ordering, duplicates, versions)."""


class InvalidState(LedgerServiceError):
    """The target is in the wrong lifecycle state for this operation."""


class MissingConfig(LedgerServiceError):
    """Something the engine needs (an income source, a policy) has not been configured."""
