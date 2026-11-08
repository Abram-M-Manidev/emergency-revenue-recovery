"""Phase 2: caller contact details — phone validation, privacy, corrections.

Written from the 2026-10-31 test call:
- the caller said nine digits, the number was kept, and the same caller ended
  up with two customer records (one under the caller ID, one under the nine
  digits);
- the model told the caller "I have a record for this phone number";
- nothing let a corrected name or address reach a customer record.

The whole tool stack runs over in-memory repositories, wired as `deps.py`
wires it, with a real conversation carrying a caller ID.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timezone

import pytest

from app.application.services.ai_brain_service import AIBrainService
from app.application.services.appointment_service import AppointmentService
from app.application.services.customer_service import CustomerService
from app.application.services.dispatch_service import DispatchService
from app.application.services.prompt_builder import build_system_prompt
from app.application.services.voice_tool_executor import VoiceToolExecutor
from app.domain.ai.tools import CREATE_SERVICE_REQUEST, ToolErrors, ToolInvocation
from app.domain.entities.business_hours import WeeklyHours
from app.domain.entities.business_profile import BusinessProfile, BusinessType
from app.domain.entities.conversation import ConversationChannel
from app.domain.entities.conversation_message import MessageRole
from app.domain.exceptions import EntityAlreadyExistsError, InvalidPhoneNumberError
from app.infrastructure.scheduling.database_availability_provider import (
    DatabaseAvailabilityProvider,
)
from app.shared.utils.phone import (
    PHONE_INCOMPLETE,
    PHONE_INVALID,
    PHONE_MISSING,
    PHONE_NEEDS_COUNTRY_CODE,
    check_phone_number,
    legacy_phone_keys,
    region_for_country,
    valid_phone_number,
)
from tests.fakes import (
    FakeAIProvider,
    FakeAppointmentRepository,
    FakeBookingLock,
    FakeBusinessHoursRepository,
    FakeBusinessProfileRepository,
    FakeCallerIdentityRepository,
    FakeContactCorrectionRepository,
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
    default_reply,
    fake_settings,
)

_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()
_NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
_CALLER_ID = "+16305550100"
_STATED = "6305550184"
_STATED_E164 = "+16305550184"


def _request(**overrides: object) -> dict:
    arguments: dict = {
        "customer_name": "Myra",
        "customer_phone": _STATED,
        "service_address": "12 Birch Street, San Jose",
        "problem_description": "AC is not cooling.",
        "classification": "non_emergency",
        "service_name": None,
        "use_caller_id": False,
        "confirmed_correction": False,
    }
    arguments.update(overrides)
    return arguments


def _emergency(**overrides: object) -> dict:
    return _request(
        problem_description="Smoke is coming from the furnace.",
        classification="emergency",
        **overrides,
    )


def _profile(country: str | None) -> BusinessProfile:
    now = datetime.now(timezone.utc)
    return BusinessProfile(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        business_type=BusinessType.HVAC,
        display_name="Lucky HVAC Services",
        phone_number=None,
        timezone="UTC",
        address_line1=None,
        address_line2=None,
        city=None,
        state=None,
        postal_code=None,
        country=country,
        website=None,
        created_at=now,
        updated_at=now,
    )


class _Harness:
    def __init__(self, *, country: str | None = "US") -> None:
        self.settings = fake_settings()
        self.conversations = FakeConversationRepository()
        self.outcomes = FakeConversationOutcomeRepository(self.conversations)
        self.appointments = FakeAppointmentRepository()
        self.tickets = FakeEmergencyTicketRepository()
        self.customers = FakeCustomerRepository()
        self.identities = FakeCallerIdentityRepository(self.customers)
        self.profiles = FakeBusinessProfileRepository(_profile(country))
        technicians = FakeTechnicianProfileRepository()
        hours = FakeBusinessHoursRepository(
            [
                WeeklyHours(
                    id=uuid.uuid4(),
                    organization_id=_ORG_ID,
                    day_of_week=day,
                    is_closed=False,
                    open_time=time(8, 0),
                    close_time=time(17, 0),
                )
                for day in range(7)
            ]
        )
        services = FakeServiceRepository([])
        offered = FakeOfferedSlotRepository()
        self.corrections = FakeContactCorrectionRepository()
        self.customer_service = CustomerService(
            customer_repository=self.customers,
            conversation_outcome_repository=self.outcomes,
            emergency_ticket_repository=self.tickets,
            appointment_repository=self.appointments,
            caller_identity_repository=self.identities,
            conversation_repository=self.conversations,
            business_profile_repository=self.profiles,
        )
        self.dispatch = DispatchService(
            emergency_ticket_repository=self.tickets,
            technician_profile_repository=technicians,
            conversation_outcome_repository=self.outcomes,
            conversation_repository=self.conversations,
            user_repository=FakeUserRepository(),
            role_repository=FakeRoleRepository(),
        )
        self.appointment_service = AppointmentService(
            appointment_repository=self.appointments,
            technician_profile_repository=technicians,
            conversation_outcome_repository=self.outcomes,
            service_repository=services,
            business_hours_repository=hours,
            business_profile_repository=self.profiles,
            availability_provider=DatabaseAvailabilityProvider(
                appointment_repository=self.appointments,
                business_hours_repository=hours,
                business_profile_repository=self.profiles,
                service_repository=services,
                technician_profile_repository=technicians,
                settings=self.settings,
                now=_NOW,
            ),
            booking_lock=FakeBookingLock(),
            offered_slot_repository=offered,
        )
        self.factory = VoiceToolExecutor(
            clock=lambda: _NOW,
            appointment_service=self.appointment_service,
            dispatch_service=self.dispatch,
            customer_service=self.customer_service,
            conversation_outcome_repository=self.outcomes,
            service_repository=services,
            business_profile_repository=self.profiles,
            offered_slot_repository=offered,
            settings=self.settings,
            conversation_repository=self.conversations,
            contact_correction_repository=self.corrections,
        )

    async def call_from(self, caller_id: str | None, *, org: uuid.UUID = _ORG_ID) -> uuid.UUID:
        conversation = await self.conversations.create(
            organization_id=org, channel=ConversationChannel.VOICE, caller_phone_number=caller_id
        )
        return conversation.id

    async def tool(
        self,
        conversation_id: uuid.UUID,
        arguments: dict,
        *,
        org: uuid.UUID = _ORG_ID,
        heard: bool = True,
    ) -> dict:
        """One whole caller turn: the tool runs at the turn's real index (the
        number of messages stored before it), then the turn's two messages
        are stored — marked interrupted when the reply never reached the
        caller, as P1 does."""
        turn_index = len(await self.conversations.list_messages(conversation_id))
        result = await self.tool_at(turn_index, conversation_id, arguments, org=org)
        customer = await self.conversations.add_message(
            conversation_id, role=MessageRole.CUSTOMER, content="..."
        )
        reply = await self.conversations.add_message(
            conversation_id, role=MessageRole.ASSISTANT, content="..."
        )
        if not heard:
            await self.conversations.mark_messages_interrupted(
                [customer.id, reply.id], at=datetime.now(timezone.utc)
            )
        return result

    async def tool_at(
        self,
        turn_index: int,
        conversation_id: uuid.UUID,
        arguments: dict,
        *,
        org: uuid.UUID = _ORG_ID,
    ) -> dict:
        executor = self.factory.bind(org, conversation_id, turn_index)
        result = await executor.execute(
            ToolInvocation(id="call_1", name=CREATE_SERVICE_REQUEST.name, arguments=arguments)
        )
        return result.content

    def brain(self, provider: FakeAIProvider) -> AIBrainService:
        return AIBrainService(
            conversation_repository=self.conversations,
            conversation_outcome_repository=self.outcomes,
            ai_provider=provider,
            business_profile_repository=self.profiles,
            business_hours_repository=FakeBusinessHoursRepository(),
            service_repository=FakeServiceRepository(),
            service_area_repository=FakeServiceAreaRepository(),
            faq_repository=FakeFAQRepository(),
            emergency_keyword_repository=FakeEmergencyKeywordRepository(),
            settings=self.settings,
            tool_executor_factory=self.factory,
        )


# --- P2-A: the validator itself -----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "region", "expected"),
    [
        ("6305550184", "US", "+16305550184"),
        ("(630) 555-0184", "US", "+16305550184"),
        ("1 630 555 0184", "US", "+16305550184"),
        ("+1 630 555 0184", None, "+16305550184"),
        ("07400 123456", "GB", "+447400123456"),
        ("+44 7400 123456", "US", "+447400123456"),
        ("81234 56789", "IN", "+918123456789"),
        ("+91 81234 56789", None, "+918123456789"),
        ("0412 345 678", "AU", "+61412345678"),
    ],
)
def test_valid_numbers_normalise_to_e164(raw, region, expected):
    assert valid_phone_number(raw, region=region) == expected


@pytest.mark.parametrize(
    ("raw", "region", "problem"),
    [
        # The 2026-10-31 number: nine digits of a ten-digit US number.
        ("630555018", "US", PHONE_INCOMPLETE),
        ("six three zero", "US", PHONE_MISSING),
        ("", "US", PHONE_MISSING),
        (None, "US", PHONE_MISSING),
        # A 555 area code does not exist.
        ("5551234567", "US", PHONE_INVALID),
        # No country configured: a national number is not guessed at.
        ("6305550184", None, PHONE_NEEDS_COUNTRY_CODE),
    ],
)
def test_invalid_numbers_are_rejected_with_a_reason(raw, region, problem):
    check = check_phone_number(raw, region=region)
    assert check.e164 is None
    assert check.problem == problem


@pytest.mark.parametrize("country", [None, "", "  ", "ZZ", "Narnia"])
def test_an_unknown_country_gives_no_region_rather_than_a_default(country):
    assert region_for_country(country) is None


def test_country_codes_map_to_regions():
    assert region_for_country("US") == "US"
    assert region_for_country("usa") == "US"
    assert region_for_country("IN") == "IN"
    assert region_for_country("GBR") == "GB"


@pytest.mark.parametrize("withheld", ["anonymous", "Restricted", "+266696687", "sip:abc@x", ""])
def test_withheld_or_malformed_caller_ids_are_not_numbers(withheld):
    assert valid_phone_number(withheld) is None


def test_legacy_keys_cover_pre_e164_storage_forms():
    assert set(legacy_phone_keys("+16305550184")) == {"16305550184", "6305550184"}


# --- P2-A: through the tool ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_nine_digit_number_is_refused_and_nothing_is_recorded():
    """The 2026-10-31 regression. The number is refused so the assistant asks
    again, and no request, customer, or outcome phone is written."""
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)

    result = await harness.tool(conversation, _request(customer_phone="630555018"))

    assert result["success"] is False
    assert result["error"] == ToolErrors.INVALID_PHONE_NUMBER
    assert result["phone_problem"] == PHONE_INCOMPLETE
    assert result["digits_heard"] == 9
    # The number itself never travels back to the model.
    assert "630555018" not in str(result)
    assert harness.customers._customers == {}
    assert await harness.appointments.get_by_conversation_id(conversation) is None
    # Nor is it quietly swapped for the caller ID.
    assert await harness.outcomes.get_by_conversation_id(conversation) is None


@pytest.mark.asyncio
async def test_a_stated_number_is_stored_in_e164():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)

    result = await harness.tool(conversation, _request(customer_phone="(630) 555-0184"))

    assert result["success"] is True
    assert result["callback_number_source"] == "stated"
    outcome = await harness.outcomes.get_by_conversation_id(conversation)
    assert outcome is not None and outcome.customer_phone == _STATED_E164
    assert set(c.phone_number for c in harness.customers._customers.values()) == {_STATED_E164}


@pytest.mark.asyncio
async def test_the_caller_id_is_never_used_as_a_callback_number_unasked():
    """Separation: with no stated number, the caller ID is offered to the
    model as a question for the caller, not silently copied into the record
    that becomes the customer key."""
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)

    result = await harness.tool(conversation, _request(customer_phone=""))

    assert result["success"] is False
    assert result["error"] == ToolErrors.MISSING_REQUIRED_FIELDS
    assert result["missing_fields"] == ["customer_phone"]
    assert "use_caller_id" in result["detail"]
    assert harness.customers._customers == {}


@pytest.mark.asyncio
async def test_a_confirmed_caller_id_becomes_the_callback_number():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)

    result = await harness.tool(conversation, _request(customer_phone="", use_caller_id=True))

    assert result["success"] is True
    assert result["callback_number_source"] == "caller_id_confirmed"
    outcome = await harness.outcomes.get_by_conversation_id(conversation)
    assert outcome is not None and outcome.customer_phone == _CALLER_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_id", [None, "anonymous", "+266696687"])
async def test_a_withheld_caller_id_cannot_be_confirmed(caller_id):
    harness = _Harness()
    conversation = await harness.call_from(caller_id)

    result = await harness.tool(conversation, _request(customer_phone="", use_caller_id=True))

    assert result["success"] is False
    assert result["missing_fields"] == ["customer_phone"]
    assert "withheld or unavailable" in result["detail"]


@pytest.mark.asyncio
async def test_an_emergency_goes_ahead_on_the_unconfirmed_caller_id_without_keying_a_customer():
    """A dispatcher needs a way to call back, so the ticket gets the caller
    ID — but nobody confirmed it, so it is not written to the outcome and does
    not become a customer record."""
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)

    result = await harness.tool(conversation, _emergency(customer_phone=""))

    assert result["success"] is True
    assert result["callback_number_source"] == "caller_id_unconfirmed"
    assert "customer_phone" in result["still_needed"]
    ticket = await harness.tickets.get_by_conversation_id(conversation)
    assert ticket is not None and ticket.customer_phone == _CALLER_ID
    outcome = await harness.outcomes.get_by_conversation_id(conversation)
    assert outcome is not None and outcome.customer_phone is None
    assert harness.customers._customers == {}


@pytest.mark.asyncio
async def test_an_emergency_with_an_invalid_number_is_never_refused():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)

    result = await harness.tool(conversation, _emergency(customer_phone="630555018"))

    assert result["success"] is True
    assert result["phone_problem"] == PHONE_INCOMPLETE
    assert "ticket was created anyway" in result["phone_problem_detail"]
    ticket = await harness.tickets.get_by_conversation_id(conversation)
    assert ticket is not None and ticket.customer_phone == _CALLER_ID


@pytest.mark.asyncio
async def test_caller_id_then_stated_number_does_not_create_two_customers():
    """The other half of the 2026-10-31 duplication: a record keyed on the
    caller ID, then a second keyed on the stated number. An unconfirmed
    caller ID no longer keys anything, so only the stated number does."""
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)

    await harness.tool(conversation, _emergency(customer_phone=""))
    await harness.tool(conversation, _emergency(customer_phone=_STATED))

    assert [c.phone_number for c in harness.customers._customers.values()] == [_STATED_E164]


@pytest.mark.asyncio
async def test_without_a_business_country_a_national_number_asks_for_the_country_code():
    harness = _Harness(country=None)
    conversation = await harness.call_from(_CALLER_ID)

    refused = await harness.tool(conversation, _request(customer_phone=_STATED))
    accepted = await harness.tool(conversation, _request(customer_phone="+1 630 555 0184"))

    assert refused["error"] == ToolErrors.INVALID_PHONE_NUMBER
    assert refused["phone_problem"] == PHONE_NEEDS_COUNTRY_CODE
    assert "country code" in refused["detail"]
    assert accepted["success"] is True


@pytest.mark.asyncio
async def test_the_same_number_in_another_tenant_is_a_separate_customer():
    harness = _Harness()
    mine = await harness.call_from(_CALLER_ID)
    theirs = await harness.call_from(_CALLER_ID, org=_OTHER_ORG_ID)

    await harness.tool(mine, _request())
    await harness.tool(theirs, _request(), org=_OTHER_ORG_ID)

    owners = sorted(str(c.organization_id) for c in harness.customers._customers.values())
    assert owners == sorted([str(_ORG_ID), str(_OTHER_ORG_ID)])


@pytest.mark.asyncio
async def test_staff_entered_numbers_are_validated_and_deduplicated():
    harness = _Harness()
    created = await harness.customer_service.create_customer(
        _ORG_ID, full_name="Walk-in", phone_number="(630) 555-0184"
    )
    assert created.phone_number == _STATED_E164

    with pytest.raises(InvalidPhoneNumberError):
        await harness.customer_service.create_customer(
            _ORG_ID, full_name="Typo", phone_number="630555018"
        )
    with pytest.raises(EntityAlreadyExistsError):
        await harness.customer_service.create_customer(
            _ORG_ID, full_name="Again", phone_number="+1 630-555-0184"
        )


@pytest.mark.asyncio
async def test_staff_cannot_create_a_duplicate_of_a_pre_e164_record():
    harness = _Harness()
    await harness.customers.create(
        organization_id=_ORG_ID, full_name="Legacy", phone_number="6305550184", address=None
    )

    with pytest.raises(EntityAlreadyExistsError):
        await harness.customer_service.create_customer(
            _ORG_ID, full_name="Legacy again", phone_number="630 555 0184"
        )


# --- P2-B: database membership is never revealed -----------------------------


async def _prompt(harness: _Harness, conversation: uuid.UUID) -> str:
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(message_to_customer="Understood."))
    await harness.brain(provider).send_message(_ORG_ID, conversation, "My AC is not cooling.")
    return provider.requests[-1].system_prompt


@pytest.mark.asyncio
async def test_a_known_callers_prompt_is_identical_to_a_strangers():
    """The model cannot reveal what it is never told: an existing customer,
    associated with this exact caller ID, changes nothing in the prompt."""
    known = _Harness()
    customer = await known.customers.create(
        organization_id=_ORG_ID,
        full_name="Myra Known",
        phone_number=_STATED_E164,
        address="77 Hidden Lane",
    )
    await known.identities.associate(_ORG_ID, customer_id=customer.id, caller_number=_CALLER_ID)
    stranger = _Harness()

    known_prompt = await _prompt(known, await known.call_from(_CALLER_ID))
    stranger_prompt = await _prompt(stranger, await stranger.call_from(_CALLER_ID))

    assert known_prompt == stranger_prompt
    for leaked in ("Myra Known", "77 Hidden Lane", _CALLER_ID, _STATED_E164):
        assert leaked not in known_prompt


@pytest.mark.asyncio
async def test_a_shared_line_and_a_failing_lookup_change_nothing_either():
    shared = _Harness()
    for name, phone in (("Alice", "+16305550111"), ("Bob", "+16305550122")):
        person = await shared.customers.create(
            organization_id=_ORG_ID, full_name=name, phone_number=phone, address=None
        )
        await shared.identities.associate(_ORG_ID, customer_id=person.id, caller_number=_CALLER_ID)
    failing = _Harness()
    failing.identities.fail_with = RuntimeError("database unavailable")
    baseline = _Harness()

    prompts = {
        await _prompt(h, await h.call_from(_CALLER_ID)) for h in (shared, failing, baseline)
    }

    assert len(prompts) == 1


def test_the_prompt_forbids_revealing_membership():
    prompt = build_system_prompt(
        profile=None,
        weekly_hours=[],
        hours_exceptions=[],
        services=[],
        service_areas=[],
        faqs=[],
        emergency_keywords=[],
        today=_NOW.date(),
        emergency_keyword_hint=False,
    )
    assert "Never say or hint whether this caller" in prompt
    assert "I have a record for this number" in prompt  # quoted as forbidden
    assert "Ask every caller for the details you need" in prompt


@pytest.mark.asyncio
async def test_tool_results_are_the_same_shape_for_new_and_existing_customers():
    """An existing customer must not change what the model learns from the
    tool: the same keys, the same source, and no name or address of theirs."""
    fresh = _Harness()
    existing = _Harness()
    await existing.customers.create(
        organization_id=_ORG_ID,
        full_name="Previous Name",
        phone_number=_STATED_E164,
        address="77 Hidden Lane",
    )

    new_result = await fresh.tool(await fresh.call_from(_CALLER_ID), _request())
    old_result = await existing.tool(await existing.call_from(_CALLER_ID), _request())

    assert set(new_result) == set(old_result)
    assert new_result["callback_number_source"] == old_result["callback_number_source"]
    assert "Previous Name" not in str(old_result)
    assert "77 Hidden Lane" not in str(old_result)


# --- P2-C: corrections --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_name_correction_reaches_the_request_and_this_calls_customer():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _request(customer_name="Meera"))

    result = await harness.tool(conversation, _request(customer_name="Myra"))

    assert result["success"] is True
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.customer_name == "Myra"
    (customer,) = harness.customers._customers.values()
    assert customer.full_name == "Myra"


@pytest.mark.asyncio
async def test_repeated_name_corrections_each_land():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    for name in ("Meera", "Mira", "Myra"):
        await harness.tool(conversation, _request(customer_name=name))

    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.customer_name == "Myra"
    (customer,) = harness.customers._customers.values()
    assert customer.full_name == "Myra"


@pytest.mark.asyncio
async def test_an_address_change_after_the_request_exists_needs_confirmation():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _request(service_address="12 Birch Street"))

    unconfirmed = await harness.tool(conversation, _request(service_address="14 Birch Street"))

    assert unconfirmed["success"] is False
    assert unconfirmed["error"] == ToolErrors.CORRECTION_NOT_CONFIRMED
    assert unconfirmed["reason"] == "not_confirmed"
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.customer_address == "12 Birch Street"

    # The caller heard the read-back and agreed, on a later turn.
    confirmed = await harness.tool(
        conversation, _request(service_address="14 Birch Street", confirmed_correction=True)
    )

    assert confirmed["success"] is True
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.customer_address == "14 Birch Street"
    (customer,) = harness.customers._customers.values()
    assert customer.address == "14 Birch Street"
    # Audit trail: the proposal is kept, marked confirmed.
    (proposal,) = harness.corrections.requests
    assert proposal.confirmed_at is not None
    assert proposal.proposed_value == "14 Birch Street"


@pytest.mark.asyncio
async def test_the_flag_alone_cannot_change_the_address():
    """The model claiming the caller agreed is not enough: with no read-back
    on an earlier turn, the change is refused and recorded as a proposal for
    the caller to confirm next."""
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _request(service_address="12 Birch Street"))

    result = await harness.tool(
        conversation, _request(service_address="14 Birch Street", confirmed_correction=True)
    )

    assert result["error"] == ToolErrors.CORRECTION_NOT_CONFIRMED
    assert result["reason"] == "not_read_back"
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.customer_address == "12 Birch Street"


@pytest.mark.asyncio
async def test_proposing_and_confirming_in_one_turn_is_refused():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _request(service_address="12 Birch Street"))
    turn = len(await harness.conversations.list_messages(conversation))

    await harness.tool_at(turn, conversation, _request(service_address="14 Birch Street"))
    same_breath = await harness.tool_at(
        turn,
        conversation,
        _request(service_address="14 Birch Street", confirmed_correction=True),
    )

    assert same_breath["error"] == ToolErrors.CORRECTION_NOT_CONFIRMED
    assert same_breath["reason"] == "not_yet_heard"


@pytest.mark.asyncio
async def test_a_read_back_the_caller_never_heard_cannot_be_confirmed():
    """P1: a reply Vapi never took in full was not heard, so the caller's
    "yes" cannot be agreement to it."""
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _request(service_address="12 Birch Street"))
    await harness.tool(conversation, _request(service_address="14 Birch Street"), heard=False)

    result = await harness.tool(
        conversation, _request(service_address="14 Birch Street", confirmed_correction=True)
    )

    assert result["error"] == ToolErrors.CORRECTION_NOT_CONFIRMED
    assert result["reason"] == "read_back_not_heard"
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.customer_address == "12 Birch Street"


@pytest.mark.asyncio
async def test_confirming_a_different_address_than_was_read_back_is_refused():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _request(service_address="12 Birch Street"))
    await harness.tool(conversation, _request(service_address="14 Birch Street"))

    result = await harness.tool(
        conversation, _request(service_address="41 Birch Street", confirmed_correction=True)
    )

    assert result["reason"] == "not_read_back"
    pending = await harness.corrections.get_pending(
        _ORG_ID, conversation, field="service_address"
    )
    assert pending is not None and pending.proposed_value == "41 Birch Street"
    assert len(harness.corrections.requests) == 2  # the earlier one is superseded, not lost


@pytest.mark.asyncio
async def test_a_pending_proposal_does_not_carry_across_tenants():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _request(service_address="12 Birch Street"))
    await harness.tool(conversation, _request(service_address="14 Birch Street"))

    assert (
        await harness.corrections.get_pending(
            _OTHER_ORG_ID, conversation, field="service_address"
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_spacing_or_case_difference_is_not_a_correction():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _request(service_address="12 Birch Street"))

    result = await harness.tool(conversation, _request(service_address="12  birch street"))

    assert result["success"] is True


@pytest.mark.asyncio
async def test_corrections_before_the_request_exists_need_no_confirmation():
    """Before any request exists the latest reported value simply wins, and
    the request is created with it."""
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    provider = FakeAIProvider()
    brain = harness.brain(provider)
    provider.queue_reply(default_reply(customer_address="12 Birch Street"))
    await brain.send_message(_ORG_ID, conversation, "It's 12 Birch Street.")
    provider.queue_reply(default_reply(customer_address="14 Birch Street"))
    await brain.send_message(_ORG_ID, conversation, "Sorry, 14 Birch Street.")

    outcome = await harness.outcomes.get_by_conversation_id(conversation)
    assert outcome is not None and outcome.customer_address == "14 Birch Street"

    result = await harness.tool(conversation, _request(service_address=""))
    assert result["success"] is True
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.customer_address == "14 Birch Street"


@pytest.mark.asyncio
async def test_an_old_value_repeated_by_the_model_cannot_undo_a_correction():
    """Once a request exists, the free-form reply no longer owns the contact
    fields: a stale name or address in a later reply is ignored, and only
    the tool (with its confirmation) can change them."""
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _request(customer_name="Myra"))
    provider = FakeAIProvider()
    provider.queue_reply(
        default_reply(
            customer_name="Meera",
            customer_address="1 Wrong Road",
            customer_phone="6305550199",
        )
    )

    await harness.brain(provider).send_message(_ORG_ID, conversation, "Thanks.")

    outcome = await harness.outcomes.get_by_conversation_id(conversation)
    assert outcome is not None
    assert outcome.customer_name == "Myra"
    assert outcome.customer_address == "12 Birch Street, San Jose"
    assert outcome.customer_phone == _STATED_E164


@pytest.mark.asyncio
async def test_an_emergency_ticket_takes_corrections_too():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _emergency(customer_name="Meera"))

    await harness.tool(conversation, _emergency(customer_name="Myra"))
    await harness.tool(conversation, _emergency(service_address="14 Birch Street"))
    await harness.tool(
        conversation,
        _emergency(service_address="14 Birch Street", confirmed_correction=True),
    )

    ticket = await harness.tickets.get_by_conversation_id(conversation)
    assert ticket is not None
    assert ticket.customer_name == "Myra"
    assert ticket.customer_address == "14 Birch Street"


@pytest.mark.asyncio
async def test_a_customer_record_from_before_the_call_is_not_rewritten():
    """A caller ID can be spoofed, and a stated number can be someone
    else's: a caller must not be able to rename an existing customer. The
    request still carries what they said; the old record only gains
    blanks."""
    harness = _Harness()
    existing = await harness.customers.create(
        organization_id=_ORG_ID, full_name="Original Owner", phone_number=_STATED_E164, address=None
    )
    conversation = await harness.call_from(_CALLER_ID)

    await harness.tool(conversation, _request(customer_name="Someone Else"))

    customer = await harness.customers.get_by_id(_ORG_ID, existing.id)
    assert customer is not None
    assert customer.full_name == "Original Owner"
    assert customer.address == "12 Birch Street, San Jose"
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.customer_name == "Someone Else"


@pytest.mark.asyncio
async def test_a_correction_cannot_reach_another_tenants_request():
    harness = _Harness()
    conversation = await harness.call_from(_CALLER_ID)
    await harness.tool(conversation, _request(customer_name="Myra"))

    # The same conversation id, bound to another organization, finds nothing
    # of this tenant's to correct.
    await harness.tool(conversation, _request(customer_name="Intruder"), org=_OTHER_ORG_ID)

    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None
    assert appointment.organization_id == _ORG_ID
    assert appointment.customer_name == "Myra"


def test_a_service_list_limits_what_may_be_booked():
    """Audit C3: a sink leak at a heating-and-cooling company must not be
    recorded or booked as if it were a job the business does."""
    from app.domain.entities.service import Service

    service = Service(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        name="AC repair",
        description=None,
        category=None,
        is_emergency_eligible=False,
        is_active=True,
        default_duration_minutes=60,
    )
    common = dict(
        profile=None,
        weekly_hours=[],
        hours_exceptions=[],
        service_areas=[],
        faqs=[],
        emergency_keywords=[],
        today=_NOW.date(),
        emergency_keyword_hint=False,
    )
    with_services = build_system_prompt(services=[service], **common)
    without_services = build_system_prompt(services=[], **common)

    assert "These are the only services this business provides" in with_services
    assert "do not record a service request or book" in with_services
    # A business with no catalogue is not told it handles nothing.
    assert "These are the only services" not in without_services


def test_the_prompt_defines_urgency_and_speaking_style():
    """Audit B4 and B2. The caller's own label is evidence, not the verdict;
    "(emergency-eligible)" is defined; at most one risk question; uncertainty
    goes to a person. Replies are short, one question at a time, with
    critical details read back."""
    prompt = build_system_prompt(
        profile=None,
        weekly_hours=[],
        hours_exceptions=[],
        services=[],
        service_areas=[],
        faqs=[],
        emergency_keywords=[],
        today=_NOW.date(),
        emergency_keyword_hint=False,
    )
    assert "You decide, not the caller" in prompt
    assert '"(emergency-eligible)" CAN be an emergency' in prompt
    assert "ask ONE short question" in prompt
    assert "offer to\nconnect" not in prompt  # wording is on one logical line
    assert "treat it as urgent and offer to connect the caller with someone" in prompt
    assert "at most ONE question per reply" in prompt
    assert "read back in groups" in prompt
