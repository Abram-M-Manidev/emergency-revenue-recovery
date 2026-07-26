"""Unit tests for CustomerService using in-memory fakes — no database, no
real LLM call. Mirrors `test_dispatch_service.py`/`test_appointment_service.py`'s
structure."""

from __future__ import annotations

import uuid

import pytest

from app.application.services.customer_service import CustomerService
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.exceptions import EntityAlreadyExistsError, EntityNotFoundError
from tests.fakes import (
    FakeAppointmentRepository,
    FakeConversationOutcomeRepository,
    FakeCustomerRepository,
    FakeEmergencyTicketRepository,
)

_ORG_ID = uuid.uuid4()


def _make_service() -> tuple[
    CustomerService,
    FakeCustomerRepository,
    FakeConversationOutcomeRepository,
    FakeEmergencyTicketRepository,
    FakeAppointmentRepository,
]:
    customers = FakeCustomerRepository()
    outcomes = FakeConversationOutcomeRepository()
    tickets = FakeEmergencyTicketRepository()
    appointments = FakeAppointmentRepository()
    service = CustomerService(
        customer_repository=customers,
        conversation_outcome_repository=outcomes,
        emergency_ticket_repository=tickets,
        appointment_repository=appointments,
    )
    return service, customers, outcomes, tickets, appointments


async def _seed_outcome(
    outcomes: FakeConversationOutcomeRepository,
    conversation_id,
    *,
    recommended_action=RecommendedAction.ANSWER_FAQ,
    customer_phone="+15551234567",
    customer_name="Jane Doe",
    customer_address="123 Main St",
):
    await outcomes.upsert(
        conversation_id,
        classification=CallClassification.NON_EMERGENCY,
        confidence=0.9,
        recommended_action=recommended_action,
        matched_service_id=None,
        customer_name=customer_name,
        customer_phone=customer_phone,
        customer_address=customer_address,
        summary="Routine inquiry.",
    )


# --- sync_customer_from_outcome ---


@pytest.mark.asyncio
async def test_sync_is_noop_when_no_outcome_exists():
    service, customers, _, _, _ = _make_service()

    result = await service.sync_customer_from_outcome(_ORG_ID, uuid.uuid4())

    assert result is None
    assert await customers.list_for_organization(_ORG_ID, limit=10, offset=0) == []


@pytest.mark.asyncio
async def test_sync_is_noop_when_outcome_has_no_phone():
    service, customers, outcomes, _, _ = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id, customer_phone=None)

    result = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    assert result is None
    assert await customers.list_for_organization(_ORG_ID, limit=10, offset=0) == []


@pytest.mark.asyncio
async def test_sync_creates_customer_regardless_of_recommended_action():
    """Unlike Dispatch/Appointments, syncing isn't gated on
    recommended_action — any outcome with a phone number is eligible."""
    service, customers, outcomes, _, _ = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id, recommended_action=RecommendedAction.ANSWER_FAQ)

    customer = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    assert customer is not None
    assert customer.full_name == "Jane Doe"
    assert customer.phone_number == "+15551234567"
    assert customer.address == "123 Main St"


@pytest.mark.asyncio
async def test_sync_matches_existing_customer_by_phone_and_does_not_overwrite():
    service, customers, outcomes, _, _ = _make_service()
    first_conversation = uuid.uuid4()
    await _seed_outcome(outcomes, first_conversation, customer_name="Jane Doe")
    first = await service.sync_customer_from_outcome(_ORG_ID, first_conversation)

    second_conversation = uuid.uuid4()
    await _seed_outcome(
        outcomes,
        second_conversation,
        customer_name="Jane D. (maybe a typo this time)",
    )
    second = await service.sync_customer_from_outcome(_ORG_ID, second_conversation)

    assert second.id == first.id
    # Later turns must not overwrite an already-created customer record.
    assert second.full_name == "Jane Doe"
    assert len(await customers.list_for_organization(_ORG_ID, limit=10, offset=0)) == 1


@pytest.mark.asyncio
async def test_sync_links_existing_ticket_and_appointment_for_the_conversation():
    service, customers, outcomes, tickets, appointments = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)
    ticket = await tickets.create(
        organization_id=_ORG_ID,
        conversation_id=conversation_id,
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Emergency.",
    )
    appointment = await appointments.create(
        organization_id=_ORG_ID,
        conversation_id=conversation_id,
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Wants a tune-up.",
        duration_minutes=None,
    )
    assert ticket.customer_id is None
    assert appointment.customer_id is None

    customer = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    linked_ticket = await tickets.get_by_id(_ORG_ID, ticket.id)
    linked_appointment = await appointments.get_by_id(_ORG_ID, appointment.id)
    assert linked_ticket.customer_id == customer.id
    assert linked_appointment.customer_id == customer.id


@pytest.mark.asyncio
async def test_sync_does_not_relink_already_linked_ticket():
    service, customers, outcomes, tickets, _ = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)
    ticket = await tickets.create(
        organization_id=_ORG_ID,
        conversation_id=conversation_id,
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Emergency.",
    )
    other_customer_id = uuid.uuid4()
    await tickets.set_customer(ticket.id, customer_id=other_customer_id)

    await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    linked_ticket = await tickets.get_by_id(_ORG_ID, ticket.id)
    assert linked_ticket.customer_id == other_customer_id


# --- get_customer / get_customer_history ---


@pytest.mark.asyncio
async def test_get_customer_raises_not_found_for_unknown_id():
    service, _, _, _, _ = _make_service()

    with pytest.raises(EntityNotFoundError):
        await service.get_customer(_ORG_ID, uuid.uuid4())


@pytest.mark.asyncio
async def test_get_customer_history_composes_tickets_and_appointments():
    service, customers, outcomes, tickets, appointments = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)
    await tickets.create(
        organization_id=_ORG_ID,
        conversation_id=conversation_id,
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Emergency.",
    )
    await appointments.create(
        organization_id=_ORG_ID,
        conversation_id=conversation_id,
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Wants a tune-up.",
        duration_minutes=None,
    )
    customer = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    history = await service.get_customer_history(_ORG_ID, customer.id)

    assert history.customer.id == customer.id
    assert len(history.tickets) == 1
    assert len(history.appointments) == 1


# --- manual create/update ---


@pytest.mark.asyncio
async def test_create_customer_manually():
    service, customers, _, _, _ = _make_service()

    customer = await service.create_customer(
        _ORG_ID, full_name="Walk-in Wanda", phone_number="+15559876543"
    )

    assert customer.full_name == "Walk-in Wanda"
    assert await customers.get_by_id(_ORG_ID, customer.id) == customer


@pytest.mark.asyncio
async def test_create_customer_with_duplicate_phone_raises():
    service, _, _, _, _ = _make_service()
    await service.create_customer(_ORG_ID, full_name="First", phone_number="+15559876543")

    with pytest.raises(EntityAlreadyExistsError):
        await service.create_customer(_ORG_ID, full_name="Second", phone_number="+15559876543")


@pytest.mark.asyncio
async def test_update_customer():
    service, _, _, _, _ = _make_service()
    customer = await service.create_customer(
        _ORG_ID, full_name="Walk-in Wanda", phone_number="+15559876543"
    )

    updated = await service.update_customer(
        _ORG_ID,
        customer.id,
        full_name="Wanda Walker",
        phone_number="+15559876543",
        email="wanda@example.com",
        address="1 Elm St",
        notes="Prefers morning appointments.",
    )

    assert updated.full_name == "Wanda Walker"
    assert updated.email == "wanda@example.com"


@pytest.mark.asyncio
async def test_update_unknown_customer_raises_not_found():
    service, _, _, _, _ = _make_service()

    with pytest.raises(EntityNotFoundError):
        await service.update_customer(
            _ORG_ID,
            uuid.uuid4(),
            full_name="Nobody",
            phone_number="+15550000000",
            email=None,
            address=None,
            notes=None,
        )
