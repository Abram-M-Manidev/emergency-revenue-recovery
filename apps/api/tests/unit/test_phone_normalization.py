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
from types import SimpleNamespace

import pytest

from app.application.services.ai_brain_service import _phone_to_persist
from app.application.services.customer_service import CustomerService
from app.domain.entities.conversation_outcome import (
    CUSTOMER_PHONE_MAX_LENGTH,
    CallClassification,
    RecommendedAction,
)
from app.shared.utils.phone import normalize_phone_number
from tests.fakes import (
    FakeAppointmentRepository,
    FakeBusinessProfileRepository,
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
        # The 2026-09-24 PSTN call. The caller said their number aloud and
        # the transcript held words, not digits. Unparseable is the correct
        # answer — this function canonicalises formatting noise around
        # digits the caller stated, and there are no digits here to find.
        # Inventing "123456789" from the words would be a guess dressed as
        # a dedupe key, and a wrong one the moment a caller says "oh" for
        # zero or "double four".
        ("one two three four five six seven eight nine", None),
        ("six three zero five five five zero one eight four", None),
    ],
)
def test_normalization_cases(raw: str | None, expected: str | None):
    assert normalize_phone_number(raw) == expected


def test_a_spoken_number_is_unparseable_rather_than_invented():
    """Pinned separately from the table because the tempting "fix" for the
    2026-09-24 outage was to teach this function English number words. That
    would push the guess into `customers.phone_number`, which is matched
    exactly for deduplication — so a misheard word would split one caller
    across records, the exact defect this module exists to prevent."""
    spoken = "one two three four five six seven eight nine"

    assert normalize_phone_number(spoken) is None
    assert normalize_phone_number(spoken) != "123456789"


def test_every_spoken_variant_of_one_number_collapses_to_one_key():
    variants = ["1 2 3 4 5 6 7 8 9", "123456789", "123-456-789", "(123) 456 789", " 123456789 "]

    assert len({normalize_phone_number(v) for v in variants}) == 1


def test_different_numbers_never_collapse_together():
    assert normalize_phone_number("+441234567") != normalize_phone_number("441234567")
    assert normalize_phone_number("6305550184") != normalize_phone_number("6305550185")


# --- The deduplication it repairs --------------------------------------------
#
# Since Phase 2 the key is the validated E.164 number, not the digits. The
# business's country decides how a national-format number is read.


def _make_customer_service(
    country: str | None = "US",
) -> tuple[CustomerService, FakeCustomerRepository, FakeConversationOutcomeRepository]:
    conversations = FakeConversationRepository()
    outcomes = FakeConversationOutcomeRepository(conversations)
    customers = FakeCustomerRepository()
    service = CustomerService(
        customer_repository=customers,
        conversation_outcome_repository=outcomes,
        emergency_ticket_repository=FakeEmergencyTicketRepository(),
        appointment_repository=FakeAppointmentRepository(),
        caller_identity_repository=FakeCallerIdentityRepository(customers),
        business_profile_repository=FakeBusinessProfileRepository(
            SimpleNamespace(country=country)  # type: ignore[arg-type]
        ),
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
    """The 2026-08-22 regression: turn 3 spelled the number out, turn 4 did
    not."""
    service, customers, outcomes = _make_customer_service()
    conversation_id = uuid.uuid4()

    await _record_outcome(outcomes, conversation_id, "6 3 0 5 5 5 0 1 8 4")
    first = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    await _record_outcome(outcomes, conversation_id, "+1 630 555 0184")
    second = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    assert first is not None and second is not None
    assert first.id == second.id
    assert len(customers._customers) == 1


@pytest.mark.asyncio
async def test_the_stored_key_is_e164():
    service, customers, outcomes = _make_customer_service()
    conversation_id = uuid.uuid4()
    await _record_outcome(outcomes, conversation_id, "(630) 555-0184")

    customer = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    assert customer is not None
    assert customer.phone_number == "+16305550184"


@pytest.mark.asyncio
async def test_a_repeat_caller_on_a_later_call_matches_the_same_record():
    """The point of deduplication: a second conversation from the same
    number must attach to the existing customer, not fork a new history."""
    service, customers, outcomes = _make_customer_service()

    first_call = uuid.uuid4()
    await _record_outcome(outcomes, first_call, "630-555-0184")
    original = await service.sync_customer_from_outcome(_ORG_ID, first_call)

    second_call = uuid.uuid4()
    await _record_outcome(outcomes, second_call, "+16305550184")
    returning = await service.sync_customer_from_outcome(_ORG_ID, second_call)

    assert original is not None and returning is not None
    assert original.id == returning.id
    assert len(customers._customers) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_key", ["6305550184", "16305550184", "+16305550184"])
async def test_a_customer_keyed_before_e164_is_matched_not_duplicated(legacy_key: str):
    """A record created before validation keeps its old digits-only key.
    The returning caller is matched to it rather than given a second record,
    and nothing about the old record is rewritten or merged."""
    service, customers, outcomes = _make_customer_service()
    legacy = await customers.create(
        organization_id=_ORG_ID, full_name="Lucky", phone_number=legacy_key, address=None
    )
    conversation_id = uuid.uuid4()
    await _record_outcome(outcomes, conversation_id, "+16305550184")

    matched = await service.sync_customer_from_outcome(_ORG_ID, conversation_id)

    assert matched is not None and matched.id == legacy.id
    assert matched.phone_number == legacy_key
    assert len(customers._customers) == 1


@pytest.mark.asyncio
async def test_the_nine_digit_number_of_2026_10_31_creates_no_customer():
    """The caller said nine digits. That is not a number anyone can call
    back, so it must not become the key of a (second) customer record."""
    service, customers, outcomes = _make_customer_service()
    conversation_id = uuid.uuid4()
    await _record_outcome(outcomes, conversation_id, "630555018")

    assert await service.sync_customer_from_outcome(_ORG_ID, conversation_id) is None
    assert customers._customers == {}


@pytest.mark.asyncio
async def test_without_a_country_only_an_international_number_is_a_key():
    """No default country is invented: a national-format number for a
    business with no country cannot be read, so it creates nothing."""
    service, customers, outcomes = _make_customer_service(country=None)

    national = uuid.uuid4()
    await _record_outcome(outcomes, national, "6305550184")
    assert await service.sync_customer_from_outcome(_ORG_ID, national) is None

    international = uuid.uuid4()
    await _record_outcome(outcomes, international, "+44 7400 123456")
    customer = await service.sync_customer_from_outcome(_ORG_ID, international)
    assert customer is not None and customer.phone_number == "+447400123456"


@pytest.mark.asyncio
async def test_the_same_number_in_two_tenants_is_two_customers():
    service, customers, outcomes = _make_customer_service()
    other_org = uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()
    await _record_outcome(outcomes, first, "+16305550184")
    await _record_outcome(outcomes, second, "+16305550184")

    mine = await service.sync_customer_from_outcome(_ORG_ID, first)
    theirs = await service.sync_customer_from_outcome(other_org, second)

    assert mine is not None and theirs is not None
    assert mine.id != theirs.id
    assert mine.organization_id == _ORG_ID and theirs.organization_id == other_org


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
    service, customers, outcomes = _make_customer_service()
    conversation_id = uuid.uuid4()
    await _record_outcome(outcomes, conversation_id, None)

    assert await service.sync_customer_from_outcome(_ORG_ID, conversation_id) is None
    assert customers._customers == {}


# --- What actually gets persisted ------------------------------------------
#
# `normalize_phone_number` deciding a value is unusable is only half the
# story; the other half is what `_persist_turn` then writes. On 2026-09-24
# it wrote the raw utterance, which did not fit `VARCHAR(32)` and took the
# whole turn down with it. These pin the decision itself, with no database
# involved — `tests/integration/test_spoken_phone_persistence.py` proves the
# same thing against the real column.


@pytest.mark.parametrize(
    "spoken",
    [
        "one two three four five six seven eight nine",
        "six three zero five five five zero one eight four",
        "unknown",
        "he didn't say",
        "",
        "   ",
    ],
)
def test_an_unusable_report_is_never_persisted_verbatim(spoken: str):
    """Whatever the transcript held, the structured field gets a real number
    or nothing — never prose, and never a fabricated stand-in."""
    persisted = _phone_to_persist(spoken, already_stored=None)

    assert persisted is None
    assert persisted != spoken


def test_an_unusable_report_keeps_the_number_already_known():
    """The model failing to hear a number is not the caller withdrawing one.
    Blanking it would leave the business no way to call back."""
    assert (
        _phone_to_persist("one two three four five six seven eight nine",
                          already_stored="6305550184")
        == "6305550184"
    )


def test_a_usable_report_still_corrects_what_is_stored():
    """The successful pilot path: a valid number wins, including over an
    earlier one, and is stored in E.164."""
    assert (
        _phone_to_persist("6 3 0 5 5 5 0 1 8 4", already_stored="+16305550111", region="US")
        == "+16305550184"
    )
    assert _phone_to_persist("+44 7400 123456", already_stored=None) == "+447400123456"


def test_an_incomplete_report_never_replaces_a_valid_number():
    """The 2026-10-31 nine digits: not stored, and not allowed to displace a
    number the caller already gave."""
    assert (
        _phone_to_persist("630555018", already_stored="+16305550184", region="US")
        == "+16305550184"
    )
    assert _phone_to_persist("630555018", already_stored=None, region="US") is None


def test_a_national_number_without_a_business_country_is_not_guessed():
    assert _phone_to_persist("6305550184", already_stored=None, region=None) is None


def test_nothing_longer_than_the_column_is_ever_returned():
    """The floor. A pathological transcript — a caller reciting an account
    number and a phone number in one breath — degrades to "nothing learned"
    rather than taking the turn down."""
    absurd = " ".join(["1234567890"] * 10)

    persisted = _phone_to_persist(absurd, already_stored="6305550184")

    assert persisted == "6305550184"
    assert len(persisted) <= CUSTOMER_PHONE_MAX_LENGTH


def test_a_value_is_not_truncated_into_a_plausible_looking_number():
    """Truncating would be worse than dropping: `"one two three four five"`
    trimmed to 32 characters is indistinguishable downstream from a number
    the caller actually gave."""
    spoken = "one two three four five six seven eight nine"

    persisted = _phone_to_persist(spoken, already_stored=None)

    assert persisted is None
    assert persisted != spoken[:CUSTOMER_PHONE_MAX_LENGTH]
