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
            clock=lambda: _NOW,
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


# --- The 2026-09-22 booking loop ---------------------------------------------
#
# A live call offered 8:00, 8:30 and 9:00, the caller chose 8:30 three times,
# and the model answered each time by reading the same three options back.
# Nothing in the transcript explained it; the tool log did. Every attempt
# arrived as date="2024-09-22", start_time="08:30" — the right minute on the
# right day of the right month, two years stale, because the model has no
# clock and the tool result carrying the real date is gone from its context
# by the time the caller answers. The instant missed the offer record, the
# refusal said "call check_availability", and that advice rebuilt the same
# wrong instant on the next turn.
#
# Every test below drives the tools exactly as that model did.

_STALE_YEAR_DATE = "2024-08-24"  # what the model sends
_REAL_DATE = "2026-08-24"  # what it was offered


@pytest.mark.asyncio
async def test_a_year_the_model_invented_still_books_the_time_the_caller_chose():
    """TEST 1. The live failure, end to end: offer, a choice carrying a stale
    year, and a booking that must still land on the offered instant."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offer(conversation, service_name="Air Conditioning Repair")

    selection = await harness.choose(
        conversation, date=_STALE_YEAR_DATE, start_time="08:00"
    )
    assert selection["success"] is True
    assert selection["selection_state"] == "selected"
    # Echoed back in the real year, so the sentence the assistant speaks is
    # the corrected one rather than the one it guessed.
    assert selection["date"] == _REAL_DATE

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date=_STALE_YEAR_DATE, start_time="08:00"
    )

    assert result["success"] is True
    assert result["status"] == "confirmed"
    assert result["date"] == _REAL_DATE
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None
    assert appointment.status is AppointmentStatus.SCHEDULED
    assert appointment.scheduled_start_at == _MONDAY_8AM


@pytest.mark.asyncio
async def test_repeating_the_same_choice_never_restarts_the_offer_cycle():
    """TEST 2. The caller said "8:30" three times. A repeat must stay a
    selection of the same slot — never a fresh search, never a refusal that
    sends the model back to read the list again."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offer(conversation, service_name="Air Conditioning Repair")

    first = await harness.choose(conversation, date=_STALE_YEAR_DATE, start_time="08:00")
    second = await harness.choose(conversation, date=_STALE_YEAR_DATE, start_time="08:00")
    third = await harness.choose(conversation, date=_REAL_DATE, start_time="08:00")

    for attempt in (first, second, third):
        assert attempt["success"] is True
        assert attempt["selection_state"] == "selected"
        assert attempt["date"] == _REAL_DATE
        assert "check_availability" not in attempt["next_step"]

    booked = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date=_STALE_YEAR_DATE, start_time="08:00"
    )
    assert booked["success"] is True


@pytest.mark.asyncio
async def test_a_successful_booking_never_sends_the_model_back_for_more_times():
    """TEST 3. The loop's exit condition. A confirmed booking must not carry
    any instruction that would put the assistant back into the availability
    flow — that advice is what turned one bad argument into four minutes of
    the same three options."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offer(conversation, service_name="Air Conditioning Repair")
    await harness.choose(conversation, date=_STALE_YEAR_DATE, start_time="08:00")

    result = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date=_STALE_YEAR_DATE, start_time="08:00"
    )

    assert result["success"] is True
    assert "check_availability" not in result["next_step"]
    assert "offered_times" not in result
    # The confirmation the caller is owed, from the result rather than from
    # anything the model remembered.
    assert result["spoken_time"]
    assert result["duration_minutes"] == 90


@pytest.mark.asyncio
async def test_a_stale_year_cannot_conjure_a_time_that_was_never_offered():
    """TEST 5. The reconciliation must not become a way in. Repairing the
    year is only ever allowed to land on a time this caller was read; a
    never-offered slot stays refused no matter which year is attached."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    availability = await harness.offer(conversation, service_name="Air Conditioning Repair")
    assert "14:00" not in {slot["start_time"] for slot in availability["slots"]}

    for attempted_date in (_STALE_YEAR_DATE, _REAL_DATE):
        selection = await harness.choose(
            conversation, date=attempted_date, start_time="14:00"
        )
        assert selection["success"] is False
        assert selection["error"] == ToolErrors.SLOT_NOT_OFFERED

        result = await harness.call(
            conversation, BOOK_APPOINTMENT.name, date=attempted_date, start_time="14:00"
        )
        assert result["success"] is False
        assert result["error"] == ToolErrors.SLOT_NOT_OFFERED

    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None
    assert appointment.status is AppointmentStatus.REQUESTED
    assert appointment.scheduled_start_at is None


@pytest.mark.asyncio
async def test_an_ambiguous_year_repair_is_refused_rather_than_guessed():
    """Two offers a year apart share a month, day and minute, so the model's
    yearless description genuinely cannot say which was meant. Refusing is
    the only correct answer — picking either would book a caller into a time
    they might never have chosen."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offered_slots.record_offered(
        _ORG_ID,
        conversation,
        [
            AvailabilitySlot(start_at=_MONDAY_8AM, duration_minutes=90),
            AvailabilitySlot(
                start_at=_MONDAY_8AM.replace(year=_MONDAY_8AM.year + 1),
                duration_minutes=90,
            ),
        ],
        _OFFER_TURN_INDEX,
    )

    selection = await harness.choose(
        conversation, date=_STALE_YEAR_DATE, start_time="08:00"
    )

    assert selection["success"] is False
    assert selection["error"] == ToolErrors.SLOT_NOT_OFFERED


@pytest.mark.asyncio
async def test_a_refusal_hands_back_the_offered_times_instead_of_another_search():
    """The other half of the loop. When a time really was not offered but
    the caller has already been read some, the recovery must be "ask which
    of these you meant" — not "call check_availability", which on the live
    call reproduced the identical list and the identical failure."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offer(conversation, service_name="Air Conditioning Repair")

    refusal = await harness.choose(conversation, date=_REAL_DATE, start_time="14:00")

    assert refusal["success"] is False
    assert refusal["error"] == ToolErrors.SLOT_NOT_OFFERED
    # Not merely silent about searching again — explicitly against it, since
    # the old advice actively told the model to do exactly that.
    assert "do NOT call check_availability".lower() in refusal["next_step"].lower()
    assert "read the same list out again" in refusal["next_step"]
    offered = refusal["offered_times"]
    assert {slot["start_time"] for slot in offered} == {"08:00", "08:30", "09:00"}
    # Given in the argument shape the model has to send back, so the repair
    # does not depend on it reconstructing a date at all.
    assert all(slot["date"] == _REAL_DATE for slot in offered)
    assert all(slot["label"] for slot in offered)


@pytest.mark.asyncio
async def test_a_refusal_with_nothing_yet_offered_still_asks_for_a_search():
    """The empty-record case keeps the original advice: with nothing read to
    the caller there is nothing to disambiguate against, and fetching real
    times is genuinely the next step."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    refusal = await harness.choose(conversation, date=_REAL_DATE, start_time="08:00")

    assert refusal["success"] is False
    assert refusal["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert "check_availability" in refusal["next_step"]
    assert "offered_times" not in refusal


@pytest.mark.asyncio
async def test_the_year_repair_does_not_weaken_the_consent_invariant():
    """TEST 7. The ladder is unchanged. A stale year does not buy the model
    a way past "the caller has not heard this yet" — the turn-index rule
    still refuses a choice recorded in the same turn it was offered."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offer(conversation, service_name="Air Conditioning Repair")

    same_turn = await harness.call(
        conversation,
        SELECT_APPOINTMENT_SLOT.name,
        turn_index=_OFFER_TURN_INDEX,
        date=_STALE_YEAR_DATE,
        start_time="08:00",
    )
    assert same_turn["success"] is False
    assert same_turn["error"] == ToolErrors.SLOT_NOT_YET_HEARD

    # And booking without a recorded choice is still refused, stale year or not.
    unchosen = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date=_STALE_YEAR_DATE, start_time="08:00"
    )
    assert unchosen["success"] is False
    assert unchosen["error"] == ToolErrors.SLOT_NOT_SELECTED
    appointment = await harness.appointments.get_by_conversation_id(conversation)
    assert appointment is not None and appointment.scheduled_start_at is None


# --- The year repair must not become an isolation hole ------------------------
#
# Reconciliation reads the offer record to interpret a date, so if it ever
# read a *wider* record than enforcement does, a stale year would be the way
# in: a request no exact lookup could satisfy would suddenly resolve against
# somebody else's offer. These mirror the exact-year isolation cases above,
# driven through the wrong-year path instead.


@pytest.mark.asyncio
async def test_a_wrong_year_cannot_reconcile_against_another_conversations_offer():
    harness = _Harness()
    offered_to = uuid.uuid4()
    booking_from = uuid.uuid4()
    await harness.intake(offered_to)
    await harness.intake(booking_from, customer_phone="5550001111")

    # Only the first caller is ever read any times.
    await harness.offer(offered_to, service_name="Air Conditioning Repair")

    selection = await harness.choose(
        booking_from, date=_STALE_YEAR_DATE, start_time="08:00"
    )
    booking = await harness.call(
        booking_from, BOOK_APPOINTMENT.name, date=_STALE_YEAR_DATE, start_time="08:00"
    )

    assert selection["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert booking["error"] == ToolErrors.SLOT_NOT_OFFERED
    stolen = await harness.appointments.get_by_conversation_id(booking_from)
    assert stolen is not None and stolen.scheduled_start_at is None


@pytest.mark.asyncio
async def test_a_wrong_year_cannot_reconcile_against_another_tenants_offer():
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    # Same conversation id, offer recorded against a different tenant.
    await harness.offered_slots.record_offered(
        _OTHER_ORG_ID,
        conversation,
        [AvailabilitySlot(start_at=_MONDAY_8AM, duration_minutes=90)],
        _OFFER_TURN_INDEX,
    )

    selection = await harness.choose(
        conversation, date=_STALE_YEAR_DATE, start_time="08:00"
    )
    booking = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date=_STALE_YEAR_DATE, start_time="08:00"
    )

    assert selection["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert booking["error"] == ToolErrors.SLOT_NOT_OFFERED


@pytest.mark.asyncio
async def test_the_repair_only_ever_returns_an_instant_that_was_offered():
    """The property the whole design rests on, asserted directly against the
    resolver rather than through a tool: whatever goes in, what comes out is
    either unchanged or a member of this conversation's offer record. There
    is no third outcome, so no argument can invent an appointment time."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offer(conversation, service_name="Air Conditioning Repair")

    offered = set(
        await harness.offered_slots.list_offered_starts(_ORG_ID, conversation)
    )
    assert len(offered) == 3

    probes = [
        ("2024-08-24", "08:00"),  # the live failure: two years stale
        ("2025-08-24", "08:30"),  # one year stale
        ("2099-08-24", "09:00"),  # far future
        ("2026-08-24", "08:00"),  # already exact
        ("2026-08-25", "08:00"),  # right year, wrong day
        ("2024-08-25", "08:00"),  # wrong year AND wrong day
        ("2024-08-24", "14:00"),  # wrong year, never-offered time
        ("2024-09-24", "08:00"),  # wrong year, wrong month
    ]
    for day, start_time in probes:
        resolved = await harness.factory._resolve_requested_slot(
            _ORG_ID, conversation, {"date": day, "start_time": start_time}
        )
        assert resolved is not None
        instant, _ = resolved
        requested = datetime.fromisoformat(f"{day}T{start_time}:00+00:00")
        assert instant in offered or instant == requested, (day, start_time)


@pytest.mark.asyncio
async def test_select_and_book_never_give_contradictory_recovery_advice():
    """Both tools fail in the SAME round routinely — they did on every failed
    turn of the 2026-09-22 call. If only one of them stopped saying "call
    check_availability", the model would read one result telling it to search
    and another telling it not to, and the live evidence is that it follows
    the search."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)
    await harness.offer(conversation, service_name="Air Conditioning Repair")

    selection = await harness.choose(conversation, date=_REAL_DATE, start_time="14:00")
    booking = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date=_REAL_DATE, start_time="14:00"
    )

    for result in (selection, booking):
        assert result["success"] is False
        assert result["error"] == ToolErrors.SLOT_NOT_OFFERED
        assert "do NOT call check_availability".lower() in result["next_step"].lower()
        assert {slot["start_time"] for slot in result["offered_times"]} == {
            "08:00",
            "08:30",
            "09:00",
        }
    assert selection["next_step"] == booking["next_step"]


@pytest.mark.asyncio
async def test_booking_with_nothing_offered_keeps_the_original_advice():
    """The empty-record case is the one where searching really is the next
    step, so that path still re-raises into the untouched refusal."""
    harness = _Harness()
    conversation = uuid.uuid4()
    await harness.intake(conversation)

    booking = await harness.call(
        conversation, BOOK_APPOINTMENT.name, date=_REAL_DATE, start_time="08:00"
    )

    assert booking["success"] is False
    assert booking["error"] == ToolErrors.SLOT_NOT_OFFERED
    assert "check_availability" in booking["next_step"]
    assert "offered_times" not in booking
