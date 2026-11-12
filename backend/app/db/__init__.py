"""Persistence for the ledger (Phase 2 M3): Postgres 16, SQLAlchemy 2 async (asyncpg), Alembic.

- ``tables``: the schema. Money is ``NUMERIC(18,2)``; timestamps are ``timestamptz`` in UTC.
- ``migrations``: Alembic. The schema is created only by migrations, which also install the triggers
  that make history append-only and approved plans immutable (invariant 5).
- ``session``: engine and session factory from ``AURELIUS_DATABASE_URL``.
- ``backup``: ``aurelius db backup`` / ``restore`` (pg_dump custom format).

The pure ledger (``app/ledger``) never imports this package; the services in ``app/services/ledger``
bridge the two.
"""
