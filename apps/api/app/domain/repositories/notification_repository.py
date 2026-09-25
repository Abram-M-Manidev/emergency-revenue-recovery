"""Persistence ports for emergency notification: where an organization wants
alerts sent, and what happened when we sent them.

Two ports rather than one because they have different lifetimes and different
readers. Settings are configuration — written rarely, by an operator, read on
every emergency. Deliveries are events — written once per ticket, read by the
tool result that decides what the assistant may say and by anyone later
auditing whether a business's alerting actually worked.

Why settings are not on `business_profiles`
--------------------------------------------
`BusinessProfile` is Business Knowledge: it is assembled into the system
prompt on every turn (see `prompt_builder.py`). A webhook URL is a
credential — a Slack or Teams incoming-webhook URL is the entire secret, and
anyone holding it can post as that integration. Putting one on the profile
would place it one careless field-loop away from being sent to OpenAI as
prompt text. A separate table keeps the two categories physically apart, so
that leak is not merely avoided but unavailable.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from app.domain.notifications.emergency import (
    DeliveryStatus,
    NotificationChannel,
    NotificationDelivery,
)
from app.domain.notifications.settings import NotificationSettings


class NotificationSettingsRepository(ABC):
    """Where one organization's emergency alerts should go."""

    @abstractmethod
    async def get_destination(
        self, organization_id: uuid.UUID
    ) -> tuple[NotificationChannel, str] | None:
        """The configured channel and its destination, or None when this
        organization has not set alerting up (or has switched it off).

        Returning None rather than raising because "not configured" is an
        ordinary state, not an error: a business can run a pilot reading its
        dispatch queue by hand. What must never happen is the assistant
        claiming an alert on such a call, and the None flows through to
        `DeliveryStatus.NOT_CONFIGURED` for exactly that reason.

        The destination is a secret for every channel we support, so it is
        returned only to the provider that needs it and never logged, never
        echoed into a tool result, and never placed in a prompt."""
        ...

    @abstractmethod
    async def get_settings(self, organization_id: uuid.UUID) -> NotificationSettings | None:
        """The operator-visible view: channel, enabled, and a masked hint.

        Deliberately a different method from `get_destination` rather than a
        flag on it. The two have different audiences — this one answers an
        authenticated admin over HTTP, that one feeds the provider — and
        keeping them apart means a response serialiser cannot accidentally
        reach a field holding the raw credential, because the object it is
        handed does not have one.

        Unlike `get_destination`, this returns a disabled configuration
        rather than None, so an operator can see that alerting exists but is
        switched off."""
        ...

    @abstractmethod
    async def upsert_settings(
        self,
        organization_id: uuid.UUID,
        *,
        channel: NotificationChannel,
        destination: str,
        is_enabled: bool,
    ) -> NotificationSettings:
        """Creates or replaces this organization's configuration.

        Upsert rather than create-then-update because there is exactly one
        row per tenant (enforced by a unique index), and making the caller
        branch on whether it already exists would be a check-then-write race
        for no benefit.

        `destination` must already have been validated — see
        `validate_webhook_destination`. This is storage, and storage is the
        wrong layer to be deciding whether a URL is safe for the server to
        call."""
        ...

    @abstractmethod
    async def set_enabled(
        self, organization_id: uuid.UUID, *, is_enabled: bool
    ) -> NotificationSettings | None:
        """Flips alerting on or off, returning the updated view, or None when
        nothing is configured.

        A dedicated method rather than a re-`upsert_settings` with the same
        destination, because the alternative would mean reading the stored
        credential out of the database and writing it back just to change a
        boolean. Every place the raw destination is read is a place it can
        leak; this keeps that set as small as it can be — one method, feeding
        the provider."""
        ...

    @abstractmethod
    async def delete_settings(self, organization_id: uuid.UUID) -> bool:
        """Removes this organization's configuration entirely, returning
        whether there was one.

        Distinct from disabling it. Disabling keeps the destination so it can
        be switched back on; deleting is what an operator does when the
        endpoint is wrong or has been rotated, and it should leave no copy of
        the old credential behind."""
        ...


class NotificationDeliveryRepository(ABC):
    """The record of alerting attempts, one row per emergency ticket."""

    @abstractmethod
    async def get_for_ticket(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> NotificationDelivery | None:
        """This ticket's delivery record, scoped by organization so one
        tenant can never read another's alerting state."""
        ...

    @abstractmethod
    async def enqueue(
        self,
        organization_id: uuid.UUID,
        ticket_id: uuid.UUID,
        *,
        channel: NotificationChannel | None,
        provider: str,
        status: DeliveryStatus,
        next_attempt_at: datetime | None,
    ) -> tuple[NotificationDelivery, bool]:
        """Writes this ticket's outbox row, returning it and whether this call
        created it.

        Called inside the transaction that creates the ticket, so the two
        commit or roll back together — the property the outbox exists for.
        Performs no I/O beyond the insert: nothing leaves the system until
        the transaction has committed.

        Idempotent by ticket (INSERT ... ON CONFLICT DO NOTHING against the
        unique index), so a ticket synced by both the tool loop and the
        webhook's outcome sync gets exactly one row and so exactly one
        alert."""
        ...

    @abstractmethod
    async def lock_next_due(
        self, *, now: datetime, ticket_id: uuid.UUID | None = None
    ) -> NotificationDelivery | None:
        """Locks and returns one row whose alert is due, or None.

        `FOR UPDATE SKIP LOCKED`: the row stays locked until the caller's
        transaction ends, and every other worker skips it rather than waiting
        — so two workers, or a retry racing the post-commit send, can never
        both send the same alert. A worker that dies mid-send releases the
        lock with its connection, and the row is simply due again.

        Only `pending`/`failed` rows with a due `next_attempt_at` qualify, so
        a delivered row can never be selected again. `ticket_id` narrows the
        scan to one ticket, for the immediate post-commit send."""
        ...

    @abstractmethod
    async def record_attempt(
        self,
        delivery_id: uuid.UUID,
        *,
        status: DeliveryStatus,
        error_code: str | None,
        provider: str,
        next_attempt_at: datetime | None,
    ) -> NotificationDelivery:
        """Writes the outcome of one send attempt on a row this transaction
        holds locked, incrementing the attempt count and setting (or
        clearing) when it is next due."""
        ...
