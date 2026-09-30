"""Paging a named on-call person about an emergency, escalating to a backup
when nobody acknowledges, and never claiming more than the records show.

See `app/domain/paging/page.py` for the state machine and vocabulary. This
service owns the orchestration; providers own one send; repositories own the
locking that makes it safe to run on every uvicorn worker at once.

Built on the same transactional outbox as the webhook alert
-----------------------------------------------------------
1. `enqueue` runs inside the transaction that creates the ticket. It writes
   the page and its first notifications, and does no I/O. The ticket and its
   page commit or roll back together, so nobody is paged about a ticket that
   does not exist. (`DispatchService` isolates this call in its own savepoint:
   a paging defect can cost the page, never the ticket.)
2. After commit, the request's post-commit hook sends the first
   notifications at once; the outbox poller sends retries, escalates overdue
   pages, and recovers anything a crash left behind.

Unlike the alert outbox, a send is split across two short transactions —
`claim_next_send` (commit), the provider call with no lock held,
`record_send_result` (commit). The claim leaves the row `sending` under a
lease, so a worker that dies mid-send is detected when the lease expires and
the notification is attempted again, counted against its attempt budget. That
is also why delivery is at-least-once, never exactly-once: a worker that dies
after the provider accepted but before recording it causes one duplicate
page, carrying the same idempotency key. For paging that is the right side to
err on — a duplicate text at 3am is an annoyance; a missing one is a missed
emergency.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from app.core.config import Settings
from app.domain.entities.emergency_ticket import EmergencyTicket
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User
from app.domain.exceptions import (
    AuthorizationError,
    EntityNotFoundError,
    InvalidAcknowledgementLinkError,
)
from app.domain.paging.page import (
    AcknowledgementMethod,
    CallerPagingState,
    EmergencyPage,
    NewPageNotification,
    PageMessage,
    PageNotification,
    PageNotificationStatus,
    PageStatus,
    PagingOutcome,
    PagingReceipt,
    RecipientRole,
    caller_paging_state,
    every_attempt_failed,
)
from app.domain.paging.port import AckLinkSigner, PagingProvider
from app.domain.paging.settings import PagingChannel
from app.domain.repositories.emergency_ticket_repository import EmergencyTicketRepository
from app.domain.repositories.organization_repository import OrganizationRepository
from app.domain.repositories.paging_repository import (
    EmergencyPageRepository,
    PagingSettingsRepository,
)
from app.domain.transactions import AfterCommit, NullAfterCommit

logger = structlog.get_logger("app.paging")

# Extra time a claimed send may run past the provider timeout before the
# notification is treated as abandoned by a dead worker.
_LEASE_MARGIN_SECONDS = 30.0
# Longest wait between two attempts of one notification.
_MAX_RETRY_DELAY_SECONDS = 300.0
# Bounds on what is read aloud / texted, so one long summary cannot turn a
# page into several billed SMS segments or a minute of speech.
_MAX_SUMMARY_CHARS = 240


@dataclass(frozen=True, slots=True)
class PageView:
    """A page and its notifications, for the dashboard and the assistant."""

    page: EmergencyPage
    notifications: list[PageNotification]

    @property
    def caller_state(self) -> CallerPagingState:
        return caller_paging_state(self.page, self.notifications)


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    page: EmergencyPage
    #: False when the page was already acknowledged — the request is
    #: idempotent, and the original acknowledgement is kept as it was.
    newly_acknowledged: bool


@dataclass(frozen=True, slots=True)
class ClaimedSend:
    """A notification this worker has claimed. `message` is None when the
    claim resolved it without sending (budget spent, ticket unreadable)."""

    notification: PageNotification
    message: PageMessage | None


class EmergencyPagingService:
    def __init__(
        self,
        *,
        settings_repository: PagingSettingsRepository,
        page_repository: EmergencyPageRepository,
        settings: Settings,
        # Needed only to send (the worker); a request that only enqueues or
        # acknowledges does not build messages.
        ticket_repository: EmergencyTicketRepository | None = None,
        organization_repository: OrganizationRepository | None = None,
        provider: PagingProvider | None = None,
        signer: AckLinkSigner | None = None,
        # The request's post-commit hook and what to run on it: the
        # immediate first send of a page just created.
        after_commit: AfterCommit | None = None,
        deliver_after_commit: Callable[[uuid.UUID], Awaitable[object]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings_repository = settings_repository
        self._pages = page_repository
        self._settings = settings
        self._tickets = ticket_repository
        self._organizations = organization_repository
        self._provider = provider
        self._signer = signer
        self._after_commit = after_commit or NullAfterCommit()
        self._deliver_after_commit = deliver_after_commit
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --- Inside the ticket's transaction ------------------------------------

    async def enqueue(self, ticket: EmergencyTicket) -> EmergencyPage | None:
        """Starts paging for this ticket, or returns None when this business
        does not page anyone (off, or no primary, or no channel).

        Idempotent by ticket. Sends nothing: the first notifications go out
        from the post-commit hook, once the ticket is durable."""
        organization_id = ticket.organization_id
        paging = await self._settings_repository.get(organization_id)
        if paging is None or not paging.is_operational or paging.primary_number is None:
            return None

        now = self._clock()
        page, created = await self._pages.create(
            organization_id,
            ticket.id,
            ack_timeout_seconds=paging.ack_timeout_seconds,
            escalate_at=now + timedelta(seconds=paging.ack_timeout_seconds),
            notifications=[
                NewPageNotification(RecipientRole.PRIMARY, channel, paging.primary_number)
                for channel in paging.channels
            ],
            now=now,
        )
        if created:
            # Channels and whether a backup exists — never a number.
            logger.info(
                "emergency_page_created",
                organization_id=str(organization_id),
                ticket_id=str(ticket.id),
                page_id=str(page.id),
                channels=[channel.value for channel in paging.channels],
                has_backup=paging.backup_number is not None,
                ack_timeout_seconds=paging.ack_timeout_seconds,
            )
        if page.status.is_escalating and self._deliver_after_commit is not None:
            deliver = self._deliver_after_commit
            page_id = page.id

            async def _deliver() -> None:
                await deliver(page_id)

            self._after_commit.register(_deliver)
        return page

    # --- Reads -----------------------------------------------------------------

    async def get_for_ticket(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> PageView | None:
        page = await self._pages.get_for_ticket(organization_id, ticket_id)
        if page is None:
            return None
        return PageView(page, await self._pages.list_notifications(organization_id, page.id))

    async def caller_state(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> CallerPagingState:
        view = await self.get_for_ticket(organization_id, ticket_id)
        return view.caller_state if view is not None else CallerPagingState.OFF

    async def list_recent(self, organization_id: uuid.UUID, *, limit: int) -> list[PageView]:
        pages = await self._pages.list_for_organization(organization_id, limit=limit)
        return [
            PageView(page, await self._pages.list_notifications(organization_id, page.id))
            for page in pages
        ]

    # --- Acknowledgement ---------------------------------------------------------

    async def acknowledge_with_link(self, token: str) -> Acknowledgement:
        """Acknowledges on behalf of the recipient the signed link was sent
        to. The verified token is the only authority: it names the page and
        the role, and the page's organization comes from the row itself —
        so a link can only ever acknowledge the one page it was issued for.
        Malformed, forged and expired links fail identically."""
        if self._signer is None:
            raise InvalidAcknowledgementLinkError()
        verified = self._signer.verify(token)
        if verified is None:
            logger.info("emergency_page_ack_link_rejected", reason="bad_signature")
            raise InvalidAcknowledgementLinkError()
        page_id, role = verified
        page = await self._pages.lock_by_id(page_id)
        if page is None:
            logger.info("emergency_page_ack_link_rejected", reason="unknown_page")
            raise InvalidAcknowledgementLinkError()
        ttl = timedelta(hours=self._settings.PAGING_ACK_LINK_TTL_HOURS)
        if self._clock() - page.created_at > ttl:
            logger.info(
                "emergency_page_ack_link_rejected",
                reason="expired",
                organization_id=str(page.organization_id),
                page_id=str(page.id),
            )
            raise InvalidAcknowledgementLinkError()
        return await self._acknowledge(
            page, role=role, via=AcknowledgementMethod.LINK, user_id=None
        )

    async def acknowledge_by_user(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID, *, user: User
    ) -> Acknowledgement:
        """A signed-in user acknowledges from the dashboard.

        Needs dispatch authority — `dispatch:manage` (Owner/Admin) or
        `dispatch:update_assigned` (technicians, who are who gets paged). A
        read-only Member can see the page but not silence its escalation.
        Scoped by the user's own organization: another tenant's ticket is
        simply not found."""
        if not (
            user.has_permission(Permissions.DISPATCH_MANAGE)
            or user.has_permission(Permissions.DISPATCH_UPDATE_ASSIGNED)
        ):
            raise AuthorizationError("Acknowledging an emergency page requires dispatch access.")
        page = await self._pages.lock_for_ticket(organization_id, ticket_id)
        if page is None:
            raise EntityNotFoundError("EmergencyPage", str(ticket_id))
        return await self._acknowledge(
            page, role=None, via=AcknowledgementMethod.DASHBOARD, user_id=user.id
        )

    async def _acknowledge(
        self,
        page: EmergencyPage,
        *,
        role: RecipientRole | None,
        via: AcknowledgementMethod,
        user_id: uuid.UUID | None,
    ) -> Acknowledgement:
        """On a row the caller's transaction holds locked, so this and an
        escalation at the same instant are strictly ordered."""
        if page.status is PageStatus.ACKNOWLEDGED:
            logger.info(
                "emergency_page_ack_duplicate",
                organization_id=str(page.organization_id),
                page_id=str(page.id),
                via=via.value,
            )
            return Acknowledgement(page, newly_acknowledged=False)
        previous = page.status
        updated = await self._pages.mark_acknowledged(
            page.id, at=self._clock(), role=role, via=via, user_id=user_id
        )
        canceled = await self._pages.cancel_unsent(page.id)
        logger.info(
            "emergency_page_acknowledged",
            organization_id=str(page.organization_id),
            ticket_id=str(page.ticket_id),
            page_id=str(page.id),
            via=via.value,
            role=role.value if role else None,
            previous_status=previous.value,
            notifications_canceled=canceled,
        )
        return Acknowledgement(updated, newly_acknowledged=True)

    # --- Worker: sending ---------------------------------------------------------

    async def claim_next_send(self, *, page_id: uuid.UUID | None = None) -> ClaimedSend | None:
        """Claims one due notification and builds its message. The caller
        commits, then calls `send`, then `record_send_result` in a new
        transaction. Returns None when nothing is due."""
        now = self._clock()
        lease_until = now + timedelta(
            seconds=self._settings.PAGING_SEND_TIMEOUT_SECONDS + _LEASE_MARGIN_SECONDS
        )
        notification = await self._pages.claim_next_notification(
            now=now, lease_until=lease_until, page_id=page_id
        )
        if notification is None:
            return None

        if notification.attempts > self._settings.PAGING_MAX_ATTEMPTS:
            # Only reachable by re-claiming a row whose worker died mid-send
            # after it had used the last attempt: stop, loudly.
            await self._finish(
                notification,
                PagingReceipt(PagingOutcome.FAILED, self._provider_name(), error_code="worker_lost"),
                terminal=True,
            )
            return ClaimedSend(notification, None)

        message = await self._message_for(notification)
        if message is None:
            await self._finish(
                notification,
                PagingReceipt(
                    PagingOutcome.REJECTED, self._provider_name(), error_code="ticket_unavailable"
                ),
                terminal=True,
            )
            return ClaimedSend(notification, None)
        return ClaimedSend(notification, message)

    async def send(self, claimed: ClaimedSend) -> PagingReceipt:
        """One bounded provider call, with no database work. Never raises."""
        assert claimed.message is not None
        provider = self._provider
        if provider is None or not provider.supports(claimed.message.channel):
            return PagingReceipt(
                PagingOutcome.NOT_CONFIGURED,
                self._provider_name(),
                error_code="no_provider_for_channel",
            )
        try:
            return await asyncio.wait_for(
                provider.send(claimed.message),
                timeout=self._settings.PAGING_SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return PagingReceipt(PagingOutcome.FAILED, provider.name, error_code="timeout")
        except Exception as exc:
            # Type only: the message could quote the destination or body.
            logger.error(
                "emergency_page_provider_raised",
                organization_id=str(claimed.notification.organization_id),
                page_id=str(claimed.notification.page_id),
                error=type(exc).__name__,
            )
            return PagingReceipt(PagingOutcome.FAILED, provider.name, error_code="provider_error")

    async def record_send_result(
        self, claimed: ClaimedSend, receipt: PagingReceipt
    ) -> PageNotification | None:
        """Records what the provider said, schedules a retry or ends the
        notification, and brings escalation forward when the current
        recipient has become unreachable on every channel."""
        return await self._finish(claimed.notification, receipt, terminal=False)

    async def _finish(
        self, notification: PageNotification, receipt: PagingReceipt, *, terminal: bool
    ) -> PageNotification | None:
        now = self._clock()
        attempts = notification.attempts
        next_attempt_at: datetime | None = None
        sent_at: datetime | None = None
        if receipt.outcome is PagingOutcome.ACCEPTED:
            status = PageNotificationStatus.SENT
            sent_at = now
        elif (
            receipt.outcome is PagingOutcome.FAILED
            and not terminal
            and attempts < self._settings.PAGING_MAX_ATTEMPTS
        ):
            status = PageNotificationStatus.RETRYING
            next_attempt_at = now + self._retry_delay(attempts)
        else:
            status = PageNotificationStatus.FAILED

        updated = await self._pages.record_notification_result(
            notification.id,
            claimed_attempt=attempts,
            status=status,
            provider=receipt.provider,
            provider_message_id=receipt.provider_message_id,
            error_code=receipt.error_code,
            next_attempt_at=next_attempt_at,
            sent_at=sent_at,
        )
        if updated is None:
            # The lease ran out and another worker re-claimed this row; its
            # result is the one that stands.
            logger.warning(
                "emergency_page_result_superseded",
                organization_id=str(notification.organization_id),
                page_id=str(notification.page_id),
                notification_id=str(notification.id),
            )
            return None

        # Role, channel and outcome — never the number or the message.
        log = logger.error if status is PageNotificationStatus.FAILED else logger.info
        log(
            "emergency_page_notification_attempted",
            organization_id=str(notification.organization_id),
            page_id=str(notification.page_id),
            notification_id=str(notification.id),
            role=notification.role.value,
            channel=notification.channel.value,
            provider=receipt.provider,
            outcome=receipt.outcome.value,
            status=status.value,
            error_code=receipt.error_code,
            attempts=attempts,
            retry_scheduled=next_attempt_at is not None,
            # Accepted by a provider is NOT acknowledged by a person.
            acknowledged=False,
        )
        if status is PageNotificationStatus.FAILED:
            await self._escalate_early_if_unreachable(updated, now=now)
        return updated

    async def _escalate_early_if_unreachable(
        self, notification: PageNotification, *, now: datetime
    ) -> None:
        page = await self._pages.get_by_id(notification.organization_id, notification.page_id)
        if page is None or not page.status.is_escalating:
            return
        if page.current_role is not notification.role:
            return
        notifications = await self._pages.list_notifications(page.organization_id, page.id)
        if not every_attempt_failed(notifications, notification.role):
            return
        moved = await self._pages.bring_escalation_forward(
            page.id, expected_status=page.status, at=now
        )
        if moved:
            logger.warning(
                "emergency_page_recipient_unreachable",
                organization_id=str(page.organization_id),
                page_id=str(page.id),
                role=notification.role.value,
            )

    # --- Worker: escalation --------------------------------------------------------

    async def escalate_next_due(self) -> EmergencyPage | None:
        """Moves one overdue page on: primary -> backup, or -> unresolved.

        Runs on a row locked `FOR UPDATE SKIP LOCKED`, and an
        acknowledgement takes the same lock, so an acknowledgement arriving
        at the deadline either lands first (and there is nothing to escalate)
        or lands after (and still acknowledges, cancelling the backup's
        unsent pages). Returns the page it changed, or None."""
        now = self._clock()
        page = await self._pages.lock_next_escalation_due(now=now)
        if page is None:
            return None

        if page.status is PageStatus.PAGING_PRIMARY:
            paging = await self._settings_repository.get(page.organization_id)
            backup = (
                paging.backup_number
                if paging is not None and paging.is_enabled and paging.channels
                else None
            )
            if paging is not None and backup is not None:
                updated = await self._pages.mark_escalated(
                    page.id,
                    at=now,
                    escalate_at=now + timedelta(seconds=page.ack_timeout_seconds),
                )
                created = await self._pages.add_notifications(
                    page.organization_id,
                    page.id,
                    [
                        NewPageNotification(RecipientRole.BACKUP, channel, backup)
                        for channel in paging.channels
                    ],
                    now=now,
                )
                logger.warning(
                    "emergency_page_escalated",
                    organization_id=str(page.organization_id),
                    ticket_id=str(page.ticket_id),
                    page_id=str(page.id),
                    to_role=RecipientRole.BACKUP.value,
                    notifications_created=created,
                )
                return updated
            return await self._unresolve(page, now=now, reason="no_backup_configured")

        notifications = await self._pages.list_notifications(page.organization_id, page.id)
        reason = (
            "backup_unreachable"
            if every_attempt_failed(notifications, RecipientRole.BACKUP)
            else "no_acknowledgement"
        )
        return await self._unresolve(page, now=now, reason=reason)

    async def _unresolve(
        self, page: EmergencyPage, *, now: datetime, reason: str
    ) -> EmergencyPage:
        updated = await self._pages.mark_unresolved(page.id, at=now, reason=reason)
        # The loudest line paging writes: an emergency nobody took.
        logger.error(
            "emergency_page_unresolved",
            organization_id=str(page.organization_id),
            ticket_id=str(page.ticket_id),
            page_id=str(page.id),
            reason=reason,
            escalated=page.escalated_at is not None,
        )
        return updated

    # --- internals ---

    def _provider_name(self) -> str:
        return self._provider.name if self._provider is not None else "none"

    def _retry_delay(self, attempts_so_far: int) -> timedelta:
        base = self._settings.PAGING_RETRY_BASE_SECONDS
        return timedelta(
            seconds=min(base * (2 ** (attempts_so_far - 1)), _MAX_RETRY_DELAY_SECONDS)
        )

    async def _message_for(self, notification: PageNotification) -> PageMessage | None:
        """The text or spoken sentence for one notification, from the ticket
        as it is now — read back under the NOTIFICATION's own organization,
        so a corrupted row can never carry one tenant's emergency to another
        tenant's technician."""
        if self._tickets is None:
            raise RuntimeError("sending a page needs a ticket repository")
        page = await self._pages.get_by_id(notification.organization_id, notification.page_id)
        if page is None:
            return None
        ticket = await self._tickets.get_by_id(notification.organization_id, page.ticket_id)
        if ticket is None:
            logger.error(
                "emergency_page_ticket_unavailable",
                organization_id=str(notification.organization_id),
                page_id=str(notification.page_id),
            )
            return None
        business = "your business"
        if self._organizations is not None:
            organization = await self._organizations.get_by_id(notification.organization_id)
            if organization is not None:
                business = organization.name
        summary = _bounded(ticket.summary)
        if notification.channel is PagingChannel.SMS:
            body = self._sms_body(notification, page, ticket, business, summary)
        else:
            body = (
                f"This is an emergency page for {business}. A caller reported: {summary}. "
                "Check your text messages or the dispatch dashboard, and acknowledge the "
                "emergency there. Answering this call does not acknowledge it."
            )
        return PageMessage(
            organization_id=notification.organization_id,
            ticket_id=ticket.id,
            channel=notification.channel,
            to=notification.destination,
            body=body,
            idempotency_key=notification.idempotency_key,
        )

    def _sms_body(
        self,
        notification: PageNotification,
        page: EmergencyPage,
        ticket: EmergencyTicket,
        business: str,
        summary: str,
    ) -> str:
        lines = [f"EMERGENCY for {business}: {summary}"]
        if ticket.customer_name:
            lines.append(f"Caller: {_bounded(ticket.customer_name, 80)}")
        if ticket.customer_phone:
            lines.append(f"Callback: {ticket.customer_phone}")
        if ticket.customer_address:
            lines.append(f"Address: {_bounded(ticket.customer_address, 160)}")
        base_url = self._settings.PAGING_ACK_BASE_URL
        if base_url and self._signer is not None:
            token = self._signer.issue(page.id, notification.role)
            # In the fragment, never the path or query: a fragment is not sent
            # to any server, so the token stays out of web-server and proxy
            # access logs and out of Referer headers.
            lines.append(f"Acknowledge: {base_url}/ack#{token}")
        else:
            lines.append("Acknowledge it in the dispatch dashboard.")
        return "\n".join(lines)


def _bounded(text: str, limit: int = _MAX_SUMMARY_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
