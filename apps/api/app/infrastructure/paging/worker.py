"""The emergency-paging outbox worker.

Same shape as `EmergencyAlertOutbox` — Postgres is the queue, one worker
per uvicorn process, `FOR UPDATE SKIP LOCKED` so they never collide — with
two ways in:

- `deliver(page_id)` — the request's post-commit hook, so a new page's first
  notifications go out the moment its ticket is committed.
- `run_forever()` — the poller: escalates overdue pages, sends retries, and
  recovers anything a crash or restart left behind (including a send whose
  worker died mid-flight, once its lease expires).

Every step runs in its own short transaction, and the provider call itself
runs with no transaction open at all.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.emergency_paging_service import EmergencyPagingService
from app.core.config import Settings
from app.domain.paging.page import PageStatus
from app.domain.paging.port import AckLinkSigner, PagingProvider
from app.infrastructure.database.repositories import (
    SqlAlchemyEmergencyTicketRepository,
    SqlAlchemyOrganizationRepository,
)
from app.infrastructure.database.repositories.paging_repository_impl import (
    SqlAlchemyEmergencyPageRepository,
    SqlAlchemyPagingSettingsRepository,
)
from app.infrastructure.paging.ack_links import HmacAckLinkSigner
from app.infrastructure.paging.providers import build_paging_provider

logger = structlog.get_logger("app.paging.worker")

# Upper bound on steps per drain, so one tick can never monopolise a worker.
_MAX_PER_DRAIN = 25


class EmergencyPagingWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        provider: PagingProvider | None = None,
        signer: AckLinkSigner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._provider = provider or build_paging_provider(settings)
        self._signer = signer or HmacAckLinkSigner(settings.JWT_SECRET_KEY)
        self._clock = clock

    def _service(self, session: AsyncSession) -> EmergencyPagingService:
        return EmergencyPagingService(
            settings_repository=SqlAlchemyPagingSettingsRepository(session),
            page_repository=SqlAlchemyEmergencyPageRepository(session),
            ticket_repository=SqlAlchemyEmergencyTicketRepository(session),
            organization_repository=SqlAlchemyOrganizationRepository(session),
            provider=self._provider,
            signer=self._signer,
            settings=self._settings,
            clock=self._clock,
        )

    async def deliver(self, page_id: uuid.UUID | None = None) -> int:
        """Sends due notifications — only this page's when one is given — and
        returns how many were attempted or closed. Never raises."""
        handled = 0
        while handled < _MAX_PER_DRAIN:
            try:
                async with self._session_factory() as session:
                    claimed = await self._service(session).claim_next_send(page_id=page_id)
                    await session.commit()
            except Exception as exc:
                # Rolled back: the row is untouched and still due.
                logger.error("emergency_page_claim_failed", error=type(exc).__name__)
                return handled
            if claimed is None:
                return handled
            handled += 1
            if claimed.message is None:
                continue  # resolved at claim time, nothing to send

            # No transaction is open across the provider call: the claim is
            # committed, and `send` touches no repository.
            async with self._session_factory() as session:
                receipt = await self._service(session).send(claimed)
                await session.rollback()

            try:
                async with self._session_factory() as session:
                    await self._service(session).record_send_result(claimed, receipt)
                    await session.commit()
            except Exception as exc:
                # The row stays `sending`; when its lease expires it is due
                # again and re-attempted. A page that was accepted may be sent
                # twice — the at-least-once side, deliberately.
                logger.error("emergency_page_record_failed", error=type(exc).__name__)
                return handled
        return handled

    async def escalate(self) -> int:
        """Moves every overdue page on, sending the backup's pages at once.
        Returns how many pages changed. Never raises."""
        changed = 0
        while changed < _MAX_PER_DRAIN:
            try:
                async with self._session_factory() as session:
                    page = await self._service(session).escalate_next_due()
                    await session.commit()
            except Exception as exc:
                logger.error("emergency_page_escalation_failed", error=type(exc).__name__)
                return changed
            if page is None:
                return changed
            changed += 1
            if page.status is PageStatus.PAGING_BACKUP:
                await self.deliver(page.id)
        return changed

    async def run_once(self) -> None:
        await self.escalate()
        await self.deliver()

    async def run_forever(self) -> None:
        """Polls until cancelled (at shutdown)."""
        interval = self._settings.NOTIFICATION_OUTBOX_POLL_SECONDS
        logger.info("emergency_paging_worker_started", poll_seconds=interval)
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                # A poller that dies on one bad tick silently stops every
                # escalation in this worker. Log and keep going.
                logger.error("emergency_paging_tick_failed", error=type(exc).__name__)
            await asyncio.sleep(interval)


_worker: EmergencyPagingWorker | None = None


def get_paging_worker() -> EmergencyPagingWorker:
    """The process-wide worker, built lazily; overridable in tests through
    `deps.get_paging_worker`."""
    global _worker
    if _worker is None:
        from app.core.config import get_settings
        from app.infrastructure.database.session import AsyncSessionLocal

        _worker = EmergencyPagingWorker(settings=get_settings(), session_factory=AsyncSessionLocal)
    return _worker
