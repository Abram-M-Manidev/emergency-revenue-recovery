"""Savepoint port: run one piece of work so that, if it fails, only that
work is undone.

Exists because a live voice turn is ONE database transaction (`get_db`)
that contains several independent pieces of work — tool calls that book
appointments and open emergency tickets, the turn's own persistence, the
post-turn syncs — and the caller hears the result of the early ones before
the late ones run. On PostgreSQL a single failed statement aborts the whole
transaction: every later statement fails, `get_db` rolls everything back,
and an appointment the caller was just told is booked, or an emergency
ticket a dispatcher was just paged about, silently disappears.

Catching the exception is not enough on its own, which is the trap this port
closes. Several best-effort paths already caught broad `Exception` and
carried on — but the transaction underneath was already aborted, so the
"recovery" only moved the failure to the next statement. A savepoint is the
only thing that actually returns the transaction to a usable state.

Kept in `domain` for the same reason `locks.py` is: zero framework imports,
so services depend on the capability rather than on SQLAlchemy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager


class Savepoints(ABC):
    @abstractmethod
    def isolate(self) -> AbstractAsyncContextManager[None]:
        """Runs the block inside a savepoint.

        Clean exit keeps the block's writes. Any exception — including
        cancellation — undoes exactly the block's writes and is re-raised
        untouched, leaving the surrounding transaction usable.

        The block must not swallow a database error itself: a savepoint
        exited "cleanly" over an aborted transaction cannot be released.
        Best-effort callers therefore put the savepoint INSIDE their
        `try`, never around it."""
        ...


class NullSavepoints(Savepoints):
    """No-op implementation for unit tests and in-memory fakes, which have
    no transaction to protect. Same rationale as `NullCallLock`."""

    def isolate(self) -> AbstractAsyncContextManager[None]:
        return _null_context()


@asynccontextmanager
async def _null_context() -> AsyncIterator[None]:
    yield


class AfterCommit(ABC):
    """Work that must happen only once the request's transaction has
    committed — the half of the outbox pattern that talks to the outside
    world.

    The callback runs after a SUCCESSFUL commit and never otherwise: a
    rolled-back request runs nothing, so no external effect can ever refer to
    a record that does not exist. Callbacks registered inside a savepoint that
    later rolled back still run, so they must be written to re-check the
    database rather than trust the moment they were registered — the outbox
    worker does exactly that."""

    @abstractmethod
    def register(self, callback: Callable[[], Awaitable[None]]) -> None: ...


class NullAfterCommit(AfterCommit):
    """Drops callbacks. For unit tests and in-memory fakes, where there is no
    commit to wait for and the test drives delivery explicitly."""

    def register(self, callback: Callable[[], Awaitable[None]]) -> None:
        return None
