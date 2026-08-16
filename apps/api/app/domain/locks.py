"""Mutual-exclusion port for work that must not run concurrently for the
same logical entity.

Kept in `domain` for the same reason `ai/provider.py` is: it has zero
framework or infrastructure imports and exists so the application layer can
depend on the capability rather than on PostgreSQL. The only production
implementation is `infrastructure/database/locks.py`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager


class CallLock(ABC):
    """Serialises processing of one live phone call across every worker
    process.

    Vapi sends a Custom-LLM request per transcription update, and those
    requests overlap: a live call produced five requests for one spoken
    sentence, three of them arriving while an earlier one was still
    generating. Without serialisation each of them reads the same stale
    conversation history and answers independently."""

    @abstractmethod
    def hold(self, key: str) -> AbstractAsyncContextManager[None]:
        """Blocks until no other holder of `key` is running, anywhere in the
        deployment.

        Implementations must guarantee release on success, exception, and
        cancellation — a lock leaked by a failed turn would wedge the rest
        of a live emergency call."""
        ...


class NullCallLock(CallLock):
    """No-op implementation for callers that genuinely need no
    serialisation. Exists so a missing lock is an explicit choice at the
    wiring site rather than an `if self._lock is not None` branch threaded
    through the service."""

    def hold(self, key: str) -> AbstractAsyncContextManager[None]:
        return _null_context()


def _null_context() -> AbstractAsyncContextManager[None]:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm() -> AsyncIterator[None]:
        yield

    return _cm()
