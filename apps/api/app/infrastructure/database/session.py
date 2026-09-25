"""Async SQLAlchemy engine/session management.

A single engine is created per process and reused across requests; sessions
are short-lived and scoped to a single request via the `get_db` dependency.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.domain.exceptions import DomainError
from app.infrastructure.database.transactions import AFTER_COMMIT_KEY

_logger = structlog.get_logger("app.database")


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    if settings.is_testing:
        # `pytest` runs every `tests/integration/*` module against this one
        # process-wide engine, each with its own module-scoped fixture that
        # drops/recreates the schema. A pooled connection opened by one
        # module can outlive that module's event-loop context, and
        # `pool_pre_ping`'s liveness check on it then fails with "attached
        # to a different loop" the moment a later module reuses it — a
        # known asyncpg/SQLAlchemy-async interaction. NullPool sidesteps it
        # entirely by never reusing a connection across requests, which is
        # fine for tests (low volume, correctness over pooling throughput).
        return create_async_engine(
            settings.DATABASE_URL, echo=settings.DATABASE_ECHO, poolclass=NullPool
        )
    return create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DATABASE_ECHO,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
        pool_pre_ping=True,
    )


engine: AsyncEngine = create_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session.

    Commits on clean exit, rolls back on any exception, and always closes.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as exc:
            session.info.pop(AFTER_COMMIT_KEY, None)
            # Never silent. On a streamed voice turn this can run after the
            # caller has heard the whole reply — a failed commit there means
            # the backend has no record of something the caller was told —
            # and until this line the only trace was an unattributed
            # traceback. Bound request/call ids attach via contextvars. Type
            # only: a driver error quotes its parameters, i.e. caller PII.
            # A `DomainError` is an ordinary refusal (404, 409, ...) that
            # already has its own log line, so it stays at info.
            log = _logger.info if isinstance(exc, DomainError) else _logger.error
            log("db_transaction_rolled_back", error=type(exc).__name__)
            try:
                await session.rollback()
            except Exception as rollback_exc:
                # The connection itself is broken — typically a query that
                # was cancelled mid-flight, which leaves SQLAlchemy unable to
                # roll back ("Can't reconnect until invalid transaction is
                # rolled back"). Invalidate rather than close: close would
                # return a connection still `idle in transaction` holding
                # this request's locks; invalidation discards it, and
                # Postgres aborts the transaction when it goes.
                _logger.error(
                    "db_connection_invalidated", error=type(rollback_exc).__name__
                )
                await session.invalidate()
            raise
        finally:
            await session.close()
        await _run_after_commit(session)


async def _run_after_commit(session: AsyncSession) -> None:
    """Runs the callbacks registered for this request, now that its
    transaction has committed.

    Only reached on a clean commit: the `except` above clears them and
    re-raises. Each callback is isolated — one failing must not stop the
    next, and none may surface as an error for a request that has already
    succeeded. They do their own database work on fresh sessions; this
    request's session is closed by the time they run.

    Awaited rather than spawned as a background task: on a streamed voice
    turn this runs after the response body has been sent, so the caller
    waits for nothing, and awaiting means the work cannot be lost when a
    worker shuts down between the commit and a task being scheduled. Whatever
    is still undone is picked up by the outbox poller."""
    callbacks = session.info.pop(AFTER_COMMIT_KEY, None) or []
    for callback in callbacks:
        try:
            await callback()
        except Exception as exc:
            _logger.error("after_commit_callback_failed", error=type(exc).__name__)
