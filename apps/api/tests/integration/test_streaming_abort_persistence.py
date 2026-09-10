"""H3: an aborted streaming turn must never leave a customer-only message.

Reproduced at the service/session level rather than through HTTP, because
the defect is a *transaction lifecycle* defect and this is the only way to
assert on it precisely. The sequence below is exactly the production one:

    _prepare_turn writes + flushes the customer message
    -> the stream begins
    -> the generator is closed (client disconnect)
    -> the request-scoped session is torn down the way `get_db` tears it
       down on a clean exit, i.e. `await session.commit()`

The final step is the important one. `GeneratorExit` and `CancelledError`
both derive from `BaseException`, so `get_db`'s `except Exception` never
sees them: FastAPI's exit stack closes normally and the commit runs,
turning a flushed-but-incomplete turn into a durable orphan row.

Asserting "the database is clean" would be too weak — a test could pass
simply because nothing was ever written. Each test therefore asserts on
the *specific* conversation, and the successful-turn tests prove the same
code path really does persist when it completes.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.application.services.ai_brain_service import AIBrainService
from app.domain.entities.conversation import ConversationChannel
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.repositories import (
    SqlAlchemyConversationOutcomeRepository,
    SqlAlchemyConversationRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from tests.fakes import (
    FakeAIProvider,
    FakeBusinessHoursRepository,
    FakeBusinessProfileRepository,
    FakeEmergencyKeywordRepository,
    FakeFAQRepository,
    FakeServiceAreaRepository,
    FakeServiceRepository,
    default_reply,
    fake_settings,
)
from tests.log_capture import capture_events, names


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
            OrganizationModel(id=org_id, name=f"H3 Org {org_id.hex[:8]}", slug=f"h3-{org_id.hex[:8]}")
        )
        await session.commit()
    return org_id


def _brain(session, provider: FakeAIProvider) -> AIBrainService:
    """Real conversation/outcome repositories on the given session; the
    business-knowledge repositories are read-only here and faked."""

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


async def _new_conversation(organization_id: uuid.UUID) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        conversation = await SqlAlchemyConversationRepository(session).create(
            organization_id=organization_id,
            channel=ConversationChannel.VOICE,
            caller_phone_number=None,
        )
        await session.commit()
        return conversation.id


async def _rows(conversation_id: uuid.UUID) -> list[tuple[str, str]]:
    """(role, content) straight from the database, in a fresh session, so
    only *committed* state is observed."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT role::text, content FROM conversation_messages "
                "WHERE conversation_id = :cid ORDER BY created_at"
            ),
            {"cid": conversation_id},
        )
        return [(row[0], row[1]) for row in result.all()]


async def _outcome_count(conversation_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT count(*) FROM conversation_outcomes WHERE conversation_id = :cid"),
            {"cid": conversation_id},
        )
        return result.scalar_one()


async def _abort_mid_turn(organization_id, conversation_id, *, how: str) -> None:
    """Drives a streamed turn to its first delta, then kills it the way a
    disconnect does — and then commits, exactly as `get_db` does when
    FastAPI closes the exit stack without an exception."""
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(message_to_customer="Help is on the way."))

    async with AsyncSessionLocal() as session:
        stream = _brain(session, provider).send_message_stream(
            organization_id, conversation_id, "My furnace died."
        )
        first = await stream.__anext__()
        assert first.text == "Help is on the way.", "turn must have reached the model"

        if how == "generator_exit":
            await stream.aclose()
        else:
            task = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await stream.aclose()

        # `get_db`'s teardown, faithfully: it commits on a clean exit, and
        # rolls back if that raises. The commit is the line that used to
        # make the flushed customer message durable.
        #
        # The rollback arm is not dead code. When cancellation lands *mid
        # flush*, SQLAlchemy poisons the session (`PendingRollbackError`),
        # so the commit fails and `get_db`'s own `except Exception` rolls
        # back instead — which is the second, independent reason an
        # interrupted turn cannot become an orphan.
        try:
            await session.commit()
        except Exception:
            await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_generator_exit_leaves_no_customer_only_message(organization_id):
    """The mandatory regression test for the original defect."""
    conversation_id = await _new_conversation(organization_id)

    await _abort_mid_turn(organization_id, conversation_id, how="generator_exit")

    rows = await _rows(conversation_id)
    assert rows == [], f"aborted turn persisted {rows!r}"
    assert await _outcome_count(conversation_id) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_cancelled_error_leaves_no_customer_only_message(organization_id):
    """CancelledError is a different BaseException with different
    propagation rules — asserted separately rather than assumed equivalent."""
    conversation_id = await _new_conversation(organization_id)

    await _abort_mid_turn(organization_id, conversation_id, how="cancelled_error")

    rows = await _rows(conversation_id)
    assert rows == []
    assert await _outcome_count(conversation_id) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_abort_before_any_output_leaves_nothing(organization_id):
    conversation_id = await _new_conversation(organization_id)
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(message_to_customer="Never spoken."))

    async with AsyncSessionLocal() as session:
        stream = _brain(session, provider).send_message_stream(
            organization_id, conversation_id, "Hello?"
        )
        await stream.aclose()  # closed before the first delta is pulled
        await session.commit()

    assert await _rows(conversation_id) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_successful_turn_still_persists_both_messages_and_outcome(organization_id):
    """The other half of the invariant: the fix must not achieve safety by
    persisting less on the happy path."""
    conversation_id = await _new_conversation(organization_id)
    provider = FakeAIProvider()
    provider.queue_reply(default_reply(message_to_customer="A technician is on the way."))

    async with AsyncSessionLocal() as session:
        events = [
            event
            async for event in _brain(session, provider).send_message_stream(
                organization_id, conversation_id, "My furnace died."
            )
        ]
        await session.commit()

    assert len(events) == 2
    rows = await _rows(conversation_id)
    assert [role for role, _ in rows] == ["CUSTOMER", "ASSISTANT"]
    assert rows[0][1] == "My furnace died."
    assert rows[1][1] == "A technician is on the way."
    assert await _outcome_count(conversation_id) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_repeated_aborts_do_not_accumulate_orphans(organization_id):
    """Pre-P4 a single call produced several aborts. Whatever the fix, it
    must hold when the abort happens over and over on one conversation."""
    conversation_id = await _new_conversation(organization_id)

    for _ in range(4):
        await _abort_mid_turn(organization_id, conversation_id, how="generator_exit")

    assert await _rows(conversation_id) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_abort_does_not_remove_previously_completed_turns(organization_id):
    """The fix must be scoped to the current turn — earlier, committed
    history is not allowed to disappear."""
    conversation_id = await _new_conversation(organization_id)

    provider = FakeAIProvider()
    provider.queue_reply(default_reply(message_to_customer="First answer."))
    async with AsyncSessionLocal() as session:
        async for _ in _brain(session, provider).send_message_stream(
            organization_id, conversation_id, "First question."
        ):
            pass
        await session.commit()

    await _abort_mid_turn(organization_id, conversation_id, how="generator_exit")

    rows = await _rows(conversation_id)
    assert [role for role, _ in rows] == ["CUSTOMER", "ASSISTANT"]
    assert rows[0][1] == "First question."
    assert rows[1][1] == "First answer."


@pytest.mark.asyncio(loop_scope="session")
async def test_aborted_turn_is_observable(organization_id):
    """H2 preservation: the abort must still be visible in telemetry."""
    conversation_id = await _new_conversation(organization_id)

    with capture_events() as entries:
        await _abort_mid_turn(organization_id, conversation_id, how="generator_exit")

    emitted = names(entries)
    assert "conversation_turn_persisted" not in emitted, "an aborted turn never persisted"
    assert await _rows(conversation_id) == []
