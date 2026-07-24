"""Async SQLAlchemy engine/session management.

A single engine is created per process and reused across requests; sessions
are short-lived and scoped to a single request via the `get_db` dependency.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings


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
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
