"""The appointment-consent invariant: OFFERED -> CALLER SELECTED -> BOOKED.

`test_offered_slot_enforcement.py` covers the first arrow — a time the caller
was never read cannot be booked. This module covers the second, which that
one cannot: a time the caller *was* read but never *chose* must not be
booked either.

The failure being reproduced
----------------------------
A real-model run offered three times and booked one of them without the
caller ever answering. Every one of those three was genuinely offered, so
the offered-slot check approved the write. What was missing is any record
that the caller picked anything.

What is actually enforced, and what is not
------------------------------------------
Nothing here parses English. Resolving "the first one", "the 9:30", or "yeah
that works" to an instant is the model's job and stays the model's job —
these tests pass the model's *conclusion* to the backend and assert on what
the backend does with it. There is deliberately no test asserting that the
string "the first one" maps to slot zero, because no code does that and none
should: a keyword table would be a second, worse interpreter that goes stale
the moment a caller phrases it differently.

What the backend enforces is narrower and mechanical:

1. the instant was offered to THIS conversation, in THIS organization, and
2. the caller has spoken since it was offered — the selection lands in a
   strictly later turn than the offer, and
3. exactly one selection is live at a time, and
4. the booking is for that selected instant.

Turn indices are conversation-message counts the backend derives from its own
storage, which is what makes (2) unfakeable: a model can claim anything about
what the caller said, but it cannot manufacture a conversation turn.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest

from app.application.services.appointment_service import AppointmentService
from app.application.services.customer_service import CustomerService
from app.application.services.dispatch_service import DispatchService
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
from app.domain.entities.offered_slot import SlotSelectionVerdict
from app.domain.entities.service import Service
from app.domain.exceptions import SlotNotSelectedError
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
    FakeOfferedSlotRepository,
    FakeRoleRepository,
    FakeServiceRepository,
    FakeTechnicianProfileRepository,
    FakeUserRepository,
    fake_settings,
)

_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()

# Message counts, not turn ordinals: a completed turn writes two messages, so
# these advance by two. `_OFFER_TURN` is the turn the assistant reads times
# out on; `_ANSWER_TURN` is the next one, where the caller can reply.
_OFFER_TURN = 2
_ANSWER_TURN = 4
_LATER_TURN = 6

_NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
_MONDAY_8AM = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)

_AC_REPAIR = Service(
    id=uuid.uuid4(),
    organization_id=_ORG_ID,
    name="Air Conditioning Repair",
    description=None,
    category="cooling",
    is_emergency_eligible=False,
    is_active=True,
    default_duration_minutes=90,
)

_FRANK = {
    "customer_name": "Frank",
    "customer_phone": "5550001111",
    "service_address": "11 69 Street, California",
    "problem_description": "AC running but not cooling.",
    "classification": "non_emergency",
    "service_name": "Air Conditioning Repair",
}


def _profile(organization_id: uuid.UUID = _ORG_ID) -> BusinessProfile:
    now = datetime.now(timezone.utc)
    return BusinessProfile(
        id=uuid.uuid4(),
        organization_id=organization_id,
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


def _week() -> list[WeeklyHours]:
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


class _Harness:
    """One set of repositories shared by every conversation, so the isolation
    cases below are real rather than vacuous."""

    def __init__(self) -> None:
        self.settings = fake_settings()
        self.conversations = FakeConversationRepository()
        self.outcomes = FakeConversationOutcomeRepository(self.conversations)
        self.appointments = FakeAppointmentRepository()
        self.offered_slots = FakeOfferedSlotRepository()
        self.booking_lock = FakeBookingLock()
        self.customers = FakeCustomerRepository()
        self.tickets = FakeEmergencyTicketRepository()
        technicians = FakeTechnicianProfileRepository()
        services = FakeServiceRepository([_AC_REPAIR])
        profiles = FakeBusinessProfileRepository(_profile())
        hours = FakeBusinessHoursRepository(_week())

        availability = DatabaseAvailabilityProvider(
            appointment_repository=self.appointments,
            business_hours_repository=hours,
            business_profile_repository=profiles,
            service_repository=services,
            technician_profile_repository=technicians,
            settings=self.settings,
            now=_NOW,
        )
        self.appointment_service = AppointmentService(
            appointment_repository=self.appointments,
            technician_profile_repository=technicians,
            conversation_outcome_repository=self.outcomes,
            service_repository=services,
            business_hours_repository=hours,
            business_profile_repository=profiles,
            availability_provider=availability,
            booking_lock=self.booking_lock,
            offered_slot_repository=self.offered_slots,
        )
        self.factory = VoiceToolExecutor(
            appointment_service=self.appointment_service,
            dispatch_service=DispatchService(
                emergency_ticket_repository=self.tickets,
                technician_profile_repository=technicians,
                conversation_outcome_repository=self.outcomes,
                conversation_repository=self.conversations,
                user_repository=FakeUserRepository(),
                role_repository=FakeRoleRepository(),
            ),
            customer_service=CustomerService(
                customer_repository=self.customers,
                conversation_outcome_repository=self.outcomes,
                emergency_ticket_repository=self.tickets,
                appointment_repository=self.appointments,
                caller_identity_repository=FakeCallerIdentityRepository(self.customers),
            ),
            conversation_outcome_repository=self.outcomes,
            service_repository=services,
            business_profile_repository=profiles,
            offered_slot_repository=self.offered_slots,
            settings=self.settings,
        )

    async def call(
        self,
        conversation_id: uuid.UUID,
        tool_name: str,
        *,
        organization_id: uuid.UUID = _ORG_ID,
        turn: int = _ANSWER_TURN,
        **arguments: object,
    ) -> dict:
        executor = self.factory.bind(organization_id, conversation_id, turn)
        result = await executor.execute(
            ToolInvocation(id=f"c_{uuid.uuid4().hex[:8]}", name=tool_name, arguments=arguments)
        )
        return result.content

    async def intake(self, conversation_id: uuid.UUID, **overrides: object) -> dict:
        return await self.call(
            conversation_id,
            CREATE_SERVICE_REQUEST.name,
            turn=_OFFER_TURN,
            **{**_FRANK, **overrides},
        )

    async def offer(self, conversation_id: uuid.UUID, **arguments: object) -> list[dict]:
        """A real availability search on the offering turn. Returns the slots
        the caller would have been read, in the order they were read."""
        result = await self.call(
            conversation_id, CHECK_AVAILABILITY.name, turn=_OFFER_TURN, **arguments
        )
        assert result["success"] is True, result
        return result["slots"]

    async def intake_and_offer(self, conversation_id: uuid.UUID) -> list[dict]:
        await self.intake(conversation_id)
        return await self.offer(conversation_id, service_name=_AC_REPAIR.name)

    async def selection(self, conversation_id: uuid.UUID):
        return await self.appointment_service.get_active_selection_for_conversation(
            _ORG_ID, conversation_id
        )

    async def appointment_time(self, conversation_id: uuid.UUID) -> datetime | None:
        appointment = await self.appointments.get_by_conversation_id(conversation_id)
        return appointment.scheduled_start_at if appointment else None


# =============================================================================
# CASE A — offered, never chosen, model books anyway. The observed failure.
# =============================================================================


@pytest.mark.asyncio
async def test_case_a_booking_without_a_selection_is_refused():
    """Three times read out, no answer from the caller, and the model goes
    straight to booking. This is the run that motivated the whole invariant."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    assert len(slots) >= 2, "the fixture must offer a real choice"

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, slot_id=slots[0]["slot_id"]
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_SELECTED
    assert result["selection_state"] == "none"
    # Nothing reserved, and the appointment still holds no time.
    assert await harness.appointment_time(conversation) is None
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.status is AppointmentStatus.REQUESTED


@pytest.mark.asyncio
async def test_case_a_the_refusal_tells_the_assistant_to_ask_rather_than_retry():
    """A refusal the assistant cannot act on becomes a retry loop on a live
    call, so the recovery text is part of the contract."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, slot_id=slots[0]["slot_id"]
    )

    guidance = result["next_step"]
    assert "select_appointment_slot" in guidance
    assert "wait for their answer" in guidance
    # And it must not tell the caller anything happened.
    assert "Do NOT tell them anything is booked" in guidance


@pytest.mark.asyncio
async def test_case_a_offering_and_booking_inside_one_turn_can_never_succeed():
    """The tightest version: offer, 'select', and book, all before the caller
    has said a word. The selection is refused on turn ordering alone, so the
    booking has nothing to stand on."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    # Everything on ONE turn — the shape a model falls into when it treats
    # its own offer as the caller's agreement.
    slots = await harness.call(
        conversation, CHECK_AVAILABILITY.name, turn=_ANSWER_TURN, service_name=_AC_REPAIR.name
    )
    chosen = slots["slots"][0]
    selection = await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, turn=_ANSWER_TURN, slot_id=chosen["slot_id"]
    )
    booking = await harness.call(
        conversation, BOOK_APPOINTMENT.name, turn=_ANSWER_TURN, slot_id=chosen["slot_id"]
    )

    assert selection["success"] is False
    assert selection["error"] == ToolErrors.SLOT_NOT_YET_HEARD
    assert booking["success"] is False
    assert booking["error"] == ToolErrors.SLOT_NOT_SELECTED
    assert await harness.appointment_time(conversation) is None


# =============================================================================
# CASE B — the legitimate path still works
# =============================================================================


@pytest.mark.asyncio
async def test_case_b_offer_then_select_then_book_succeeds():
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    chosen = slots[0]

    selection = await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=chosen["slot_id"]
    )
    booking = await harness.call(
        conversation, BOOK_APPOINTMENT.name, slot_id=chosen["slot_id"]
    )

    assert selection["success"] is True
    assert selection["selection_state"] == "selected"
    assert selection["start_time"] == chosen["start_time"]
    assert booking["success"] is True
    assert booking["status"] == "confirmed"

    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None
    assert appointment.status is AppointmentStatus.SCHEDULED
    assert appointment.scheduled_start_at == _MONDAY_8AM


@pytest.mark.asyncio
async def test_case_b_a_selection_recorded_by_date_and_time_also_books():
    """A `slot_id` does not survive to the next turn, so on the real
    cross-turn path the caller's choice arrives as a date and a clock time."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake_and_offer(conversation)

    selection = await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, date="2026-08-24", start_time="08:00"
    )
    booking = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"
    )

    assert selection["success"] is True
    assert booking["success"] is True
    assert await harness.appointment_time(conversation) == _MONDAY_8AM


@pytest.mark.asyncio
async def test_case_b_the_selection_result_never_claims_the_time_is_booked():
    """Selection is not reservation. If its result read as a confirmation the
    assistant would say so, and the booking could still fail afterwards."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)

    selection = await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[0]["slot_id"]
    )

    guidance = selection["next_step"].lower()
    assert "book_appointment" in guidance
    assert "do not tell them it is booked yet" in guidance


# =============================================================================
# CASES C & D — which of the offered times the caller meant
#
# The model resolves the words; these assert the backend faithfully records
# the slot the model concluded on, and — crucially — that a DIFFERENT
# conclusion produces a different booking. If both indices booked the same
# row the invariant would be decorative.
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spoken_choice", "offered_index"),
    [
        ("the first one", 0),
        ("the second option", 1),
        ("the last one", 2),
    ],
)
async def test_cases_c_and_d_the_chosen_offer_index_is_what_gets_booked(
    spoken_choice: str, offered_index: int
):
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    assert len(slots) == 3, "three offers, so first/second/last are distinguishable"
    # What the MODEL concluded `spoken_choice` referred to. The mapping is the
    # model's; the assertion is that the backend books exactly that one.
    chosen = slots[offered_index]

    await harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=chosen["slot_id"])
    booking = await harness.call(
        conversation, BOOK_APPOINTMENT.name, slot_id=chosen["slot_id"]
    )

    assert booking["success"] is True, spoken_choice
    assert booking["start_time"] == chosen["start_time"]
    stored = await harness.appointment_time(conversation)
    assert stored is not None
    assert stored.strftime("%H:%M") == chosen["start_time"]


@pytest.mark.asyncio
async def test_case_d_selecting_one_offer_does_not_authorise_its_neighbours():
    """The narrow property the whole design turns on: consent attaches to an
    instant, not to the list the instant came from."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)

    await harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[1]["slot_id"])
    wrong = await harness.call(
        conversation, BOOK_APPOINTMENT.name, slot_id=slots[2]["slot_id"]
    )

    assert wrong["success"] is False
    assert wrong["error"] == ToolErrors.SLOT_NOT_SELECTED
    assert await harness.appointment_time(conversation) is None


# =============================================================================
# CASE E — the caller changes their mind
# =============================================================================


@pytest.mark.asyncio
async def test_case_e_a_new_selection_supersedes_the_previous_one():
    """"No, actually make it ten." The older choice must stop being bookable
    the moment a newer one is recorded."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    first, second = slots[0], slots[1]

    await harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=first["slot_id"])
    await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, turn=_LATER_TURN, slot_id=second["slot_id"]
    )

    live = await harness.selection(conversation)
    assert live is not None
    assert live.start_at.strftime("%H:%M") == second["start_time"]

    # The superseded time is refused...
    stale = await harness.call(
        conversation, BOOK_APPOINTMENT.name, turn=_LATER_TURN, slot_id=first["slot_id"]
    )
    assert stale["success"] is False
    assert stale["error"] == ToolErrors.SLOT_NOT_SELECTED

    # ...and the current one is honoured.
    booking = await harness.call(
        conversation, BOOK_APPOINTMENT.name, turn=_LATER_TURN, slot_id=second["slot_id"]
    )
    assert booking["success"] is True
    assert booking["start_time"] == second["start_time"]


@pytest.mark.asyncio
async def test_case_e_exactly_one_selection_is_ever_live():
    """The production table enforces this with a partial unique index; the
    fake mirrors it. Either way `get_active_selection` must have one answer."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)

    for slot in slots:
        await harness.call(
            conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slot["slot_id"]
        )

    selected = [
        offer
        for (org, conv, _), offer in harness.offered_slots.offers.items()
        if org == _ORG_ID and conv == conversation and offer.is_selected
    ]
    assert len(selected) == 1
    assert selected[0].start_at.strftime("%H:%M") == slots[-1]["start_time"]


# =============================================================================
# CASE F — ambiguity
#
# An ambiguous answer is one the model must NOT resolve. The prompt tells it
# to ask instead of guessing; what the backend guarantees is that not calling
# the tool leaves nothing bookable, and that a guess at a time which was
# never offered is refused outright.
# =============================================================================


@pytest.mark.asyncio
async def test_case_f_an_unresolved_answer_leaves_nothing_bookable():
    """The model heard something it could not pin to one of the three, so it
    records no selection. Every offered time must still be unbookable."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)

    assert await harness.selection(conversation) is None
    for slot in slots:
        result = await harness.call(
            conversation, BOOK_APPOINTMENT.name, slot_id=slot["slot_id"]
        )
        assert result["success"] is False
        assert result["error"] == ToolErrors.SLOT_NOT_SELECTED
    assert await harness.appointment_time(conversation) is None


@pytest.mark.asyncio
async def test_case_f_a_guessed_time_that_was_never_offered_is_refused_at_selection():
    """"Morning sometime" resolved to a plausible-but-unoffered instant. The
    selection is refused, so the guess never reaches the booking path."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake_and_offer(conversation)

    result = await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, date="2026-08-24", start_time="11:15"
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert result["selection_state"] == "none"
    assert await harness.selection(conversation) is None


@pytest.mark.asyncio
async def test_case_f_an_unidentifiable_selection_is_reported_not_crashed():
    """Neither a slot_id nor a date/time pair — the model called the tool with
    nothing usable. A live call must get a sentence, not an exception."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake_and_offer(conversation)

    result = await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=None, date=None, start_time=None
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.INVALID_SLOT
    assert await harness.selection(conversation) is None


# =============================================================================
# CASE G — the chosen slot stops being real
# =============================================================================


@pytest.mark.asyncio
async def test_case_g_a_selected_slot_taken_by_someone_else_does_not_book():
    """Consent is necessary, not sufficient. The caller genuinely chose this
    time; it was taken while they were talking."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    chosen = slots[0]
    await harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=chosen["slot_id"])

    # A different conversation takes it first.
    rival = uuid.uuid4()
    other = await harness.appointments.create(
        organization_id=_ORG_ID,
        conversation_id=rival,
        matched_service_id=_AC_REPAIR.id,
        customer_name="Someone Else",
        customer_phone="5559998888",
        customer_address="2 Other Road",
        summary="Got there first.",
        duration_minutes=90,
    )
    await harness.appointments.schedule(
        _ORG_ID,
        other.id,
        scheduled_start_at=_MONDAY_8AM,
        duration_minutes=90,
        technician_user_id=None,
        assigned_at=datetime.now(timezone.utc),
    )

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, slot_id=chosen["slot_id"]
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_UNAVAILABLE
    assert await harness.appointment_time(conversation) is None


@pytest.mark.asyncio
async def test_case_g_a_slot_that_died_releases_the_selection():
    """The dead choice must not survive to authorise a silent retry: if the
    slot frees up again, the caller has to be asked afresh."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    await harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[0]["slot_id"])

    rival = uuid.uuid4()
    other = await harness.appointments.create(
        organization_id=_ORG_ID,
        conversation_id=rival,
        matched_service_id=_AC_REPAIR.id,
        customer_name="Someone Else",
        customer_phone="5559998888",
        customer_address="2 Other Road",
        summary="Got there first.",
        duration_minutes=90,
    )
    await harness.appointments.schedule(
        _ORG_ID,
        other.id,
        scheduled_start_at=_MONDAY_8AM,
        duration_minutes=90,
        technician_user_id=None,
        assigned_at=datetime.now(timezone.utc),
    )
    failed = await harness.call(
        conversation, BOOK_APPOINTMENT.name, slot_id=slots[0]["slot_id"]
    )
    assert failed["error"] == ToolErrors.SLOT_UNAVAILABLE

    assert await harness.selection(conversation) is None
    assert failed["selection_state"] == "none"
    # The offer itself survives — it really was read out — so re-offering it
    # later is honest. It is just no longer *chosen*.
    offered = await harness.offered_slots.list_offered_starts(_ORG_ID, conversation)
    assert _MONDAY_8AM in offered


@pytest.mark.asyncio
async def test_case_g_a_selected_slot_that_has_since_passed_is_refused():
    """A long call can outlive the time the caller picked."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    stale = _NOW - timedelta(days=2)
    await harness.offered_slots.record_offered(
        _ORG_ID, conversation, [AvailabilitySlot(start_at=stale, duration_minutes=90)], _OFFER_TURN
    )
    selection = await harness.call(
        conversation,
        SELECT_APPOINTMENT_SLOT.name,
        date=stale.date().isoformat(),
        start_time=stale.strftime("%H:%M"),
    )
    assert selection["success"] is True  # the caller really did choose it

    result = await harness.call(
        conversation,
        BOOK_APPOINTMENT.name,
        date=stale.date().isoformat(),
        start_time=stale.strftime("%H:%M"),
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_IN_THE_PAST
    assert await harness.appointment_time(conversation) is None


@pytest.mark.asyncio
async def test_case_g_a_wrong_year_is_not_offered_and_so_not_selectable():
    """The model reaching for a date a year out — the shape of a date-grounding
    slip. It was never offered, so it cannot be chosen, let alone booked."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake_and_offer(conversation)

    selection = await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, date="2025-08-24", start_time="08:00"
    )
    booking = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2025-08-24", start_time="08:00"
    )

    assert selection["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert booking["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert await harness.appointment_time(conversation) is None


# =============================================================================
# CASE H — the model books something the caller did not choose
# =============================================================================


@pytest.mark.asyncio
async def test_case_h_the_service_layer_refuses_regardless_of_the_tool_layer():
    """Enforced where the write happens, not in the tool wrapper — so a future
    caller of `book_for_conversation` inherits the guarantee automatically."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    await harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[0]["slot_id"])

    unchosen = datetime.strptime(
        f"2026-08-24 {slots[1]['start_time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=timezone.utc)

    with pytest.raises(SlotNotSelectedError):
        await harness.appointment_service.book_for_conversation(
            _ORG_ID, conversation, start_at=unchosen, duration_minutes=90
        )
    assert await harness.appointment_time(conversation) is None


@pytest.mark.asyncio
async def test_case_h_a_selection_cannot_be_borrowed_from_another_conversation():
    """Two live calls on one tenant. One caller chose a time; that must not
    authorise the other's booking of the same time."""
    harness = _Harness()
    chooser, freeloader = uuid.uuid4(), uuid.uuid4()
    slots = await harness.intake_and_offer(chooser)
    await harness.intake(freeloader, customer_phone="5550002222")
    await harness.offer(freeloader, service_name=_AC_REPAIR.name)

    await harness.call(chooser, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[0]["slot_id"])

    result = await harness.call(
        freeloader, BOOK_APPOINTMENT.name, slot_id=slots[0]["slot_id"]
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_SELECTED
    assert await harness.appointment_time(freeloader) is None
    # The chooser's own selection is untouched by the attempt.
    assert await harness.selection(chooser) is not None


@pytest.mark.asyncio
async def test_case_h_a_selection_in_another_organization_authorises_nothing():
    """Tenant isolation on the new state. The selection query is scoped by
    organization first, so a cross-tenant id is inert rather than dangerous."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    # Same conversation id, offered and chosen under a DIFFERENT tenant.
    await harness.offered_slots.record_offered(
        _OTHER_ORG_ID,
        conversation,
        [AvailabilitySlot(start_at=_MONDAY_8AM, duration_minutes=90)],
        _OFFER_TURN,
    )
    await harness.offered_slots.mark_selected(
        _OTHER_ORG_ID, conversation, _MONDAY_8AM, _ANSWER_TURN
    )

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"
    )

    assert result["success"] is False
    # Refused at the offered gate, which runs first — the cross-tenant offer
    # is not visible to this organization at all.
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert await harness.appointment_time(conversation) is None


@pytest.mark.asyncio
async def test_case_h_selecting_across_tenants_is_also_refused():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offered_slots.record_offered(
        _OTHER_ORG_ID,
        conversation,
        [AvailabilitySlot(start_at=_MONDAY_8AM, duration_minutes=90)],
        _OFFER_TURN,
    )

    result = await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, date="2026-08-24", start_time="08:00"
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert await harness.selection(conversation) is None


# =============================================================================
# CASE I — turn ordering and superseded turns
# =============================================================================


@pytest.mark.asyncio
async def test_case_i_a_selection_is_valid_from_any_later_turn():
    """The rule is "strictly later", not "the very next one" — a caller who
    asks two more questions before choosing must still be able to choose."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)

    result = await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, turn=_LATER_TURN, slot_id=slots[0]["slot_id"]
    )

    assert result["success"] is True
    live = await harness.selection(conversation)
    assert live is not None and live.selected_turn_index == _LATER_TURN


@pytest.mark.asyncio
async def test_case_i_a_superseded_turn_cannot_select_what_it_only_just_offered():
    """Vapi re-sends a request per transcript growth, so several requests for
    one utterance share a turn index. None of them may treat its own offer as
    an answer."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    for _ in range(3):
        offered = await harness.call(
            conversation,
            CHECK_AVAILABILITY.name,
            turn=_ANSWER_TURN,
            service_name=_AC_REPAIR.name,
        )
        result = await harness.call(
            conversation,
            SELECT_APPOINTMENT_SLOT.name,
            turn=_ANSWER_TURN,
            slot_id=offered["slots"][0]["slot_id"],
        )
        assert result["success"] is False
        assert result["error"] == ToolErrors.SLOT_NOT_YET_HEARD

    assert await harness.selection(conversation) is None


@pytest.mark.asyncio
async def test_case_i_re_offering_does_not_invalidate_a_choice_already_made():
    """A later `check_availability` must not push `offered_turn_index` forward
    — doing so would retroactively unmake a selection the caller really made,
    and the assistant would ask them to choose the same time twice."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    await harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[0]["slot_id"])

    # The model checks again on a later turn, re-offering the same times.
    await harness.call(
        conversation, CHECK_AVAILABILITY.name, turn=_LATER_TURN, service_name=_AC_REPAIR.name
    )

    live = await harness.selection(conversation)
    assert live is not None, "the re-offer erased a real selection"
    booking = await harness.call(
        conversation, BOOK_APPOINTMENT.name, turn=_LATER_TURN, slot_id=slots[0]["slot_id"]
    )
    assert booking["success"] is True


@pytest.mark.asyncio
async def test_case_i_a_duplicate_booking_attempt_is_idempotent_not_a_second_row():
    """The model retrying the same booking — a real recovery pattern — must
    not create a second appointment or need a second selection."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    await harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[0]["slot_id"])

    first = await harness.call(conversation, BOOK_APPOINTMENT.name, slot_id=slots[0]["slot_id"])
    second = await harness.call(conversation, BOOK_APPOINTMENT.name, slot_id=slots[0]["slot_id"])

    assert first["success"] is True
    assert second["success"] is True
    assert first["appointment_id"] == second["appointment_id"]
    assert len(harness.appointments._appointments) == 1


@pytest.mark.asyncio
async def test_case_i_a_booked_call_still_refuses_an_unchosen_move():
    """After a successful booking the selection stays put, so moving the
    appointment needs the caller to choose again rather than the model
    deciding on their behalf."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    await harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[0]["slot_id"])
    await harness.call(conversation, BOOK_APPOINTMENT.name, slot_id=slots[0]["slot_id"])

    moved = await harness.call(
        conversation, BOOK_APPOINTMENT.name, turn=_LATER_TURN, slot_id=slots[2]["slot_id"]
    )

    assert moved["success"] is False
    assert moved["error"] == ToolErrors.SLOT_NOT_SELECTED
    assert await harness.appointment_time(conversation) == _MONDAY_8AM


# =============================================================================
# CASE J — concurrency, and the protections that must not have regressed
# =============================================================================


@pytest.mark.asyncio
async def test_case_j_two_consenting_callers_racing_one_slot_never_overlap():
    """Both callers genuinely chose the same time, so the loser must lose on
    capacity — inside the lock — rather than on consent. If consent short-
    circuited the race this test would pass while proving nothing, hence the
    explicit SLOT_UNAVAILABLE assertion."""
    harness = _Harness()
    first, second = uuid.uuid4(), uuid.uuid4()
    first_slots = await harness.intake_and_offer(first)
    await harness.intake(second, customer_phone="5550002222")
    second_slots = await harness.offer(second, service_name=_AC_REPAIR.name)
    assert first_slots[0]["slot_id"] == second_slots[0]["slot_id"]

    await harness.call(first, SELECT_APPOINTMENT_SLOT.name, slot_id=first_slots[0]["slot_id"])
    await harness.call(second, SELECT_APPOINTMENT_SLOT.name, slot_id=second_slots[0]["slot_id"])

    results = await asyncio.gather(
        harness.call(first, BOOK_APPOINTMENT.name, slot_id=first_slots[0]["slot_id"]),
        harness.call(second, BOOK_APPOINTMENT.name, slot_id=second_slots[0]["slot_id"]),
    )

    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0]["error"] == ToolErrors.SLOT_UNAVAILABLE
    assert harness.booking_lock.acquisitions >= 2
    assert harness.booking_lock.max_concurrent == 1


@pytest.mark.asyncio
async def test_case_j_concurrent_selections_on_one_call_leave_one_winner():
    """Two overlapping transcription-driven turns both recording a choice. The
    clear-then-set pair must not leave two live selections behind — in
    production the partial unique index is the backstop."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)

    await asyncio.gather(
        harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[0]["slot_id"]),
        harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[1]["slot_id"]),
    )

    live = [
        offer
        for (org, conv, _), offer in harness.offered_slots.offers.items()
        if org == _ORG_ID and conv == conversation and offer.is_selected
    ]
    assert len(live) == 1


# =============================================================================
# The service-level verdicts, tested directly
# =============================================================================


@pytest.mark.asyncio
async def test_the_verdict_for_a_time_that_was_never_offered():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    verdict, offered = await harness.appointment_service.select_slot_for_conversation(
        _ORG_ID, conversation, start_at=_MONDAY_8AM, turn_index=_ANSWER_TURN
    )

    assert verdict is SlotSelectionVerdict.NOT_OFFERED
    assert offered is None


@pytest.mark.asyncio
async def test_the_verdict_when_the_caller_has_not_spoken_since_the_offer():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.offered_slots.record_offered(
        _ORG_ID,
        conversation,
        [AvailabilitySlot(start_at=_MONDAY_8AM, duration_minutes=90)],
        _ANSWER_TURN,
    )

    verdict, offered = await harness.appointment_service.select_slot_for_conversation(
        _ORG_ID, conversation, start_at=_MONDAY_8AM, turn_index=_ANSWER_TURN
    )

    assert verdict is SlotSelectionVerdict.NOT_YET_HEARD
    assert offered is not None and offered.selected_at is None


@pytest.mark.asyncio
async def test_the_verdict_for_a_genuine_choice():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.offered_slots.record_offered(
        _ORG_ID,
        conversation,
        [AvailabilitySlot(start_at=_MONDAY_8AM, duration_minutes=90)],
        _OFFER_TURN,
    )

    verdict, offered = await harness.appointment_service.select_slot_for_conversation(
        _ORG_ID, conversation, start_at=_MONDAY_8AM, turn_index=_ANSWER_TURN
    )

    assert verdict is SlotSelectionVerdict.RECORDED
    assert offered is not None
    assert offered.is_selected
    assert offered.selected_turn_index == _ANSWER_TURN
    assert offered.offered_turn_index == _OFFER_TURN


# =============================================================================
# Emergency calls never reach the appointment ladder at all
# =============================================================================


@pytest.mark.asyncio
async def test_an_emergency_call_cannot_select_a_slot():
    """Selection must refuse an emergency for the same reason booking does —
    otherwise the model loops through consent trying to reach a booking that
    can never complete."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.call(
        conversation,
        CREATE_SERVICE_REQUEST.name,
        turn=_OFFER_TURN,
        **{**_FRANK, "classification": "emergency"},
    )

    result = await harness.call(
        conversation, SELECT_APPOINTMENT_SLOT.name, date="2026-08-24", start_time="08:00"
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.EMERGENCY_NOT_BOOKABLE
    assert await harness.selection(conversation) is None


# =============================================================================
# Progress carried across turns
# =============================================================================


@pytest.mark.asyncio
async def test_progress_tells_a_later_turn_the_caller_has_already_chosen():
    """Tool results are not in the transcript, so without this the next turn
    re-offers the same list and asks a caller who has already answered to
    answer again."""
    harness = _Harness()
    conversation = uuid.uuid4()
    slots = await harness.intake_and_offer(conversation)
    await harness.call(conversation, SELECT_APPOINTMENT_SLOT.name, slot_id=slots[0]["slot_id"])

    progress = await harness.factory.describe_progress(_ORG_ID, conversation)

    assert progress is not None
    assert "ALREADY chosen" in progress
    assert "call book_appointment for that time now" in progress


@pytest.mark.asyncio
async def test_progress_does_not_claim_a_choice_that_was_never_made():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake_and_offer(conversation)

    progress = await harness.factory.describe_progress(_ORG_ID, conversation)

    assert progress is not None
    assert "ALREADY chosen" not in progress
    assert "No appointment time is booked yet" in progress
