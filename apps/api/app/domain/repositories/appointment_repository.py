from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

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
    ) -> Appointment: ...
