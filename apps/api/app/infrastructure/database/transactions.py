"""SQLAlchemy implementation of the `Savepoints` port."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.transactions import AfterCommit, Savepoints

# The key under which post-commit callbacks ride on the session. Read by
# `get_db`, which is the only place that knows when the commit happened.
AFTER_COMMIT_KEY = "errs_after_commit"


class SqlAlchemySavepoints(Savepoints):
    """`SAVEPOINT` / `ROLLBACK TO SAVEPOINT` on the request's own session, via
    `AsyncSession.begin_nested()` — the same primitive the ticket,
    appointment and customer repositories already use for their racing
    inserts.

    A bare `async with begin_nested(): yield` gets one case wrong. When code
    inside the block swallows a database error itself (and several
    best-effort lookups deliberately do), the block exits "cleanly",
    SQLAlchemy tries to RELEASE the savepoint, Postgres refuses because the
    transaction is aborted — and SQLAlchemy re-raises without rolling back
    to the savepoint, leaving the session unusable for the rest of the
    request. Verified against Postgres 16 rather than assumed; that state
    cannot be recovered through the public API after the fact.

    So the failure is surfaced *before* the release, still inside the
    context manager: pending ORM writes are flushed and a trivial statement
    is run. On an aborted transaction either raises, and SQLAlchemy's own
    context manager then rolls the savepoint back exactly as it does for any
    error raised in the block — including an ORM flush error, which leaves
    the nested transaction in a state a hand-written `is_active` check
    misreads. The cost is one round trip per isolated block, on a path that
    already makes several."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def isolate(self) -> AbstractAsyncContextManager[None]:
        return self._isolate()

    @asynccontextmanager
    async def _isolate(self) -> AsyncIterator[None]:
        async with self._session.begin_nested():
            yield
            await self._session.flush()
            await self._session.execute(text("SELECT 1"))


class SessionAfterCommit(AfterCommit):
    """Queues callbacks on the request's session for `get_db` to run once
    its commit has succeeded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def register(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._session.info.setdefault(AFTER_COMMIT_KEY, []).append(callback)
