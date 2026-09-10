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

Two ways an appointment gets a real time on it, and they are deliberately
not the same code path:

- `schedule_appointment` — the staff action, from the dashboard. A human
  chose the time, so it is permissive: business hours are enforced, but a
  time in the past is allowed (recording a visit that already happened is
  legitimate admin work).
- `book_for_conversation` — the AI booking path, behind the
  `book_appointment` tool. It re-verifies everything from scratch inside
  the booking lock and rejects a past time, a closed day, and a full slot
  alike, because none of those is ever something to promise a live caller.

Both take the same organization-wide `BookingLock` around verify-then-write,
so a dispatcher and a caller cannot be handed the same slot. Until this
existed there was no conflict detection anywhere — two staff members could
double-book a technician, and the AI could not offer a time at all.

The AI path additionally climbs a consent ladder, every rung of which is a
stored fact rather than a model claim:

    OFFERED          `check_availability` recorded reading this time out
       |             (`OfferedSlotRepository.record_offered`)
    SELECTED         the caller answered, in a LATER turn, and the model
       |             interpreted that answer as this time
       |             (`select_slot_for_conversation`)
    VALIDATED        the slot is still real: not past, business open, has
       |             capacity — re-derived inside the booking lock
    BOOKED           the write

`select_slot_for_conversation` is the rung that a prompt cannot supply. The
model decides *which* offered time the caller meant — that is language
understanding, and it is good at it — but it cannot decide *that* the caller
answered, because the turn index it would have to forge is derived from the
backend's own persisted message history.

Availability itself is not computed here. It comes from the
`AvailabilityProvider` port (`app/domain/availability.py`), so the
database-derived engine can later be swapped for Google Calendar,
ServiceTitan, or Jobber without touching this service."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.availability import AvailabilityProvider
from app.domain.entities.appointment import Appointment, AppointmentStatus
from app.domain.entities.availability import (
    AvailabilityQuery,
    AvailabilityResult,
    SlotVerdict,
)
from app.domain.entities.conversation_outcome import ConversationOutcome, RecommendedAction
from app.domain.entities.offered_slot import OfferedSlot, SlotSelectionVerdict
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User
from app.domain.exceptions import (
    AppointmentOutsideBusinessHoursError,
    AppointmentSlotInThePastError,
    AppointmentSlotUnavailableError,
    AuthorizationError,
    AvailabilityUnavailableError,
    EntityNotFoundError,
    InvalidAppointmentStatusTransitionError,
    SlotNotOfferedError,
    SlotNotSelectedError,
)
from app.domain.locks import BookingLock, NullBookingLock
from app.domain.repositories.appointment_repository import AppointmentRepository
from app.domain.repositories.business_hours_repository import BusinessHoursRepository
from app.domain.repositories.business_profile_repository import BusinessProfileRepository
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.domain.repositories.offered_slot_repository import OfferedSlotRepository
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

# Used only when an appointment carries no duration and its matched service
# has none either. Mirrors `Settings.SCHEDULING_DEFAULT_DURATION_MINUTES`;
# not read from settings here because this service has never taken a
# `Settings` dependency and adding one for a single fallback would widen its
# constructor for every existing caller.
_FALLBACK_DURATION_MINUTES = 60


def _is_blank(value: str | None) -> bool:
    """Treats `None`, `""`, and whitespace-only alike.

    A deliberate private copy of the identical helpers in `DispatchService`
    and `CustomerService`, for the reason `CustomerService` already records:
    these three services are peers that know nothing about each other, and
    coupling them through a shared utility would be a wider change than the
    behaviour it supports."""
    return value is None or not value.strip()


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
        # Optional so every pre-existing construction site keeps working
        # unchanged. Without a provider there is no availability search and
        # no conflict check — exactly the behaviour before this change —
        # and `find_availability` says so rather than pretending.
        availability_provider: AvailabilityProvider | None = None,
        booking_lock: BookingLock | None = None,
        # Records what `check_availability` offered and what the caller
        # then chose, so booking can refuse both a time nobody was read and
        # a time nobody picked. Optional only so existing construction sites
        # keep working; `book_for_conversation` refuses outright without it
        # rather than skipping the checks.
        offered_slot_repository: OfferedSlotRepository | None = None,
    ) -> None:
        self._appointments = appointment_repository
        self._technicians = technician_profile_repository
        self._outcomes = conversation_outcome_repository
        self._services = service_repository
        self._business_hours = business_hours_repository
        self._business_profiles = business_profile_repository
        self._availability = availability_provider
        self._booking_lock = booking_lock or NullBookingLock()
        self._offered_slots = offered_slot_repository

    # --- Automatic appointment creation (the AI Brain -> Appointments seam) ---

    async def sync_appointment_from_outcome(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Appointment | None:
        outcome = await self._outcomes.get_by_conversation_id(conversation_id)
        if outcome is None:
            return None

        # Looked up *before* the `recommended_action` gate. Once an
        # appointment exists, later turns must still be able to fill in
        # contact details the AI has since learned — and by then the model
        # has usually moved the action on to `none`, because from its point
        # of view the booking is done. Gating the lookup left a real
        # SCHEDULED appointment on 2026-08-23 with no name, phone, or
        # address on it: a job a dispatcher could not act on.
        existing = await self.get_appointment_for_conversation(
            organization_id, conversation_id
        )
        if existing is None and outcome.recommended_action is not (
            RecommendedAction.BOOK_APPOINTMENT
        ):
            # Creation stays gated: only a booking recommendation may bring a
            # new appointment into existence.
            return None

        if existing is not None:
            # Already requested on an earlier turn — later turns must not
            # re-copy (possibly stale) AI fields onto an appointment that may
            # already carry real scheduling progress. But an appointment
            # opened before the caller gave their number or address would
            # otherwise keep those blanks forever, leaving staff with nobody
            # to call back. Fill in only what is still missing, exactly as
            # `DispatchService` has done for tickets since `decaa31`.
            return await self._backfill_contact_details(organization_id, existing, outcome)

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

    async def _backfill_contact_details(
        self,
        organization_id: uuid.UUID,
        appointment: Appointment,
        outcome: ConversationOutcome,
    ) -> Appointment:
        """Copies contact details the AI has since learned onto an
        appointment that was created without them.

        Strictly additive, mirroring `DispatchService._backfill_contact_details`
        field for field: a value is written only when the appointment's own
        is blank AND the outcome has something to put there. A staff
        correction therefore always wins over the AI, and a later turn that
        *loses* a detail can never blank out a value already recorded. When
        nothing is missing this performs no write at all.

        This is the gap the 2026-08-22 live call exposed: the outcome held
        the caller's phone number and address, the appointment row held
        empty strings for both, and nothing ever reconciled them."""
        updates = {
            field: getattr(outcome, field)
            for field in ("customer_name", "customer_phone", "customer_address")
            if _is_blank(getattr(appointment, field)) and not _is_blank(getattr(outcome, field))
        }
        if not updates:
            return appointment

        return await self._appointments.backfill_contact_details(
            organization_id, appointment.id, **updates
        )

    # --- Availability (the AI Brain -> scheduling seam) ---

    async def find_availability(
        self, organization_id: uuid.UUID, query: AvailabilityQuery
    ) -> AvailabilityResult:
        """Real, currently-bookable slots for this organization.

        Raises rather than returning an empty result when no provider is
        configured, because the two mean opposite things: an empty result is
        the honest answer "we are fully booked", while a missing provider
        means the question was never actually asked. Letting the second
        masquerade as the first is how an assistant ends up telling a caller
        the week is full when nothing was ever checked."""
        if self._availability is None:
            raise AvailabilityUnavailableError()
        return await self._availability.find_slots(organization_id, query)

    async def select_slot_for_conversation(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        start_at: datetime,
        turn_index: int,
    ) -> tuple[SlotSelectionVerdict, OfferedSlot | None]:
        """Records that the caller chose `start_at` — the CALLER SELECTED rung
        of the consent ladder in this module's docstring.

        Returns a verdict rather than raising, because every outcome here is
        an ordinary conversational event the assistant must speak its way out
        of, not an error: a caller can name a time that was never offered, or
        the model can jump the gun and "select" during the same turn it
        offered. Both are recoverable in one sentence.

        The turn rule is the whole point. `turn_index` counts conversation
        messages the backend has already persisted, so it advances only when
        a real turn completes. An offer made at index N cannot be answered
        before index N+1, because at index N the caller had not heard it yet.
        A model that offers and immediately "selects" therefore fails this
        check every time, no matter how convincingly it reports what the
        caller said — which is exactly the failure this method exists to make
        impossible.

        Note what is deliberately NOT checked: whether the slot is still
        free. Selection is a record of what the caller said, and re-deriving
        availability here would either reject a choice the caller genuinely
        made (leaving them told "no" for a time that was offered seconds ago)
        or go stale before the write anyway. Freshness is `book_for_
        conversation`'s job, inside the lock, where it cannot go stale."""
        if self._offered_slots is None:
            # Fails closed, exactly as booking does: with no record of what
            # was offered there is no way to tell a choice from an invention.
            raise AvailabilityUnavailableError()

        offered = await self._offered_slots.get_offered(
            organization_id, conversation_id, start_at
        )
        if offered is None:
            return SlotSelectionVerdict.NOT_OFFERED, None

        if offered.offered_turn_index >= turn_index:
            return SlotSelectionVerdict.NOT_YET_HEARD, offered

        selected = await self._offered_slots.mark_selected(
            organization_id, conversation_id, start_at, turn_index
        )
        if selected is None:
            # The row vanished between the read and the write — a cascade
            # delete of the conversation is the only way that happens. Treat
            # it as never offered rather than reporting a success with
            # nothing behind it.
            return SlotSelectionVerdict.NOT_OFFERED, None
        return SlotSelectionVerdict.RECORDED, selected

    async def get_active_selection_for_conversation(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> OfferedSlot | None:
        """The time this caller has currently chosen, if any.

        Read-only, and used to tell the model what it is already holding —
        so a new turn resumes from the caller's actual choice instead of
        asking them to pick again."""
        if self._offered_slots is None:
            return None
        return await self._offered_slots.get_active_selection(
            organization_id, conversation_id
        )

    async def book_for_conversation(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        start_at: datetime,
        duration_minutes: int | None = None,
    ) -> Appointment:
        """Books the appointment belonging to one conversation onto a real
        time — the write behind the `book_appointment` tool.

        Everything the slot claims is re-derived here rather than trusted
        from the earlier availability search: the seconds a caller spends
        saying "yes, Monday" are enough for another call or a dispatcher to
        take the same slot. The verify-then-write pair runs inside
        `_booking_lock`, so the check cannot go stale between the two
        statements."""
        if self._availability is None or self._offered_slots is None:
            # Fails closed. Without the offered-slot record there is no way to
            # tell a time the caller chose from one the model invented, and
            # booking on a guess is the failure this method exists to prevent.
            raise AvailabilityUnavailableError()

        appointment = await self._appointments.get_by_conversation_id(conversation_id)
        if appointment is None or appointment.organization_id != organization_id:
            # Cross-tenant or genuinely absent are the same thing from the
            # caller's point of view — the convention used throughout.
            raise EntityNotFoundError("Appointment", str(conversation_id))
        if appointment.status in _CLOSED_STATUSES:
            raise InvalidAppointmentStatusTransitionError(
                f"Cannot book an appointment that is already {appointment.status.value}."
            )

        # The consent check, and the reason this method cannot be satisfied by
        # a prompt rule. A slot is bookable only if `check_availability`
        # handed it to *this* conversation: the query is scoped by both
        # organization and conversation, so neither another tenant's offer nor
        # another call's can authorise this booking.
        #
        # Re-deriving availability here would not do — on 2026-08-23 the model
        # booked a time that genuinely was free and the engine would have
        # approved. "Available" is not "offered".
        offered_duration = await self._offered_slots.offered_duration_minutes(
            organization_id, conversation_id, start_at
        )
        if offered_duration is None:
            raise SlotNotOfferedError()

        # Offered is not chosen. The check above proves this caller heard the
        # time; it says nothing about whether they picked it, and on a real
        # run the model offered three slots and booked one in the same breath
        # — all three passed the check above. Consent is a stored selection,
        # recorded in a later turn than the offer, and there is exactly one
        # of them per conversation.
        selection = await self._offered_slots.get_active_selection(
            organization_id, conversation_id
        )
        if selection is None or selection.start_at != start_at:
            raise SlotNotSelectedError()

        # The length the caller was actually quoted wins over one re-derived
        # now, so the visit they agreed to is the visit that gets booked.
        resolved_duration = duration_minutes or offered_duration

        # Keyed on the organization, not the slot. A slot-keyed lock would
        # let two bookings whose times *overlap* without being identical
        # (10:00-11:30 and 10:30-12:00) take different keys and both pass
        # the capacity check — the exact race this exists to prevent.
        # Serialising an organization's bookings is cheap at field-service
        # scale and is unambiguously correct.
        async with self._booking_lock.hold(str(organization_id)):
            verdict = await self._availability.verify_slot(
                organization_id,
                start_at=start_at,
                duration_minutes=resolved_duration,
                exclude_appointment_id=appointment.id,
            )
            if verdict is not SlotVerdict.BOOKABLE:
                # The caller's choice is no longer purchasable — taken while
                # they were talking, or a time the business turns out to be
                # shut for. Drop the selection but keep the offer: the time
                # really was read out, so re-offering it later is honest,
                # while letting the dead choice stand would authorise a
                # silent retry of a slot the caller can no longer have.
                #
                # Safe to persist: the tool executor turns these into a
                # structured tool result rather than letting them abort the
                # request, so this transaction commits rather than rolling
                # back.
                await self._offered_slots.clear_selection(
                    organization_id, conversation_id
                )
            if verdict is SlotVerdict.IN_THE_PAST:
                raise AppointmentSlotInThePastError()
            if verdict is SlotVerdict.OUTSIDE_BUSINESS_HOURS:
                raise AppointmentOutsideBusinessHoursError()
            if verdict is SlotVerdict.FULL:
                raise AppointmentSlotUnavailableError()

            return await self._appointments.schedule(
                organization_id,
                appointment.id,
                scheduled_start_at=start_at,
                duration_minutes=resolved_duration,
                # Left unassigned on purpose: picking *which* technician goes
                # is a dispatch decision with context the caller does not
                # have (skills, territory, who is already out). The caller
                # gets a confirmed time; staff assign the person.
                technician_user_id=None,
                assigned_at=datetime.now(timezone.utc),
            )

    async def _default_duration_for(
        self, organization_id: uuid.UUID, service_id: uuid.UUID | None
    ) -> int:
        if service_id is not None:
            service = await self._services.get_by_id(organization_id, service_id)
            if service is not None and service.default_duration_minutes:
                return service.default_duration_minutes
        return _FALLBACK_DURATION_MINUTES

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

    async def get_appointment_for_conversation(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Appointment | None:
        """The appointment this conversation produced, if any.

        Returns None rather than raising, because most conversations have
        none. Used to tell the AI Brain what a call has already achieved —
        see `ToolExecutorFactory.describe_progress`."""
        appointment = await self._appointments.get_by_conversation_id(conversation_id)
        if appointment is None or appointment.organization_id != organization_id:
            return None
        return appointment

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

        # Same organization-wide booking lock the voice path takes, so a
        # dispatcher scheduling from the dashboard and a caller booking over
        # the phone cannot both be handed the same slot. Before this, nothing
        # anywhere stopped two staff members double-booking a technician.
        async with self._booking_lock.hold(str(organization_id)):
            if self._availability is not None:
                verdict = await self._availability.verify_slot(
                    organization_id,
                    start_at=scheduled_start_at,
                    duration_minutes=duration_minutes,
                    # Rescheduling must not collide with the time this
                    # appointment currently holds.
                    exclude_appointment_id=appointment_id,
                )
                # Only capacity is fatal here. Business hours are already
                # enforced above, and `IN_THE_PAST` is deliberately allowed:
                # a staff member recording a visit that has already happened
                # is a legitimate admin action. The AI booking path
                # (`book_for_conversation`) rejects all three, because none
                # of them is ever something to promise a live caller.
                if verdict is SlotVerdict.FULL:
                    raise AppointmentSlotUnavailableError()

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

        if (
            not acting_user.has_permission(Permissions.APPOINTMENTS_MANAGE)
            and appointment.assigned_technician_user_id != acting_user.id
        ):
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
