"""An emergency page: getting one named person to *acknowledge* an emergency,
escalating when they do not, and never pretending either happened.

Why this exists alongside the webhook alert
-------------------------------------------
`emergency_notification_deliveries` answers "did a provider accept our
message to the team's channel?". That is a broadcast; it cannot tell anyone
whether a human saw it. Paging answers a different question — "has a
responsible person said *I have it*?" — and so it needs things the alert
does not: a named recipient, a deadline, a backup, and an explicit
acknowledgement that only a person can produce.

Three facts that are routinely conflated, and must not be
---------------------------------------------------------
- **sent**: a provider (the SMS gateway, the telephony API) accepted the
  message. Nothing more. A voice call "sent" may ring out unanswered; an SMS
  "sent" may sit unread on a phone in a drawer.
- **acknowledged**: a person took an explicit action — opened the signed link
  in the text and pressed Acknowledge, or pressed Acknowledge in the
  dashboard. The only state that stops escalation, and the only one that
  licenses "the on-call technician has acknowledged".
- **on the way**: nothing in this system records it. Acknowledgement is "I
  have it", not "I am driving there", so nothing here ever licenses it.

The state machine (one `EmergencyPage` per ticket)
--------------------------------------------------
    paging_primary --(ack)--> acknowledged
    paging_primary --(deadline, backup configured)--> paging_backup
    paging_primary --(deadline, no backup)--> unresolved
    paging_backup  --(ack)--> acknowledged
    paging_backup  --(deadline)--> unresolved
    unresolved     --(ack)--> acknowledged        # a late "I have it" still counts

`unresolved` is explicit on purpose: "nobody acknowledged" is the incident an
operator must see, not an absence of data. The deadline is brought forward to
"now" when every notification to the current recipient has failed outright —
waiting out an acknowledgement window for a page that never left the building
would only delay the backup.

Each `PageNotification` (one per recipient × channel) is its own small outbox
row: queued → sending → sent | retrying → … | failed, or canceled when the
page was acknowledged before it went out.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.domain.paging.settings import PagingChannel, mask_paging_number


class PageStatus(str, Enum):
    PAGING_PRIMARY = "paging_primary"
    PAGING_BACKUP = "paging_backup"
    ACKNOWLEDGED = "acknowledged"
    UNRESOLVED = "unresolved"

    @property
    def is_escalating(self) -> bool:
        """Still waiting on someone, with a deadline running."""
        return self in (PageStatus.PAGING_PRIMARY, PageStatus.PAGING_BACKUP)


class RecipientRole(str, Enum):
    PRIMARY = "primary"
    BACKUP = "backup"


class AcknowledgementMethod(str, Enum):
    #: The signed link in the page's text message.
    LINK = "link"
    #: A signed-in user with dispatch authority pressed Acknowledge.
    DASHBOARD = "dashboard"


class PageNotificationStatus(str, Enum):
    QUEUED = "queued"
    #: Claimed by a worker, under a lease. If the worker dies the lease
    #: expires and the row is attempted again.
    SENDING = "sending"
    #: The provider accepted it. NOT delivered to a person, NOT acknowledged.
    SENT = "sent"
    #: The last attempt failed; another is scheduled.
    RETRYING = "retrying"
    #: Terminal: rejected, or every attempt failed.
    FAILED = "failed"
    #: Terminal: the page was acknowledged before this went out.
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            PageNotificationStatus.SENT,
            PageNotificationStatus.FAILED,
            PageNotificationStatus.CANCELED,
        )


class PagingOutcome(str, Enum):
    """What a provider reported about one send attempt, in our vocabulary."""

    #: The provider took responsibility for the message.
    ACCEPTED = "accepted"
    #: A permanent refusal (invalid number, unverified destination, bad
    #: credentials). Retrying would get the same answer.
    REJECTED = "rejected"
    #: Transient (timeout, 5xx, 429, network). Worth retrying.
    FAILED = "failed"
    #: No provider is set up to send on this channel at all.
    NOT_CONFIGURED = "not_configured"


class CallerPagingState(str, Enum):
    """The only part of paging the assistant ever sees — coarse on purpose,
    so no recipient, number or timing can leak into a prompt, and each value
    maps to exactly one sentence the assistant is allowed to say."""

    #: This business does not page anyone (or no page exists).
    OFF = "off"
    #: Pages are queued or in flight; nobody has accepted one yet.
    QUEUED = "queued"
    #: At least one page was accepted by a provider; nobody has acknowledged.
    SENT = "sent"
    #: A person explicitly acknowledged.
    ACKNOWLEDGED = "acknowledged"
    #: Every attempt failed; nobody was paged.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EmergencyPage:
    id: uuid.UUID
    organization_id: uuid.UUID
    ticket_id: uuid.UUID
    status: PageStatus
    #: When the current recipient's acknowledgement window closes. None once
    #: the page is acknowledged or unresolved.
    escalate_at: datetime | None
    ack_timeout_seconds: int
    escalated_at: datetime | None
    acknowledged_at: datetime | None
    acknowledged_by_role: RecipientRole | None
    acknowledged_via: AcknowledgementMethod | None
    acknowledged_by_user_id: uuid.UUID | None
    unresolved_at: datetime | None
    unresolved_reason: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def current_role(self) -> RecipientRole:
        return (
            RecipientRole.BACKUP
            if self.status is PageStatus.PAGING_BACKUP or self.escalated_at is not None
            else RecipientRole.PRIMARY
        )


@dataclass(frozen=True, slots=True)
class PageNotification:
    id: uuid.UUID
    organization_id: uuid.UUID
    page_id: uuid.UUID
    role: RecipientRole
    channel: PagingChannel
    #: Snapshot of the number paged, taken when this row was created, so the
    #: record says who was actually paged even if settings change later.
    #: Never logged, never returned unmasked outside the Owner's settings.
    destination: str
    status: PageNotificationStatus
    attempts: int
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    provider: str | None
    provider_message_id: str | None
    error_code: str | None
    sent_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def destination_hint(self) -> str:
        return mask_paging_number(self.destination) or ""

    @property
    def idempotency_key(self) -> str:
        """Stable across every attempt of this one notification, so a
        receiver (or a provider that supports it) can discard the duplicate
        a crashed worker may cause."""
        return f"emergency_page:{self.page_id}:{self.role.value}:{self.channel.value}"


@dataclass(frozen=True, slots=True)
class NewPageNotification:
    """A notification to create: who, how."""

    role: RecipientRole
    channel: PagingChannel
    destination: str


@dataclass(frozen=True, slots=True)
class PageMessage:
    """One message handed to a provider. Built at send time from the ticket
    as it is now, so details the caller gave after the page started are
    included."""

    organization_id: uuid.UUID
    ticket_id: uuid.UUID
    channel: PagingChannel
    to: str
    #: SMS body, or the sentence spoken on the call.
    body: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PagingReceipt:
    outcome: PagingOutcome
    provider: str
    #: The provider's own id for the message/call (e.g. a Twilio SID), kept
    #: for tracing. Never a response body.
    provider_message_id: str | None = None
    #: A short token — `timeout`, `http_503`, `invalid_number` — never a
    #: response body, which can echo the destination back.
    error_code: str | None = None


def caller_paging_state(
    page: EmergencyPage | None, notifications: Sequence[PageNotification]
) -> CallerPagingState:
    """Reduces a page to the one fact the assistant may act on.

    Order matters: acknowledgement beats everything; any accepted send beats
    in-flight work; and only when nothing is left in flight and nothing was
    ever accepted is it `failed`."""
    if page is None:
        return CallerPagingState.OFF
    if page.status is PageStatus.ACKNOWLEDGED:
        return CallerPagingState.ACKNOWLEDGED
    if any(n.status is PageNotificationStatus.SENT for n in notifications):
        return CallerPagingState.SENT
    if any(not n.status.is_terminal for n in notifications):
        return CallerPagingState.QUEUED
    if page.status.is_escalating and not notifications:
        # A page with no notifications yet is still starting up.
        return CallerPagingState.QUEUED
    return CallerPagingState.FAILED


def every_attempt_failed(
    notifications: Sequence[PageNotification], role: RecipientRole
) -> bool:
    """True when this recipient cannot be reached at all: they have
    notifications, and every one of them failed terminally. Used to escalate
    immediately instead of waiting out an acknowledgement window for a page
    that never left the building."""
    mine = [n for n in notifications if n.role is role]
    return bool(mine) and all(n.status is PageNotificationStatus.FAILED for n in mine)
