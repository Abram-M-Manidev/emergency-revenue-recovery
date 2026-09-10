"""Alerting a human about an emergency ticket, and being honest about whether
it worked.

This is the service behind the one sentence ESSR could not previously back:
"a dispatcher has been alerted." It owns the parts that must not live in a
provider — idempotency, the attempt budget, the time bound, and the
persisted answer to "was anyone actually told?" — and leaves the provider
with a single job, one send.

Ordering, and why the ticket comes first
-----------------------------------------
The ticket is written before any alert is attempted, and a failed alert never
rolls it back. A caller who reports a gas leak must end up in the dispatch
queue whether or not Slack was reachable; losing the record because the
notification failed would turn a degraded alert into a lost emergency. What
the failure changes is what the assistant is permitted to *say*, not what the
business ends up holding.

Latency, on a live phone call
------------------------------
Every millisecond here is silence the caller hears, so the whole operation
runs under one wall-clock budget
(`NOTIFICATION_TIMEOUT_SECONDS` x `NOTIFICATION_MAX_ATTEMPTS`, both bounded
and small by default) and the caller-facing path never waits on anything
else. Exceeding it is not an exception: it is a `FAILED` delivery, which the
assistant then reports truthfully.
"""

from __future__ import annotations

import asyncio
import uuid

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
from app.domain.repositories.notification_repository import (
    NotificationDeliveryRepository,
    NotificationSettingsRepository,
)
from app.shared.logging.timing import elapsed_ms, now

logger = structlog.get_logger("app.notifications")


class EmergencyNotificationService:
    def __init__(
        self,
        *,
        provider: NotificationPort,
        settings_repository: NotificationSettingsRepository,
        delivery_repository: NotificationDeliveryRepository,
        settings: Settings,
    ) -> None:
        self._provider = provider
        self._settings_repository = settings_repository
        self._deliveries = delivery_repository
        self._settings = settings

    async def notify_ticket(self, ticket: EmergencyTicket) -> NotificationDelivery:
        """Ensures exactly one alert goes out for this ticket, and reports
        what happened.

        Safe to call repeatedly — and it is called repeatedly, by design: the
        tool loop calls it when `create_service_request` opens a ticket, and
        the webhook's outcome sync calls it again on the same ticket when a
        re-sent transcript replays the turn. The second call returns the first
        call's answer rather than sending again.
        """
        organization_id = ticket.organization_id
        started_at = now()

        destination = await self._resolve_destination(organization_id)
        channel = destination[0] if destination else None

        delivery, is_ours_to_send = await self._deliveries.claim(
            organization_id,
            ticket.id,
            channel=channel,
            provider=self._provider.name,
        )

        # Someone else already owns this ticket's alert: report their result
        # rather than sending a second time. The exception is a previous
        # attempt that FAILED while budget remains, which is the retry path a
        # transient Slack outage needs.
        if not is_ours_to_send and not self._should_retry(delivery):
            logger.info(
                "emergency_notification_already_handled",
                organization_id=str(organization_id),
                ticket_id=str(ticket.id),
                status=delivery.status.value,
                attempts=delivery.attempts,
            )
            return delivery

        receipt = await self._attempt(ticket, destination)
        delivery = await self._deliveries.record_result(
            organization_id,
            ticket.id,
            status=receipt.status,
            error_code=receipt.error_code,
            provider=receipt.provider,
        )

        # One line per emergency alert, in derived fields only. The
        # destination is a credential and the caller's name, number and
        # address are PII; neither appears here, and the ticket id is enough
        # to join to everything else during an incident review.
        logger.info(
            "emergency_notification_attempted",
            organization_id=str(organization_id),
            ticket_id=str(ticket.id),
            conversation_id=str(ticket.conversation_id),
            provider=receipt.provider,
            channel=channel.value if channel else None,
            status=receipt.status.value,
            error_code=receipt.error_code,
            attempts=delivery.attempts,
            alerted_a_human=delivery.alerted_a_human,
            elapsed_ms=elapsed_ms(started_at),
        )
        return delivery

    async def get_delivery(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> NotificationDelivery | None:
        """This ticket's alerting state, for callers that need the answer
        without triggering a send."""
        return await self._deliveries.get_for_ticket(organization_id, ticket_id)

    # --- internals ---

    def _should_retry(self, delivery: NotificationDelivery) -> bool:
        """A previous attempt failed and there is budget left.

        Only `FAILED` is retried. `DELIVERED` is done; `NOT_CONFIGURED` is an
        onboarding gap that retrying cannot fix and would only turn into
        wasted latency on every subsequent turn of the call; `PENDING` means
        another request is in flight right now, and racing it is how one
        incident becomes two pages."""
        return (
            delivery.status is DeliveryStatus.FAILED
            and delivery.attempts < self._settings.NOTIFICATION_MAX_ATTEMPTS
        )

    async def _resolve_destination(
        self, organization_id: uuid.UUID
    ) -> tuple[NotificationChannel, str] | None:
        """The organization's configured target, or None.

        Read per call and never cached on the service: one instance serves
        every tenant in the process, so a cached destination would be a
        cross-tenant leak waiting for a refactor. A storage failure here is
        deliberately not swallowed into "no destination" — that would be
        indistinguishable from a business that never configured one, and
        would quietly stop alerting an organization that had."""
        return await self._settings_repository.get_destination(organization_id)

    async def _attempt(
        self,
        ticket: EmergencyTicket,
        destination: tuple[NotificationChannel, str] | None,
    ) -> NotificationReceipt:
        """One bounded send.

        The provider is contractually non-raising, but this wraps it anyway:
        a defect in an adapter must degrade to "we could not confirm the
        alert" rather than aborting a turn mid-sentence, which on a phone
        call is a silent hang-up on someone reporting an emergency.
        """
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
