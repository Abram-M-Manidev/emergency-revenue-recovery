from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from app.domain.entities.analytics import DailyRevenue
from app.domain.entities.emergency_ticket import EmergencyTicket, TicketStatus


class EmergencyTicketRepository(ABC):
    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        matched_service_id: uuid.UUID | None,
        customer_name: str | None,
        customer_phone: str | None,
        customer_address: str | None,
        summary: str,
    ) -> EmergencyTicket:
        """Idempotent by `conversation_id`: implementations must catch a
        unique-constraint conflict on that column and return the
        already-existing ticket instead of raising — a concurrent retry of
        the same AI Brain turn must never fail or duplicate a ticket."""
        ...

    @abstractmethod
    async def get_by_id(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> EmergencyTicket | None: ...

    @abstractmethod
    async def get_by_conversation_id(
        self, conversation_id: uuid.UUID
    ) -> EmergencyTicket | None: ...

    @abstractmethod
    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        status: TicketStatus | None = None,
        limit: int,
        offset: int,
    ) -> list[EmergencyTicket]: ...

    @abstractmethod
    async def assign(
        self,
        ticket_id: uuid.UUID,
        *,
        technician_user_id: uuid.UUID,
        assigned_at: datetime,
    ) -> EmergencyTicket: ...

    @abstractmethod
    async def update_status(
        self,
        ticket_id: uuid.UUID,
        *,
        status: TicketStatus,
        closed_at: datetime | None = None,
        actual_value: Decimal | None = None,
    ) -> EmergencyTicket:
        """`actual_value` (Milestone 8), when provided, is persisted
        regardless of the target status — it is meaningful when closing a
        ticket as RESOLVED, but the repository does not enforce that; see
        `DispatchService.update_ticket_status`."""
        ...

    @abstractmethod
    async def set_customer(
        self, ticket_id: uuid.UUID, *, customer_id: uuid.UUID
    ) -> EmergencyTicket:
        """Links a ticket to a `Customer` (Milestone 7) after the fact —
        called by `CustomerService.sync_customer_from_outcome`, never at
        ticket-creation time."""
        ...

    @abstractmethod
    async def list_by_customer_id(
        self, organization_id: uuid.UUID, customer_id: uuid.UUID
    ) -> list[EmergencyTicket]: ...

    # --- Analytics (Milestone 8) aggregate queries ---

    @abstractmethod
    async def count_created_in_range(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> int: ...

    @abstractmethod
    async def count_closed_in_range(
        self,
        organization_id: uuid.UUID,
        *,
        status: TicketStatus,
        start: datetime | None,
        end: datetime,
    ) -> int:
        """Counts tickets whose `closed_at` (not `created_at`) falls in the
        range and whose current status matches — i.e. "closed as RESOLVED
        during this window", not "created during this window"."""
        ...

    @abstractmethod
    async def sum_actual_value_in_range(
        self,
        organization_id: uuid.UUID,
        *,
        status: TicketStatus,
        start: datetime | None,
        end: datetime,
    ) -> Decimal:
        """Sums `actual_value` across tickets closed (by `closed_at`) with
        the given status in the range. Returns `Decimal("0")` when there is
        nothing to sum, never `None`."""
        ...

    @abstractmethod
    async def revenue_by_day(
        self,
        organization_id: uuid.UUID,
        *,
        status: TicketStatus,
        start: datetime | None,
        end: datetime,
    ) -> list[DailyRevenue]: ...

    @abstractmethod
    async def average_resolution_minutes(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> float | None:
        """Average `closed_at - created_at`, in minutes, across tickets
        closed as RESOLVED (by `closed_at`) in the range. `None` when there
        are none."""
        ...
