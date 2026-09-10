"""Unit tests for `DatabaseAvailabilityProvider` — the engine that decides
what the assistant is allowed to offer a caller.

Run against the in-memory repository fakes rather than a database. That is
not a shortcut: the provider depends only on repository *ports*, so the
fakes exercise the real slot arithmetic, the real capacity rule, and the
real overlap logic. `tests/integration/test_scheduling_workflow.py` proves
the same behaviour against Postgres.

The clock is injected (`now=`) throughout. Slot generation is entirely a
function of "now", so pinning it is what makes these assertions stable
rather than dependent on the day the suite happens to run."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.domain.entities.appointment import AppointmentStatus
from app.domain.entities.availability import AvailabilityQuery, SlotVerdict
from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.entities.business_profile import BusinessProfile, BusinessType
from app.domain.entities.service import Service
from app.infrastructure.scheduling.database_availability_provider import (
    DatabaseAvailabilityProvider,
)
from tests.fakes import (
    FakeAppointmentRepository,
    FakeBusinessHoursRepository,
    FakeBusinessProfileRepository,
    FakeServiceRepository,
    FakeTechnicianProfileRepository,
    fake_settings,
)

_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()

# 2026-08-24 is a Monday. 06:00 UTC is two hours before the 08:00 open, so
# with the default 120-minute lead time the first bookable slot is exactly
# opening time — which keeps every expectation below readable.
_MONDAY = date(2026, 8, 24)
_NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)


def _weekly(day_of_week: int, open_h: int = 8, close_h: int = 17, *, closed: bool = False):
    return WeeklyHours(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        day_of_week=day_of_week,
        is_closed=closed,
        open_time=None if closed else time(open_h, 0),
        close_time=None if closed else time(close_h, 0),
    )


def _standard_week() -> list[WeeklyHours]:
    """Mon-Fri 08:00-17:00, Sat 09:00-14:00, Sun closed — the same shape the
    dev organization actually has configured."""
    return [
        *[_weekly(d) for d in range(0, 5)],
        _weekly(5, 9, 14),
        _weekly(6, closed=True),
    ]


def _service(minutes: int | None, name: str = "Air Conditioning Repair") -> Service:
    return Service(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        name=name,
        description=None,
        category="cooling",
        is_emergency_eligible=False,
        is_active=True,
        default_duration_minutes=minutes,
    )


def _profile(tz: str = "UTC") -> BusinessProfile:
    now = datetime.now(timezone.utc)
    return BusinessProfile(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        business_type=BusinessType.HVAC,
        display_name="Northside Heating & Cooling",
        phone_number=None,
        timezone=tz,
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


def _make_provider(
    *,
    services: list[Service] | None = None,
    weekly: list[WeeklyHours] | None = None,
    exceptions: list[HoursException] | None = None,
    profile: BusinessProfile | None = None,
    now: datetime = _NOW,
    **setting_overrides: object,
) -> tuple[
    DatabaseAvailabilityProvider, FakeAppointmentRepository, FakeTechnicianProfileRepository
]:
    appointments = FakeAppointmentRepository()
    technicians = FakeTechnicianProfileRepository()
    provider = DatabaseAvailabilityProvider(
        appointment_repository=appointments,
        business_hours_repository=FakeBusinessHoursRepository(
            weekly if weekly is not None else _standard_week(), exceptions
        ),
        business_profile_repository=FakeBusinessProfileRepository(
            profile if profile is not None else _profile()
        ),
        service_repository=FakeServiceRepository(services),
        technician_profile_repository=technicians,
        settings=fake_settings(**setting_overrides),
        now=now,
    )
    return provider, appointments, technicians


async def _schedule(
    appointments: FakeAppointmentRepository,
    *,
    start_at: datetime,
    duration_minutes: int,
    organization_id: uuid.UUID = _ORG_ID,
) -> uuid.UUID:
    """Creates an appointment and moves it to SCHEDULED, which is the only
    state that occupies calendar time."""
    appointment = await appointments.create(
        organization_id=organization_id,
        conversation_id=uuid.uuid4(),
        matched_service_id=None,
        customer_name="Existing Customer",
        customer_phone="+15551110000",
        customer_address="1 Existing Way",
        summary="Already on the books.",
        duration_minutes=duration_minutes,
    )
    await appointments.schedule(
        organization_id,
        appointment.id,
        scheduled_start_at=start_at,
        duration_minutes=duration_minutes,
        technician_user_id=None,
        assigned_at=datetime.now(timezone.utc),
    )
    return appointment.id


# --- Capacity ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_capacity_of_one_applies_when_no_technicians_are_configured():
    """The chosen capacity model: an org that has not modelled its workforce
    still gets offerable slots, rather than looking permanently full."""
    provider, _, technicians = _make_provider()

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    assert await technicians.list_for_organization(_ORG_ID) == []
    assert len(result.slots) == 3
    assert result.slots[0].start_at == datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_configured_default_capacity_allows_parallel_bookings():
    provider, appointments, _ = _make_provider(SCHEDULING_DEFAULT_CAPACITY=2)
    eight_am = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    await _schedule(appointments, start_at=eight_am, duration_minutes=60)

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    # One of two capacity units is taken, so 08:00 is still offerable.
    assert result.slots[0].start_at == eight_am


@pytest.mark.asyncio
async def test_capacity_follows_the_on_call_technician_count_once_a_roster_exists():
    provider, appointments, technicians = _make_provider(SCHEDULING_DEFAULT_CAPACITY=5)
    await technicians.create(
        organization_id=_ORG_ID, user_id=uuid.uuid4(), phone_number="+15550001111"
    )
    eight_am = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    await _schedule(appointments, start_at=eight_am, duration_minutes=60)

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    # The roster wins over the configured default: one technician, one job,
    # so 08:00 is consumed even though the default would have allowed five.
    assert eight_am not in [slot.start_at for slot in result.slots]


@pytest.mark.asyncio
async def test_technicians_all_off_call_means_nothing_is_offered():
    """An org that *has* a roster and has taken everyone off call is making a
    deliberate statement, unlike one that never configured technicians."""
    provider, _, technicians = _make_provider()
    user_id = uuid.uuid4()
    await technicians.create(
        organization_id=_ORG_ID, user_id=user_id, phone_number="+15550001111"
    )
    await technicians.set_on_call(_ORG_ID, user_id, False)

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    assert result.slots == ()


# --- Existing appointments consume capacity ---------------------------------


@pytest.mark.asyncio
async def test_an_existing_scheduled_appointment_consumes_its_slot():
    provider, appointments, _ = _make_provider()
    await _schedule(
        appointments,
        start_at=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        duration_minutes=60,
    )

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    offered = [slot.start_at for slot in result.slots]
    # 08:00 and 08:30 both overlap the existing 08:00-09:00 job; 09:00 is the
    # first free start.
    assert datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc) not in offered
    assert datetime(2026, 8, 24, 8, 30, tzinfo=timezone.utc) not in offered
    assert offered[0] == datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_a_requested_appointment_holds_no_time_and_consumes_nothing():
    """REQUESTED is the state the 2026-08-22 call left its appointment in.
    It carries no `scheduled_start_at`, so it must not block the calendar."""
    provider, appointments, _ = _make_provider()
    await appointments.create(
        organization_id=_ORG_ID,
        conversation_id=uuid.uuid4(),
        matched_service_id=None,
        customer_name="Lucky",
        customer_phone="123456789",
        customer_address="16th Street, California",
        summary="AC running but not cooling.",
        duration_minutes=90,
    )

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    assert result.slots[0].start_at == datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_another_organizations_appointment_never_consumes_our_capacity():
    provider, appointments, _ = _make_provider()
    await _schedule(
        appointments,
        start_at=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        duration_minutes=60,
        organization_id=_OTHER_ORG_ID,
    )

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    assert result.slots[0].start_at == datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)


# --- Duration and business hours --------------------------------------------


@pytest.mark.asyncio
async def test_duration_comes_from_the_matched_service():
    service = _service(90)
    provider, _, _ = _make_provider(services=[service])

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery(service_id=service.id))

    assert result.duration_minutes == 90
    assert result.slots[0].end_at == datetime(2026, 8, 24, 9, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_duration_falls_back_to_the_configured_default_without_a_service():
    provider, _, _ = _make_provider(SCHEDULING_DEFAULT_DURATION_MINUTES=45)

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    assert result.duration_minutes == 45


@pytest.mark.asyncio
async def test_a_visit_must_finish_before_closing_time():
    """A 120-minute job cannot start at 16:00 on a day that closes at 17:00,
    even though 16:00 is inside business hours."""
    service = _service(120)
    provider, _, _ = _make_provider(
        services=[service],
        # Push the search window to the end of the day so the last legal
        # start is what gets asserted.
        SCHEDULING_MAX_SLOTS=20,
    )

    result = await provider.find_slots(
        _ORG_ID, AvailabilityQuery(service_id=service.id, earliest_time=time(14, 0))
    )

    monday_starts = [s.start_at for s in result.slots if s.start_at.date() == _MONDAY]
    assert max(monday_starts) == datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)
    # And the rule holds on every day the search returned, not just Monday:
    # no visit may run past its own day's 17:00 close.
    assert all(slot.end_at.hour <= 17 for slot in result.slots)


@pytest.mark.asyncio
async def test_a_closed_day_is_skipped_entirely():
    # Sunday 2026-08-30 is closed; searching from it rolls into Monday.
    provider, _, _ = _make_provider(now=datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc))

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery(preferred_date=date(2026, 8, 30)))

    assert result.slots[0].start_at == datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_a_dated_exception_overrides_the_weekly_schedule():
    holiday = HoursException(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        date=_MONDAY,
        is_closed=True,
        open_time=None,
        close_time=None,
        label="Company holiday",
    )
    provider, _, _ = _make_provider(exceptions=[holiday])

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    assert all(slot.start_at.date() != _MONDAY for slot in result.slots)


@pytest.mark.asyncio
async def test_an_exception_can_open_a_normally_closed_day():
    sunday = date(2026, 8, 30)
    special = HoursException(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        date=sunday,
        is_closed=False,
        open_time=time(10, 0),
        close_time=time(12, 0),
        label="Emergency weekend cover",
    )
    provider, _, _ = _make_provider(
        exceptions=[special], now=datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)
    )

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery(preferred_date=sunday))

    assert result.slots[0].start_at == datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_no_configured_hours_offers_nothing_rather_than_guessing():
    """Deliberately asymmetric with staff scheduling, which does not block on
    missing setup. Offering a caller a time the business never agreed to is
    exactly the fabrication this work exists to remove."""
    provider, _, _ = _make_provider(weekly=[])

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    assert result.slots == ()


# --- Time constraints --------------------------------------------------------


@pytest.mark.asyncio
async def test_slots_before_the_minimum_lead_time_are_never_offered():
    """Now is 07:30 with a 120-minute lead, so 08:00 and 09:00 are too soon
    even though the business is open."""
    provider, _, _ = _make_provider(now=datetime(2026, 8, 24, 7, 30, tzinfo=timezone.utc))

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    assert result.slots[0].start_at == datetime(2026, 8, 24, 9, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_earliest_and_latest_time_bound_the_search():
    provider, _, _ = _make_provider(SCHEDULING_MAX_SLOTS=20)

    result = await provider.find_slots(
        _ORG_ID, AvailabilityQuery(earliest_time=time(13, 0), latest_time=time(14, 0))
    )

    starts = [slot.start_at for slot in result.slots if slot.start_at.date() == _MONDAY]
    assert starts == [
        datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 24, 13, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc),
    ]


@pytest.mark.asyncio
async def test_days_to_search_is_clamped_to_the_configured_maximum():
    """The model can emit any integer the schema allows; an unbounded search
    is a slow query on a live call, not a better answer."""
    provider, _, _ = _make_provider(SCHEDULING_MAX_SEARCH_DAYS=2, SCHEDULING_MAX_SLOTS=200)

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery(days_to_search=3650))

    assert {slot.start_at.date() for slot in result.slots} == {_MONDAY, date(2026, 8, 25)}


@pytest.mark.asyncio
async def test_slots_are_rendered_in_the_organizations_timezone():
    """America/Chicago is UTC-5 in August, so an 08:00 local open is 13:00
    UTC. Getting this wrong books a caller into the wrong hour."""
    provider, _, _ = _make_provider(profile=_profile("America/Chicago"))

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    assert result.timezone == "America/Chicago"
    assert result.slots[0].start_at == datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc)


# --- verify_slot -------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_slot_accepts_a_slot_that_find_slots_offered():
    provider, _, _ = _make_provider()
    offered = (await provider.find_slots(_ORG_ID, AvailabilityQuery())).slots[0]

    verdict = await provider.verify_slot(
        _ORG_ID, start_at=offered.start_at, duration_minutes=offered.duration_minutes
    )

    assert verdict is SlotVerdict.BOOKABLE


@pytest.mark.asyncio
async def test_verify_slot_rejects_a_time_in_the_past():
    provider, _, _ = _make_provider()

    verdict = await provider.verify_slot(
        _ORG_ID,
        start_at=datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc),
        duration_minutes=60,
    )

    assert verdict is SlotVerdict.IN_THE_PAST


@pytest.mark.asyncio
async def test_verify_slot_rejects_a_closed_day():
    provider, _, _ = _make_provider()

    verdict = await provider.verify_slot(
        _ORG_ID,
        # Sunday.
        start_at=datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
        duration_minutes=60,
    )

    assert verdict is SlotVerdict.OUTSIDE_BUSINESS_HOURS


@pytest.mark.asyncio
async def test_verify_slot_reports_full_when_capacity_is_consumed():
    provider, appointments, _ = _make_provider()
    ten_am = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    await _schedule(appointments, start_at=ten_am, duration_minutes=60)

    verdict = await provider.verify_slot(_ORG_ID, start_at=ten_am, duration_minutes=60)

    assert verdict is SlotVerdict.FULL


@pytest.mark.asyncio
async def test_verify_slot_excludes_the_appointment_being_rescheduled():
    """Without this a reschedule onto its own time would conflict with
    itself, and rescheduling would be impossible."""
    provider, appointments, _ = _make_provider()
    ten_am = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    appointment_id = await _schedule(appointments, start_at=ten_am, duration_minutes=60)

    verdict = await provider.verify_slot(
        _ORG_ID,
        start_at=ten_am,
        duration_minutes=60,
        exclude_appointment_id=appointment_id,
    )

    assert verdict is SlotVerdict.BOOKABLE


@pytest.mark.asyncio
async def test_back_to_back_appointments_do_not_collide():
    """The overlap interval is half-open, so a 10:00-11:00 job leaves 11:00
    bookable. Getting this wrong would waste a third of the working day."""
    provider, appointments, _ = _make_provider()
    await _schedule(
        appointments,
        start_at=datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc),
        duration_minutes=60,
    )

    verdict = await provider.verify_slot(
        _ORG_ID,
        start_at=datetime(2026, 8, 24, 11, 0, tzinfo=timezone.utc),
        duration_minutes=60,
    )

    assert verdict is SlotVerdict.BOOKABLE


@pytest.mark.asyncio
async def test_partial_overlap_is_still_a_conflict():
    provider, appointments, _ = _make_provider()
    await _schedule(
        appointments,
        start_at=datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc),
        duration_minutes=90,
    )

    verdict = await provider.verify_slot(
        _ORG_ID,
        # Starts 30 minutes before the existing job ends.
        start_at=datetime(2026, 8, 24, 11, 0, tzinfo=timezone.utc),
        duration_minutes=60,
    )

    assert verdict is SlotVerdict.FULL


@pytest.mark.asyncio
async def test_a_cancelled_appointment_releases_its_slot():
    provider, appointments, _ = _make_provider()
    ten_am = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    appointment_id = await _schedule(appointments, start_at=ten_am, duration_minutes=60)
    await appointments.update_status(
        _ORG_ID, appointment_id, status=AppointmentStatus.CANCELED, closed_at=_NOW
    )

    verdict = await provider.verify_slot(_ORG_ID, start_at=ten_am, duration_minutes=60)

    assert verdict is SlotVerdict.BOOKABLE


@pytest.mark.asyncio
async def test_slot_ids_round_trip_through_the_availability_result():
    """The assistant quotes a `slot_id` back verbatim when booking, so a slot
    the engine offered must decode to exactly the time it offered."""
    provider, _, _ = _make_provider()
    slot = (await provider.find_slots(_ORG_ID, AvailabilityQuery())).slots[0]

    from app.domain.entities.availability import AvailabilitySlot

    parsed = AvailabilitySlot.parse_slot_id(slot.slot_id)

    assert parsed == (slot.start_at, slot.duration_minutes)
    assert slot.end_at == slot.start_at + timedelta(minutes=slot.duration_minutes)


# --- Excluding one appointment from the search -------------------------------
#
# The 2026-08-27 defect. A caller was offered 08:00/08:30/09:00 and chose
# 08:30; a superseded turn booked it successfully but the caller never heard
# the confirmation. The recovery turn re-checked availability, counted that
# same appointment as occupied, and the assistant told the caller the times it
# had just offered were "not actually available". `verify_slot` had always
# excluded the appointment being written; `find_slots` had no way to.


@pytest.mark.asyncio
async def test_find_slots_can_exclude_one_appointment_from_occupancy():
    provider, appointments, _ = _make_provider()
    appointment_id = await _schedule(
        appointments,
        start_at=datetime(2026, 8, 24, 8, 30, tzinfo=timezone.utc),
        duration_minutes=90,
    )

    blocked = await provider.find_slots(_ORG_ID, AvailabilityQuery())
    excluded = await provider.find_slots(
        _ORG_ID, AvailabilityQuery(exclude_appointment_id=appointment_id)
    )

    # Unexcluded, the 08:30-10:00 job swallows every earlier start: 08:00,
    # 08:30, 09:00 and 09:30 all overlap it, so 10:00 is the first free one.
    assert blocked.slots[0].start_at == datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    # Excluded, the caller is shown the time they actually chose.
    assert [slot.start_at for slot in excluded.slots] == [
        datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 24, 8, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc),
    ]


@pytest.mark.asyncio
async def test_an_unset_exclusion_counts_exactly_what_it_counted_before():
    """`!=` against None must exclude nothing, so every existing caller of
    `find_slots` is unaffected."""
    provider, appointments, _ = _make_provider()
    await _schedule(
        appointments,
        start_at=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        duration_minutes=60,
    )

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery())

    assert result.slots[0].start_at == datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_excluding_one_appointment_leaves_every_other_one_blocking():
    """The exclusion names a single id — it is not a switch that turns
    conflict detection off."""
    provider, appointments, _ = _make_provider()
    mine = await _schedule(
        appointments,
        start_at=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        duration_minutes=90,
    )
    await _schedule(
        appointments,
        start_at=datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc),
        duration_minutes=90,
    )

    result = await provider.find_slots(_ORG_ID, AvailabilityQuery(exclude_appointment_id=mine))

    offered = [slot.start_at for slot in result.slots]
    assert datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc) in offered
    # The other job is untouched by the exclusion and still holds 10:00-11:30.
    assert datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc) not in offered
    assert datetime(2026, 8, 24, 10, 30, tzinfo=timezone.utc) not in offered
