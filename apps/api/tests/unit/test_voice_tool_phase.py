"""The voice transport's behaviour around a tool round.

Drives the whole stack a live call drives — `VoiceService` ->
`AIBrainService` -> provider -> `VoiceToolExecutor` -> the real appointment,
dispatch, and customer services — with only the model and the database
swapped for doubles. So these tests answer the question the 2026-08-22 call
raised in the form the caller experiences it: what does the phone line
actually emit, and in what order?

Two properties matter and neither is cosmetic:

- The caller hears something during the tool round. A tool round is a second
  model call, and Vapi has already hung one call up on `silence-timed-out`.
- The caller hears *nothing that claims an outcome* until the tools have
  run. The holding phrase is system-authored and states only that work is
  in progress, so it cannot become the unbacked promise this work exists to
  eliminate.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone

import pytest

from app.application.services.ai_brain_service import AIBrainService
from app.application.services.appointment_service import AppointmentService
from app.application.services.customer_service import CustomerService
from app.application.services.dispatch_service import DispatchService
from app.application.services.voice_service import (
    _DEFAULT_PROGRESS_PHRASE,
    _PROGRESS_PHRASES,
    TranscriptSupersession,
    VoiceService,
    VoiceTextDelta,
    VoiceTurnComplete,
)
from app.application.services.voice_tool_executor import VoiceToolExecutor
from app.domain.ai.tools import (
    BOOK_APPOINTMENT,
    CHECK_AVAILABILITY,
    CREATE_SERVICE_REQUEST,
    SELECT_APPOINTMENT_SLOT,
)
from app.domain.entities.appointment import AppointmentStatus
from app.domain.entities.business_hours import WeeklyHours
from app.domain.entities.business_profile import BusinessProfile, BusinessType
from app.domain.entities.service import Service
from app.domain.entities.voice_line import VoiceLine, VoiceProvider
from app.infrastructure.scheduling.database_availability_provider import (
    DatabaseAvailabilityProvider,
)
from tests.fakes import (
    FakeAppointmentRepository,
    FakeBookingLock,
    FakeBusinessHoursRepository,
    FakeBusinessProfileRepository,
    FakeCallerIdentityRepository,
    FakeCallLock,
    FakeConversationOutcomeRepository,
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeEmergencyKeywordRepository,
    FakeEmergencyTicketRepository,
    FakeFAQRepository,
    FakeOfferedSlotRepository,
    FakeRoleRepository,
    FakeServiceAreaRepository,
    FakeServiceRepository,
    FakeTechnicianProfileRepository,
    FakeUserRepository,
    ScriptedToolAIProvider,
    default_reply,
    fake_settings,
)
from tests.unit.test_voice_service import FakeVoiceCallRepository, FakeVoiceLineRepository

_ORG_ID = uuid.uuid4()
_ASSISTANT_ID = "asst_tool_phase"
_CALL_ID = "call_tool_phase_1"
_NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
_MONDAY = date(2026, 8, 24)

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

_LUCKY = {
    "customer_name": "Lucky",
    "customer_phone": "123456789",
    "service_address": "16th Street, California",
    "problem_description": "AC is running but not cooling the house.",
    "classification": "non_emergency",
    "service_name": "Air Conditioning Repair",
}

# Per tool, so the caller hears work that matches what they asked for.
# Imported rather than copied, so a reworded phrase cannot leave these
# tests asserting stale text.
_BOOKING_PHRASE = _PROGRESS_PHRASES["book_appointment"]
_AVAILABILITY_PHRASE = _PROGRESS_PHRASES["check_availability"]
_LOGGING_PHRASE = _PROGRESS_PHRASES["create_service_request"]
_ANY_PROGRESS_PHRASE = {*_PROGRESS_PHRASES.values(), _DEFAULT_PROGRESS_PHRASE}


class _VoiceHarness:
    def __init__(self, provider: ScriptedToolAIProvider) -> None:
        self.settings = fake_settings()
        self.conversations = FakeConversationRepository()
        self.outcomes = FakeConversationOutcomeRepository(self.conversations)
        self.appointments = FakeAppointmentRepository()
        self.tickets = FakeEmergencyTicketRepository()
        self.customers = FakeCustomerRepository()
        self.technicians = FakeTechnicianProfileRepository()
        self.services = FakeServiceRepository([_AC_REPAIR])
        self.profiles = FakeBusinessProfileRepository(_profile())
        self.hours = FakeBusinessHoursRepository(_standard_week())
        self.offered_slots = FakeOfferedSlotRepository()

        availability = DatabaseAvailabilityProvider(
            appointment_repository=self.appointments,
            business_hours_repository=self.hours,
            business_profile_repository=self.profiles,
            service_repository=self.services,
            technician_profile_repository=self.technicians,
            settings=self.settings,
            now=_NOW,
        )
        appointment_service = AppointmentService(
            appointment_repository=self.appointments,
            technician_profile_repository=self.technicians,
            conversation_outcome_repository=self.outcomes,
            service_repository=self.services,
            business_hours_repository=self.hours,
            business_profile_repository=self.profiles,
            availability_provider=availability,
            booking_lock=FakeBookingLock(),
            offered_slot_repository=self.offered_slots,
        )
        tool_factory = VoiceToolExecutor(
            appointment_service=appointment_service,
            dispatch_service=DispatchService(
                emergency_ticket_repository=self.tickets,
                technician_profile_repository=self.technicians,
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
            service_repository=self.services,
            business_profile_repository=self.profiles,
            offered_slot_repository=self.offered_slots,
            settings=self.settings,
        )
        ai_brain = AIBrainService(
            conversation_repository=self.conversations,
            conversation_outcome_repository=self.outcomes,
            ai_provider=provider,
            business_profile_repository=self.profiles,
            business_hours_repository=self.hours,
            service_repository=self.services,
            service_area_repository=FakeServiceAreaRepository(),
            faq_repository=FakeFAQRepository(),
            emergency_keyword_repository=FakeEmergencyKeywordRepository(),
            settings=self.settings,
            tool_executor_factory=tool_factory,
        )
        self.voice = VoiceService(
            voice_line_repository=FakeVoiceLineRepository([_voice_line()]),
            voice_call_repository=FakeVoiceCallRepository(),
            conversation_repository=self.conversations,
            ai_brain_service=ai_brain,
            call_lock=FakeCallLock(),
            supersession=TranscriptSupersession(),
        )
        self.provider = provider

    async def turn(self, utterance: str) -> tuple[list[str], VoiceTurnComplete | None]:
        spoken: list[str] = []
        complete: VoiceTurnComplete | None = None
        async for event in self.voice.handle_chat_completion_stream(
            vapi_call_id=_CALL_ID,
            assistant_id=_ASSISTANT_ID,
            phone_number_id=None,
            customer_number="+15551230000",
            customer_utterance=utterance,
        ):
            if isinstance(event, VoiceTextDelta):
                spoken.append(event.text)
            else:
                complete = event
        return spoken, complete


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


def _voice_line() -> VoiceLine:
    now = datetime.now(timezone.utc)
    return VoiceLine(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        provider=VoiceProvider.VAPI,
        vapi_assistant_id=_ASSISTANT_ID,
        vapi_phone_number_id=None,
        phone_number="+15005550006",
        is_active=True,
        created_at=now,
        updated_at=now,
    )


# --- The holding phrase ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_round_emits_the_holding_phrase_before_any_reply_text():
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round([(CREATE_SERVICE_REQUEST.name, _LUCKY)])
    provider.queue_reply(default_reply(message_to_customer="I've logged your request."))
    harness = _VoiceHarness(provider)

    spoken, _ = await harness.turn("My AC is running but not cooling.")

    assert spoken[0] == _LOGGING_PHRASE
    assert spoken[1:] == ["I've logged your request."]


def test_no_progress_phrase_claims_an_outcome():
    """Every progress phrase is emitted before its tool has run, so any
    completed-tense claim in one would be unbacked by construction.

    Naming the work is fine and now required — "I'll book that appointment
    now" is a promise of effort. What must never appear is a word that says
    it already happened."""
    for phrase in _ANY_PROGRESS_PHRASE:
        lowered = phrase.lower()
        for forbidden in (
            "booked",
            "scheduled",
            "confirmed",
            "dispatched",
            "is set",
            "all set",
            "you're set",
        ):
            assert forbidden not in lowered, f"{phrase!r} claims an outcome"


def test_every_progress_phrase_is_well_formed_for_speech():
    """These are read aloud by TTS. "one moment" tacked on after a comma was
    rendered as a clipped "1 moment", so each phrase ends as its own
    sentence with the word spelled out."""
    for phrase in _ANY_PROGRESS_PHRASE:
        assert phrase == phrase.strip()
        assert phrase[0].isupper(), f"{phrase!r} does not start a sentence"
        assert phrase.endswith("."), f"{phrase!r} does not end a sentence"
        assert "1 moment" not in phrase
        assert ", one moment" not in phrase, f"{phrase!r} joins the pause with a comma"


def test_the_booking_phrase_says_it_is_booking_not_checking():
    """The live regression: the caller said "I see it's 9, but please book
    that" and heard "Let me check that for you, one moment" — an
    availability lookup, not the booking they asked for."""
    booking = _BOOKING_PHRASE.lower()

    assert "book" in booking
    assert "check" not in booking
    assert "availability" not in booking
    # And the availability phrase remains distinct from it.
    assert _AVAILABILITY_PHRASE != _BOOKING_PHRASE
    assert "check" in _AVAILABILITY_PHRASE.lower()


def test_a_round_bundling_several_tools_announces_the_booking():
    """Booking is the outcome the caller is most specifically waiting on."""
    from app.application.services.voice_service import _progress_phrase

    assert (
        _progress_phrase(("create_service_request", "book_appointment"))
        == _BOOKING_PHRASE
    )
    assert (
        _progress_phrase(("create_service_request", "check_availability"))
        == _AVAILABILITY_PHRASE
    )
    assert _progress_phrase(("some_future_tool",)) == _DEFAULT_PROGRESS_PHRASE


@pytest.mark.asyncio
async def test_the_holding_phrase_is_said_once_even_across_several_tool_rounds():
    """Two rounds saying it twice in a row sounds like a stuck line."""
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round([(CREATE_SERVICE_REQUEST.name, _LUCKY)])
    provider.queue_tool_round([(CHECK_AVAILABILITY.name, {"service_name": "Air Conditioning Repair"})])
    provider.queue_reply(default_reply(message_to_customer="I have Monday at 8 AM."))
    harness = _VoiceHarness(provider)

    spoken, _ = await harness.turn("Anytime Monday works.")

    assert sum(1 for line in spoken if line in _ANY_PROGRESS_PHRASE) == 1
    assert spoken[0] == _LOGGING_PHRASE


@pytest.mark.asyncio
async def test_no_holding_phrase_when_the_model_already_announced_the_work():
    """A response can carry both speech and a tool call. Following the
    model's own "I'll check that for you now" with the system's "let me check
    that for you, one moment" is the same sentence twice."""
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round(
        [(CHECK_AVAILABILITY.name, {"service_name": "Air Conditioning Repair"})],
        speak="I'll check our availability for you now.",
    )
    provider.queue_reply(default_reply(message_to_customer="I have Monday at 8 AM."))
    harness = _VoiceHarness(provider)

    spoken, _ = await harness.turn("Anytime Monday works.")

    assert not _ANY_PROGRESS_PHRASE & set(spoken)
    assert spoken == ["I'll check our availability for you now.", "I have Monday at 8 AM."]


@pytest.mark.asyncio
async def test_the_caller_still_hears_something_during_that_tool_round():
    """Suppressing the holding phrase must never reintroduce dead air — the
    model's own announcement has to reach the caller before the tools run."""
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round(
        [(CHECK_AVAILABILITY.name, {})], speak="One moment, checking the schedule."
    )
    provider.queue_reply(default_reply(message_to_customer="Monday at 8 AM is free."))
    harness = _VoiceHarness(provider)

    spoken, _ = await harness.turn("What have you got?")

    assert spoken[0] == "One moment, checking the schedule."


@pytest.mark.asyncio
async def test_a_later_silent_round_does_not_echo_the_models_own_announcement():
    """Observed on a real streamed call: the model narrated round one and
    called a tool silently in round two, so the caller heard
    "...while I check available appointment times." immediately followed by
    "Let me check that for you, one moment." Dead air is a property of the
    turn, not of one round."""
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round(
        [(CREATE_SERVICE_REQUEST.name, _LUCKY)],
        speak="I'm logging that now, please hold while I check our times.",
    )
    provider.queue_tool_round([(CHECK_AVAILABILITY.name, {})])
    provider.queue_reply(default_reply(message_to_customer="I have Monday at 8 AM."))
    harness = _VoiceHarness(provider)

    spoken, _ = await harness.turn("My AC is not cooling.")

    assert not _ANY_PROGRESS_PHRASE & set(spoken)
    assert spoken == [
        "I'm logging that now, please hold while I check our times.",
        "I have Monday at 8 AM.",
    ]


@pytest.mark.asyncio
async def test_a_silent_tool_round_still_gets_the_holding_phrase():
    """The suppression must be conditional, not a removal — when the model
    says nothing, the system line is the only thing between the caller and
    silence."""
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round([(CHECK_AVAILABILITY.name, {})])
    provider.queue_reply(default_reply(message_to_customer="Monday at 8 AM is free."))
    harness = _VoiceHarness(provider)

    spoken, _ = await harness.turn("What have you got?")

    assert spoken[0] == _AVAILABILITY_PHRASE


@pytest.mark.asyncio
async def test_a_turn_with_no_tool_round_says_no_holding_phrase():
    provider = ScriptedToolAIProvider()
    provider.queue_reply(default_reply(message_to_customer="What is the address?"))
    harness = _VoiceHarness(provider)

    spoken, _ = await harness.turn("Hello?")

    assert spoken == ["What is the address?"]


@pytest.mark.asyncio
async def test_the_holding_phrase_is_never_written_into_the_transcript():
    """It is a transport concern. Persisting it would replay into every later
    prompt of the call as something the assistant supposedly said."""
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round([(CREATE_SERVICE_REQUEST.name, _LUCKY)])
    provider.queue_reply(default_reply(message_to_customer="I've logged your request."))
    harness = _VoiceHarness(provider)

    _, complete = await harness.turn("My AC is not cooling.")

    assert complete is not None
    messages = await harness.conversations.list_messages(complete.result.conversation_id)
    assert [m.content for m in messages if m.role.value == "assistant"] == [
        "I've logged your request."
    ]


# --- The whole workflow, through the phone line ------------------------------


@pytest.mark.asyncio
async def test_the_full_create_check_book_workflow_over_the_voice_transport():
    """The sequence the 2026-08-22 call could not perform, driven end to end
    through `VoiceService`."""
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round([(CREATE_SERVICE_REQUEST.name, _LUCKY)])
    provider.queue_tool_round(
        [(CHECK_AVAILABILITY.name, {"service_name": "Air Conditioning Repair"})]
    )
    harness = _VoiceHarness(provider)

    await harness.turn("My AC is running but it's not cooling.")

    # The availability result the model would have received.
    availability = harness.provider.results[-1].content
    assert availability["success"] is True
    offered = availability["slots"][0]
    assert offered["date"] == "2026-08-24"
    assert offered["start_time"] == "08:00"

    # The caller picks it. Recording the choice and committing it are two
    # separate calls now, and the booking is refused without the first.
    provider.queue_tool_round(
        [
            (SELECT_APPOINTMENT_SLOT.name, {"slot_id": offered["slot_id"]}),
            (BOOK_APPOINTMENT.name, {"slot_id": offered["slot_id"]}),
        ]
    )
    provider.queue_reply(
        default_reply(
            message_to_customer="Your appointment is confirmed for Monday at 8 AM.",
            is_conversation_complete=True,
        )
    )
    spoken, complete = await harness.turn("Monday at eight works.")

    booking = harness.provider.results[-1].content
    assert booking["success"] is True
    assert booking["status"] == "confirmed"

    # The assistant only confirmed after that success, and the call ends.
    assert spoken[0] == _BOOKING_PHRASE
    assert "confirmed" in spoken[-1]
    assert complete is not None and complete.result.should_end_call is True

    # And the database state matches what the caller was told.
    appointment = await harness.appointments.get_by_conversation_id(
        complete.result.conversation_id
    )
    assert appointment is not None
    assert appointment.status is AppointmentStatus.SCHEDULED
    assert appointment.scheduled_start_at == datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_a_failed_booking_leaves_the_appointment_unscheduled():
    """The invariant, from the transport's side: when the tool result says
    the booking failed, there is no scheduled time for any confirmation to
    have been about."""
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round([(CREATE_SERVICE_REQUEST.name, _LUCKY)])
    provider.queue_tool_round(
        [(BOOK_APPOINTMENT.name, {"date": "2026-08-30", "start_time": "10:00"})]
    )
    provider.queue_reply(
        default_reply(message_to_customer="I'm sorry, we're closed that day.")
    )
    harness = _VoiceHarness(provider)

    _, complete = await harness.turn("Can you come Sunday morning?")

    booking = harness.provider.results[-1].content
    assert booking["success"] is False
    # Refused as never-offered before feasibility is even considered:
    # the model picked this Sunday itself.
    assert booking["error"] == "SLOT_NOT_OFFERED"

    assert complete is not None
    appointment = await harness.appointments.get_by_conversation_id(
        complete.result.conversation_id
    )
    assert appointment is not None
    assert appointment.status is AppointmentStatus.REQUESTED
    assert appointment.scheduled_start_at is None
