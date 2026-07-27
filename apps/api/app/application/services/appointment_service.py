"""Orchestrates Appointment Management: turns an AI Brain `ConversationOutcome`
(Milestone 3) into a real `Appointment` request, and lets staff pick a real
date/time and technician and track it through to completion.

This service never talks to the AI Brain or Voice modules — it only reads
`ConversationOutcomeRepository`, already owned by the AI Brain module,
exactly the seam `conversation_outcome.py`'s docstring describes
("Appointment Management... reads conversation_outcomes directly to find
work to act on"). Neither `AIBrainService` nor `VoiceService` is aware this
module exists — the automatic-appointment-creation trigger lives in the API
layer (see `api/v1/endpoints/ai_conversations.py` and `vapi_webhooks.py`),
one call to `sync_appointment_from_outcome` right after each of those
already calls into the AI Brain, mirroring exactly how `DispatchService` is
wired in for emergency tickets.

Unlike Dispatch, the AI Brain never picks a concrete time slot for an
appointment — it only classifies the intent. Scheduling a real date/time and
technician is always a staff action (`schedule_appointment`), validated only
against the org's existing `BusinessHours` — there is no per-technician
availability/conflict engine in this milestone."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.entities.appointment import Appointment, AppointmentStatus
from app.domain.entities.conversation_outcome import RecommendedAction
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User
from app.domain.exceptions import (
    AppointmentOutsideBusinessHoursError,
    AuthorizationError,
    EntityNotFoundError,
    InvalidAppointmentStatusTransitionError,
)
from app.domain.repositories.appointment_repository import AppointmentRepository
from app.domain.repositories.business_hours_repository import BusinessHoursRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.service_repository import ServiceRepository
from app.domain.repositories.technician_profile_repository import TechnicianProfileRepository

# Legal status transitions: REQUESTED -> SCHEDULED only happens through
# schedule_appointment (it needs extra required fields the generic status
# endpoint has no business accepting), so it is deliberately absent here.
# From SCHEDULED, an appointment can be COMPLETED, CANCELED, or NO_SHOW.
# REQUESTED can only be CANCELED directly. All three terminal statuses have
# no outgoing edges.
_ALLOWED_TRANSITIONS: dict[AppointmentStatus, frozenset[AppointmentStatus]] = {
    AppointmentStatus.REQUESTED: frozenset({AppointmentStatus.CANCELED}),
    AppointmentStatus.SCHEDULED: frozenset(
        {AppointmentStatus.COMPLETED, AppointmentStatus.CANCELED, AppointmentStatus.NO_SHOW}
    ),
    AppointmentStatus.COMPLETED: frozenset(),
    AppointmentStatus.CANCELED: frozenset(),
    AppointmentStatus.NO_SHOW: frozenset(),
}
_CLOSED_STATUSES = frozenset(
    {AppointmentStatus.COMPLETED, AppointmentStatus.CANCELED, AppointmentStatus.NO_SHOW}
)


class AppointmentService:
    def __init__(
        self,
        *,
        appointment_repository: AppointmentRepository,
        technician_profile_repository: TechnicianProfileRepository,
        conversation_outcome_repository: ConversationOutcomeRepository,
        service_repository: ServiceRepository,
        business_hours_repository: BusinessHoursRepository,
        business_profile_repository: BusinessProfileRepository,
    ) -> None:
        self._appointments = appointment_repository
        self._technicians = technician_profile_repository
        self._outcomes = conversation_outcome_repository
        self._services = service_repository
        self._business_hours = business_hours_repository
        self._business_profiles = business_profile_repository

    # --- Automatic appointment creation (the AI Brain -> Appointments seam) ---

    async def sync_appointment_from_outcome(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Appointment | None:
        outcome = await self._outcomes.get_by_conversation_id(conversation_id)
        if outcome is None:
            return None
        if outcome.recommended_action is not RecommendedAction.BOOK_APPOINTMENT:
            return None

        existing = await self._appointments.get_by_conversation_id(conversation_id)
        if existing is not None:
            # Already requested on an earlier turn — later turns of the
            # same conversation must not re-copy (possibly stale) AI fields
            # onto an appointment that may already have real scheduling
            # progress on it.
            return existing

        duration_minutes: int | None = None
        if outcome.matched_service_id is not None:
            matched_service = await self._services.get_by_id(
                organization_id, outcome.matched_service_id
            )
            if matched_service is not None:
                duration_minutes = matched_service.default_duration_minutes

        return await self._appointments.create(
            organization_id=organization_id,
            conversation_id=conversation_id,
            matched_service_id=outcome.matched_service_id,
            customer_name=outcome.customer_name,
            customer_phone=outcome.customer_phone,
            customer_address=outcome.customer_address,
            summary=outcome.summary,
            duration_minutes=duration_minutes,
        )

    # --- Appointments ---

    async def list_appointments(
        self,
        organization_id: uuid.UUID,
        *,
        status: AppointmentStatus | None = None,
        limit: int,
        offset: int,
    ) -> list[Appointment]:
        return await self._appointments.list_for_organization(
            organization_id, status=status, limit=limit, offset=offset
        )

    async def get_appointment(
        self, organization_id: uuid.UUID, appointment_id: uuid.UUID
    ) -> Appointment:
        appointment = await self._appointments.get_by_id(organization_id, appointment_id)
        if appointment is None:
            # Cross-tenant id: from the caller's point of view, another
            # org's appointment simply doesn't exist — same convention as
            # DispatchService.get_ticket.
            raise EntityNotFoundError("Appointment", str(appointment_id))
        return appointment

    async def schedule_appointment(
        self,
        organization_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        scheduled_start_at: datetime,
        duration_minutes: int,
        technician_user_id: uuid.UUID | None = None,
    ) -> Appointment:
        appointment = await self.get_appointment(organization_id, appointment_id)
        if appointment.status in _CLOSED_STATUSES:
            raise InvalidAppointmentStatusTransitionError(
                f"Cannot schedule an appointment that is already {appointment.status.value}."
            )

        if technician_user_id is not None:
            technician = await self._technicians.get_by_user_id(technician_user_id)
            if technician is None or technician.organization_id != organization_id:
                raise EntityNotFoundError("TechnicianProfile", str(technician_user_id))

        await self._ensure_within_business_hours(organization_id, scheduled_start_at)

        return await self._appointments.schedule(
            organization_id,
            appointment_id,
            scheduled_start_at=scheduled_start_at,
            duration_minutes=duration_minutes,
            technician_user_id=technician_user_id,
            assigned_at=datetime.now(timezone.utc),
        )

    async def update_appointment_status(
        self,
        organization_id: uuid.UUID,
        appointment_id: uuid.UUID,
        new_status: AppointmentStatus,
        *,
        acting_user: User,
        actual_value: Decimal | None = None,
    ) -> Appointment:
        appointment = await self.get_appointment(organization_id, appointment_id)

        if not acting_user.has_permission(Permissions.APPOINTMENTS_MANAGE):
            if appointment.assigned_technician_user_id != acting_user.id:
                raise AuthorizationError(
                    "You can only update the status of appointments assigned to you."
                )

        if new_status not in _ALLOWED_TRANSITIONS[appointment.status]:
            raise InvalidAppointmentStatusTransitionError(
                f"Cannot move an appointment from {appointment.status.value} to {new_status.value}."
            )

        closed_at = datetime.now(timezone.utc) if new_status in _CLOSED_STATUSES else None
        return await self._appointments.update_status(
            organization_id, appointment_id, status=new_status, closed_at=closed_at, actual_value=actual_value
        )

    async def cancel_appointment(
        self, organization_id: uuid.UUID, appointment_id: uuid.UUID, *, acting_user: User
    ) -> Appointment:
        return await self.update_appointment_status(
            organization_id, appointment_id, AppointmentStatus.CANCELED, acting_user=acting_user
        )

    # --- Business hours validation ---

    async def _ensure_within_business_hours(
        self, organization_id: uuid.UUID, scheduled_start_at: datetime
    ) -> None:
        profile = await self._business_profiles.get_by_organization_id(organization_id)
        weekly = await self._business_hours.get_weekly(organization_id)
        if not weekly:
            # Hours never configured for this org — nothing to validate
            # against yet, so don't block scheduling on missing setup.
            return

        local_dt = (
            scheduled_start_at.astimezone(ZoneInfo(profile.timezone))
            if profile is not None
            else scheduled_start_at
        )

        exceptions = await self._business_hours.list_exceptions(organization_id)
        exception = next((e for e in exceptions if e.date == local_dt.date()), None)
        if exception is not None:
            open_time, close_time = (
                (None, None) if exception.is_closed else (exception.open_time, exception.close_time)
            )
        else:
            day_hours = next((w for w in weekly if w.day_of_week == local_dt.weekday()), None)
            if day_hours is None or day_hours.is_closed:
                raise AppointmentOutsideBusinessHoursError()
            open_time, close_time = day_hours.open_time, day_hours.close_time

        if open_time is None or close_time is None or not (open_time <= local_dt.time() < close_time):
            raise AppointmentOutsideBusinessHoursError()
