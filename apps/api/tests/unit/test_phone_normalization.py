"""Phone-number canonicalisation, and the customer deduplication that
depends on it.

Written from a real defect. A verification call on 2026-08-22 produced two
`Customer` rows for one caller, because the model transcribed the same
digits as "1 2 3 4 5 6 7 8 9" on one turn and "123456789" on the next, and
`customers.phone_number` is matched exactly. The caller's history was split
across two records — the precise failure the unified customer record exists
to prevent.
"""

from __future__ import annotations

import uuid

import pytest

from app.application.services.customer_service import CustomerService
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.shared.utils.phone import normalize_phone_number
from tests.fakes import (
    FakeAppointmentRepository,
    FakeCallerIdentityRepository,
    FakeConversationOutcomeRepository,
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeEmergencyTicketRepository,
)

_ORG_ID = uuid.uuid4()


# --- The utility -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The exact pair that produced two customer rows on the live call.
        ("1 2 3 4 5 6 7 8 9", "123456789"),
        ("123456789", "123456789"),
        # Formatting a caller or a transcript might introduce.
        ("(630) 555-0184", "6305550184"),
        ("630-555-0184", "6305550184"),
        ("630.555.0184", "6305550184"),
        ("  6305550184  ", "6305550184"),
        # International form is preserved, because +441234 and 441234 are
        # genuinely different numbers.
        ("+44 20 7946 0958", "+442079460958"),
        ("+1 (555) 010-9999", "+15550109999"),
        # Nothing usable.
        (None, None),
        ("", None),
        ("   ", None),
        ("unknown", None),
        ("---", None),
    ],
)
def test_normalization_cases(raw: str | None, expected: str | None):
    assert normalize_phone_number(raw) == expected


def test_every_spoken_variant_of_one_number_collapses_to_one_key():
    variants = ["1 2 3 4 5 6 7 8 9", "123456789", "123-456-789", "(123) 456 789", " 123456789 "]

    assert len({normalize_phone_number(v) for v in variants}) == 1


def test_different_numbers_never_collapse_together():
    assert normalize_phone_number("+441234567") != normalize_phone_number("441234567")
    assert normalize_phone_number("6305550184") != normalize_phone_number("6305550185")


# --- The deduplication it repairs --------------------------------------------


def _make_customer_service() -> tuple[
    CustomerService, FakeCustomerRepository, FakeConversationOutcomeRepository
]:
    conversations = FakeConversationRepository()
    outcomes = FakeConversationOutcomeRepository(conversations)
    customers = FakeCustomerRepository()
    service = CustomerService(
        customer_repository=customers,
        conversation_outcome_repository=outcomes,
        emergency_ticket_repository=FakeEmergencyTicketRepository(),
        appointment_repository=FakeAppointmentRepository(),
        caller_identity_repository=FakeCallerIdentityRepository(customers),
    )
    return service, customers, outcomes


async def _record_outcome(
    outcomes: FakeConversationOutcomeRepository, conversation_id: uuid.UUID, phone: str | None
) -> None:
    await outcomes.upsert(
        conversation_id,
        classification=CallClassification.NON_EMERGENCY,
        confidence=0.9,
        recommended_action=RecommendedAction.BOOK_APPOINTMENT,
        matched_service_id=None,
        customer_name="Lucky",
        customer_phone=phone,
        customer_address="16th Street, California",
        summary="AC running but not cooling.",
    )


@pytest.mark.asyncio
async def test_two_spellings_of_one_number_across_turns_produce_one_customer():
    """The regression itself: turn 3 spelled the number out, turn 4 did
    not."""
    service, customers, outcomes = _make_customer_service()
    conversation_id = uuid.uuid4()

    await _record_outcome(outcomes, conversation_id, "1 2 3 4 5 6 7 8 9")
    first = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    await _record_outcome(outcomes, conversation_id, "123456789")
    second = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    assert first is not None and second is not None
    assert first.id == second.id
    assert len(customers._customers) == 1


@pytest.mark.asyncio
async def test_the_stored_key_is_the_canonical_form():
    service, customers, outcomes = _make_customer_service()
    conversation_id = uuid.uuid4()
    await _record_outcome(outcomes, conversation_id, "(630) 555-0184")

    customer = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    assert customer is not None
    assert customer.phone_number == "6305550184"
    assert await customers.get_by_phone_number(_ORG_ID, "6305550184") is not None


@pytest.mark.asyncio
async def test_a_repeat_caller_on_a_later_call_matches_the_same_record():
    """The point of deduplication: a second conversation from the same
    number must attach to the existing customer, not fork a new history."""
    service, customers, outcomes = _make_customer_service()

    first_call = uuid.uuid4()
    await _record_outcome(outcomes, first_call, "630-555-0184")
    original = await service.sync_customer_from_outcome(_ORG_ID, first_call)

    second_call = uuid.uuid4()
    await _record_outcome(outcomes, second_call, "6305550184")
    returning = await service.sync_customer_from_outcome(_ORG_ID, second_call)

    assert original is not None and returning is not None
    assert original.id == returning.id
    assert len(customers._customers) == 1


@pytest.mark.asyncio
async def test_an_unusable_phone_number_creates_no_customer():
    """A record keyed on "unknown" would merge unrelated callers into one."""
    service, customers, outcomes = _make_customer_service()
    conversation_id = uuid.uuid4()
    await _record_outcome(outcomes, conversation_id, "unknown")

    assert await service.sync_customer_from_outcome(_ORG_ID, conversation_id) is None
    assert customers._customers == {}


@pytest.mark.asyncio
async def test_a_missing_phone_number_still_creates_no_customer():
    """Pre-existing behaviour, retained: this used to be an `is None` check
    and must not have been widened by the normalisation."""
    service, customers, outcomes = _make_customer_service()
    conversation_id = uuid.uuid4()
    await _record_outcome(outcomes, conversation_id, None)

    assert await service.sync_customer_from_outcome(_ORG_ID, conversation_id) is None
    assert customers._customers == {}
