"""Alerting a human about an emergency ticket, reliably, and being honest
about whether it has happened yet.

This is the service behind the one sentence ESSR could not previously back:
"a dispatcher has been alerted." It owns the parts that must not live in a
provider — the outbox, idempotency, the retry schedule, the time bound, and
the persisted answer to "was anyone actually told?" — and leaves the provider
with a single job, one send.

The transactional outbox
------------------------
Alerts used to be sent from inside the live voice turn, before the request's
transaction committed. Anything that failed later in the turn — or the commit
itself — rolled the ticket back after the dispatcher had already been paged
about it. The send is now split in two:

1. `enqueue` runs inside the transaction that creates the ticket and writes
   the ticket's delivery row as `pending`. No I/O. The ticket and its alert
   commit or roll back together, so an alert can never exist for a ticket
   that does not, and a committed ticket can never lack its alert.
2. `deliver_next_due` runs only after commit — immediately, from the
   request's post-commit hook, and on a schedule from the outbox poller for
   retries and anything a crash left behind. It locks one due row with
   `FOR UPDATE SKIP LOCKED`, sends, and records the result in the same
   transaction, so concurrent workers can never send the same alert twice.

A failed send never touches the ticket; it only reschedules the row with
backoff until `NOTIFICATION_MAX_ATTEMPTS` is spent.

Delivery is at-least-once in exactly one case: a worker that dies after the
provider accepted the alert but before its transaction commits. The row is
then still due and is sent again. Every alert carries a ticket-derived
`idempotency_key` so a receiver can discard that duplicate. Exactly-once is
not achievable against a third-party webhook; this is the closest honest
guarantee.

What the caller may be told
---------------------------
Because the send now happens after the turn, the turn that creates the
ticket can only truthfully say the team is BEING alerted (`is_queued`). A
later turn reads the row back and may say "a dispatcher has been alerted"
only once it reads `delivered`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import structlog

from app.core.config import Settings
from app.domain.entities.emergency_ticket import EmergencyTicket
from app.domain.notifications.emergency import (
    DeliveryStatus,
    EmergencyAlert,
    NotificationChannel,
    NotificationDelivery,
    NotificationReceipt,
)
from app.domain.notifications.provider import NotificationPort
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository
from app.domain.repositories.notification_repository import (
    NotificationDeliveryRepository,
    NotificationSettingsRepository,
)
from app.domain.transactions import AfterCommit, NullAfterCommit
from app.shared.logging.timing import elapsed_ms, now

logger = structlog.get_logger("app.notifications")

# Longest wait between two attempts, whatever the backoff says. An emergency
# alert that is still failing should be retried often enough that a fixed
# endpoint starts receiving within minutes, not hours.
_MAX_RETRY_DELAY_SECONDS = 900


class EmergencyNotificationService:
    def __init__(
        self,
        *,
        provider: NotificationPort,
        settings_repository: NotificationSettingsRepository,
        delivery_repository: NotificationDeliveryRepository,
        settings: Settings,
        # Where a queued alert's ticket is read back from at send time —
        # scoped by the delivery row's own organization. Required to send;
        # a service built only to enqueue does not need it.
        ticket_repository: EmergencyTicketRepository | None = None,
        # The request's post-commit hook, and what to run on it: the
        # immediate send of the alert just queued. Both optional — without
        # them the alert is still queued and the poller delivers it.
        after_commit: AfterCommit | None = None,
        deliver_after_commit: Callable[[uuid.UUID], Awaitable[object]] | None = None,
    ) -> None:
        self._provider = provider
        self._settings_repository = settings_repository
        self._deliveries = delivery_repository
        self._settings = settings
        self._tickets = ticket_repository
        self._after_commit = after_commit or NullAfterCommit()
        self._deliver_after_commit = deliver_after_commit

    # --- Inside the ticket's transaction ------------------------------------

    async def enqueue(self, ticket: EmergencyTicket) -> NotificationDelivery:
        """Queues the alert for this ticket in the current transaction.

        Idempotent by ticket, so the tool loop and the webhook's outcome sync
        both calling it for one ticket queue exactly one alert. An
        organization with nowhere to send is recorded as `not_configured`
        immediately — terminal, never retried, and never licensing a claim.
        """
        organization_id = ticket.organization_id
        destination = await self._resolve_destination(organization_id)
        delivery, created = await self._deliveries.enqueue(
            organization_id,
            ticket.id,
            channel=destination[0] if destination else None,
            provider=self._provider.name,
            status=DeliveryStatus.PENDING if destination else DeliveryStatus.NOT_CONFIGURED,
            next_attempt_at=datetime.now(timezone.utc) if destination else None,
        )
        if created:
            logger.info(
                "emergency_alert_enqueued",
                organization_id=str(organization_id),
                ticket_id=str(ticket.id),
                status=delivery.status.value,
            )
        if delivery.is_queued and self._deliver_after_commit is not None:
            deliver = self._deliver_after_commit
            ticket_id = ticket.id

            async def _deliver() -> None:
                await deliver(ticket_id)

            self._after_commit.register(_deliver)
        return delivery

    async def get_delivery(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> NotificationDelivery | None:
        """This ticket's alerting state, without sending anything."""
        return await self._deliveries.get_for_ticket(organization_id, ticket_id)

    # --- After commit: the outbox worker --------------------------------------

    async def deliver_next_due(
        self, *, ticket_id: uuid.UUID | None = None, at: datetime | None = None
    ) -> NotificationDelivery | None:
        """Sends one due alert and records what happened, or returns None when
        nothing is due.

        Must run in its own transaction, committed by the caller: the row is
        locked from selection until that commit, which is what stops a second
        worker sending the same alert. Never raises for a send failure — that
        is recorded and rescheduled."""
        if self._tickets is None:
            raise RuntimeError("deliver_next_due needs a ticket repository")
        started_at = now()
        current = at or datetime.now(timezone.utc)

        delivery = await self._deliveries.lock_next_due(now=current, ticket_id=ticket_id)
        if delivery is None:
            return None
        organization_id = delivery.organization_id

        # Read back under the DELIVERY ROW'S organization, never any other.
        # A row whose ticket is not this organization's — impossible through
        # the write path, but a corrupted or hand-edited row must still not
        # carry one tenant's emergency to another tenant's destination — is
        # closed as failed without sending anything.
        ticket = await self._tickets.get_by_id(organization_id, delivery.ticket_id)
        if ticket is None:
            logger.error(
                "emergency_alert_ticket_unavailable",
                organization_id=str(organization_id),
                delivery_id=str(delivery.id),
            )
            return await self._deliveries.record_attempt(
                delivery.id,
                status=DeliveryStatus.FAILED,
                error_code="ticket_unavailable",
                provider=self._provider.name,
                next_attempt_at=None,
            )

        destination = await self._resolve_destination(organization_id)
        receipt = await self._attempt(ticket, destination)

        attempts = delivery.attempts + 1
        next_attempt_at = None
        if receipt.status is DeliveryStatus.FAILED:
            if attempts < self._settings.NOTIFICATION_MAX_ATTEMPTS:
                next_attempt_at = current + self._retry_delay(attempts)
            else:
                # Loud on purpose: an emergency nobody could be told about is
                # the incident an operator must hear about.
                logger.error(
                    "emergency_alert_abandoned",
                    organization_id=str(organization_id),
                    ticket_id=str(ticket.id),
                    attempts=attempts,
                    error_code=receipt.error_code,
                )

        updated = await self._deliveries.record_attempt(
            delivery.id,
            status=receipt.status,
            error_code=receipt.error_code,
            provider=receipt.provider,
            next_attempt_at=next_attempt_at,
        )
        # Derived fields only. The destination is a credential and the
        # caller's details are PII; the ticket id joins to everything else.
        logger.info(
            "emergency_notification_attempted",
            organization_id=str(organization_id),
            ticket_id=str(ticket.id),
            conversation_id=str(ticket.conversation_id),
            provider=receipt.provider,
            channel=destination[0].value if destination else None,
            status=receipt.status.value,
            error_code=receipt.error_code,
            attempts=updated.attempts,
            alerted_a_human=updated.alerted_a_human,
            retry_scheduled=next_attempt_at is not None,
            elapsed_ms=elapsed_ms(started_at),
        )
        return updated

    # --- internals ---

    def _retry_delay(self, attempts_so_far: int) -> timedelta:
        """Exponential backoff from `NOTIFICATION_RETRY_BASE_SECONDS`,
        capped, so a transient outage is retried within seconds and a longer
        one still gets an attempt every few minutes."""
        base = self._settings.NOTIFICATION_RETRY_BASE_SECONDS
        seconds = min(base * (3 ** (attempts_so_far - 1)), _MAX_RETRY_DELAY_SECONDS)
        return timedelta(seconds=seconds)

    async def _resolve_destination(
        self, organization_id: uuid.UUID
    ) -> tuple[NotificationChannel, str] | None:
        """The organization's configured target, or None.

        Read per call and never cached: one instance serves every tenant in
        the process, so a cached destination would be a cross-tenant leak
        waiting for a refactor."""
        return await self._settings_repository.get_destination(organization_id)

    async def _attempt(
        self,
        ticket: EmergencyTicket,
        destination: tuple[NotificationChannel, str] | None,
    ) -> NotificationReceipt:
        """One bounded send. The provider is contractually non-raising, but
        this wraps it anyway: an adapter defect must become a recorded,
        retried failure, never an exception that loses the outbox row's
        state."""
        alert = _alert_for(ticket)
        target = destination[1] if destination else None
        try:
            return await asyncio.wait_for(
                self._provider.send(alert, target),
                timeout=self._settings.NOTIFICATION_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return NotificationReceipt(
                status=DeliveryStatus.FAILED,
                provider=self._provider.name,
                error_code="timeout",
            )
        except Exception:
            logger.error(
                "emergency_notification_provider_raised",
                organization_id=str(ticket.organization_id),
                ticket_id=str(ticket.id),
                provider=self._provider.name,
                exc_info=True,
            )
            return NotificationReceipt(
                status=DeliveryStatus.FAILED,
                provider=self._provider.name,
                error_code="provider_error",
            )


def _alert_for(ticket: EmergencyTicket) -> EmergencyAlert:
    """Built at SEND time from the ticket as it is now, so contact details
    the caller gave after the ticket was opened reach the dispatcher too."""
    return EmergencyAlert(
        organization_id=ticket.organization_id,
        ticket_id=ticket.id,
        conversation_id=ticket.conversation_id,
        summary=ticket.summary,
        customer_name=ticket.customer_name,
        customer_phone=ticket.customer_phone,
        customer_address=ticket.customer_address,
        created_at=ticket.created_at,
    )
