"""A phone number the caller says aloud must not be able to destroy the turn.

Reproduces the 2026-09-24 PSTN pilot failure, which no unit test could have
caught. The caller gave their number as words; the model reported it
verbatim — `"one two three four five six seven eight nine"`, 44 characters —
and it was written straight into `conversation_outcomes.customer_phone`,
which is `VARCHAR(32)`. Postgres raised `StringDataRightTruncationError`, the
request transaction rolled back, and the *entire* turn vanished: the customer
message, the assistant message, the outcome, and the three appointment times
`check_availability` had just recorded as offered.

The caller had already heard those times. The next turn therefore began from
the pre-offer state, so "Twelve PM" was read as an opening remark rather than
a choice, and the assistant restarted intake. It looked exactly like
conversational amnesia and was in fact a failed write.

Why this test is Postgres-backed and not a unit test
----------------------------------------------------
`tests/fakes.py` stores strings in dictionaries. No fake enforces a column
width, so the offending value round-trips happily through every in-memory
repository in this suite — which is precisely why 761 passing tests said
nothing. The constraint that broke production exists only in the database, so
the regression has to be asserted against the database.

`test_the_column_still_rejects_the_raw_utterance` pins that constraint
directly. Without it the other tests here could pass for the wrong reason:
if the column were ever widened, the guard under test would become decorative
and nothing would say so.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.application.services.ai_brain_service import AIBrainService
from app.application.services.appointment_service import AppointmentService
from app.domain.entities.availability import AvailabilitySlot
from app.domain.entities.conversation import ConversationChannel
from app.domain.entities.conversation_outcome import (
    CUSTOMER_PHONE_MAX_LENGTH,
    CallClassification,
    RecommendedAction,
)
from app.domain.entities.offered_slot import SlotSelectionVerdict
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.repositories import (
    SqlAlchemyConversationOutcomeRepository,
    SqlAlchemyConversationRepository,
    SqlAlchemyOfferedSlotRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from tests.fakes import (
    FakeAIProvider,
    FakeAppointmentRepository,
    FakeBusinessHoursRepository,
    FakeBusinessProfileRepository,
    FakeEmergencyKeywordRepository,
    FakeFAQRepository,
    FakeServiceAreaRepository,
    FakeServiceRepository,
    FakeTechnicianProfileRepository,
    default_reply,
    fake_settings,
)

# The exact string the live model produced. Kept verbatim, because its
# length is the whole point: shortening it would silently stop reproducing
# the defect.
_SPOKEN_PHONE = "one two three four five six seven eight nine"

# What the same caller's number looked like on the pilot call that DID book
# successfully. The model transcribed digits that time; nothing else about
# the two calls differed on this path.
_DIGITS_PHONE = "123456789"

# The three times the caller was read before the turn was lost, in UTC.
_OFFER_TURN_INDEX = 2
_SELECTION_TURN_INDEX = 4


def _slot_times() -> list[datetime]:
    base = datetime.now(timezone.utc).replace(
        hour=16, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    return [base, base + timedelta(minutes=30), base + timedelta(minutes=60)]


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def database_ready():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Database not reachable, skipping integration test: {exc}")
    yield
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(loop_scope="session")
async def organization_id(database_ready) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            OrganizationModel(
                id=org_id,
                name=f"Spoken Phone Org {org_id.hex[:8]}",
                slug=f"spoken-{org_id.hex[:8]}",
            )
        )
        await session.commit()
    return org_id


def _brain(session, provider: FakeAIProvider) -> AIBrainService:
    """Real conversation/outcome repositories on the given session — those
    are the ones the defect ran through. Business knowledge is read-only
    here and faked."""
    return AIBrainService(
        conversation_repository=SqlAlchemyConversationRepository(session),
        conversation_outcome_repository=SqlAlchemyConversationOutcomeRepository(session),
        ai_provider=provider,
        business_profile_repository=FakeBusinessProfileRepository(),
        business_hours_repository=FakeBusinessHoursRepository(),
        service_repository=FakeServiceRepository(),
        service_area_repository=FakeServiceAreaRepository(),
        faq_repository=FakeFAQRepository(),
        emergency_keyword_repository=FakeEmergencyKeywordRepository(),
        settings=fake_settings(AI_MAX_CONVERSATION_TURNS=20),
    )


def _appointments(session) -> AppointmentService:
    """Only `offered_slot_repository` is real: `select_slot_for_conversation`
    touches nothing else, and this test is about whether the offer record
    survived the turn."""
    return AppointmentService(
        appointment_repository=FakeAppointmentRepository(),
        technician_profile_repository=FakeTechnicianProfileRepository(),
        conversation_outcome_repository=SqlAlchemyConversationOutcomeRepository(session),
        service_repository=FakeServiceRepository(),
        business_hours_repository=FakeBusinessHoursRepository(),
        business_profile_repository=FakeBusinessProfileRepository(),
        offered_slot_repository=SqlAlchemyOfferedSlotRepository(session),
    )


async def _new_conversation(organization_id: uuid.UUID) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        conversation = await SqlAlchemyConversationRepository(session).create(
            organization_id=organization_id,
            channel=ConversationChannel.VOICE,
            caller_phone_number=None,
        )
        await session.commit()
        return conversation.id


async def _run_turn(
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    reported_phone: str | None,
    utterance: str,
    offer_slots: list[datetime] | None = None,
) -> None:
    """One request, faithfully: `check_availability` records its offer and
    the turn persists, both inside the single session `get_db` hands the
    request, committed the way `get_db` commits it on a clean exit.

    Any exception is left to propagate, so a regression surfaces as a failed
    test rather than a quietly empty database — which is exactly how the
    production defect hid.
    """
    provider = FakeAIProvider()
    provider.queue_reply(
        default_reply(
            message_to_customer="I have 11:00, 11:30 and 12:00 available.",
            classification=CallClassification.NON_EMERGENCY,
            recommended_action=RecommendedAction.BOOK_APPOINTMENT,
            customer_name="Lucky",
            customer_phone=reported_phone,
            customer_address="Fifteenth Street, Lisle",
            summary="AC running but not cooling.",
        )
    )

    async with AsyncSessionLocal() as session:
        if offer_slots:
            await SqlAlchemyOfferedSlotRepository(session).record_offered(
                organization_id,
                conversation_id,
                [
                    AvailabilitySlot(start_at=start, duration_minutes=60)
                    for start in offer_slots
                ],
                _OFFER_TURN_INDEX,
            )

        stream = _brain(session, provider).send_message_stream(
            organization_id, conversation_id, utterance
        )
        async for _event in stream:
            pass

        await session.commit()


async def _committed_outcome(conversation_id: uuid.UUID):
    """Read in a fresh session, so only committed state is observed."""
    async with AsyncSessionLocal() as session:
        return await SqlAlchemyConversationOutcomeRepository(session).get_by_conversation_id(
            conversation_id
        )


async def _committed_messages(conversation_id: uuid.UUID) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT role::text, content FROM conversation_messages "
                "WHERE conversation_id = :cid ORDER BY created_at"
            ),
            {"cid": conversation_id},
        )
        return [(row[0], row[1]) for row in result.all()]


async def _committed_offers(conversation_id: uuid.UUID, organization_id: uuid.UUID):
    async with AsyncSessionLocal() as session:
        return await SqlAlchemyOfferedSlotRepository(session).list_offered_starts(
            organization_id, conversation_id
        )


# --- The constraint that broke production -----------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_column_still_rejects_the_raw_utterance(organization_id):
    """Pins the ceiling the guard exists for.

    If this ever stops raising, `customer_phone` has been widened and the
    guard in `_phone_to_persist` is no longer load-bearing — which is worth
    failing a build over, because the next unbounded value would go straight
    back to production."""
    assert len(_SPOKEN_PHONE) > CUSTOMER_PHONE_MAX_LENGTH

    conversation_id = await _new_conversation(organization_id)
    with pytest.raises(DBAPIError):
        async with AsyncSessionLocal() as session:
            await SqlAlchemyConversationOutcomeRepository(session).upsert(
                conversation_id,
                classification=CallClassification.NON_EMERGENCY,
                confidence=0.9,
                recommended_action=RecommendedAction.BOOK_APPOINTMENT,
                matched_service_id=None,
                customer_name="Lucky",
                customer_phone=_SPOKEN_PHONE,
                customer_address="Fifteenth Street, Lisle",
                summary="AC running but not cooling.",
            )
            await session.commit()


# --- The production regression ----------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_spoken_phone_number_does_not_destroy_the_turn(organization_id):
    """The failure, end to end: offer three times, then persist a turn whose
    model output carries the caller's number as words."""
    conversation_id = await _new_conversation(organization_id)
    slots = _slot_times()

    await _run_turn(
        organization_id,
        conversation_id,
        reported_phone=_SPOKEN_PHONE,
        utterance="My name is Lucky and my number is one two three four five six seven eight nine.",
        offer_slots=slots,
    )

    # The turn survived.
    messages = await _committed_messages(conversation_id)
    assert [role for role, _ in messages] == ["CUSTOMER", "ASSISTANT"]

    outcome = await _committed_outcome(conversation_id)
    assert outcome is not None
    assert outcome.customer_name == "Lucky"
    assert outcome.summary == "AC running but not cooling."

    # The raw utterance is nowhere near the structured field, and nothing
    # was invented to fill it.
    assert outcome.customer_phone is None

    # The offer survived with it — the half the caller had already heard.
    offers = await _committed_offers(conversation_id, organization_id)
    assert sorted(offers) == sorted(slots)


@pytest.mark.asyncio(loop_scope="session")
async def test_the_next_turn_can_still_select_the_offered_slot(organization_id):
    """The caller-visible consequence, pinned. "Twelve PM" must still resolve
    against an offer made on the turn that carried the bad phone value."""
    conversation_id = await _new_conversation(organization_id)
    slots = _slot_times()
    noon = slots[-1]

    await _run_turn(
        organization_id,
        conversation_id,
        reported_phone=_SPOKEN_PHONE,
        utterance="My number is one two three four five six seven eight nine.",
        offer_slots=slots,
    )

    async with AsyncSessionLocal() as session:
        verdict, offered = await _appointments(session).select_slot_for_conversation(
            organization_id,
            conversation_id,
            start_at=noon,
            turn_index=_SELECTION_TURN_INDEX,
        )
        await session.commit()

    assert verdict is SlotSelectionVerdict.RECORDED
    assert offered is not None
    assert offered.start_at == noon


# --- The behaviour that must not have changed -------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_spoken_number_never_erases_one_already_known(organization_id):
    """An unusable report means the model learned nothing this turn, not that
    the caller withdrew their number. Losing it would leave the business with
    no way to call back."""
    conversation_id = await _new_conversation(organization_id)

    await _run_turn(
        organization_id,
        conversation_id,
        reported_phone=_DIGITS_PHONE,
        utterance="My number is 123456789.",
    )
    assert (await _committed_outcome(conversation_id)).customer_phone == _DIGITS_PHONE

    await _run_turn(
        organization_id,
        conversation_id,
        reported_phone=_SPOKEN_PHONE,
        utterance="Sorry, it's one two three four five six seven eight nine.",
    )

    outcome = await _committed_outcome(conversation_id)
    assert outcome.customer_phone == _DIGITS_PHONE


@pytest.mark.asyncio(loop_scope="session")
async def test_a_real_correction_still_replaces_the_stored_number(organization_id):
    """The successful pilot path, unchanged: a number that canonicalises wins,
    including when it corrects an earlier one."""
    conversation_id = await _new_conversation(organization_id)

    await _run_turn(
        organization_id,
        conversation_id,
        reported_phone=_DIGITS_PHONE,
        utterance="My number is 123456789.",
    )
    await _run_turn(
        organization_id,
        conversation_id,
        reported_phone="6 3 0 5 5 5 0 1 8 4",
        utterance="Actually it's 630 555 0184.",
    )

    outcome = await _committed_outcome(conversation_id)
    assert outcome.customer_phone == "6305550184"
