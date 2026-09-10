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
        organization_id: uuid.UUID,
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
        reschedule. `organization_id` scopes the lookup itself (Milestone 9
        tenant-isolation hardening)."""
        ...

    @abstractmethod
    async def update_status(
        self,
        organization_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        status: AppointmentStatus,
        closed_at: datetime | None = None,
        actual_value: Decimal | None = None,
    ) -> Appointment:
        """`actual_value` (Milestone 8), when provided, is persisted
        regardless of the target status — it is meaningful when closing an
        appointment as COMPLETED, but the repository does not enforce that;
        see `AppointmentService.update_appointment_status`. `organization_id`
        scopes the lookup itself (Milestone 9 tenant-isolation hardening)."""
        ...

    @abstractmethod
    async def set_customer(
        self, organization_id: uuid.UUID, appointment_id: uuid.UUID, *, customer_id: uuid.UUID
    ) -> Appointment:
        """Links an appointment to a `Customer` (Milestone 7) after the
        fact — called by `CustomerService.sync_customer_from_outcome`,
        never at appointment-creation time. `organization_id` scopes the
        lookup itself (Milestone 9 tenant-isolation hardening)."""
        ...

    @abstractmethod
    async def backfill_contact_details(
        self,
        organization_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        customer_name: str | None = None,
        customer_phone: str | None = None,
        customer_address: str | None = None,
    ) -> Appointment:
        """Fills in caller contact details learned *after* the appointment
        was created — the exact counterpart of
        `EmergencyTicketRepository.backfill_contact_details`, which
        Appointments never received.

        The gap was real and observable: an appointment is requested on the
        first turn the AI recommends booking, routinely before the caller
        has stated their number or address, and `sync_appointment_from_outcome`
        deliberately does not re-copy AI fields onto an existing row. The
        live call of 2026-08-22 therefore left an appointment whose
        `customer_phone`/`customer_address` were empty strings while the
        conversation outcome held both — nobody to call back.

        Deliberately narrow: only the three contact columns are writable, so
        no scheduling state (`status`, `scheduled_start_at`,
        `duration_minutes`, assignment, `closed_at`, `actual_value`,
        `customer_id`) can be disturbed. Each argument is applied only when
        not `None`; deciding *which* fields are safe to fill is
        `AppointmentService`'s job, not this layer's.

        `organization_id` scopes the lookup, so a mismatched tenant finds
        nothing and raises `EntityNotFoundError` rather than writing."""
        ...

    @abstractmethod
    async def list_scheduled_in_range(
        self, organization_id: uuid.UUID, *, start_at: datetime, end_at: datetime
    ) -> list[Appointment]:
        """Every `SCHEDULED` appointment occupying any part of
        `[start_at, end_at)`, soonest first.

        Exists so an availability search costs one query instead of one per
        candidate slot. Searching a week at 30-minute granularity generates
        well over a hundred candidates, and asking the database about each
        one separately put that many sequential round-trips inside a live
        call's request — a request that is also holding the call's advisory
        lock. Loading the window once and counting overlaps in memory is the
        same answer for a fraction of the latency, because the number of
        booked jobs in a week is small even when the number of candidate
        slots is not.

        `count_overlapping` remains the authority for a *single* time, where
        one query is already the minimum and correctness under concurrency
        matters more than round-trips."""
        ...

    @abstractmethod
    async def count_overlapping(
        self,
        organization_id: uuid.UUID,
        *,
        start_at: datetime,
        end_at: datetime,
        exclude_appointment_id: uuid.UUID | None = None,
    ) -> int:
        """How many `SCHEDULED` appointments occupy any part of
        `[start_at, end_at)`.

        The half-open interval is what makes back-to-back appointments legal:
        a 10:00-11:30 job and an 11:30-13:00 job do not overlap. Only
        `SCHEDULED` rows count — `REQUESTED` ones hold no time yet, and
        `COMPLETED`/`CANCELED`/`NO_SHOW` no longer occupy the calendar.

        `exclude_appointment_id` omits one row from the count so that
        rescheduling an appointment does not conflict with the time it
        currently holds.

        Used by the availability engine both to generate slots and, inside
        the booking lock, to re-verify the one being taken."""
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
