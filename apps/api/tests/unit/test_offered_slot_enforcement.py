"""The booking-consent invariant, enforced by backend state.

A live call on 2026-08-23 booked an appointment the caller never chose. The
model had a valid time in its prompt, the caller gave their details, and it
called `book_appointment` directly — no `check_availability`, no options
read out, no choice made. The appointment was `SCHEDULED` for a Monday
morning nobody had agreed to.

The rule these tests pin is therefore not "the prompt says to offer first"
but:

    book_appointment succeeds  =>  check_availability returned that exact
    slot to THIS conversation, in THIS organization, earlier in the call.

Re-deriving availability cannot substitute for it: the slot that was wrongly
booked genuinely *was* free, so an availability check would have approved it.
Only a record of what was offered can tell the two apart.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from structlog.testing import capture_logs

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
from app.domain.entities.service import Service
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

# Turn indices are conversation-message counts. Offers land on the earlier
# one and the caller answers on the later, which is the shape every real
# call has: you cannot answer a question you have not been asked.
_OFFER_TURN_INDEX = 2
_TURN_INDEX = 4
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
    "customer_phone": "1 2 3 4 5 6 7 8 9",
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
    """Shared repositories, so several conversations (and organizations) run
    against one set of records — which is what makes the isolation tests
    meaningful rather than vacuous."""

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
        turn_index: int = _TURN_INDEX,
        **arguments: object,
    ) -> dict:
        executor = self.factory.bind(organization_id, conversation_id, turn_index)
        result = await executor.execute(
            ToolInvocation(id=f"c_{uuid.uuid4().hex[:8]}", name=tool_name, arguments=arguments)
        )
        return result.content

    async def intake(self, conversation_id: uuid.UUID, **overrides: object) -> dict:
        return await self.call(
            conversation_id, CREATE_SERVICE_REQUEST.name, **{**_FRANK, **overrides}
        )

    async def offer(
        self,
        conversation_id: uuid.UUID,
        *,
        organization_id: uuid.UUID = _ORG_ID,
        **arguments: object,
    ) -> dict:
        """A real `check_availability`, on the turn before the caller answers."""
        return await self.call(
            conversation_id,
            CHECK_AVAILABILITY.name,
            organization_id=organization_id,
            turn_index=_OFFER_TURN_INDEX,
            **arguments,
        )

    async def choose(
        self,
        conversation_id: uuid.UUID,
        *,
        organization_id: uuid.UUID = _ORG_ID,
        **arguments: object,
    ) -> dict:
        """The caller naming one of the times they were read, on the turn
        after the offer — the consent step booking now requires."""
        return await self.call(
            conversation_id,
            SELECT_APPOINTMENT_SLOT.name,
            organization_id=organization_id,
            **arguments,
        )


# --- 1 & 2: booking without, or beyond, an offer ------------------------------


@pytest.mark.asyncio
async def test_booking_without_any_prior_availability_check_is_refused():
    """The 2026-08-23 call, reproduced exactly: intake, then straight to
    booking a time the caller was never read."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="10:30"
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert "check_availability" in result["next_step"]

    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None
    assert appointment.status is AppointmentStatus.REQUESTED
    assert appointment.scheduled_start_at is None


@pytest.mark.asyncio
async def test_a_slot_that_was_never_returned_is_refused_even_though_it_is_free():
    """The distinction a re-derived availability check cannot make: this time
    is genuinely bookable, it simply was not offered."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    availability = await harness.call(
        conversation, CHECK_AVAILABILITY.name, service_name="Air Conditioning Repair"
    )
    offered_times = {slot["start_time"] for slot in availability["slots"]}
    assert "14:00" not in offered_times  # free, but beyond the offered window

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="14:00"
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.scheduled_start_at is None


# --- 3: the legitimate path still works ---------------------------------------


@pytest.mark.asyncio
async def test_check_then_book_the_offered_slot_succeeds():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    availability = await harness.offer(
        conversation, service_name="Air Conditioning Repair"
    )
    chosen = availability["slots"][0]
    await harness.choose(conversation, slot_id=chosen["slot_id"])

    result = await harness.call(conversation, BOOK_APPOINTMENT.name, slot_id=chosen["slot_id"])

    assert result["success"] is True
    assert result["status"] == "confirmed"
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None
    assert appointment.status is AppointmentStatus.SCHEDULED
    assert appointment.scheduled_start_at == _MONDAY_8AM


@pytest.mark.asyncio
async def test_booking_by_date_and_time_works_when_that_time_was_offered():
    """The cross-turn path: a `slot_id` does not survive to the next turn, so
    the caller's choice arrives as a date and a time."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offer(conversation)
    await harness.choose(conversation, date="2026-08-24", start_time="08:00")

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"
    )

    assert result["success"] is True
    assert result["start_time"] == "08:00"


@pytest.mark.asyncio
async def test_the_booking_uses_the_duration_the_caller_was_quoted():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offer(conversation, service_name="Air Conditioning Repair")
    await harness.choose(conversation, date="2026-08-24", start_time="08:00")

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"
    )

    assert result["duration_minutes"] == 90
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.duration_minutes == 90


# --- 4 & 5: isolation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_offer_made_to_another_conversation_cannot_be_booked():
    """Two callers on the same line get the same times. One being offered a
    slot must not authorise the other to take it."""
    harness = _Harness()
    offered_to = uuid.uuid4()
    booking_from = uuid.uuid4()
    await harness.intake(offered_to)
    await harness.intake(booking_from, customer_phone="5550001111")

    await harness.call(offered_to, CHECK_AVAILABILITY.name)

    result = await harness.call(
        booking_from, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED
    stolen = await harness.appointments.get_by_conversation_id(booking_from)
    assert stolen is not None and stolen.scheduled_start_at is None


@pytest.mark.asyncio
async def test_an_offer_made_in_another_organization_cannot_be_booked():
    """The offer record is queried org-first, so a cross-tenant id is inert
    rather than dangerous."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    # The same conversation id, but the offer recorded against a different
    # tenant — the only difference between authorised and not.
    await harness.offered_slots.record_offered(
        _OTHER_ORG_ID,
        conversation,
        [AvailabilitySlot(start_at=_MONDAY_8AM, duration_minutes=90)],
        _OFFER_TURN_INDEX,
    )

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED


# --- 6: the booking lock is still taken ---------------------------------------


@pytest.mark.asyncio
async def test_two_callers_racing_one_offered_slot_never_overlap():
    """Consent enforcement must not have displaced the conflict machinery:
    both callers were legitimately offered the slot, and exactly one gets it."""
    harness = _Harness()
    first, second = uuid.uuid4(), uuid.uuid4()
    await harness.intake(first)
    await harness.intake(second, customer_phone="5550002222")
    await harness.offer(first)
    await harness.offer(second)
    await harness.choose(first, date="2026-08-24", start_time="08:00")
    await harness.choose(second, date="2026-08-24", start_time="08:00")

    results = await asyncio.gather(
        harness.call(first, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"),
        harness.call(second, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"),
    )

    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0]["error"] == ToolErrors.SLOT_UNAVAILABLE
    assert harness.booking_lock.acquisitions >= 2
    assert harness.booking_lock.max_concurrent == 1


# --- 8: the model cannot reach SCHEDULED without an offer ---------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"date": "2026-08-24", "start_time": "10:30"},
        {"date": "2026-08-25", "start_time": "09:00"},
        {"slot_id": "slot_20260824T103000Z_90"},
    ],
)
async def test_no_invented_argument_shape_can_reach_scheduled(arguments: dict):
    """Whichever way the model names an unoffered time — a plausible date, a
    different day, or a well-formed slot_id it constructed itself — the row
    stays REQUESTED."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    result = await harness.call(conversation, BOOK_APPOINTMENT.name, **arguments)

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None
    assert appointment.status is AppointmentStatus.REQUESTED
    assert appointment.scheduled_start_at is None


@pytest.mark.asyncio
async def test_re_offering_the_same_slot_does_not_accumulate_or_fail():
    """`check_availability` runs several times in a normal conversation."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    for _ in range(3):
        await harness.offer(conversation)

    assert len(harness.offered_slots.offers) == 3  # three distinct times, not nine
    await harness.choose(conversation, date="2026-08-24", start_time="08:00")
    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"
    )
    assert result["success"] is True


# --- 9 & 10: contact details reach the appointment ----------------------------


@pytest.mark.asyncio
async def test_a_contactless_appointment_is_backfilled_once_details_are_known():
    """The 2026-08-23 call left a SCHEDULED appointment with no name, phone,
    or address: the legacy outcome-sync seam created it before the caller had
    given them, and the backfill was gated on `recommended_action`, which the
    model had since moved to `none`."""
    from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction

    harness = _Harness()
    conversation = uuid.uuid4()

    # Turn one: a booking recommendation with nothing learned yet — exactly
    # what the legacy seam acts on.
    await harness.outcomes.upsert(
        conversation,
        classification=CallClassification.NON_EMERGENCY,
        confidence=0.9,
        recommended_action=RecommendedAction.BOOK_APPOINTMENT,
        matched_service_id=None,
        customer_name=None,
        customer_phone=None,
        customer_address=None,
        summary="AC not cooling.",
    )
    await harness.appointment_service.sync_appointment_from_outcome(_ORG_ID, conversation)
    created = await harness.appointments.get_by_conversation_id(conversation)
    assert created is not None and created.customer_phone is None

    # A later turn knows the details, and the action has moved on.
    await harness.outcomes.upsert(
        conversation,
        classification=CallClassification.NON_EMERGENCY,
        confidence=0.9,
        recommended_action=RecommendedAction.NONE,
        matched_service_id=None,
        customer_name="Frank",
        customer_phone="123456789",
        customer_address="11 69 Street, California",
        summary="Booked AC repair.",
    )
    await harness.appointment_service.sync_appointment_from_outcome(_ORG_ID, conversation)

    filled = await harness.appointments.get_by_conversation_id(conversation)
    assert filled is not None
    assert filled.customer_name == "Frank"
    assert filled.customer_phone == "123456789"
    assert filled.customer_address == "11 69 Street, California"


@pytest.mark.asyncio
async def test_the_backfill_still_refuses_to_create_without_a_booking_recommendation():
    """Ungating the backfill must not have ungated creation."""
    from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction

    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.outcomes.upsert(
        conversation,
        classification=CallClassification.NON_EMERGENCY,
        confidence=0.9,
        recommended_action=RecommendedAction.ANSWER_FAQ,
        matched_service_id=None,
        customer_name="Frank",
        customer_phone="123456789",
        customer_address="11 69 Street",
        summary="Just a question.",
    )

    assert await harness.appointment_service.sync_appointment_from_outcome(
        _ORG_ID, conversation
    ) is None
    assert await harness.appointments.get_by_conversation_id(conversation) is None


@pytest.mark.asyncio
async def test_a_spelled_out_phone_number_still_normalises_to_one_customer():
    """Consent enforcement must not have disturbed deduplication."""
    harness = _Harness()
    conversation = uuid.uuid4()

    await harness.intake(conversation, customer_phone="1 2 3 4 5 6 7 8 9")
    await harness.intake(conversation, customer_phone="123456789")

    assert len(harness.customers._customers) == 1
    customer = await harness.customers.get_by_phone_number(_ORG_ID, "123456789")
    assert customer is not None
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.customer_phone == "123456789"


# --- Refusal diagnostics ------------------------------------------------------
#
# The 2026-08-23 refusal recorded nothing but its own error code, so the
# requested instant was unrecoverable and a 12/24-hour slip could not be told
# from a timezone slip. These pin what is logged — and, just as importantly,
# what is not.

_APPROVED_DIAGNOSTIC_FIELDS = {
    "requested_start_at",
    "organization_timezone",
    "organization_id",
    "conversation_id",
    "had_slot_id",
    "had_date",
    "had_start_time",
    "slot_id_parsed",
    "offered_count",
    "nearest_offered_start_at",
}


def _refusal_entry(captured: list[dict]) -> dict:
    """The one diagnostic event, from structlog's own capture.

    `caplog` cannot see these: structlog is configured with its own logger
    factory, so nothing reaches stdlib's record list."""
    matches = [
        entry for entry in captured if entry.get("event") == "book_appointment_slot_not_offered"
    ]
    assert len(matches) == 1, f"expected exactly one diagnostic event, got {len(matches)}"
    return matches[0]


@pytest.mark.asyncio
async def test_a_refusal_logs_only_the_approved_derived_fields():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.call(conversation, CHECK_AVAILABILITY.name)

    with capture_logs() as captured:
        result = await harness.call(
            conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="03:00"
        )
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED

    entry = _refusal_entry(captured)
    payload = {k: v for k, v in entry.items() if k not in {"event", "log_level"}}
    assert set(payload) == _APPROVED_DIAGNOSTIC_FIELDS, (
        "the diagnostic must carry exactly the approved fields"
    )

    # The facts that would have settled the live investigation in one glance.
    assert payload["requested_start_at"] == "2026-08-24T03:00:00+00:00"
    assert payload["organization_timezone"] == "UTC"
    assert payload["had_date"] is True and payload["had_start_time"] is True
    assert payload["had_slot_id"] is False and payload["slot_id_parsed"] is False
    assert payload["offered_count"] == 3
    assert payload["nearest_offered_start_at"] == "2026-08-24T08:00:00+00:00"


@pytest.mark.asyncio
async def test_a_refusal_never_logs_caller_details():
    """The model can misfill an argument; an address must not reach a log
    line because of it."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    with capture_logs() as captured:
        await harness.call(
            conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="03:00"
        )

    entry = _refusal_entry(captured)
    rendered = str(entry)
    for personal in ("Frank", "123456789", "1 2 3 4", "11 69 Street", "California"):
        assert personal not in rendered
    assert "arguments" not in entry


@pytest.mark.asyncio
async def test_a_refusal_with_no_prior_offers_reports_an_empty_record():
    """`offered_count=0` distinguishes "nothing was ever offered" from
    "offered, but not this time" — different bugs entirely."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"
    )

    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert await harness.offered_slots.list_offered_starts(_ORG_ID, conversation) == []


@pytest.mark.asyncio
async def test_an_unresolvable_requested_time_does_not_crash():
    """A time that cannot be resolved never reaches the consent check, so it
    is refused as INVALID_SLOT — and the diagnostic path, which needs a
    resolved instant, is simply not entered."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    result = await harness.call(conversation, BOOK_APPOINTMENT.name, slot_id="slot_nonsense")

    assert result["success"] is False
    assert result["error"] == ToolErrors.INVALID_SLOT


@pytest.mark.asyncio
async def test_a_diagnostics_failure_never_breaks_the_refusal(monkeypatch):
    """The log is best-effort: a broken diagnostic must not turn a clean
    refusal into a failed turn."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    async def _explode(*args: object, **kwargs: object) -> list:
        raise RuntimeError("diagnostics backend down")

    monkeypatch.setattr(harness.offered_slots, "list_offered_starts", _explode)

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="08:00"
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_NOT_OFFERED


# --- 13: a refusal never looks like a confirmation ----------------------------


@pytest.mark.asyncio
async def test_a_refused_booking_carries_nothing_confirmable():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-24", start_time="10:30"
    )

    assert result["success"] is False
    for key in ("appointment_id", "status", "spoken_time", "date", "start_time"):
        assert key not in result, f"a refusal must not carry {key!r}"


@pytest.mark.asyncio
async def test_an_offered_slot_that_has_since_passed_is_still_refused():
    """Consent is necessary, not sufficient — feasibility is still checked
    after it. A slot offered earlier in a long call can fall into the past."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    stale = _NOW - timedelta(days=2)
    await harness.offered_slots.record_offered(
        _ORG_ID,
        conversation,
        [AvailabilitySlot(start_at=stale, duration_minutes=90)],
        _OFFER_TURN_INDEX,
    )
    # Chosen too, so this test still proves what it says it does: the refusal
    # comes from the slot being in the past, not from consent being missing.
    await harness.offered_slots.mark_selected(
        _ORG_ID, conversation, stale, _TURN_INDEX
    )

    result = await harness.call(
        conversation,
        BOOK_APPOINTMENT.name,
        date=stale.date().isoformat(),
        start_time=stale.strftime("%H:%M"),
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.SLOT_IN_THE_PAST


@pytest.mark.asyncio
async def test_an_offered_slot_on_a_now_closed_day_is_still_refused():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    sunday = datetime.combine(date(2026, 8, 30), time(10, 0), tzinfo=timezone.utc)
    await harness.offered_slots.record_offered(
        _ORG_ID,
        conversation,
        [AvailabilitySlot(start_at=sunday, duration_minutes=90)],
        _OFFER_TURN_INDEX,
    )
    await harness.offered_slots.mark_selected(
        _ORG_ID, conversation, sunday, _TURN_INDEX
    )

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date="2026-08-30", start_time="10:00"
    )

    assert result["success"] is False
    assert result["error"] == ToolErrors.OUTSIDE_BUSINESS_HOURS


# --- Self-blocking availability (2026-08-27) ---------------------------------
#
# A caller chose 08:30 from 08:00/08:30/09:00. A superseded turn booked it
# successfully, but the caller never heard the confirmation, so the recovery
# turn checked availability again — and the caller's own 90-minute booking
# removed 08:00-09:30 from the result. The assistant then told them the times
# it had just offered were "not actually available" and pushed them to 11:00.
#
# The consent invariant is untouched by these: booking still requires an offer
# to this conversation. What changes is only which appointments count as
# occupied while searching.


@pytest.mark.asyncio
async def test_a_caller_re_checking_after_booking_still_sees_their_own_slot():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    first = await harness.offer(conversation, service_name=_AC_REPAIR.name)
    # The middle slot, exactly as on the live call.
    chosen = first["slots"][1]
    assert chosen["start_time"] == "08:30"
    await harness.choose(conversation, slot_id=chosen["slot_id"])
    booked = await harness.call(conversation, BOOK_APPOINTMENT.name, slot_id=chosen["slot_id"])
    assert booked["success"] is True

    again = await harness.call(
        conversation, CHECK_AVAILABILITY.name, service_name=_AC_REPAIR.name
    )

    assert [slot["start_time"] for slot in again["slots"]] == ["08:00", "08:30", "09:00"]


@pytest.mark.asyncio
async def test_another_conversations_booking_still_blocks_the_slot():
    """The half that must not regress: excluding *your own* appointment must
    not excuse anyone else's."""
    harness = _Harness()
    owner, other = uuid.uuid4(), uuid.uuid4()
    await harness.intake(owner)
    await harness.intake(other)

    first = await harness.offer(owner, service_name=_AC_REPAIR.name)
    await harness.choose(owner, slot_id=first["slots"][1]["slot_id"])
    booked = await harness.call(
        owner, BOOK_APPOINTMENT.name, slot_id=first["slots"][1]["slot_id"]
    )
    assert booked["success"] is True

    seen_by_other = await harness.call(
        other, CHECK_AVAILABILITY.name, service_name=_AC_REPAIR.name
    )

    start_times = [slot["start_time"] for slot in seen_by_other["slots"]]
    assert "08:30" not in start_times
    # 08:30-10:00 is taken, so the next caller's first free start is 10:00.
    assert start_times[0] == "10:00"


@pytest.mark.asyncio
async def test_a_caller_can_rebook_the_slot_their_own_appointment_already_holds():
    """`verify_slot` has always excluded the appointment being written, so a
    retry against the caller's own held slot must still succeed."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    first = await harness.offer(conversation, service_name=_AC_REPAIR.name)
    await harness.choose(conversation, slot_id=first["slots"][1]["slot_id"])
    await harness.call(conversation, BOOK_APPOINTMENT.name, slot_id=first["slots"][1]["slot_id"])

    again = await harness.call(
        conversation, CHECK_AVAILABILITY.name, service_name=_AC_REPAIR.name
    )
    same = next(slot for slot in again["slots"] if slot["start_time"] == "08:30")
    result = await harness.call(conversation, BOOK_APPOINTMENT.name, slot_id=same["slot_id"])

    assert result["success"] is True
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None
    assert appointment.status is AppointmentStatus.SCHEDULED
    assert appointment.scheduled_start_at == datetime(2026, 8, 24, 8, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_a_conversation_holding_no_appointment_searches_exactly_as_before():
    harness = _Harness()

    result = await harness.call(uuid.uuid4(), CHECK_AVAILABILITY.name)

    assert [slot["start_time"] for slot in result["slots"]] == ["08:00", "08:30", "09:00"]
