"""PostgreSQL-backed implementations of the `CallLock` and `BookingLock`
ports.

Uses a transaction-scoped advisory lock, which is the only mechanism
already available in this stack that works across the four uvicorn worker
processes `docker-compose.prod.yml` runs. An `asyncio.Lock` would only
serialise requests that happened to land on the same worker, and uvicorn
accepts from a shared socket, so two requests for one call can be handled
by two different processes."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.locks import BookingLock, CallLock

# Postgres advisory locks are keyed by a signed 64-bit integer, so the call
# id is hashed into that space. blake2b rather than the built-in `hash()`
# because the latter is randomised per process by PYTHONHASHSEED — four
# workers would derive four different keys for the same call and serialise
# nothing at all.
_SIGNED_64_MIN = -(2**63)
_UNSIGNED_64 = 2**64


def _advisory_key(value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    unsigned = int.from_bytes(digest, "big")
    return unsigned - _UNSIGNED_64 if unsigned >= 2**63 else unsigned


@asynccontextmanager
async def _hold_advisory_xact_lock(session: AsyncSession, key: str) -> AsyncIterator[None]:
    """The one implementation of "take a transaction-scoped advisory lock on
    this string", shared by both lock ports below so they can never drift on
    key derivation or release semantics."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _advisory_key(key)})
    yield


class PostgresAdvisoryCallLock(CallLock):
    """Serialises on `pg_advisory_xact_lock`, which Postgres releases
    automatically when the surrounding transaction commits or rolls back.

    Transaction-scoped rather than session-scoped deliberately: a
    session-scoped `pg_advisory_lock` survives until explicitly unlocked,
    and because SQLAlchemy returns connections to a pool, a single missed
    unlock would strand the lock on a pooled connection and permanently
    block that call id. The transaction variant cannot leak — the process
    dying, the request erroring, or the task being cancelled all end the
    transaction and release it.

    The consequence is that the lock is held until the request's session
    commits (see `get_db`), not until the `hold()` block exits. That is a
    superset of the critical section, which is safe: the whole request for
    one call is what we want serialised."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def hold(self, key: str) -> AbstractAsyncContextManager[None]:
        # Deliberately unprefixed: this key derivation predates the booking
        # lock and is left byte-identical, so a rolling deploy can never have
        # two workers deriving different advisory keys for the same live
        # call. The booking lock namespaces its own keys instead.
        return _hold_advisory_xact_lock(self._session, key)


class PostgresAdvisoryBookingLock(BookingLock):
    """Slot-keyed twin of `PostgresAdvisoryCallLock`, with identical
    transaction-scoped release semantics (see that class for why
    transaction-scoped rather than session-scoped).

    Both locks are held simultaneously during a voice booking — the call
    lock for the whole request, this one only around the verify-then-write
    — which cannot deadlock: call keys are distinct per call, and every
    holder acquires the call lock first and the slot lock second, so the
    acquisition order is global and no cycle can form.

    Booking keys are prefixed `booking:` before hashing, so this lock space
    cannot collide with the (deliberately unprefixed) call one."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def hold(self, key: str) -> AbstractAsyncContextManager[None]:
        return _hold_advisory_xact_lock(self._session, f"booking:{key}")
