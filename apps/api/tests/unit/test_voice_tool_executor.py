"""Unit tests for `VoiceToolExecutor` — the three business tools the AI
Brain calls mid-turn.

These are not tests of tool *definitions*. Every case below runs the real
`AppointmentService`, `DispatchService`, `CustomerService`, and
`DatabaseAvailabilityProvider` over in-memory repositories, so a passing
test means a tool call genuinely produced (or genuinely refused to produce)
a record.

The invariant under test throughout: a tool result carrying
`"success": true` corresponds to a real state change, and one carrying
`"success": false` corresponds to no state change at all. That pairing is
the only thing standing between the assistant and the 2026-08-22 failure,
where a booking was announced that never happened.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, time, timezone

import pytest

from app.application.services.appointment_service import AppointmentService
from app.application.services.customer_service import CustomerService
from app.application.services.dispatch_service import DispatchService
from app.application.services.emergency_notification_service import (
    EmergencyNotificationService,
)
from app.application.services.voice_tool_executor import VoiceToolExecutor
from app.domain.ai.tools import (
    BOOK_APPOINTMENT,
    CHECK_AVAILABILITY,
    CREATE_SERVICE_REQUEST,
    SELECT_APPOINTMENT_SLOT,
    ToolErrors,
    ToolInvocation,
)
from app.domain.entities.appointment import AppointmentStatus
from app.domain.entities.availability import AvailabilitySlot
from app.domain.entities.business_hours import WeeklyHours
from app.domain.entities.business_profile import BusinessProfile, BusinessType
from app.domain.entities.emergency_ticket import TicketStatus
from app.domain.entities.service import Service
from app.domain.notifications.emergency import DeliveryStatus, NotificationChannel
from app.infrastructure.scheduling.database_availability_provider import (
    DatabaseAvailabilityProvider,
)
from tests.fakes import (
    FakeAppointmentRepository,
    FakeBookingLock,
    FakeBusinessHoursRepository,
    FakeBusinessProfileRepository,
    FakeCallerIdentityRepository,
    FakeConversationOutcomeRepository,
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeEmergencyTicketRepository,
    FakeNotificationDeliveryRepository,
    FakeNotificationProvider,
    FakeNotificationSettingsRepository,
    FakeOfferedSlotRepository,
    FakeRoleRepository,
    FakeServiceRepository,
    FakeTechnicianProfileRepository,
    FakeUserRepository,
    fake_settings,
)

_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()
_CONVERSATION_ID = uuid.uuid4()

# Conversation-message counts, which is what a turn index is. The harness
# executes at `_TURN_INDEX` and its `offer()` helper records at the earlier
# `_OFFER_TURN_INDEX`, so an offer here stands for one the caller has already
# heard and could have answered. Tests that need the "offered and booked in
# one breath" case drive both numbers explicitly instead.
_OFFER_TURN_INDEX = 2
_TURN_INDEX = 4

_MONDAY = date(2026, 8, 24)
_NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)

_AC_REPAIR = Service(
    id=uuid.uuid4(),
    organization_id=_ORG_ID,
    name="Air Conditioning Repair",
    description="Diagnose and repair a cooling fault.",
    category="cooling",
    is_emergency_eligible=False,
    is_active=True,
    default_duration_minutes=90,
)

# The exact arguments the assistant should produce from the real 2026-08-22
# call, so these tests read as a replay of it.
_LUCKY = {
    "customer_name": "Lucky",
    "customer_phone": "123456789",
    "service_address": "16th Street, California",
    "problem_description": "AC is running but not cooling the house.",
    "classification": "non_emergency",
    "service_name": "Air Conditioning Repair",
}


class _Harness:
    """The whole tool stack, wired the way `deps.py` wires it in production
    but over in-memory repositories."""

    def __init__(
        self,
        *,
        notification_status: DeliveryStatus | None = None,
        **setting_overrides: object,
    ) -> None:
        self.settings = fake_settings(**setting_overrides)
        self.conversations = FakeConversationRepository()
        self.outcomes = FakeConversationOutcomeRepository(self.conversations)
        self.appointments = FakeAppointmentRepository()
        self.tickets = FakeEmergencyTicketRepository()
        self.customers = FakeCustomerRepository()
        self.technicians = FakeTechnicianProfileRepository()
        self.booking_lock = FakeBookingLock()
        self.offered_slots = FakeOfferedSlotRepository()
        self.services = FakeServiceRepository([_AC_REPAIR])
        self.profiles = FakeBusinessProfileRepository(_profile())
        self.hours = FakeBusinessHoursRepository(_standard_week())

        self.availability = DatabaseAvailabilityProvider(
            appointment_repository=self.appointments,
            business_hours_repository=self.hours,
            business_profile_repository=self.profiles,
            service_repository=self.services,
            technician_profile_repository=self.technicians,
            settings=self.settings,
            now=_NOW,
        )
        self.appointment_service = AppointmentService(
            appointment_repository=self.appointments,
            technician_profile_repository=self.technicians,
            conversation_outcome_repository=self.outcomes,
            service_repository=self.services,
            business_hours_repository=self.hours,
            business_profile_repository=self.profiles,
            availability_provider=self.availability,
            booking_lock=self.booking_lock,
            offered_slot_repository=self.offered_slots,
        )
        self.dispatch_service = DispatchService(
            emergency_ticket_repository=self.tickets,
            technician_profile_repository=self.technicians,
            conversation_outcome_repository=self.outcomes,
            conversation_repository=self.conversations,
            user_repository=FakeUserRepository(),
            role_repository=FakeRoleRepository(),
        )
        self.customer_service = CustomerService(
            customer_repository=self.customers,
            conversation_outcome_repository=self.outcomes,
            emergency_ticket_repository=self.tickets,
            appointment_repository=self.appointments,
            caller_identity_repository=FakeCallerIdentityRepository(self.customers),
        )
        # None means "no alerting configured", which is the production
        # default and the state in which the assistant must never claim a
        # dispatcher was alerted. Tests that want the alerted branch ask for
        # it explicitly.
        self.notification_provider = (
            FakeNotificationProvider(status=notification_status)
            if notification_status is not None
            else None
        )
        self.notification_deliveries = FakeNotificationDeliveryRepository()
        self.notifications = (
            EmergencyNotificationService(
                provider=self.notification_provider,
                settings_repository=FakeNotificationSettingsRepository(
                    {_ORG_ID: (NotificationChannel.WEBHOOK, "https://example.invalid/hook")}
                ),
                delivery_repository=self.notification_deliveries,
                settings=self.settings,
            )
            if self.notification_provider is not None
            else None
        )
        self.factory = VoiceToolExecutor(
            appointment_service=self.appointment_service,
            dispatch_service=self.dispatch_service,
            customer_service=self.customer_service,
            conversation_outcome_repository=self.outcomes,
            service_repository=self.services,
            business_profile_repository=self.profiles,
            offered_slot_repository=self.offered_slots,
            settings=self.settings,
            emergency_notification_service=self.notifications,
        )
        self.executor = self.factory.bind(_ORG_ID, _CONVERSATION_ID, _TURN_INDEX)

    async def offer(self, start_at: datetime, duration_minutes: int = 90) -> None:
        """Records an offer without going through `check_availability`.

        Recorded at `_OFFER_TURN_INDEX`, a turn before the harness's own
        `_TURN_INDEX`, so it stands for a time the caller has already been
        read — which is what these tests need in order to be about the
        booking rather than about the offer.

        For tests whose subject is the booking itself; the offer-to-booking
        path proper is covered by `test_offered_slot_enforcement.py`."""
        await self.offered_slots.record_offered(
            _ORG_ID,
            _CONVERSATION_ID,
            [AvailabilitySlot(start_at=start_at, duration_minutes=duration_minutes)],
            _OFFER_TURN_INDEX,
        )

    async def choose(self, start_at: datetime, duration_minutes: int = 90) -> None:
        """Offers a slot AND records the caller choosing it.

        The precondition for any booking after the consent invariant landed.
        Deliberately a separate helper from `offer()` rather than folded into
        it: the tests that assert a booking is REFUSED without a choice call
        `offer()` alone, and merging the two would quietly delete that
        distinction from every test in this file."""
        await self.offer(start_at, duration_minutes)
        await self.offered_slots.mark_selected(
            _ORG_ID, _CONVERSATION_ID, start_at, _TURN_INDEX
        )

    async def call(self, tool_name: str, **arguments: object) -> dict:
        result = await self.executor.execute(
            ToolInvocation(id=f"call_{uuid.uuid4().hex[:8]}", name=tool_name, arguments=arguments)
        )
        return result.content

    async def call_at(self, turn_index: int, tool_name: str, **arguments: object) -> dict:
        """Runs one tool as if it were invoked on a different conversation
        turn.

        The consent invariant is a comparison between two turn indices, so a
        test that only ever executes at one index cannot exercise it. This is
        how the offer-then-choose flow gets modelled honestly — through the
        real bound executor at each turn — rather than by writing selection
        state straight into the repository."""
        executor = self.factory.bind(_ORG_ID, _CONVERSATION_ID, turn_index)
        result = await executor.execute(
            ToolInvocation(id=f"call_{uuid.uuid4().hex[:8]}", name=tool_name, arguments=arguments)
        )
        return result.content


def _profile() -> BusinessProfile:
    now = datetime.now(timezone.utc)
    return BusinessProfile(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        business_type=BusinessType.HVAC,
        display_name="Northside Heating & Cooling",
        phone_number=None,
        timezone="UTC",
        address_line1=None,
        address_line2=None,
        city=None,
        state=None,
        postal_code=None,
        country="US",
        website=None,
        created_at=now,
        updated_at=now,
    )


def _standard_week() -> list[WeeklyHours]:
    return [
        WeeklyHours(
            id=uuid.uuid4(),
            organization_id=_ORG_ID,
            day_of_week=day,
            is_closed=day == 6,
            open_time=None if day == 6 else time(8, 0),
            close_time=None if day == 6 else time(17, 0),
        )
        for day in range(7)
    ]


# --- create_service_request --------------------------------------------------


@pytest.mark.asyncio
async def test_create_service_request_creates_a_real_appointment_and_customer():
    harness = _Harness()

    result = await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    assert result["success"] is True
    assert result["service_request_type"] == "appointment"
    assert result["status"] == AppointmentStatus.REQUESTED.value
    assert result["bookable"] is True
    assert result["duration_minutes"] == 90
    assert result["service_name"] == "Air Conditioning Repair"

    appointment = await harness.appointments.get_by_conversation_id(_CONVERSATION_ID)
    assert appointment is not None
    assert str(appointment.id) == result["service_request_id"]
    # The contact details the 2026-08-22 appointment was missing.
    assert appointment.customer_phone == "123456789"
    assert appointment.customer_address == "16th Street, California"
    # And it is linked to a real, deduplicable customer record.
    customer = await harness.customers.get_by_phone_number(_ORG_ID, "123456789")
    assert customer is not None
    assert appointment.customer_id == customer.id


@pytest.mark.asyncio
async def test_create_service_request_writes_the_outcome_the_existing_seam_reads():
    """The tool reuses the AI-Brain -> Dispatch/Appointments/Customers seam
    rather than inserting rows itself, so Analytics and the dashboard keep
    working unchanged."""
    harness = _Harness()

    await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    outcome = await harness.outcomes.get_by_conversation_id(_CONVERSATION_ID)
    assert outcome is not None
    assert outcome.classification.value == "non_emergency"
    assert outcome.recommended_action.value == "book_appointment"
    assert outcome.matched_service_id == _AC_REPAIR.id
    assert outcome.customer_name == "Lucky"


@pytest.mark.asyncio
async def test_emergency_creates_a_ticket_and_is_explicitly_not_bookable():
    harness = _Harness()

    result = await harness.call(
        CREATE_SERVICE_REQUEST.name, **{**_LUCKY, "classification": "emergency"}
    )

    assert result["success"] is True
    assert result["service_request_type"] == "emergency_ticket"
    assert result["priority"] == "emergency"
    assert result["bookable"] is False

    ticket = await harness.tickets.get_by_conversation_id(_CONVERSATION_ID)
    assert ticket is not None and ticket.status is TicketStatus.NEW
    assert await harness.appointments.get_by_conversation_id(_CONVERSATION_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field",
    ["customer_name", "customer_phone", "service_address", "problem_description"],
)
async def test_missing_required_fields_are_named_so_the_assistant_can_ask(missing_field: str):
    harness = _Harness()

    result = await harness.call(CREATE_SERVICE_REQUEST.name, **{**_LUCKY, missing_field: "  "})

    assert result["success"] is False
    assert result["error"] == ToolErrors.MISSING_REQUIRED_FIELDS
    assert result["missing_fields"] == [missing_field]
    # Nothing was written on a validation failure.
    assert await harness.appointments.get_by_conversation_id(_CONVERSATION_ID) is None
    assert await harness.outcomes.get_by_conversation_id(_CONVERSATION_ID) is None


@pytest.mark.asyncio
async def test_an_unrecognised_classification_is_rejected_rather_than_guessed():
    harness = _Harness()

    result = await harness.call(
        CREATE_SERVICE_REQUEST.name, **{**_LUCKY, "classification": "urgent-ish"}
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.INVALID_ARGUMENTS
    assert await harness.appointments.get_by_conversation_id(_CONVERSATION_ID) is None


@pytest.mark.asyncio
async def test_create_service_request_is_idempotent_across_repeated_calls():
    """The assistant is told it may call this again after a correction, and
    Vapi retries turns — neither may produce a second appointment."""
    harness = _Harness()

    first = await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)
    second = await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    assert first["service_request_id"] == second["service_request_id"]
    assert len(harness.appointments._appointments) == 1


@pytest.mark.asyncio
async def test_an_unmatched_service_name_still_succeeds_with_the_default_duration():
    harness = _Harness(SCHEDULING_DEFAULT_DURATION_MINUTES=60)

    result = await harness.call(
        CREATE_SERVICE_REQUEST.name, **{**_LUCKY, "service_name": "Unicycle Repair"}
    )

    assert result["success"] is True
    assert result["service_name"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quoted",
    [
        "air conditioning repair",  # case differs
        "  Air Conditioning Repair  ",  # transcription whitespace
        "Air Conditioning",  # a prefix of the catalogue name
    ],
)
async def test_case_whitespace_and_partial_service_names_match(quoted: str):
    harness = _Harness()

    result = await harness.call(
        CREATE_SERVICE_REQUEST.name, **{**_LUCKY, "service_name": quoted}
    )

    assert result["service_name"] == "Air Conditioning Repair"
    assert result["duration_minutes"] == 90


@pytest.mark.asyncio
async def test_an_abbreviation_sharing_no_words_falls_back_rather_than_guessing():
    """"AC repair" shares no token with "Air Conditioning Repair", and three
    catalogue entries contain "repair". Guessing between them would apply a
    wrong visit length, offer wrong-sized slots, and risk double-booking — so
    the matcher declines and the default duration applies."""
    harness = _Harness(SCHEDULING_DEFAULT_DURATION_MINUTES=60)

    result = await harness.call(
        CREATE_SERVICE_REQUEST.name, **{**_LUCKY, "service_name": "ac repair"}
    )

    assert result["success"] is True
    assert result["service_name"] is None
    assert result["duration_minutes"] is None  # no matched service, so unset on the record


# --- check_availability ------------------------------------------------------


@pytest.mark.asyncio
async def test_check_availability_returns_real_slots_with_speakable_labels():
    harness = _Harness()

    result = await harness.call(
        CHECK_AVAILABILITY.name, service_name="Air Conditioning Repair"
    )

    assert result["success"] is True
    assert result["duration_minutes"] == 90
    assert result["timezone"] == "UTC"
    assert len(result["slots"]) == 3

    first = result["slots"][0]
    assert first["date"] == "2026-08-24"
    assert first["start_time"] == "08:00"
    assert first["end_time"] == "09:30"
    assert first["label"] == "Monday, August 24 at 8 AM"
    assert first["slot_id"].startswith("slot_")


@pytest.mark.asyncio
async def test_no_available_slots_is_a_success_with_guidance_not_a_failure():
    """A fully-booked week is a real answer. Reporting it as a failure would
    push the assistant toward apologising for a system fault it does not
    have — or worse, inventing a time."""
    harness = _Harness()
    # Take every technician off call so capacity is zero.
    user_id = uuid.uuid4()
    await harness.technicians.create(
        organization_id=_ORG_ID, user_id=user_id, phone_number="+15550001111"
    )
    await harness.technicians.set_on_call(_ORG_ID, user_id, False)

    result = await harness.call(CHECK_AVAILABILITY.name)

    assert result["success"] is True
    assert result["slots"] == []
    assert result["reason"] == "NO_SLOTS_IN_RANGE"
    assert "next_step" in result


@pytest.mark.asyncio
async def test_a_malformed_preferred_date_is_rejected_rather_than_ignored():
    harness = _Harness()

    result = await harness.call(CHECK_AVAILABILITY.name, preferred_date="next Tuesday")

    assert result["success"] is False
    assert result["error"] == ToolErrors.INVALID_ARGUMENTS


@pytest.mark.asyncio
async def test_availability_honours_the_callers_stated_time_window():
    """The 2026-08-22 caller said "anytime from morning, 8 to evening"."""
    harness = _Harness()

    result = await harness.call(
        CHECK_AVAILABILITY.name, earliest_time="13:00", latest_time="14:00"
    )

    assert [slot["start_time"] for slot in result["slots"]] == ["13:00", "13:30", "14:00"]


# --- book_appointment --------------------------------------------------------


async def _create_then_offer(harness: _Harness) -> dict:
    """Intake plus an availability search, both on the turn BEFORE the one
    the harness executes on — so the returned slot is one the caller has
    already been read and could legitimately answer.

    Stops short of the answer. Tests asserting that a booking is refused
    without consent use this; tests whose subject is the booking itself use
    `_create_then_choose`."""
    await harness.call_at(_OFFER_TURN_INDEX, CREATE_SERVICE_REQUEST.name, **_LUCKY)
    availability = await harness.call_at(
        _OFFER_TURN_INDEX, CHECK_AVAILABILITY.name, service_name="Air Conditioning Repair"
    )
    return availability["slots"][0]


async def _create_then_choose(harness: _Harness) -> dict:
    """The full consent ladder, driven through the real tools: offered on one
    turn, chosen by the caller on the next.

    Every test that expects a booking to succeed goes through here, which
    means none of them can pass by accident if the selection step regresses."""
    slot = await _create_then_offer(harness)
    chosen = await harness.call(SELECT_APPOINTMENT_SLOT.name, slot_id=slot["slot_id"])
    assert chosen["success"] is True, chosen
    return slot


@pytest.mark.asyncio
async def test_booking_an_offered_slot_actually_schedules_the_appointment():
    """The end-to-end invariant: success implies a persisted
    `scheduled_start_at`."""
    harness = _Harness()
    slot = await _create_then_choose(harness)

    result = await harness.call(BOOK_APPOINTMENT.name, slot_id=slot["slot_id"])

    assert result["success"] is True
    assert result["status"] == "confirmed"
    assert result["date"] == "2026-08-24"
    assert result["start_time"] == "08:00"
    assert result["end_time"] == "09:30"
    assert result["spoken_time"] == "Monday, August 24 at 8 AM"

    appointment = await harness.appointments.get_by_conversation_id(_CONVERSATION_ID)
    assert appointment is not None
    assert str(appointment.id) == result["appointment_id"]
    assert appointment.status is AppointmentStatus.SCHEDULED
    assert appointment.scheduled_start_at == datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    assert appointment.duration_minutes == 90


@pytest.mark.asyncio
async def test_booking_takes_the_booking_lock():
    """The verify-then-write pair must be serialised, or two callers can both
    pass the capacity check before either writes."""
    harness = _Harness()
    slot = await _create_then_choose(harness)

    await harness.call(BOOK_APPOINTMENT.name, slot_id=slot["slot_id"])

    assert harness.booking_lock.acquisitions >= 1


@pytest.mark.asyncio
async def test_booking_a_slot_someone_else_took_fails_and_writes_nothing():
    harness = _Harness()
    slot = await _create_then_choose(harness)

    # A second conversation takes the same slot first.
    other_conversation = uuid.uuid4()
    other = await harness.appointments.create(
        organization_id=_ORG_ID,
        conversation_id=other_conversation,
        matched_service_id=_AC_REPAIR.id,
        customer_name="Someone Else",
        customer_phone="+15559998888",
        customer_address="2 Other Road",
        summary="Got there first.",
        duration_minutes=90,
    )
    await harness.appointments.schedule(
        _ORG_ID,
        other.id,
        scheduled_start_at=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        duration_minutes=90,
        technician_user_id=None,
        assigned_at=_NOW,
    )

    result = await harness.call(BOOK_APPOINTMENT.name, slot_id=slot["slot_id"])

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_UNAVAILABLE
    ours = await harness.appointments.get_by_conversation_id(_CONVERSATION_ID)
    assert ours is not None
    # The failure left our appointment exactly as it was: still a request,
    # still holding no time. This is the pairing the assistant relies on.
    assert ours.status is AppointmentStatus.REQUESTED
    assert ours.scheduled_start_at is None


@pytest.mark.asyncio
async def test_booking_a_past_slot_is_refused():
    harness = _Harness()
    await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    result = await harness.call(BOOK_APPOINTMENT.name, date="2026-08-20", start_time="10:00")

    assert result["success"] is False
    # Refused for never having been offered, before feasibility is even
    # considered — the authorisation check runs first, and a past time is
    # one `check_availability` can never have returned.
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED
    appointment = await harness.appointments.get_by_conversation_id(_CONVERSATION_ID)
    assert appointment is not None and appointment.scheduled_start_at is None


@pytest.mark.asyncio
async def test_booking_outside_business_hours_is_refused():
    harness = _Harness()
    await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    # Sunday, when this organization is closed — and never offered.
    result = await harness.call(BOOK_APPOINTMENT.name, date="2026-08-30", start_time="10:00")

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED


@pytest.mark.asyncio
async def test_booking_before_creating_a_service_request_is_refused():
    harness = _Harness()

    result = await harness.call(BOOK_APPOINTMENT.name, date="2026-08-24", start_time="10:00")

    assert result["success"] is False
    assert result["error"] == ToolErrors.NO_SERVICE_REQUEST


@pytest.mark.asyncio
async def test_booking_on_an_emergency_call_is_refused_without_looping():
    """`NO_SERVICE_REQUEST` would tell the assistant to create one and retry.
    An emergency must be refused with a code that ends the attempt."""
    harness = _Harness()
    await harness.call(
        CREATE_SERVICE_REQUEST.name, **{**_LUCKY, "classification": "emergency"}
    )

    result = await harness.call(BOOK_APPOINTMENT.name, date="2026-08-24", start_time="10:00")

    assert result["success"] is False
    assert result["error"] == ToolErrors.EMERGENCY_NOT_BOOKABLE
    assert "next_step" in result


@pytest.mark.asyncio
async def test_a_hallucinated_slot_id_with_no_fallback_is_refused():
    harness = _Harness()
    await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    result = await harness.call(BOOK_APPOINTMENT.name, slot_id="slot_whenever_you_like")

    assert result["success"] is False
    assert result["error"] == ToolErrors.INVALID_SLOT


@pytest.mark.asyncio
async def test_a_garbled_slot_id_falls_back_to_an_explicit_date_and_time():
    harness = _Harness()
    await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    await harness.choose(datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc))

    result = await harness.call(
        BOOK_APPOINTMENT.name, slot_id="not-a-slot", date="2026-08-24", start_time="10:00"
    )

    assert result["success"] is True
    assert result["start_time"] == "10:00"


@pytest.mark.asyncio
async def test_rebooking_moves_the_same_appointment_rather_than_conflicting():
    """A caller changing their mind must not collide with the slot they
    currently hold."""
    harness = _Harness()
    slot = await _create_then_choose(harness)
    await harness.call(BOOK_APPOINTMENT.name, slot_id=slot["slot_id"])

    await harness.choose(datetime(2026, 8, 24, 11, 0, tzinfo=timezone.utc))

    result = await harness.call(BOOK_APPOINTMENT.name, date="2026-08-24", start_time="11:00")

    assert result["success"] is True
    assert result["start_time"] == "11:00"
    assert len(harness.appointments._appointments) == 1


# --- Emergency vs standard language ------------------------------------------
#
# A live call on 2026-08-22 classified an AC-not-cooling fault as
# non-emergency, booked it as a standard appointment, and then confirmed it
# with "an emergency technician will be dispatched". The caller was given two
# contradictory answers and would believe the more alarming one.
#
# The wording itself is the model's, so what is pinned here is the guidance
# it reads at the moment it composes that sentence.

_EMERGENCY_WORDS = ("emergency technician", "emergency dispatch", "emergency service")


@pytest.mark.asyncio
async def test_a_successful_booking_tells_the_assistant_to_use_standard_language():
    harness = _Harness()
    slot = await _create_then_choose(harness)

    result = await harness.call(BOOK_APPOINTMENT.name, slot_id=slot["slot_id"])

    assert result["success"] is True
    guidance = result["next_step"]
    assert "standard appointment" in guidance
    assert "emergency" in guidance  # names what to avoid
    assert "Do NOT describe it as" in guidance


@pytest.mark.asyncio
async def test_a_booked_non_emergency_progress_note_forbids_emergency_wording():
    harness = _Harness()
    slot = await _create_then_choose(harness)
    await harness.call(BOOK_APPOINTMENT.name, slot_id=slot["slot_id"])

    progress = await harness.factory.describe_progress(_ORG_ID, _CONVERSATION_ID)

    assert progress is not None
    assert "standard appointment" in progress
    assert "never as emergency service or an emergency dispatch" in progress


@pytest.mark.asyncio
async def test_a_non_emergency_service_request_never_mentions_dispatch():
    """The appointment branch's guidance must not seed emergency framing."""
    harness = _Harness()

    result = await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    assert result["priority"] == "standard"
    lowered = " ".join(str(v) for v in result.values()).lower()
    for word in _EMERGENCY_WORDS:
        assert word not in lowered
    assert "dispatcher" not in lowered


@pytest.mark.asyncio
async def test_the_emergency_flow_keeps_its_emergency_behaviour():
    """The separation must cut both ways — an emergency still routes as one.

    What the assistant may SAY about a dispatcher is a separate question,
    settled by `dispatcher_alerted` and covered in
    `test_emergency_notification.py`. This test is about routing, so it
    asserts on the routing fields only."""
    harness = _Harness(notification_status=DeliveryStatus.DELIVERED)

    result = await harness.call(
        CREATE_SERVICE_REQUEST.name, **{**_LUCKY, "classification": "emergency"}
    )

    assert result["priority"] == "emergency"
    assert result["bookable"] is False
    assert result["service_request_type"] == "emergency_ticket"
    assert result["dispatcher_alerted"] is True
    assert "dispatcher has been alerted" in result["next_step"]

    progress = await harness.factory.describe_progress(_ORG_ID, _CONVERSATION_ID)
    assert progress is not None
    assert "A dispatcher has been alerted" in progress
    assert "do NOT offer or attempt an appointment" in progress


@pytest.mark.asyncio
async def test_a_failed_booking_carries_no_confirmation_guidance():
    """The standard-language instruction rides on success only, so a refusal
    cannot be mistaken for something to confirm."""
    harness = _Harness()
    await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    result = await harness.call(BOOK_APPOINTMENT.name, date="2026-08-30", start_time="10:00")

    assert result["success"] is False
    assert "Confirm this as a standard appointment" not in str(result)
    appointment = await harness.appointments.get_by_conversation_id(_CONVERSATION_ID)
    assert appointment is not None
    assert appointment.status is AppointmentStatus.REQUESTED
    assert appointment.scheduled_start_at is None


# --- Executor contract -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_tool_name_is_reported_not_raised():
    harness = _Harness()

    result = await harness.call("delete_all_appointments")

    assert result["success"] is False
    assert result["error"] == ToolErrors.UNKNOWN_TOOL


@pytest.mark.asyncio
async def test_a_slow_tool_times_out_into_a_speakable_failure():
    harness = _Harness(AI_TOOL_TIMEOUT_SECONDS=0.01)

    async def _never_returns(*args: object, **kwargs: object) -> dict:
        await asyncio.sleep(5)
        return {"success": True}

    harness.factory._check_availability = _never_returns  # type: ignore[method-assign]

    result = await harness.call(CHECK_AVAILABILITY.name)

    assert result["success"] is False
    assert result["error"] == ToolErrors.TIMEOUT


@pytest.mark.asyncio
async def test_an_unexpected_exception_becomes_an_internal_error_not_a_dropped_call():
    harness = _Harness()

    async def _explodes(*args: object, **kwargs: object) -> dict:
        raise RuntimeError("the database fell over")

    harness.factory._check_availability = _explodes  # type: ignore[method-assign]

    result = await harness.call(CHECK_AVAILABILITY.name)

    assert result["success"] is False
    assert result["error"] == ToolErrors.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_tool_arguments_cannot_redirect_a_write_to_another_tenant():
    """No tool schema accepts an organization, and the executor reads none
    from arguments — so an injected id is inert rather than dangerous."""
    harness = _Harness()

    result = await harness.call(
        CREATE_SERVICE_REQUEST.name,
        **_LUCKY,
        organization_id=str(_OTHER_ORG_ID),
        conversation_id=str(uuid.uuid4()),
    )

    assert result["success"] is True
    appointment = await harness.appointments.get_by_conversation_id(_CONVERSATION_ID)
    assert appointment is not None
    assert appointment.organization_id == _ORG_ID


# --- Availability widening ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_requested_day_with_nothing_free_widens_to_the_nearest_times():
    """A live call asked for "tomorrow", which was a Sunday this business is
    closed on. The search returned nothing, the assistant reported nothing,
    and the conversation stalled — while three slots existed the next working
    day."""
    harness = _Harness()

    # 2026-08-30 is a Sunday this organization is closed on, and
    # `days_to_search=1` is what the model emits when a caller names one
    # specific day — which is exactly how the live call reached zero slots,
    # since an unbounded search would simply have rolled forward to Monday.
    result = await harness.call(
        CHECK_AVAILABILITY.name, preferred_date="2026-08-30", days_to_search=1
    )

    assert result["success"] is True
    assert result["slots"], "expected the widened search to find the next open day"
    assert result["widened_search"] is True
    assert result["widened_from"] == "2026-08-30"
    assert "nearest availability" in result["next_step"]


@pytest.mark.asyncio
async def test_an_unconstrained_search_that_finds_nothing_does_not_claim_to_have_widened():
    harness = _Harness()
    user_id = uuid.uuid4()
    await harness.technicians.create(
        organization_id=_ORG_ID, user_id=user_id, phone_number="+15550001111"
    )
    await harness.technicians.set_on_call(_ORG_ID, user_id, False)

    result = await harness.call(CHECK_AVAILABILITY.name)

    assert result["slots"] == []
    assert "widened_search" not in result


# --- Call progress (what the model is told it has already done) --------------


@pytest.mark.asyncio
async def test_no_progress_is_reported_before_anything_has_happened():
    harness = _Harness()

    assert await harness.factory.describe_progress(_ORG_ID, _CONVERSATION_ID) is None


@pytest.mark.asyncio
async def test_progress_tells_the_model_not_to_re_create_the_service_request():
    """The defect this exists for: across seven turns of a live call the
    model called create_service_request seven times and never reached
    booking, because each turn it believed it was starting over."""
    harness = _Harness()
    await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    progress = await harness.factory.describe_progress(_ORG_ID, _CONVERSATION_ID)

    assert progress is not None
    assert "already been created" in progress
    assert "Do NOT call create_service_request again" in progress
    assert "No appointment time is booked yet" in progress


@pytest.mark.asyncio
async def test_progress_never_hands_the_model_a_ready_to_book_argument():
    """The 2026-08-23 regression. An earlier version listed live times *and*
    their `date=`/`start_time=` arguments; the model took one and booked a
    Monday morning the caller had never been read. Progress may say that
    availability exists — never which times, and never something bookable."""
    harness = _Harness()
    await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)

    progress = await harness.factory.describe_progress(_ORG_ID, _CONVERSATION_ID)

    assert progress is not None
    assert "date=" not in progress
    assert "start_time=" not in progress
    assert "08:00" not in progress
    assert "8 AM" not in progress
    # It still tells the model availability exists and how to get it.
    assert "There is availability" in progress
    assert "Call check_availability" in progress


@pytest.mark.asyncio
async def test_progress_reports_a_booked_appointment_as_confirmed():
    harness = _Harness()
    slot = await _create_then_choose(harness)
    await harness.call(BOOK_APPOINTMENT.name, slot_id=slot["slot_id"])

    progress = await harness.factory.describe_progress(_ORG_ID, _CONVERSATION_ID)

    assert progress is not None
    assert "BOOKED for Monday, August 24 at 8 AM" in progress
    assert "No appointment time is booked yet" not in progress


@pytest.mark.asyncio
async def test_progress_on_an_emergency_forbids_offering_an_appointment():
    harness = _Harness(notification_status=DeliveryStatus.DELIVERED)
    await harness.call(
        CREATE_SERVICE_REQUEST.name, **{**_LUCKY, "classification": "emergency"}
    )

    progress = await harness.factory.describe_progress(_ORG_ID, _CONVERSATION_ID)

    assert progress is not None
    assert "An emergency ticket has already been created" in progress
    assert "do NOT offer or attempt an appointment" in progress


@pytest.mark.asyncio
async def test_progress_never_invents_a_time_when_nothing_is_free():
    harness = _Harness()
    await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)
    user_id = uuid.uuid4()
    await harness.technicians.create(
        organization_id=_ORG_ID, user_id=user_id, phone_number="+15550001111"
    )
    await harness.technicians.set_on_call(_ORG_ID, user_id, False)

    progress = await harness.factory.describe_progress(_ORG_ID, _CONVERSATION_ID)

    assert progress is not None
    assert "no availability" in progress
    assert "Do not invent a time" in progress


@pytest.mark.asyncio
async def test_a_bound_executor_only_ever_touches_its_own_conversation():
    harness = _Harness()
    other_conversation = uuid.uuid4()
    other_executor = harness.factory.bind(_ORG_ID, other_conversation, _TURN_INDEX)

    await harness.call(CREATE_SERVICE_REQUEST.name, **_LUCKY)
    await other_executor.execute(
        ToolInvocation(
            id="call_other",
            name=CREATE_SERVICE_REQUEST.name,
            arguments={**_LUCKY, "customer_name": "Different Caller"},
        )
    )

    ours = await harness.appointments.get_by_conversation_id(_CONVERSATION_ID)
    theirs = await harness.appointments.get_by_conversation_id(other_conversation)
    assert ours is not None and theirs is not None
    assert ours.id != theirs.id
    assert ours.customer_name == "Lucky"
    assert theirs.customer_name == "Different Caller"
