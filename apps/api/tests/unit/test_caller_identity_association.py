"""P5 association capture, and proof that it did not disturb C1.

Recording which telephony line a customer called from is bookkeeping that
sits *beside* C1, not inside it: it writes one association row and touches
no customer field. These tests pin both halves of that — the association
happens, and `phone_number`/`full_name`/`address`/`email`/`notes` are
exactly what C1 alone would have left.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.application.services.customer_service import CustomerService
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from tests.fakes import (
    FakeAppointmentRepository,
    FakeCallerIdentityRepository,
    FakeConversationOutcomeRepository,
    FakeCustomerRepository,
    FakeEmergencyTicketRepository,
)
from tests.log_capture import capture_events

_ORG_ID = uuid.uuid4()
_CALLER = "+919999999999"


def _make_service():
    customers = FakeCustomerRepository()
    outcomes = FakeConversationOutcomeRepository()
    identities = FakeCallerIdentityRepository(customers)
    service = CustomerService(
        customer_repository=customers,
        conversation_outcome_repository=outcomes,
        emergency_ticket_repository=FakeEmergencyTicketRepository(),
        appointment_repository=FakeAppointmentRepository(),
        caller_identity_repository=identities,
    )
    return service, customers, outcomes, identities


async def _seed_outcome(outcomes, conversation_id, **overrides):
    kwargs = dict(
        classification=CallClassification.NON_EMERGENCY,
        confidence=0.9,
        recommended_action=RecommendedAction.ANSWER_FAQ,
        matched_service_id=None,
        customer_name="Lucky",
        customer_phone="123456789",
        customer_address=None,
        summary="Routine inquiry.",
    )
    kwargs.update(overrides)
    await outcomes.upsert(conversation_id, **kwargs)


@pytest.mark.asyncio
async def test_caller_number_is_associated_with_the_resolved_customer():
    service, _, outcomes, identities = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)

    customer = await service.sync_customer_from_outcome(
        _ORG_ID, conversation_id, caller_number=_CALLER
    )

    assert customer is not None
    assert (_ORG_ID, _CALLER, customer.id) in identities.associations


@pytest.mark.asyncio
async def test_association_is_idempotent_across_turns():
    """Runs on every turn of a call, not once — a repeat must refresh, not
    duplicate or raise."""
    service, _, outcomes, identities = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)

    for _ in range(4):
        customer = await service.sync_customer_from_outcome(
            _ORG_ID, conversation_id, caller_number=_CALLER
        )

    assert customer is not None
    assert len(identities.associations) == 1
    assert identities.associations[(_ORG_ID, _CALLER, customer.id)] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("absent", [None, "", "   "])
async def test_no_association_without_a_caller_number(absent):
    service, _, outcomes, identities = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)

    await service.sync_customer_from_outcome(_ORG_ID, conversation_id, caller_number=absent)

    assert identities.associations == {}


@pytest.mark.asyncio
async def test_default_call_without_caller_number_keeps_c1_behaviour():
    """The text path calls this with no caller number at all."""
    service, customers, outcomes, identities = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)

    customer = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    assert customer is not None
    assert identities.associations == {}
    assert len(await customers.list_for_organization(_ORG_ID, limit=10, offset=0)) == 1


@pytest.mark.asyncio
async def test_association_does_not_alter_any_customer_field():
    """P5 writes an association, never a customer field. Compared against a
    run with association capture disabled — the customer must be identical."""
    with_capture, customers_a, outcomes_a, _ = _make_service()
    conversation_a = uuid.uuid4()
    await _seed_outcome(outcomes_a, conversation_a, customer_address="16 Street")
    grounded = await with_capture.sync_customer_from_outcome(
        _ORG_ID, conversation_a, caller_number=_CALLER
    )

    without_capture, customers_b, outcomes_b, _ = _make_service()
    conversation_b = uuid.uuid4()
    await _seed_outcome(outcomes_b, conversation_b, customer_address="16 Street")
    plain = await without_capture.sync_customer_from_outcome(_ORG_ID, conversation_b)

    assert grounded is not None and plain is not None
    assert (grounded.full_name, grounded.phone_number, grounded.address) == (
        plain.full_name,
        plain.phone_number,
        plain.address,
    )
    assert (grounded.email, grounded.notes) == (plain.email, plain.notes)


@pytest.mark.asyncio
async def test_c1_blank_fill_still_runs_alongside_association_capture():
    """C1's additive backfill must be untouched by P5."""
    service, customers, outcomes, identities = _make_service()

    first = uuid.uuid4()
    await _seed_outcome(outcomes, first, customer_address=None)
    created = await service.sync_customer_from_outcome(_ORG_ID, first, caller_number=_CALLER)
    assert created is not None and created.address is None

    second = uuid.uuid4()
    await _seed_outcome(outcomes, second, customer_address="16 Street, California")
    updated = await service.sync_customer_from_outcome(_ORG_ID, second, caller_number=_CALLER)

    assert updated is not None
    assert updated.id == created.id
    assert updated.address == "16 Street, California"
    assert len(identities.associations) == 1


@pytest.mark.asyncio
async def test_no_customer_resolved_means_no_association():
    """An outcome with no phone number creates no customer — and therefore
    nothing to associate a caller ID with."""
    service, _, outcomes, identities = _make_service()
    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id, customer_phone=None)

    customer = await service.sync_customer_from_outcome(
        _ORG_ID, conversation_id, caller_number=_CALLER
    )

    assert customer is None
    assert identities.associations == {}


@pytest.mark.asyncio
async def test_two_customers_on_one_line_are_both_recorded():
    """The data model represents a shared line honestly; refusing to ground
    is the application's decision, not the repository's."""
    service, _, outcomes, identities = _make_service()

    for phone in ("111", "222"):
        conversation_id = uuid.uuid4()
        await _seed_outcome(outcomes, conversation_id, customer_phone=phone)
        await service.sync_customer_from_outcome(
            _ORG_ID, conversation_id, caller_number=_CALLER
        )

    assert len(identities.associations) == 2


# --- P5 blocker fix: association capture is best-effort ---


@pytest.mark.asyncio
async def test_association_failure_does_not_escape_or_break_the_sync():
    """The association write is the last statement of the outcome sync, so
    an exception escaping it would break a turn that had already fully
    succeeded — no `[DONE]`, no `endCall`. It must be swallowed and logged.

    Asserted on the surrounding behaviour, not merely on "no exception":
    the customer is still returned, C1's blank-fill still happened, and
    nothing was persisted as an association."""
    service, customers, outcomes, identities = _make_service()

    # A customer that exists with a blank address, so C1 has real work to do
    # on this very call.
    first = uuid.uuid4()
    await _seed_outcome(outcomes, first, customer_address=None)
    created = await service.sync_customer_from_outcome(_ORG_ID, first)
    assert created is not None and created.address is None

    identities.fail_associate_with = RuntimeError("association storage unavailable")

    second = uuid.uuid4()
    await _seed_outcome(outcomes, second, customer_address="16 Street, California")

    with capture_events() as entries:
        # 2. must not raise
        customer = await service.sync_customer_from_outcome(
            _ORG_ID, second, caller_number=_CALLER
        )

    # 3. the sync still completes and returns the customer
    assert customer is not None
    assert customer.id == created.id

    # 4. C1 blank-fill still ran despite the association failing afterwards
    assert customer.address == "16 Street, California"
    persisted = await customers.get_by_id(_ORG_ID, created.id)
    assert persisted is not None and persisted.address == "16 Street, California"

    # 6. nothing was persisted as an association
    assert identities.associations == {}

    # 7. the failure is logged, without the caller's phone number
    failures = [e for e in entries if e.get("event") == "caller_identity_association_failed"]
    assert len(failures) == 1
    assert failures[0]["customer_id"] == str(created.id)
    assert failures[0]["organization_id"] == str(_ORG_ID)
    assert _CALLER not in repr(failures[0]), "caller number must not be logged"


@pytest.mark.asyncio
async def test_association_failure_still_links_ticket_and_appointment():
    """5. The rest of the outcome-sync path is unaffected — the links are
    established before the association attempt and must survive it."""
    customers = FakeCustomerRepository()
    outcomes = FakeConversationOutcomeRepository()
    tickets = FakeEmergencyTicketRepository()
    identities = FakeCallerIdentityRepository(customers)
    identities.fail_associate_with = RuntimeError("association storage unavailable")
    service = CustomerService(
        customer_repository=customers,
        conversation_outcome_repository=outcomes,
        emergency_ticket_repository=tickets,
        appointment_repository=FakeAppointmentRepository(),
        caller_identity_repository=identities,
    )

    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)
    ticket = await tickets.create(
        organization_id=_ORG_ID,
        conversation_id=conversation_id,
        matched_service_id=None,
        customer_name=None,
        customer_phone=None,
        customer_address=None,
        summary="No heat.",
    )
    assert ticket.customer_id is None

    customer = await service.sync_customer_from_outcome(
        _ORG_ID, conversation_id, caller_number=_CALLER
    )

    assert customer is not None
    linked = await tickets.get_by_conversation_id(conversation_id)
    assert linked is not None
    assert linked.customer_id == customer.id, "ticket link must survive the failed association"


@pytest.mark.asyncio
async def test_cancellation_still_propagates_through_association_capture():
    """`Exception`, not `BaseException`: a cancelled turn must still unwind
    so P1's lock and the request transaction end, exactly as H3 requires."""
    service, _, outcomes, identities = _make_service()
    identities.fail_associate_with = asyncio.CancelledError()

    conversation_id = uuid.uuid4()
    await _seed_outcome(outcomes, conversation_id)

    with pytest.raises(asyncio.CancelledError):
        await service.sync_customer_from_outcome(
            _ORG_ID, conversation_id, caller_number=_CALLER
        )
