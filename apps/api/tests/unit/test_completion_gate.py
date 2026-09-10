"""The turn-scoped failed-booking completion gate.

`is_conversation_complete` is the model's own assertion, and it will set it
while apologising for a booking that did not happen — hanging up on a caller
who has just been told their appointment could not be made, before they can
answer. The gate withholds completion for that one turn.

It is keyed on *what the tools did this turn*, never on appointment state.
An unscheduled appointment is the correct end state when no availability was
found and a callback was offered; blocking that would leave the line silent,
which is the failure mode that has already ended a real call on
`silence-timed-out`. The tests below pin both halves: the block, and the
five completions that must remain untouched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timezone

import pytest

from app.application.services.ai_brain_service import AIBrainService
from app.application.services.appointment_service import AppointmentService
from app.application.services.customer_service import CustomerService
from app.application.services.dispatch_service import DispatchService
from app.application.services.voice_service import (
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
from app.domain.entities.business_hours import WeeklyHours
from app.domain.entities.business_profile import BusinessProfile, BusinessType
from app.domain.entities.conversation import ConversationStatus
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
_ASSISTANT_ID = "asst_completion_gate"
_NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)

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
    "customer_phone": "123456789",
    "service_address": "59th Street, California",
    "problem_description": "AC running but not cooling.",
    "classification": "non_emergency",
    "service_name": "Air Conditioning Repair",
}

# A time no `check_availability` ever returned, so booking it is refused.
_NEVER_OFFERED = {"date": "2026-08-24", "start_time": "03:00"}


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


def _week(closed: bool = False) -> list[WeeklyHours]:
    return [
        WeeklyHours(
            id=uuid.uuid4(),
            organization_id=_ORG_ID,
            day_of_week=day,
            is_closed=closed or day == 6,
            open_time=None if (closed or day == 6) else time(8, 0),
            close_time=None if (closed or day == 6) else time(17, 0),
        )
        for day in range(7)
    ]


class _Harness:
    def __init__(self, provider: ScriptedToolAIProvider, *, closed: bool = False) -> None:
        self.settings = fake_settings()
        self.conversations = FakeConversationRepository()
        self.outcomes = FakeConversationOutcomeRepository(self.conversations)
        self.appointments = FakeAppointmentRepository()
        self.tickets = FakeEmergencyTicketRepository()
        self.customers = FakeCustomerRepository()
        self.offered_slots = FakeOfferedSlotRepository()
        technicians = FakeTechnicianProfileRepository()
        services = FakeServiceRepository([_AC_REPAIR])
        profiles = FakeBusinessProfileRepository(_profile())
        hours = FakeBusinessHoursRepository(_week(closed))

        availability = DatabaseAvailabilityProvider(
            appointment_repository=self.appointments,
            business_hours_repository=hours,
            business_profile_repository=profiles,
            service_repository=services,
            technician_profile_repository=technicians,
            settings=self.settings,
            now=_NOW,
        )
        appointment_service = AppointmentService(
            appointment_repository=self.appointments,
            technician_profile_repository=technicians,
            conversation_outcome_repository=self.outcomes,
            service_repository=services,
            business_hours_repository=hours,
            business_profile_repository=profiles,
            availability_provider=availability,
            booking_lock=FakeBookingLock(),
            offered_slot_repository=self.offered_slots,
        )
        tool_factory = VoiceToolExecutor(
            appointment_service=appointment_service,
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
        self.ai_brain = AIBrainService(
            conversation_repository=self.conversations,
            conversation_outcome_repository=self.outcomes,
            ai_provider=provider,
            business_profile_repository=profiles,
            business_hours_repository=hours,
            service_repository=services,
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
            ai_brain_service=self.ai_brain,
            call_lock=FakeCallLock(),
            supersession=TranscriptSupersession(),
        )
        self.provider = provider
        self.call_id = f"call_{uuid.uuid4().hex[:8]}"

    async def stream_turn(self, utterance: str) -> tuple[list[str], VoiceTurnComplete]:
        spoken: list[str] = []
        complete: VoiceTurnComplete | None = None
        async for event in self.voice.handle_chat_completion_stream(
            vapi_call_id=self.call_id,
            assistant_id=_ASSISTANT_ID,
            phone_number_id=None,
            customer_number="+15551230000",
            customer_utterance=utterance,
        ):
            if isinstance(event, VoiceTextDelta):
                spoken.append(event.text)
            else:
                complete = event
        assert complete is not None
        return spoken, complete

    async def json_turn(self, utterance: str):
        return await self.voice.handle_chat_completion(
            vapi_call_id=self.call_id,
            assistant_id=_ASSISTANT_ID,
            phone_number_id=None,
            customer_number="+15551230000",
            customer_utterance=utterance,
        )

    async def offer_turn(self) -> None:
        """Runs the opening turn: intake, then a real availability search.

        Exists because a caller cannot choose a time in the same turn it was
        read to them, so any test whose subject is a SUCCESSFUL booking needs
        the offer to have happened on an earlier turn. Without this the tests
        below would be silently re-testing the consent gate instead of the
        completion gate they are named for."""
        self.provider.queue_tool_round([(CREATE_SERVICE_REQUEST.name, _FRANK)])
        self.provider.queue_tool_round([(CHECK_AVAILABILITY.name, {})])
        self.provider.queue_reply(
            default_reply(message_to_customer="I have a few times available.")
        )
        await self.stream_turn("My heating has stopped working.")

    async def conversation_status(self, conversation_id: uuid.UUID) -> ConversationStatus:
        conversation = await self.conversations.get_by_id(_ORG_ID, conversation_id)
        assert conversation is not None
        return conversation.status


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


def _failing_booking_provider(message: str = "I'm sorry, I couldn't book that time.") -> ScriptedToolAIProvider:
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round([(CREATE_SERVICE_REQUEST.name, _FRANK)])
    provider.queue_tool_round([(BOOK_APPOINTMENT.name, _NEVER_OFFERED)])
    provider.queue_reply(
        default_reply(message_to_customer=message, is_conversation_complete=True)
    )
    return provider


# --- 1: the block -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_booking_with_complete_true_does_not_end_the_call():
    harness = _Harness(_failing_booking_provider())

    spoken, complete = await harness.stream_turn("Book me for three in the morning.")

    assert harness.provider.results[-1].content["success"] is False
    assert complete.result.should_end_call is False
    assert (
        await harness.conversation_status(complete.result.conversation_id)
        is ConversationStatus.ACTIVE
    )
    # The caller still hears the failure — withholding the hang-up must not
    # withhold the explanation.
    assert "couldn't book that time" in "".join(spoken)


# --- 2: recovery in the same turn -------------------------------------------


@pytest.mark.asyncio
async def test_a_booking_that_fails_then_succeeds_may_still_end_the_call():
    provider = ScriptedToolAIProvider()
    harness = _Harness(provider)
    await harness.offer_turn()

    provider.queue_tool_round([(BOOK_APPOINTMENT.name, _NEVER_OFFERED)])
    provider.queue_tool_round([(CHECK_AVAILABILITY.name, {})])
    provider.queue_tool_round(
        [(SELECT_APPOINTMENT_SLOT.name, {"date": "2026-08-24", "start_time": "08:00"})]
    )
    provider.queue_tool_round(
        [(BOOK_APPOINTMENT.name, {"date": "2026-08-24", "start_time": "08:00"})]
    )
    provider.queue_reply(
        default_reply(
            message_to_customer="You're confirmed for Monday at 8 AM.",
            is_conversation_complete=True,
        )
    )

    _, complete = await harness.stream_turn("Book the first one.")

    assert harness.provider.results[-1].content["success"] is True
    assert complete.result.should_end_call is True
    assert (
        await harness.conversation_status(complete.result.conversation_id)
        is ConversationStatus.COMPLETED
    )


# --- 3-5: completions that must remain untouched ------------------------------


@pytest.mark.asyncio
async def test_an_emergency_completion_is_untouched():
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round(
        [(CREATE_SERVICE_REQUEST.name, {**_FRANK, "classification": "emergency"})]
    )
    provider.queue_reply(
        default_reply(
            message_to_customer="A dispatcher has been alerted and will call you.",
            is_conversation_complete=True,
        )
    )
    harness = _Harness(provider)

    _, complete = await harness.stream_turn("There's smoke coming from the unit.")

    assert complete.result.should_end_call is True
    assert (
        await harness.conversation_status(complete.result.conversation_id)
        is ConversationStatus.COMPLETED
    )


@pytest.mark.asyncio
async def test_an_faq_completion_with_no_tools_is_untouched():
    provider = ScriptedToolAIProvider()
    provider.queue_reply(
        default_reply(
            message_to_customer="We're open eight until five, Monday to Friday.",
            is_conversation_complete=True,
        )
    )
    harness = _Harness(provider)

    _, complete = await harness.stream_turn("What are your hours?")

    assert complete.result.should_end_call is True
    assert (
        await harness.conversation_status(complete.result.conversation_id)
        is ConversationStatus.COMPLETED
    )


@pytest.mark.asyncio
async def test_a_no_availability_completion_is_untouched():
    """The case a DB-state gate would have broken: an appointment exists and
    is deliberately unscheduled, and ending with a callback offer is correct.
    No booking was attempted, so nothing is withheld."""
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round([(CREATE_SERVICE_REQUEST.name, _FRANK)])
    provider.queue_tool_round([(CHECK_AVAILABILITY.name, {})])
    provider.queue_reply(
        default_reply(
            message_to_customer="Nothing is free this week — the office will call you back.",
            is_conversation_complete=True,
        )
    )
    harness = _Harness(provider, closed=True)

    _, complete = await harness.stream_turn("Anything this week?")

    assert harness.provider.results[-1].content["slots"] == []
    appointment = await harness.appointments.get_by_conversation_id(
        complete.result.conversation_id
    )
    assert appointment is not None and appointment.scheduled_start_at is None
    assert complete.result.should_end_call is True
    assert (
        await harness.conversation_status(complete.result.conversation_id)
        is ConversationStatus.COMPLETED
    )


@pytest.mark.asyncio
async def test_a_callback_completion_after_a_failed_availability_check_is_untouched():
    """A failed *non-booking* tool must not gate completion either."""
    provider = ScriptedToolAIProvider()
    provider.queue_tool_round([(CHECK_AVAILABILITY.name, {"preferred_date": "not-a-date"})])
    provider.queue_reply(
        default_reply(
            message_to_customer="I'll have the office call you back.",
            is_conversation_complete=True,
        )
    )
    harness = _Harness(provider)

    _, complete = await harness.stream_turn("Call me back instead.")

    assert harness.provider.results[-1].content["success"] is False
    assert complete.result.should_end_call is True


# --- 6: the next turn recovers normally ---------------------------------------


@pytest.mark.asyncio
async def test_the_following_turn_can_complete_normally():
    """The block costs exactly one turn. The caller answers, and the call
    ends as it should — so the line can never be held open indefinitely."""
    harness = _Harness(_failing_booking_provider())
    _, first = await harness.stream_turn("Book me for three in the morning.")
    assert first.result.should_end_call is False

    harness.provider.queue_reply(
        default_reply(
            message_to_customer="No problem — we'll call you back. Goodbye.",
            is_conversation_complete=True,
        )
    )
    _, second = await harness.stream_turn("Never mind, I'll call back later.")

    assert second.result.should_end_call is True
    assert (
        await harness.conversation_status(second.result.conversation_id)
        is ConversationStatus.COMPLETED
    )


# --- 7: transport parity ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_non_streaming_transport_gates_identically():
    harness = _Harness(_failing_booking_provider())

    result = await harness.json_turn("Book me for three in the morning.")

    assert result.should_end_call is False
    assert (
        await harness.conversation_status(result.conversation_id) is ConversationStatus.ACTIVE
    )


@pytest.mark.asyncio
async def test_the_non_streaming_transport_allows_a_recovered_booking():
    provider = ScriptedToolAIProvider()
    harness = _Harness(provider)
    await harness.offer_turn()

    provider.queue_tool_round([(BOOK_APPOINTMENT.name, _NEVER_OFFERED)])
    provider.queue_tool_round([(CHECK_AVAILABILITY.name, {})])
    provider.queue_tool_round(
        [(SELECT_APPOINTMENT_SLOT.name, {"date": "2026-08-24", "start_time": "08:00"})]
    )
    provider.queue_tool_round(
        [(BOOK_APPOINTMENT.name, {"date": "2026-08-24", "start_time": "08:00"})]
    )
    provider.queue_reply(
        default_reply(message_to_customer="Confirmed.", is_conversation_complete=True)
    )

    result = await harness.json_turn("Book the first one.")

    assert result.should_end_call is True


# --- 10: a clean booking is never mistaken for a failed one -------------------


@pytest.mark.asyncio
async def test_a_successful_booking_sets_no_failed_state():
    provider = ScriptedToolAIProvider()
    harness = _Harness(provider)
    await harness.offer_turn()

    provider.queue_tool_round(
        [(SELECT_APPOINTMENT_SLOT.name, {"date": "2026-08-24", "start_time": "08:00"})]
    )
    provider.queue_tool_round(
        [(BOOK_APPOINTMENT.name, {"date": "2026-08-24", "start_time": "08:00"})]
    )
    provider.queue_reply(
        default_reply(
            message_to_customer="You're confirmed for Monday at 8 AM.",
            is_conversation_complete=True,
        )
    )

    _, complete = await harness.stream_turn("The first one, please.")

    assert harness.provider.results[-1].content["success"] is True
    assert complete.result.should_end_call is True
    appointment = await harness.appointments.get_by_conversation_id(
        complete.result.conversation_id
    )
    assert appointment is not None and appointment.scheduled_start_at is not None
