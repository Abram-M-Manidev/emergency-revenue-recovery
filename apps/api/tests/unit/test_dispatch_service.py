"""Unit tests for DispatchService using in-memory fakes — no database, no
real LLM call. Mirrors `test_ai_brain_service.py`'s structure."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

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


# --- contact-detail backfill ---
#
# A ticket is opened on the first turn the AI classifies an emergency,
# which is normally before the caller has given their name, number, or
# address. A live web call produced exactly that: correct details in
# `conversation_outcomes`, blank contact columns on the ticket, and a
# dispatcher with nobody to call back.


async def _seed_outcome(outcomes, conversation_id, **overrides):
    fields = dict(
        classification=CallClassification.EMERGENCY,
        confidence=0.95,
        recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        matched_service_id=None,
        customer_name=None,
        customer_phone=None,
        customer_address=None,
        summary="Furnace failure, no heat.",
    )
    fields.update(overrides)
    await outcomes.upsert(conversation_id, **fields)


@pytest.mark.asyncio
async def test_later_turn_backfills_contact_details_missing_at_ticket_creation():
    service, tickets, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()

    # Turn 1: emergency recognised, caller hasn't identified themselves yet.
    await _seed_outcome(outcomes, conversation_id)
    created = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)
    assert created.customer_name is None
    assert created.customer_phone is None
    assert created.customer_address is None

    # Turn 2: caller gives name, number, and address.
    await _seed_outcome(
        outcomes,
        conversation_id,
        customer_name="Lucky",
        customer_phone="123456789",
        customer_address="1600 Street, California",
    )
    updated = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    assert updated.id == created.id, "must update in place, not open a second ticket"
    assert updated.customer_name == "Lucky"
    assert updated.customer_phone == "123456789"
    assert updated.customer_address == "1600 Street, California"
    assert len(await tickets.list_for_organization(_ORG_ID, limit=10, offset=0)) == 1


@pytest.mark.asyncio
async def test_backfill_treats_empty_strings_as_missing():
    """The AI emits "" for a detail it hasn't heard, which is what the live
    call actually stored — indistinguishable from not knowing it."""
    service, _, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id, customer_name="", customer_phone="   ")
    await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    await _seed_outcome(
        outcomes, conversation_id, customer_name="Lucky", customer_phone="123456789"
    )
    updated = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    assert updated.customer_name == "Lucky"
    assert updated.customer_phone == "123456789"


@pytest.mark.asyncio
async def test_a_lost_detail_never_blanks_a_value_already_on_the_ticket():
    """Half of this case was reversed on 2026-09-22; this is the half that
    stands. A later turn that simply *omits* a field the model reported
    earlier must not erase it — the model dropping a detail is not the
    caller retracting it."""
    service, _, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(
        outcomes,
        conversation_id,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
    )
    created = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    await _seed_outcome(
        outcomes,
        conversation_id,
        customer_name="Jane Doe",
        customer_phone=None,
        customer_address="   ",
    )
    updated = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    assert updated.customer_phone == "+15551234567"
    assert updated.customer_address == "123 Main St"
    assert updated.id == created.id


@pytest.mark.asyncio
async def test_a_caller_correction_reaches_the_ticket():
    """The reversed half, and the reason for it.

    This used to assert the opposite: that a differing later value must
    never overwrite, so "an operator correction, or an earlier better
    transcription" would win. No endpoint lets an operator edit these three
    fields on a ticket — `assign` and `status` are the only writes — so the
    rule protected a workflow that does not exist, while discarding one
    that happens on most calls.

    Two real calls paid for it. A booked appointment carried "Sixteenth
    Street, California" while the outcome had since learned "Sixteenth
    Street, Lyle, California", and a caller who said "my name is John.
    Actually, sorry, it's Jonathan" was recorded as "John". On an emergency
    ticket that stale value is the address someone is dispatched to.

    `conversation_outcomes` is already last-write-wins and is what the
    dashboard reads, so the ticket holding an older value was never a
    safeguard — just an inconsistency."""
    service, _, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(
        outcomes,
        conversation_id,
        customer_name="John",
        customer_phone="+15551234567",
        customer_address="Sixteenth Street",
    )
    created = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    await _seed_outcome(
        outcomes,
        conversation_id,
        customer_name="Jonathan Reyes",
        customer_phone="+15551234567",
        customer_address="Sixteenth Street, Lisle",
    )
    updated = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    assert updated.id == created.id, "must correct in place, not open a second ticket"
    assert updated.customer_name == "Jonathan Reyes"
    assert updated.customer_address == "Sixteenth Street, Lisle"
    assert updated.customer_phone == "+15551234567"


@pytest.mark.asyncio
async def test_an_overwrite_is_logged_with_field_names_and_never_values():
    """Accepting a later value also accepts a model that *degrades* one, so
    the write must not be silent. Caller PII never enters a log line, so the
    record is which fields moved, not what they became."""
    service, _, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(
        outcomes, conversation_id, customer_name="John", customer_phone="+15551234567"
    )
    await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    await _seed_outcome(
        outcomes,
        conversation_id,
        customer_name="Jonathan Reyes",
        customer_phone="+15551234567",
        customer_address="12 Oak Street, Lisle",
    )
    with capture_logs() as logs:
        await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    synced = [entry for entry in logs if entry["event"] == "ticket_contact_details_synced"]
    assert len(synced) == 1
    assert synced[0]["fields"] == ["customer_address", "customer_name"]
    # The address was blank before, the name was not — only the name is an
    # overwrite, and that distinction is the point of the line.
    assert synced[0]["overwritten"] == ["customer_name"]

    blob = json.dumps(synced[0])
    for secret in ("Jonathan", "Reyes", "John", "Oak Street", "Lisle", "+15551234567"):
        assert secret not in blob, f"{secret!r} leaked into a log line"


@pytest.mark.asyncio
async def test_backfill_leaves_dispatch_state_untouched():
    """The whole reason the idempotency guard exists: real dispatch
    progress must survive a later AI turn."""
    service, tickets, technicians, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)
    ticket = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    tech = _technician_user()
    await technicians.create(organization_id=_ORG_ID, user_id=tech.id, phone_number="+15005550006")
    assigned = await service.assign_ticket(_ORG_ID, ticket.id, tech.id)
    en_route = await service.update_ticket_status(
        _ORG_ID, ticket.id, TicketStatus.EN_ROUTE, acting_user=_owner_user()
    )
    assert en_route.status is TicketStatus.EN_ROUTE

    await _seed_outcome(
        outcomes, conversation_id, customer_name="Lucky", customer_phone="123456789"
    )
    after = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    # Contact details filled in...
    assert after.customer_name == "Lucky"
    assert after.customer_phone == "123456789"
    # ...while every operational field is exactly as dispatch left it.
    assert after.status is TicketStatus.EN_ROUTE
    assert after.assigned_technician_user_id == tech.id
    assert after.assigned_at == assigned.assigned_at
    assert after.closed_at is None
    assert after.actual_value is None
    assert after.summary == ticket.summary


@pytest.mark.asyncio
async def test_backfill_is_a_noop_when_nothing_is_missing():
    service, tickets, _, outcomes = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_emergency_outcome(outcomes, conversation_id)
    created = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    unchanged = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    assert unchanged == created
    assert len(await tickets.list_for_organization(_ORG_ID, limit=10, offset=0)) == 1


@pytest.mark.asyncio
async def test_backfill_cannot_reach_another_organizations_ticket():
    service, tickets, _, outcomes = _make_service()
    other_org = uuid.uuid4()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)
    ticket = await service.sync_ticket_from_outcome(_ORG_ID, conversation_id)

    await _seed_outcome(
        outcomes, conversation_id, customer_name="Lucky", customer_phone="123456789"
    )

    with pytest.raises(EntityNotFoundError):
        await service.sync_ticket_from_outcome(other_org, conversation_id)

    still_blank = await tickets.get_by_id(_ORG_ID, ticket.id)
    assert still_blank.customer_name is None
    assert still_blank.customer_phone is None


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
        _ORG_ID, ticket.id, status=TicketStatus.NEW, actual_value=Decimal("50.00")
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
