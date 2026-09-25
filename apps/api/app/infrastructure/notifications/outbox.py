"""The emergency-alert outbox worker: sends alerts whose tickets have
committed.

Two ways in, one mechanism:

- `deliver(ticket_id)` — the request's post-commit hook, so the first
  attempt goes out the moment the ticket exists, with no polling delay.
- `run_forever()` — a per-worker poll loop started in the app lifespan,
  which sends retries when they fall due and anything a crash or restart
  left behind.

Both call `EmergencyNotificationService.deliver_next_due` in a transaction
of their own, one alert per transaction, so a row lock is held only for one
bounded send. Postgres is the queue; there is no broker to run or lose.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.emergency_notification_service import (
    EmergencyNotificationService,
)
from app.core.config import Settings
from app.domain.notifications.provider import NotificationPort
from app.infrastructure.database.repositories import (
    SqlAlchemyEmergencyTicketRepository,
    SqlAlchemyNotificationDeliveryRepository,
    SqlAlchemyNotificationSettingsRepository,
)
from app.infrastructure.notifications.providers import build_notification_provider

logger = structlog.get_logger("app.notifications.outbox")

# Upper bound on alerts sent per drain, so one poll can never monopolise a
# worker. Anything left is picked up on the next tick.
_MAX_PER_DRAIN = 25


class EmergencyAlertOutbox:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        provider: NotificationPort | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._provider = provider or build_notification_provider(settings)

    async def deliver(self, ticket_id: uuid.UUID | None = None) -> int:
        """Sends due alerts — only this ticket's when one is given — and
        returns how many attempts were made. Never raises: this runs after a
        request has already succeeded, and on the poller's loop."""
        attempted = 0
        while attempted < _MAX_PER_DRAIN:
            try:
                async with self._session_factory() as session:
                    service = EmergencyNotificationService(
                        provider=self._provider,
                        settings_repository=SqlAlchemyNotificationSettingsRepository(session),
                        delivery_repository=SqlAlchemyNotificationDeliveryRepository(session),
                        ticket_repository=SqlAlchemyEmergencyTicketRepository(session),
                        settings=self._settings,
                    )
                    delivery = await service.deliver_next_due(ticket_id=ticket_id)
                    await session.commit()
            except Exception as exc:
                # The transaction rolled back, so the row is untouched and
                # still due: the poller retries it. Type only — never the
                # message, which could quote caller details.
                logger.error("emergency_alert_outbox_failed", error=type(exc).__name__)
                return attempted
            if delivery is None:
                return attempted
            attempted += 1
            if ticket_id is not None:
                return attempted
        return attempted

    async def run_forever(self) -> None:
        """Polls until cancelled (at shutdown)."""
        interval = self._settings.NOTIFICATION_OUTBOX_POLL_SECONDS
        logger.info("emergency_alert_outbox_started", poll_seconds=interval)
        while True:
            try:
                await self.deliver()
            except Exception as exc:
                # `deliver` is written not to raise, but a poller that dies on
                # one bad tick silently stops every retry in this worker for
                # the rest of its life. Log and keep polling.
                logger.error("emergency_alert_outbox_tick_failed", error=type(exc).__name__)
            await asyncio.sleep(interval)


_outbox: EmergencyAlertOutbox | None = None


def get_alert_outbox() -> EmergencyAlertOutbox:
    """The process-wide outbox. Built lazily so importing this module has no
    side effects; overridable in tests through `deps.get_alert_outbox`."""
    global _outbox
    if _outbox is None:
        from app.core.config import get_settings
        from app.infrastructure.database.session import AsyncSessionLocal

        _outbox = EmergencyAlertOutbox(settings=get_settings(), session_factory=AsyncSessionLocal)
    return _outbox
