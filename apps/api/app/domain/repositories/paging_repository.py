"""Persistence ports for emergency paging.

Two ports, for the same reason as notifications: settings are configuration
(written rarely by an Owner, read on every emergency), pages are events
(written once per ticket, then driven through their lifecycle by workers and
acknowledgements).

Every mutation that decides the page's fate happens on a row the caller's
transaction holds locked (`lock_*`), so an acknowledgement and an escalation
racing at the deadline are serialised by PostgreSQL rather than by luck.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime

from app.domain.paging.page import (
    AcknowledgementMethod,
    EmergencyPage,
    NewPageNotification,
    PageNotification,
    PageNotificationStatus,
    PageStatus,
    RecipientRole,
)
from app.domain.paging.settings import PagingSettings


class PagingSettingsRepository(ABC):
    @abstractmethod
    async def get(self, organization_id: uuid.UUID) -> PagingSettings | None: ...

    @abstractmethod
    async def upsert(self, settings: PagingSettings) -> PagingSettings:
        """One row per tenant. `settings` must already be validated."""
        ...

    @abstractmethod
    async def delete(self, organization_id: uuid.UUID) -> None: ...


class EmergencyPageRepository(ABC):
    # --- Creation (inside the ticket's transaction) ---------------------------

    @abstractmethod
    async def create(
        self,
        organization_id: uuid.UUID,
        ticket_id: uuid.UUID,
        *,
        ack_timeout_seconds: int,
        escalate_at: datetime,
        notifications: Sequence[NewPageNotification],
        now: datetime,
    ) -> tuple[EmergencyPage, bool]:
        """Writes the ticket's page and its first notifications, returning the
        page and whether this call created it.

        Idempotent by ticket (INSERT ... ON CONFLICT DO NOTHING on a unique
        index), so the tool loop and the webhook's outcome sync both creating
        the same ticket's page produce exactly one page. No I/O beyond the
        inserts: nothing is sent until the transaction commits."""
        ...

    # --- Reads, always tenant-scoped -------------------------------------------

    @abstractmethod
    async def get_for_ticket(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> EmergencyPage | None: ...

    @abstractmethod
    async def get_by_id(
        self, organization_id: uuid.UUID, page_id: uuid.UUID
    ) -> EmergencyPage | None: ...

    @abstractmethod
    async def list_for_organization(
        self, organization_id: uuid.UUID, *, limit: int
    ) -> list[EmergencyPage]: ...

    @abstractmethod
    async def list_notifications(
        self, organization_id: uuid.UUID, page_id: uuid.UUID
    ) -> list[PageNotification]: ...

    # --- Locking the page (acknowledgement and escalation) ---------------------

    @abstractmethod
    async def lock_for_ticket(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> EmergencyPage | None:
        """`FOR UPDATE` on this tenant's page for the ticket. Waits rather
        than skips: an acknowledgement must not be lost because an
        escalation happened to hold the row for a moment."""
        ...

    @abstractmethod
    async def lock_by_id(self, page_id: uuid.UUID) -> EmergencyPage | None:
        """`FOR UPDATE` by id alone — only for the signed-link path, where
        the verified token is the authority and the page's own organization
        is read from the row rather than supplied by the request."""
        ...

    @abstractmethod
    async def lock_next_escalation_due(self, *, now: datetime) -> EmergencyPage | None:
        """`FOR UPDATE SKIP LOCKED`: one escalating page whose deadline has
        passed, so two workers never escalate the same page."""
        ...

    @abstractmethod
    async def mark_acknowledged(
        self,
        page_id: uuid.UUID,
        *,
        at: datetime,
        role: RecipientRole | None,
        via: AcknowledgementMethod,
        user_id: uuid.UUID | None,
    ) -> EmergencyPage: ...

    @abstractmethod
    async def mark_escalated(
        self, page_id: uuid.UUID, *, at: datetime, escalate_at: datetime
    ) -> EmergencyPage: ...

    @abstractmethod
    async def mark_unresolved(
        self, page_id: uuid.UUID, *, at: datetime, reason: str
    ) -> EmergencyPage: ...

    @abstractmethod
    async def bring_escalation_forward(
        self, page_id: uuid.UUID, *, expected_status: PageStatus, at: datetime
    ) -> bool:
        """Moves the deadline earlier (never later), and only while the page
        is still in `expected_status` — so it can never cut short the window
        of a recipient the page has since escalated to. Used when every
        notification to the current recipient failed outright."""
        ...

    # --- Notifications -----------------------------------------------------------

    @abstractmethod
    async def add_notifications(
        self,
        organization_id: uuid.UUID,
        page_id: uuid.UUID,
        notifications: Sequence[NewPageNotification],
        *,
        now: datetime,
    ) -> int:
        """Queues notifications, idempotent per (page, role, channel), so an
        escalation replayed after a crash cannot page the backup twice.
        Returns how many rows were actually created."""
        ...

    @abstractmethod
    async def cancel_unsent(self, page_id: uuid.UUID) -> int:
        """Cancels every notification of this page not yet handed to a
        provider (queued or retrying). One already `sending` is left to
        finish: it may already be with the provider."""
        ...

    @abstractmethod
    async def claim_next_notification(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        page_id: uuid.UUID | None = None,
    ) -> PageNotification | None:
        """Claims one due notification for sending, or returns None.

        Due means: queued/retrying with `next_attempt_at <= now`, or
        `sending` with an expired lease (its worker died mid-send). Never a
        notification of an acknowledged page. `FOR UPDATE SKIP LOCKED`, then
        marked `sending` with `attempts + 1` and the lease — so once the
        caller commits the claim, no other worker can take it until the lease
        runs out, and the send itself happens with no row lock held."""
        ...

    @abstractmethod
    async def record_notification_result(
        self,
        notification_id: uuid.UUID,
        *,
        claimed_attempt: int,
        status: PageNotificationStatus,
        provider: str,
        provider_message_id: str | None,
        error_code: str | None,
        next_attempt_at: datetime | None,
        sent_at: datetime | None,
    ) -> PageNotification | None:
        """Writes the outcome of the attempt this worker claimed.

        Fenced by `claimed_attempt`: only applies while the row is still
        `sending` at that attempt number. Returns None when it is not — the
        lease expired and another worker re-claimed it — so a slow worker can
        never overwrite a newer attempt's result."""
        ...
