"""Unit tests for DispatchService using in-memory fakes — no database, no
real LLM call. Mirrors `test_ai_brain_service.py`'s structure."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.application.services.dispatch_service import DispatchService
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.emergency_ticket import TicketStatus
from app.domain.entities.rbac import DEFAULT_ROLES, TECHNICIAN_ROLE_NAME
from app.domain.entities.role import Role
from app.domain.entities.user import User
from app.domain.exceptions import (
    AuthorizationError,
    EntityAlreadyExistsError,
    EntityNotFoundError,
    InvalidTicketStatusTransitionError,
)
from tests.fakes import (
    FakeConversationOutcomeRepository,
    FakeConversationRepository,
    FakeEmergencyTicketRepository,
    FakeRoleRepository,
    FakeTechnicianProfileRepository,
    FakeUserRepository,
)

_ORG_ID = uuid.uuid4()


def _make_service() -> tuple[
    DispatchService,
    FakeEmergencyTicketRepository,
    FakeTechnicianProfileRepository,
    FakeConversationOutcomeRepository,
]:
    tickets = FakeEmergencyTicketRepository()
    technicians = FakeTechnicianProfileRepository()
    outcomes = FakeConversationOutcomeRepository()
    service = DispatchService(
        emergency_ticket_repository=tickets,
        technician_profile_repository=technicians,
        conversation_outcome_repository=outcomes,
        conversation_repository=FakeConversationRepository(),
        user_repository=FakeUserRepository(),
        role_repository=FakeRoleRepository(),
    )
    return service, tickets, technicians, outcomes


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


async def _seed_emergency_outcome(outcomes: FakeConversationOutcomeRepository, conversation_id):
    await outcomes.upsert(
        conversation_id,
        classification=CallClassification.EMERGENCY,
        confidence=0.95,
        recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        matched_service_id=None,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Basement flooding.",
    )


# --- sync_ticket_from_outcome ---


@pytest.mark.asyncio
async def test_sync_is_noop_when_no_outcome_exists():
    service, tickets, _, _ = _make_service()

    result = await service.sync_ticket_from_outcome(_ORG_ID, uuid.uuid4())

    assert result is None
    assert await tickets.list_for_organization(_ORG_ID, limit=10, offset=0) == []


@pytest.mark.asyncio
async def test_sync_is_noop_for_non_emergency_outcome():
    service, tickets, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await outcomes.upsert(
        conversation_id,
        classification=CallClassification.NON_EMERGENCY,
        confidence=0.9,
        recommended_action=RecommendedAction.ANSWER_FAQ,
        matched_service_id=None,
        customer_name=None,
        customer_phone=None,
        customer_address=None,
        summary="Asked about hours.",
    )

    result = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    assert result is None
    assert await tickets.list_for_organization(_ORG_ID, limit=10, offset=0) == []


@pytest.mark.asyncio
async def test_sync_creates_ticket_from_emergency_outcome():
    service, tickets, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_emergency_outcome(outcomes, conversation_id)

    ticket = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    assert ticket is not None
    assert ticket.status is TicketStatus.NEW
    assert ticket.customer_name == "Jane Doe"
    assert ticket.summary == "Basement flooding."


@pytest.mark.asyncio
async def test_sync_is_idempotent_across_repeated_turns():
    service, tickets, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_emergency_outcome(outcomes, conversation_id)

    first = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)
    second = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    assert first.id == second.id
    assert len(await tickets.list_for_organization(_ORG_ID, limit=10, offset=0)) == 1


# --- status transitions ---


@pytest.mark.asyncio
async def test_illegal_status_transition_is_rejected():
    service, _, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_emergency_outcome(outcomes, conversation_id)
    ticket = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)
    owner = _owner_user()

    with pytest.raises(InvalidTicketStatusTransitionError):
        await service.update_ticket_status(
            _ORG_ID, ticket.id, TicketStatus.RESOLVED, acting_user=owner
        )


@pytest.mark.asyncio
async def test_legal_transition_sequence_sets_closed_at_on_resolve():
    service, _, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_emergency_outcome(outcomes, conversation_id)
    ticket = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)
    owner = _owner_user()
    tech = _technician_user()
    await technicians.create(organization_id=_ORG_ID, user_id=tech.id, phone_number="+15005550006")

    ticket = await service.assign_ticket(_ORG_ID, ticket.id, tech.id)
    assert ticket.status is TicketStatus.ASSIGNED
    assert ticket.closed_at is None

    ticket = await service.update_ticket_status(
        _ORG_ID, ticket.id, TicketStatus.EN_ROUTE, acting_user=owner
    )
    ticket = await service.update_ticket_status(
        _ORG_ID, ticket.id, TicketStatus.RESOLVED, acting_user=owner
    )

    assert ticket.status is TicketStatus.RESOLVED
    assert ticket.closed_at is not None


@pytest.mark.asyncio
async def test_resolving_a_ticket_persists_actual_value():
    """Milestone 8: an optional dollar value captured when a ticket is
    marked RESOLVED, which Analytics later sums into "revenue recovered"."""
    service, _, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_emergency_outcome(outcomes, conversation_id)
    ticket = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)
    owner = _owner_user()
    tech = _technician_user()
    await technicians.create(organization_id=_ORG_ID, user_id=tech.id, phone_number="+15005550006")
    ticket = await service.assign_ticket(_ORG_ID, ticket.id, tech.id)
    ticket = await service.update_ticket_status(
        _ORG_ID, ticket.id, TicketStatus.EN_ROUTE, acting_user=owner
    )

    ticket = await service.update_ticket_status(
        _ORG_ID, ticket.id, TicketStatus.RESOLVED, acting_user=owner, actual_value=Decimal("199.99")
    )

    assert ticket.actual_value == Decimal("199.99")


@pytest.mark.asyncio
async def test_leaving_actual_value_unset_does_not_clear_it():
    """A second status-affecting call with no `actual_value` (e.g. the
    generic status endpoint used for something else afterward) must not
    wipe out a value staff already entered."""
    service, tickets, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_emergency_outcome(outcomes, conversation_id)
    ticket = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)
    await tickets.update_status(
        ticket.id, status=TicketStatus.NEW, actual_value=Decimal("50.00")
    )

    owner = _owner_user()
    tech = _technician_user()
    await technicians.create(organization_id=_ORG_ID, user_id=tech.id, phone_number="+15005550006")
    ticket = await service.assign_ticket(_ORG_ID, ticket.id, tech.id)
    ticket = await service.update_ticket_status(
        _ORG_ID, ticket.id, TicketStatus.EN_ROUTE, acting_user=owner
    )

    assert ticket.actual_value == Decimal("50.00")


@pytest.mark.asyncio
async def test_technician_can_only_update_own_assigned_ticket():
    service, _, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_emergency_outcome(outcomes, conversation_id)
    ticket = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    owner_tech = _technician_user()
    other_tech = _technician_user()
    await technicians.create(
        organization_id=_ORG_ID, user_id=owner_tech.id, phone_number="+15005550006"
    )
    ticket = await service.assign_ticket(_ORG_ID, ticket.id, owner_tech.id)

    with pytest.raises(AuthorizationError):
        await service.update_ticket_status(
            _ORG_ID, ticket.id, TicketStatus.EN_ROUTE, acting_user=other_tech
        )

    updated = await service.update_ticket_status(
        _ORG_ID, ticket.id, TicketStatus.EN_ROUTE, acting_user=owner_tech
    )
    assert updated.status is TicketStatus.EN_ROUTE


@pytest.mark.asyncio
async def test_assigning_a_closed_ticket_is_rejected():
    service, _, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_emergency_outcome(outcomes, conversation_id)
    ticket = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)
    owner = _owner_user()
    tech = _technician_user()
    await technicians.create(organization_id=_ORG_ID, user_id=tech.id, phone_number="+15005550006")
    ticket = await service.assign_ticket(_ORG_ID, ticket.id, tech.id)
    await service.update_ticket_status(_ORG_ID, ticket.id, TicketStatus.CANCELED, acting_user=owner)

    with pytest.raises(InvalidTicketStatusTransitionError):
        await service.assign_ticket(_ORG_ID, ticket.id, tech.id)


@pytest.mark.asyncio
async def test_assigning_unknown_technician_raises_not_found():
    service, _, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_emergency_outcome(outcomes, conversation_id)
    ticket = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    with pytest.raises(EntityNotFoundError):
        await service.assign_ticket(_ORG_ID, ticket.id, uuid.uuid4())


# --- create_technician ---


@pytest.mark.asyncio
async def test_create_technician_rejects_duplicate_email():
    service, _, _, _ = _make_service()
    await service.create_technician(
        _ORG_ID,
        full_name="Tech One",
        email="dup@example.com",
        phone_number="+15005550006",
        temporary_password="supersecret1",
    )

    with pytest.raises(EntityAlreadyExistsError):
        await service.create_technician(
            _ORG_ID,
            full_name="Tech Two",
            email="dup@example.com",
            phone_number="+15005550007",
            temporary_password="supersecret2",
        )


@pytest.mark.asyncio
async def test_create_technician_lazily_creates_technician_role():
    service, _, technicians, _ = _make_service()

    profile = await service.create_technician(
        _ORG_ID,
        full_name="New Tech",
        email="new-tech@example.com",
        phone_number="+15005550006",
        temporary_password="supersecret1",
    )

    assert profile.organization_id == _ORG_ID
    assert await technicians.get_by_user_id(profile.user_id) is not None
