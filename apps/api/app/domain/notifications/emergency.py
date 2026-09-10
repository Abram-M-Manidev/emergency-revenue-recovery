"""What an emergency alert is, and what happened when we tried to send it.

The product promise this exists to make true
--------------------------------------------
ESSR told emergency callers "a dispatcher has been alerted". Nothing in the
system sent anything: there was no outbound notification mechanism of any
kind, so that sentence was false on every call that produced one. A caller
with a gas leak was being reassured that a human knew, when no human did.

The fix is not only to send something. It is to make "we sent it" a fact the
backend can *check* before the assistant is allowed to say it — which is why
delivery is modelled as its own persisted state with its own vocabulary,
rather than as a boolean returned from a function nobody stores.

Three states that are routinely conflated, and must not be
----------------------------------------------------------
- The ticket exists. Staff will see it next time they look at the queue.
- The alert was accepted by a provider. Someone was actively told.
- The alert failed, or no provider is configured at all.

Only the second licenses "a dispatcher has been alerted". The first alone
licenses "I've logged this and the team will see it" — true, useful, and much
weaker. The third licenses neither, and must be said out loud rather than
papered over, because a caller who believes help is coming will stop looking
for it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class NotificationChannel(str, Enum):
    """How an organization wants its on-call staff reached.

    A closed enum rather than free text because each value implies a
    different provider, a different destination format, and different
    validation. `WEBHOOK` covers Slack, Teams, PagerDuty, and any HTTP
    endpoint an operations team already watches — deliberately first because
    it needs no vendor account, no per-message billing, and no phone-number
    provisioning, which makes it the one channel a pilot can actually turn on
    the day it starts.
    """

    WEBHOOK = "webhook"


class DeliveryStatus(str, Enum):
    """The lifecycle of one alert.

    `NOT_CONFIGURED` is separate from `FAILED` on purpose. Failure means we
    tried and the provider said no — worth retrying, worth alarming about.
    Not-configured means this organization never set up notifications, which
    is an onboarding gap, not an incident. Collapsing them would bury a
    fixable setup problem inside a noisy error metric, and would make it
    impossible to tell a business whose notifications are broken from one
    that never asked for them.
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"

    @property
    def alerted_a_human(self) -> bool:
        """The single question the caller-facing sentence turns on.

        A property rather than an `is DELIVERED` comparison scattered across
        call sites, so that if another terminal status is ever added there is
        exactly one place deciding whether it counts as a human having been
        told."""
        return self is DeliveryStatus.DELIVERED


@dataclass(frozen=True, slots=True)
class EmergencyAlert:
    """The message to send about one emergency ticket.

    Carries the ticket's identity and the operational minimum a dispatcher
    needs to act — who to call back, where to go, what is wrong. It
    deliberately does NOT carry the transcript, the recording, or anything
    else said in passing: this payload leaves our trust boundary and lands in
    a third-party chat tool, so it should contain what the job needs and
    nothing more.

    `idempotency_key` is derived from the ticket rather than generated per
    attempt. Vapi re-sends a webhook every time its transcript grows, and the
    tool loop can run `create_service_request` more than once in a call; both
    would otherwise page the on-call engineer repeatedly for one gas leak.
    """

    organization_id: uuid.UUID
    ticket_id: uuid.UUID
    conversation_id: uuid.UUID
    summary: str
    customer_name: str | None
    customer_phone: str | None
    customer_address: str | None
    created_at: datetime

    @property
    def idempotency_key(self) -> str:
        return f"emergency_ticket:{self.ticket_id}"


@dataclass(frozen=True, slots=True)
class NotificationReceipt:
    """What a provider reported about one send attempt.

    `status` is the provider's own verdict mapped into our vocabulary by the
    adapter — an HTTP 202 from a webhook and a queued SMS are both
    `DELIVERED` in the sense that matters: the provider accepted
    responsibility for the message. Anything short of that is `FAILED`,
    including a timeout, because an alert we cannot confirm is one we must
    not claim.

    `error_code` is a short machine-readable token (`timeout`, `http_500`,
    `transport_error`), never a response body: provider payloads can echo
    back the destination URL, which for Slack and Teams is itself a
    credential.
    """

    status: DeliveryStatus
    provider: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationDelivery:
    """The persisted record of trying to alert someone about one ticket.

    Stored rather than derived, because "was anyone actually told?" outlives
    the request that asked it: a dispatcher reviewing a missed emergency the
    next morning, an operator checking whether a business's webhook has been
    quietly broken for a week, and the tool result that decides what the
    assistant may say all read this same row.
    """

    id: uuid.UUID
    organization_id: uuid.UUID
    ticket_id: uuid.UUID
    channel: NotificationChannel | None
    provider: str
    status: DeliveryStatus
    attempts: int
    error_code: str | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def alerted_a_human(self) -> bool:
        return self.status.alerted_a_human
