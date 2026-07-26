"""Unit tests for AppointmentService using in-memory fakes — no database, no
real LLM call. Mirrors `test_dispatch_service.py`'s structure."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timezone
from decimal import Decimal

import pytest

from app.application.services.appointment_service import AppointmentService
from app.domain.entities.appointment import AppointmentStatus
from app.domain.entities.business_hours import WeeklyHours
from app.domain.entities.business_profile import BusinessProfile, BusinessType
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.rbac import DEFAULT_ROLES, TECHNICIAN_ROLE_NAME
from app.domain.entities.role import Role
from app.domain.entities.service import Service
from app.domain.entities.user import User
from app.domain.exceptions import (
    AppointmentOutsideBusinessHoursError,
    AuthorizationError,
    EntityNotFoundError,
    InvalidAppointmentStatusTransitionError,
)
from tests.fakes import (
    FakeAppointmentRepository,
    FakeBusinessHoursRepository,
    FakeBusinessProfileRepository,
    FakeConversationOutcomeRepository,
    FakeServiceRepository,
    FakeTechnicianProfileRepository,
)

_ORG_ID = uuid.uuid4()
_MONDAY_10AM = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)  # weekday() == 0
_TUESDAY_10AM = datetime(2026, 1, 6, 10, 0, tzinfo=timezone.utc)  # weekday() == 1
_TUESDAY_8AM = datetime(2026, 1, 6, 8, 0, tzinfo=timezone.utc)


def _make_service(
    *, services: list[Service] | None = None, weekly=None, exceptions=None, profile=None
) -> tuple[
    AppointmentService,
    FakeAppointmentRepository,
    FakeTechnicianProfileRepository,
    FakeConversationOutcomeRepository,
]:
    appointments = FakeAppointmentRepository()
    technicians = FakeTechnicianProfileRepository()
    outcomes = FakeConversationOutcomeRepository()
    service = AppointmentService(
        appointment_repository=appointments,
        technician_profile_repository=technicians,
        conversation_outcome_repository=outcomes,
        service_repository=FakeServiceRepository(services),
        business_hours_repository=FakeBusinessHoursRepository(weekly, exceptions),
        business_profile_repository=FakeBusinessProfileRepository(profile),
    )
    return service, appointments, technicians, outcomes


def _owner_user() -> User:
    now = datetime.now(timezone.utc)
    role = Role(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        name="Owner",
        description=None,
        is_system_role=True,
        permission_codes=frozenset(DEFAULT_ROLES["Owner"]),
    )
    return User(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        email="owner@example.com",
        hashed_password="x",
        full_name="Owner",
        is_active=True,
        is_superuser=False,
        created_at=now,
        updated_at=now,
        last_login_at=None,
        roles=(role,),
    )


def _technician_user(user_id: uuid.UUID | None = None) -> User:
    now = datetime.now(timezone.utc)
    role = Role(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        name=TECHNICIAN_ROLE_NAME,
        description=None,
        is_system_role=True,
        permission_codes=frozenset(DEFAULT_ROLES[TECHNICIAN_ROLE_NAME]),
    )
    return User(
        id=user_id or uuid.uuid4(),
        organization_id=_ORG_ID,
        email="tech@example.com",
        hashed_password="x",
        full_name="Tech",
        is_active=True,
        is_superuser=False,
        created_at=now,
        updated_at=now,
        last_login_at=None,
        roles=(role,),
    )


async def _seed_book_appointment_outcome(
    outcomes: FakeConversationOutcomeRepository,
    conversation_id,
    *,
    classification=CallClassification.NON_EMERGENCY,
    matched_service_id=None,
):
    await outcomes.upsert(
        conversation_id,
        classification=classification,
        confidence=0.9,
        recommended_action=RecommendedAction.BOOK_APPOINTMENT,
        matched_service_id=matched_service_id,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Wants a routine furnace tune-up.",
    )


def _weekday_hours(day_of_week: int, *, is_closed=False, open_time=None, close_time=None):
    return WeeklyHours(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        day_of_week=day_of_week,
        is_closed=is_closed,
        open_time=open_time,
        close_time=close_time,
    )


def _utc_profile() -> BusinessProfile:
    now = datetime.now(timezone.utc)
    return BusinessProfile(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        business_type=BusinessType.HVAC,
        display_name="Acme HVAC",
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


# --- sync_appointment_from_outcome ---


@pytest.mark.asyncio
async def test_sync_is_noop_when_no_outcome_exists():
    service, appointments, _, _ = _make_service()

    result = await service.sync_appointment_from_outcome(_ORG_ID, uuid.uuid4())

    assert result is None
    assert await appointments.list_for_organization(_ORG_ID, limit=10, offset=0) == []


@pytest.mark.asyncio
async def test_sync_is_noop_when_recommended_action_is_not_book_appointment():
    service, appointments, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await outcomes.upsert(
        conversation_id,
        classification=CallClassification.EMERGENCY,
        confidence=0.95,
        recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone=None,
        customer_address=None,
        summary="Basement flooding.",
    )

    result = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)

    assert result is None
    assert await appointments.list_for_organization(_ORG_ID, limit=10, offset=0) == []


@pytest.mark.asyncio
async def test_sync_does_not_hard_check_classification():
    """`BOOK_APPOINTMENT` is the only gate — an EMERGENCY-classified outcome
    that still recommends booking an appointment must still create one."""
    service, appointments, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(
        outcomes, conversation_id, classification=CallClassification.EMERGENCY
    )

    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)

    assert appointment is not None
    assert appointment.status is AppointmentStatus.REQUESTED


@pytest.mark.asyncio
async def test_sync_creates_appointment_from_book_appointment_outcome():
    service, appointments, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)

    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)

    assert appointment is not None
    assert appointment.status is AppointmentStatus.REQUESTED
    assert appointment.customer_name == "Jane Doe"
    assert appointment.scheduled_start_at is None
    assert appointment.duration_minutes is None


@pytest.mark.asyncio
async def test_sync_is_idempotent_across_repeated_turns():
    service, appointments, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)

    first = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)
    second = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)

    assert first.id == second.id
    assert len(await appointments.list_for_organization(_ORG_ID, limit=10, offset=0)) == 1


@pytest.mark.asyncio
async def test_sync_defaults_duration_from_matched_service():
    matched_service = Service(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        name="Furnace Tune-Up",
        description=None,
        category=None,
        is_emergency_eligible=False,
        is_active=True,
        default_duration_minutes=60,
    )
    service, appointments, _, outcomes = _make_service(services=[matched_service])
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(
        outcomes, conversation_id, matched_service_id=matched_service.id
    )

    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)

    assert appointment.duration_minutes == 60


@pytest.mark.asyncio
async def test_sync_leaves_duration_unset_when_matched_service_not_found():
    service, appointments, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(
        outcomes, conversation_id, matched_service_id=uuid.uuid4()
    )

    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)

    assert appointment.duration_minutes is None


# --- schedule_appointment ---


@pytest.mark.asyncio
async def test_schedule_appointment_happy_path():
    service, appointments, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)
    tech = _technician_user()
    await technicians.create(organization_id=_ORG_ID, user_id=tech.id, phone_number="+15005550006")

    scheduled = await service.schedule_appointment(
        _ORG_ID,
        appointment.id,
        scheduled_start_at=_MONDAY_10AM,
        duration_minutes=45,
        technician_user_id=tech.id,
    )

    assert scheduled.status is AppointmentStatus.SCHEDULED
    assert scheduled.scheduled_start_at == _MONDAY_10AM
    assert scheduled.duration_minutes == 45
    assert scheduled.assigned_technician_user_id == tech.id
    assert scheduled.assigned_at is not None


@pytest.mark.asyncio
async def test_scheduling_a_closed_appointment_is_rejected():
    service, appointments, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)
    owner = _owner_user()
    await service.cancel_appointment(_ORG_ID, appointment.id, acting_user=owner)

    with pytest.raises(InvalidAppointmentStatusTransitionError):
        await service.schedule_appointment(
            _ORG_ID, appointment.id, scheduled_start_at=_MONDAY_10AM, duration_minutes=30
        )


@pytest.mark.asyncio
async def test_scheduling_with_unknown_technician_raises_not_found():
    service, appointments, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)

    with pytest.raises(EntityNotFoundError):
        await service.schedule_appointment(
            _ORG_ID,
            appointment.id,
            scheduled_start_at=_MONDAY_10AM,
            duration_minutes=30,
            technician_user_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_scheduling_outside_business_hours_is_rejected():
    weekly = [_weekday_hours(1, open_time=time(9, 0), close_time=time(17, 0))]  # Tuesday only
    service, appointments, _, outcomes = _make_service(weekly=weekly, profile=_utc_profile())
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)

    # Monday has no configured hours at all -> treated as closed.
    with pytest.raises(AppointmentOutsideBusinessHoursError):
        await service.schedule_appointment(
            _ORG_ID, appointment.id, scheduled_start_at=_MONDAY_10AM, duration_minutes=30
        )

    # Tuesday 8am is before the 9am opening time.
    with pytest.raises(AppointmentOutsideBusinessHoursError):
        await service.schedule_appointment(
            _ORG_ID, appointment.id, scheduled_start_at=_TUESDAY_8AM, duration_minutes=30
        )


@pytest.mark.asyncio
async def test_scheduling_inside_business_hours_succeeds():
    weekly = [_weekday_hours(1, open_time=time(9, 0), close_time=time(17, 0))]  # Tuesday
    service, appointments, _, outcomes = _make_service(weekly=weekly, profile=_utc_profile())
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)

    scheduled = await service.schedule_appointment(
        _ORG_ID, appointment.id, scheduled_start_at=_TUESDAY_10AM, duration_minutes=30
    )

    assert scheduled.status is AppointmentStatus.SCHEDULED


@pytest.mark.asyncio
async def test_rescheduling_an_already_scheduled_appointment_is_allowed():
    weekly = [_weekday_hours(1, open_time=time(9, 0), close_time=time(17, 0))]
    service, appointments, _, outcomes = _make_service(weekly=weekly, profile=_utc_profile())
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)
    await service.schedule_appointment(
        _ORG_ID, appointment.id, scheduled_start_at=_TUESDAY_10AM, duration_minutes=30
    )

    rescheduled = await service.schedule_appointment(
        _ORG_ID,
        appointment.id,
        scheduled_start_at=_TUESDAY_10AM.replace(hour=14),
        duration_minutes=60,
    )

    assert rescheduled.status is AppointmentStatus.SCHEDULED
    assert rescheduled.duration_minutes == 60


# --- update_appointment_status ---


@pytest.mark.asyncio
async def test_illegal_status_transition_is_rejected():
    service, appointments, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)
    owner = _owner_user()

    with pytest.raises(InvalidAppointmentStatusTransitionError):
        await service.update_appointment_status(
            _ORG_ID, appointment.id, AppointmentStatus.COMPLETED, acting_user=owner
        )


@pytest.mark.asyncio
async def test_legal_transition_sequence_sets_closed_at_on_completed():
    service, appointments, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)
    tech = _technician_user()
    await technicians.create(organization_id=_ORG_ID, user_id=tech.id, phone_number="+15005550006")
    appointment = await service.schedule_appointment(
        _ORG_ID,
        appointment.id,
        scheduled_start_at=_MONDAY_10AM,
        duration_minutes=30,
        technician_user_id=tech.id,
    )
    owner = _owner_user()

    completed = await service.update_appointment_status(
        _ORG_ID, appointment.id, AppointmentStatus.COMPLETED, acting_user=owner
    )

    assert completed.status is AppointmentStatus.COMPLETED
    assert completed.closed_at is not None


@pytest.mark.asyncio
async def test_completing_an_appointment_persists_actual_value():
    """Milestone 8: an optional dollar value captured when an appointment
    is marked COMPLETED, which Analytics later sums into "revenue
    recovered"."""
    service, appointments, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)
    tech = _technician_user()
    await technicians.create(organization_id=_ORG_ID, user_id=tech.id, phone_number="+15005550006")
    appointment = await service.schedule_appointment(
        _ORG_ID,
        appointment.id,
        scheduled_start_at=_MONDAY_10AM,
        duration_minutes=30,
        technician_user_id=tech.id,
    )
    owner = _owner_user()

    completed = await service.update_appointment_status(
        _ORG_ID,
        appointment.id,
        AppointmentStatus.COMPLETED,
        acting_user=owner,
        actual_value=Decimal("125.00"),
    )

    assert completed.actual_value == Decimal("125.00")


@pytest.mark.asyncio
async def test_technician_can_only_update_own_assigned_appointment():
    service, appointments, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)

    owner_tech = _technician_user()
    other_tech = _technician_user()
    await technicians.create(
        organization_id=_ORG_ID, user_id=owner_tech.id, phone_number="+15005550006"
    )
    appointment = await service.schedule_appointment(
        _ORG_ID,
        appointment.id,
        scheduled_start_at=_MONDAY_10AM,
        duration_minutes=30,
        technician_user_id=owner_tech.id,
    )

    with pytest.raises(AuthorizationError):
        await service.update_appointment_status(
            _ORG_ID, appointment.id, AppointmentStatus.NO_SHOW, acting_user=other_tech
        )

    updated = await service.update_appointment_status(
        _ORG_ID, appointment.id, AppointmentStatus.NO_SHOW, acting_user=owner_tech
    )
    assert updated.status is AppointmentStatus.NO_SHOW


# --- cancel_appointment ---


@pytest.mark.asyncio
async def test_cancel_appointment_from_requested():
    service, appointments, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)
    owner = _owner_user()

    canceled = await service.cancel_appointment(_ORG_ID, appointment.id, acting_user=owner)

    assert canceled.status is AppointmentStatus.CANCELED
    assert canceled.closed_at is not None


@pytest.mark.asyncio
async def test_cancel_appointment_from_scheduled():
    service, appointments, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_book_appointment_outcome(outcomes, conversation_id)
    appointment = await service.sync_appointment_from_outcome(_ORG_ID, conversation_id)
    appointment = await service.schedule_appointment(
        _ORG_ID, appointment.id, scheduled_start_at=_MONDAY_10AM, duration_minutes=30
    )
    owner = _owner_user()

    canceled = await service.cancel_appointment(_ORG_ID, appointment.id, acting_user=owner)

    assert canceled.status is AppointmentStatus.CANCELED
