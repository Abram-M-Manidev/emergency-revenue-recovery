from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from app.domain.entities.analytics import BucketCount, DailyRevenue
from app.domain.entities.appointment import Appointment, AppointmentStatus


class AppointmentRepository(ABC):
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
        duration_minutes: int | None,
    ) -> Appointment:
        """Idempotent by `conversation_id`: implementations must catch a
        unique-constraint conflict on that column and return the
        already-existing appointment instead of raising — a concurrent
        retry of the same AI Brain turn must never fail or duplicate an
        appointment."""
        ...

    @abstractmethod
    async def get_by_id(
        self, organization_id: uuid.UUID, appointment_id: uuid.UUID
    ) -> Appointment | None: ...

    @abstractmethod
    async def get_by_conversation_id(self, conversation_id: uuid.UUID) -> Appointment | None: ...

    @abstractmethod
    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        status: AppointmentStatus | None = None,
        limit: int,
        offset: int,
    ) -> list[Appointment]:
        """Ordered soonest-scheduled-first (scheduled_start_at ascending,
        nulls last), then newest-created-first — REQUESTED (unscheduled)
        appointments get a stable ordering while SCHEDULED ones surface
        what's coming up next."""
        ...

    @abstractmethod
    async def schedule(
        self,
        appointment_id: uuid.UUID,
        *,
        scheduled_start_at: datetime,
        duration_minutes: int,
        technician_user_id: uuid.UUID | None,
        assigned_at: datetime,
    ) -> Appointment:
        """Sets scheduled_start_at/duration_minutes/
        assigned_technician_user_id/assigned_at and status=SCHEDULED in one
        write. Used for both the initial schedule and any later
        reschedule."""
        ...

    @abstractmethod
    async def update_status(
        self,
        appointment_id: uuid.UUID,
        *,
        status: AppointmentStatus,
        closed_at: datetime | None = None,
        actual_value: Decimal | None = None,
    ) -> Appointment:
        """`actual_value` (Milestone 8), when provided, is persisted
        regardless of the target status — it is meaningful when closing an
        appointment as COMPLETED, but the repository does not enforce that;
        see `AppointmentService.update_appointment_status`."""
        ...

    @abstractmethod
    async def set_customer(
        self, appointment_id: uuid.UUID, *, customer_id: uuid.UUID
    ) -> Appointment:
        """Links an appointment to a `Customer` (Milestone 7) after the
        fact — called by `CustomerService.sync_customer_from_outcome`,
        never at appointment-creation time."""
        ...

    @abstractmethod
    async def list_by_customer_id(
        self, organization_id: uuid.UUID, customer_id: uuid.UUID
    ) -> list[Appointment]: ...

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
        status: AppointmentStatus,
        start: datetime | None,
        end: datetime,
    ) -> int:
        """Counts appointments whose `closed_at` (not `created_at`) falls in
        the range and whose current status matches."""
        ...

    @abstractmethod
    async def sum_actual_value_in_range(
        self,
        organization_id: uuid.UUID,
        *,
        status: AppointmentStatus,
        start: datetime | None,
        end: datetime,
    ) -> Decimal:
        """Sums `actual_value` across appointments closed (by `closed_at`)
        with the given status in the range. Returns `Decimal("0")` when
        there is nothing to sum, never `None`."""
        ...

    @abstractmethod
    async def revenue_by_day(
        self,
        organization_id: uuid.UUID,
        *,
        status: AppointmentStatus,
        start: datetime | None,
        end: datetime,
    ) -> list[DailyRevenue]: ...

    @abstractmethod
    async def status_breakdown_in_range(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> list[BucketCount]:
        """Groups appointments *created* in the range by their current
        status — a simple distribution chart, unlike tickets (which have no
        equivalent breakdown method; see `EmergencyTicketRepository`)."""
        ...
