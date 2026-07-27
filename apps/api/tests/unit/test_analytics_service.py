"""Unit tests for AnalyticsService using in-memory fakes — no database, no
real LLM call. Mirrors `test_customer_service.py`'s structure: build the
service from fakes, seed data (directly manipulating the fakes' internal
dicts when a specific historical timestamp is needed, since `create()`
always stamps "now"), then assert on `get_summary`."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.application.services.analytics_service import AnalyticsService
from app.domain.entities.analytics import DateRangePreset
from app.domain.entities.appointment import AppointmentStatus
from app.domain.entities.business_profile import BusinessProfile, BusinessType
from app.domain.entities.conversation import ConversationChannel
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.emergency_ticket import TicketStatus
from tests.fakes import (
    FakeAppointmentRepository,
    FakeBusinessProfileRepository,
    FakeConversationOutcomeRepository,
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeEmergencyTicketRepository,
)

_ORG_ID = uuid.uuid4()
_OTHER_ORG_ID = uuid.uuid4()


def _make_service(business_profile: BusinessProfile | None = None):
    conversations = FakeConversationRepository()
    outcomes = FakeConversationOutcomeRepository(conversations)
    tickets = FakeEmergencyTicketRepository()
    appointments = FakeAppointmentRepository()
    customers = FakeCustomerRepository()
    business_profiles = FakeBusinessProfileRepository(business_profile)
    service = AnalyticsService(
        conversation_repository=conversations,
        conversation_outcome_repository=outcomes,
        emergency_ticket_repository=tickets,
        appointment_repository=appointments,
        customer_repository=customers,
        business_profile_repository=business_profiles,
    )
    return service, conversations, outcomes, tickets, appointments, customers


async def _seed_conversation(
    conversations: FakeConversationRepository,
    *,
    organization_id=_ORG_ID,
    started_at: datetime,
    channel: ConversationChannel = ConversationChannel.VOICE,
):
    conversation = await conversations.create(
        organization_id=organization_id, channel=channel, caller_phone_number="+15551234567"
    )
    backdated = replace(conversation, started_at=started_at)
    conversations._conversations[conversation.id] = backdated
    return backdated


def _make_business_profile(timezone_name: str = "UTC") -> BusinessProfile:
    now = datetime.now(timezone.utc)
    return BusinessProfile(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        business_type=BusinessType.HVAC,
        display_name="Test HVAC Co",
        phone_number=None,
        timezone=timezone_name,
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


# --- date range resolution ---


@pytest.mark.asyncio
async def test_last_7_days_excludes_a_conversation_from_ten_days_ago():
    service, conversations, *_ = _make_service()
    now = datetime.now(timezone.utc)
    await _seed_conversation(conversations, started_at=now - timedelta(days=3))
    await _seed_conversation(conversations, started_at=now - timedelta(days=10))

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.LAST_7_DAYS)

    assert summary.total_conversations == 1


@pytest.mark.asyncio
async def test_last_30_days_includes_both_recent_conversations():
    service, conversations, *_ = _make_service()
    now = datetime.now(timezone.utc)
    await _seed_conversation(conversations, started_at=now - timedelta(days=3))
    await _seed_conversation(conversations, started_at=now - timedelta(days=10))

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.LAST_30_DAYS)

    assert summary.total_conversations == 2


@pytest.mark.asyncio
async def test_all_time_has_no_lower_bound():
    service, conversations, *_ = _make_service()
    now = datetime.now(timezone.utc)
    await _seed_conversation(conversations, started_at=now - timedelta(days=400))

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.ALL_TIME)

    assert summary.total_conversations == 1
    assert summary.range_start is None


@pytest.mark.asyncio
async def test_falls_back_to_utc_when_no_business_profile_configured():
    """No BusinessProfile exists yet for this org — TODAY must still
    resolve (UTC fallback), not raise."""
    service, conversations, *_ = _make_service(business_profile=None)
    await _seed_conversation(conversations, started_at=datetime.now(timezone.utc))

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.TODAY)

    assert summary.total_conversations == 1


@pytest.mark.asyncio
async def test_other_organizations_data_is_excluded():
    service, conversations, *_ = _make_service()
    now = datetime.now(timezone.utc)
    await _seed_conversation(conversations, organization_id=_OTHER_ORG_ID, started_at=now)

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.ALL_TIME)

    assert summary.total_conversations == 0


# --- conversation breakdowns ---


@pytest.mark.asyncio
async def test_channel_and_classification_breakdowns():
    service, conversations, outcomes, *_ = _make_service()
    now = datetime.now(timezone.utc)
    voice = await _seed_conversation(conversations, started_at=now, channel=ConversationChannel.VOICE)
    text = await _seed_conversation(conversations, started_at=now, channel=ConversationChannel.TEXT)
    await outcomes.upsert(
        voice.id,
        classification=CallClassification.EMERGENCY,
        confidence=0.95,
        recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        matched_service_id=None,
        customer_name=None,
        customer_phone="+15551234567",
        customer_address=None,
        summary="Burst pipe.",
    )
    await outcomes.upsert(
        text.id,
        classification=CallClassification.NON_EMERGENCY,
        confidence=0.8,
        recommended_action=RecommendedAction.ANSWER_FAQ,
        matched_service_id=None,
        customer_name=None,
        customer_phone=None,
        customer_address=None,
        summary="Hours question.",
    )

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.ALL_TIME)

    channels = {b.label: b.count for b in summary.conversations_by_channel}
    assert channels == {"voice": 1, "text": 1}
    classifications = {b.label: b.count for b in summary.classification_breakdown}
    assert classifications == {"emergency": 1, "non_emergency": 1}
    actions = {b.label: b.count for b in summary.recommended_action_breakdown}
    assert actions == {"create_emergency_ticket": 1, "answer_faq": 1}


# --- ticket / appointment funnel + revenue ---


@pytest.mark.asyncio
async def test_ticket_revenue_and_resolution_time():
    service, conversations, _, tickets, *_ = _make_service()
    conversation_id = uuid.uuid4()
    created = datetime.now(timezone.utc) - timedelta(hours=2)
    ticket = await tickets.create(
        organization_id=_ORG_ID,
        conversation_id=conversation_id,
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Burst pipe.",
    )
    tickets._tickets[ticket.id] = replace(ticket, created_at=created)
    await tickets.update_status(
        _ORG_ID, ticket.id, status=TicketStatus.RESOLVED, closed_at=created + timedelta(minutes=90),
        actual_value=Decimal("250.00"),
    )

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.ALL_TIME)

    assert summary.tickets_created == 1
    assert summary.tickets_resolved == 1
    assert summary.ticket_revenue == Decimal("250.00")
    assert summary.total_revenue == Decimal("250.00")
    assert summary.average_ticket_resolution_minutes == pytest.approx(90.0)
    assert len(summary.revenue_by_day) == 1
    assert summary.revenue_by_day[0].amount == Decimal("250.00")


@pytest.mark.asyncio
async def test_ticket_revenue_is_not_counted_unless_resolved():
    service, _, _, tickets, *_ = _make_service()
    ticket = await tickets.create(
        organization_id=_ORG_ID,
        conversation_id=uuid.uuid4(),
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Burst pipe.",
    )
    # Canceled, not resolved — a value entered here (if any) must not count
    # as revenue recovered.
    await tickets.update_status(
        _ORG_ID, ticket.id, status=TicketStatus.CANCELED, closed_at=datetime.now(timezone.utc)
    )

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.ALL_TIME)

    assert summary.tickets_resolved == 0
    assert summary.ticket_revenue == Decimal("0")


@pytest.mark.asyncio
async def test_appointment_revenue_and_show_up_rate():
    service, _, _, _, appointments, _ = _make_service()

    completed = await appointments.create(
        organization_id=_ORG_ID,
        conversation_id=uuid.uuid4(),
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Tune-up.",
        duration_minutes=60,
    )
    await appointments.update_status(
        _ORG_ID,
        completed.id,
        status=AppointmentStatus.COMPLETED,
        closed_at=datetime.now(timezone.utc),
        actual_value=Decimal("180.50"),
    )

    no_show = await appointments.create(
        organization_id=_ORG_ID,
        conversation_id=uuid.uuid4(),
        matched_service_id=None,
        customer_name="John Roe",
        customer_phone="+15557654321",
        customer_address="456 Oak St",
        summary="Tune-up.",
        duration_minutes=60,
    )
    await appointments.update_status(
        _ORG_ID, no_show.id, status=AppointmentStatus.NO_SHOW, closed_at=datetime.now(timezone.utc)
    )

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.ALL_TIME)

    assert summary.appointments_created == 2
    assert summary.appointments_completed == 1
    assert summary.appointments_no_show == 1
    assert summary.appointment_show_up_rate == pytest.approx(0.5)
    assert summary.appointment_revenue == Decimal("180.50")
    assert summary.total_revenue == Decimal("180.50")
    statuses = {b.label: b.count for b in summary.appointment_status_breakdown}
    assert statuses == {"completed": 1, "no_show": 1}


@pytest.mark.asyncio
async def test_show_up_rate_is_none_with_no_closed_appointments():
    service, *_ = _make_service()

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.ALL_TIME)

    assert summary.appointment_show_up_rate is None
    assert summary.appointments_completed == 0


@pytest.mark.asyncio
async def test_combined_revenue_merges_ticket_and_appointment_days():
    service, _, _, tickets, appointments, _ = _make_service()
    now = datetime.now(timezone.utc)

    ticket = await tickets.create(
        organization_id=_ORG_ID,
        conversation_id=uuid.uuid4(),
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Burst pipe.",
    )
    await tickets.update_status(
        _ORG_ID, ticket.id, status=TicketStatus.RESOLVED, closed_at=now, actual_value=Decimal("100.00")
    )

    appointment = await appointments.create(
        organization_id=_ORG_ID,
        conversation_id=uuid.uuid4(),
        matched_service_id=None,
        customer_name="John Roe",
        customer_phone="+15557654321",
        customer_address="456 Oak St",
        summary="Tune-up.",
        duration_minutes=60,
    )
    await appointments.update_status(
        _ORG_ID, appointment.id, status=AppointmentStatus.COMPLETED, closed_at=now, actual_value=Decimal("50.00")
    )

    summary = await service.get_summary(_ORG_ID, preset=DateRangePreset.ALL_TIME)

    assert summary.total_revenue == Decimal("150.00")
    # Same day for both -> merged into a single DailyRevenue bucket.
    assert len(summary.revenue_by_day) == 1
    assert summary.revenue_by_day[0].amount == Decimal("150.00")


# --- customers ---


@pytest.mark.asyncio
async def test_new_and_total_customer_counts():
    service, _, _, _, _, customers = _make_service()
    recent = await customers.create(
        organization_id=_ORG_ID, full_name="Jane Doe", phone_number="+15551234567"
    )
    old = await customers.create(
        organization_id=_ORG_ID, full_name="Old Customer", phone_number="+15559999999"
    )
    now = datetime.now(timezone.utc)
    customers._customers[old.id] = replace(old, created_at=now - timedelta(days=200))
    customers._customers[recent.id] = replace(recent, created_at=now)

    summary_30d = await service.get_summary(_ORG_ID, preset=DateRangePreset.LAST_30_DAYS)
    summary_all = await service.get_summary(_ORG_ID, preset=DateRangePreset.ALL_TIME)

    assert summary_30d.new_customers == 1
    assert summary_30d.total_customers == 2
    assert summary_all.new_customers == 2
